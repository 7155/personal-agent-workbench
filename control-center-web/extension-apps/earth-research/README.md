# Google Earth Agent

Google Earth Agent is the PAW vertical workspace for hybrid geospatial work:
the Pi Session and Skill plan the task, deterministic local GIS operators run
against the bound project, and the official Earth Engine SDK submits `ee.*`
computations to Google. The right-hand Leaflet map is a projection of those
artifacts and can publish its current view and selected feature IDs back to the
Session.

## Execution planes

| Need | Owner | Receipt |
| --- | --- | --- |
| Buffer, overlay, CRS, raster and vector processing | Local GeoPandas/Rasterio/Shapely/PyProj runtime | `.earth/gis/runs/<runId>/run.json` |
| Earth Engine datasets, map tiles and reducers | Google Earth Engine through `earth_run_script` | `.earth/runs/<runId>/run.json` |
| Large image/table export | `Export.image/table.toDrive/toCloudStorage/toAsset` in a saved script | task ID, then `earth_task_status` |
| Small GeoTIFF download | `await Earth.downloadImage(image, params, filename)` | `.earth/runs/<runId>/artifacts/` |
| Table Asset upload | `earth_asset_upload` with an authenticated Earth Engine CLI | `.earth/cloud/asset-*.json` |
| GIS batch | `earth_gis_batch` | `earth.gis-batch.v1` aggregate receipt |
| GEE script batch | `earth_run_batch` | `earth.script-batch.v1` aggregate receipt |

A map tile is not a downloaded image. The Skill requires the Agent to report
the execution plane, dataset, date range, CRS, scale, output and run ID. A
submitted cloud task is not a completed task.

## Project setup

The bound project stores Earth Engine configuration in `.earth/runtime.json`:

```json
{
  "project": "authorized-google-cloud-project",
  "python": "/path/to/python-with-earthengine-api",
  "dependencies": "/path/to/node-dependencies-containing-@google/earthengine",
  "runner": "/project/.earth/adapter/0.8.0/run-script.mjs"
}
```

Use `earth_workspace` before execution. It preserves configured paths and
installs the versioned adapter inside the bound workspace. Use
`earth_gis_workspace` to prepare the local Python runtime. Credentials stay in
the user's Earth Engine configuration and never enter a prompt, run receipt or
map result.

## GIS retrieval and machine learning

`earth_gis_search` searches the versioned GIS method index for CRS, scale,
cloud/local boundaries, routing and machine-learning guidance. It is retrieval
context, not a result. `earth_ml_catalog` lists supported templates and
`earth_ml_prepare` writes a checked-in-workspace JavaScript scaffold for
Random Forest, K-means or change detection. Replace all placeholders with
verified datasets and labelled samples before calling `earth_run_script`.

## Project layers and spatial databases

The map editor keeps WGS84 GeoJSON as the editable project source under
`.earth/layers/`. Saving a selection writes a catalog entry and a real
FeatureCollection into the bound Session workspace. `earth_gis_export` converts
that source to ESRI Shapefile (including `.shp`, `.shx`, `.dbf`, `.prj` and a
zip), GeoPackage (`.gpkg` with an explicit layer name), GeoJSON or KML.

`earth_spatial_connect` and `earth_spatial_catalog` register project-owned
GeoPackage/SpatiaLite sources and secret-referenced PostGIS sources. Database
URLs and passwords never enter the workspace or receipts. Local database layer
listing is immediate; PostGIS remains `configured_pending` until its
secret-backed driver is installed. This is a source catalog, not a claim that a
cloud database was queried.

The four workbench acceptance stages are deliberately independent:

1. **基础操作**：关闭模型服务，选两块地导出 GPKG，再打开报告。导出和打开文件走确定性 Session/Package 回执。
2. **数据与编辑**：打开已有地块，编辑顶点和属性，保存后关闭重开。每次保存增加图层 revision，并保留 `.earth/layers/history/` 快照。
3. **专业 GIS**：从数据库读取图层，使用 `earth_gis_pixel` 查询真实栅格像元，并用已有 `difference` 算子挖掉内部湖泊。`earth_gis_backends` 会明确报告默认 GeoPandas 与可选 `qgis_process` 是否存在。
4. **分析与交付**：每次运行保留独立 runId；云端任务仍由 `earth_task_status` 查询；`earth_gis_bundle` 把已完成运行、显式报告/图层和 `run-manifest.json` 写入版本化成果目录。

`earth_gis_backends` 只报告 QGIS Processing 是否可用，不会把检测到
`qgis_process` 伪装成已经使用 QGIS 算法。启用 QGIS 后端前应配置
`PAW_QGIS_PROCESS`，并在对应算法回执中记录实际执行后端。

Install the isolated local runtime with
`scripts/install_earth_gis_runtime.sh`. It installs GeoPandas, Fiona, Rasterio,
Shapely and PyProj under the PAW application-support directory; the package
auto-detects that environment when a new Earth workspace is prepared.

The map's **GIS data workspace** is the working entry point. It has three linked
views: **图层** shows cloud result layers and project layers; **文件** lists the
actual Session workspace files, including GeoJSON, GeoPackage, statistics and
HTML reports; **数据库** shows registered GeoPackage/SpatiaLite/PostGIS sources
and their real status. The header always shows the bound Session workspace and
the object currently supplied to the Agent, so a chat action is traceable to a
file, layer or selected feature. Save the current selection, toggle visibility,
remove a layer from the catalog while retaining its source file, export SHP/GPKG,
connect a local database, or register a PostGIS secret reference. Use **刷新**
after an Agent connection or export task; the dock only displays entries read
back from the Session workspace.

For reports and visualizations, ask the Skill to create a self-contained
`report.html`, `statistics.csv`, `method-and-quality.md` or GIS project package
from the validated run receipt. The Files view exposes those real files; it does
not treat a chat table or a map tile as a delivered dataset.

Jev approval is optional. Configure it without putting the key in a project by
running `scripts/configure_jev_key.sh`; if no `TYPESAFE_API_KEY` or Keychain
entry is available, PAW keeps Luna Max as the approval backend and preserves the
standard compression/deterministic approval fallback.

## Acceptance task

快速验收只需要输入一句：**找适合建变电站的地块，避开河流 200 米，并导出 SHP。**
系统应显示候选区、保留真实运行记录，并生成可下载的 SHP 和 GeoPackage。

在本机可直接运行同一条验收流程：

```bash
PAW_EARTH_GIS_PYTHON="$HOME/Library/Application Support/RagIme/EarthGISRuntime/.venv/bin/python" \
  node scripts/run_earth_gis_acceptance.mjs
```

Run a power-grid siting task with local candidate parcels and exclusion zones:

1. Inspect all local inputs and report geometry, CRS, fields and extents.
2. Buffer exclusion zones, difference candidate parcels, and calculate usable
   area locally.
3. Send the surviving WGS84 candidates to Earth Engine and calculate terrain,
   water, land-cover and coverage metrics at an explicit scale.
4. Return the small candidate metrics to the local workspace and join them to
   the local geometry.
5. Build and validate an access corridor, then display the candidates, route,
   cloud layers and rejected reasons in the map.
6. Export one small result and one batch result. Show task IDs, poll the task
   state, and report any missing Cloud Storage/Drive download configuration
   instead of claiming a file exists.

The acceptance is green only when local and cloud receipts, IDs, CRS, scale,
outputs and foreground map state agree. Empty output, unknown task state,
stale run IDs and a map tile presented as a local raster are failures or
unknowns, not successful analysis.
