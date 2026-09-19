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

## Workbench 0.9

Open a local project without a Google project or a model request. Human controls and Agent tools use the same deterministic project services. The five data views are Layers, Attributes, Runs, Files and Databases; the Agent pane can collapse.

- **Identity and edits:** immutable GeoJSON snapshots, stable project/layer/feature IDs, typed property drafts and expected-revision checks. Cancel preserves the original. Historical snapshots remain accessible. Visibility changes preserve project identity.
- **Drawing:** [Leaflet-Geoman Free](https://geoman.io/docs/leaflet) replaces the drawing adapter. Multi-vertex geometry, snap tolerance in pixels, edit/cut drafts, undo/redo and explicit save/cancel retain properties and IDs. A cut operates on selected editable copies, not every visible polygon.
- **Export:** choose whole layer or selected features. A selected export with no IDs is rejected; GPKG readback validates canonical IDs, including numeric versus string identifiers. Export never resaves the source.
- **Databases and rasters:** Chinese display names; real named GPKG/SpatiaLite layer loading with source lineage and WGS84 display; point queries and bounded polygon-window statistics including holes, NoData and outside coverage. PostGIS remains a pending configuration, not a verified connection.
- **QGIS:** optional `qgis_process` JSON list/help/run adapter, following the [official command interface](https://docs.qgis.org/3.44/en/docs/user_manual/processing/standalone.html). A detected executable remains unverified until an actual algorithm succeeds. Native QGIS is not required for the local GeoPandas path.
- **Plans and delivery:** the avoidance form runs buffer → difference in one metric CRS, keeping canonical GPKG and a WGS84 preview. Each run binds input bytes, parameters and step IDs. Compare two recorded runs; repeated delivery allocates a new version. Bundles include input snapshots, GPKG when applicable, statistics CSV/JSON, map PDF/SVG, HTML, quality checks and a checksummed manifest. The report and map refer to that run, not latest workspace data.

## Remote sensing

The Remote Sensing panel freezes an analysis region independently of later sample selection. Save labeled map points/polygons into a sample layer, choose actual image bands, prepare a plan and explicitly run it.

Local Random Forest produces a classification GeoTIFF, typed class mapping, confusion matrix and feature/group holdout record. It requires at least two classes and validation groups containing every class. A one-pixel training exclusion reduces immediate adjacency leakage; this is not independent geographic or temporal validation. Imagery must actually resolve the fissures or land cover being studied. Local NDVI uses explicit red/near-infrared bands, scale/offset metadata and NoData masks. Results include a bounded WGS84 raster preview and HTML report.

Cloud NDVI/time series, before/after change and animated GIF workflows currently use Sentinel-2 SR Harmonized, SCL classes 4/5/6, a fixed ROI and explicit date range/scale. Preparation creates a unique script and does not call GEE. Execution requires a configured authenticated project; failed periods stay missing. Reports/charts are written from evaluated statistics and animation saves actual GIF bytes. Research mode saves an evidence request and can hand it to the existing Agent; it does not manufacture research findings.

## Runtime and limits

Run `scripts/install_earth_gis_runtime.sh` with Python 3.12/3.13 and uv available. It creates the existing isolated PAW GIS environment and installs GeoPandas, Fiona, Rasterio, Shapely, PyProj, NumPy, pandas, scikit-learn and matplotlib. No model key is needed for local file operations or analysis. GEE authorization and QGIS are optional, separate dependencies.

Current bounds: raster range/local learning windows up to 4 million pixels; local learning band data up to 256 MiB, 2,000 sample features and 200,000 sampled pixels; inline vector previews up to 2 MB. Exceeding a bound reports an error rather than pretending a partial result is complete. Very large dataset editing, multi-user collaboration, generic interrupted-program continuation and full ArcGIS/QGIS desktop parity are not claimed. Original inputs and prior installed packages are retained during updates.

## Acceptance

1. With the model unused, select two IDs from 100 parcels, export GPKG, read back those exact IDs, then open HTML and ensure polling leaves it open.
2. Change a vertex and remark, save and reopen: IDs, untouched values/types/nulls and the old snapshot must agree. Reject a stale edit.
3. Connect with the default Chinese name, load a named GPKG layer; query known DEM point and region values; subtract a lake and validate geometry/area.
4. Run identical inputs with 200 m and 300 m avoidance. The latter must be a spatial subset in the analysis CRS. Generate two nonoverwriting bundles and reopen one in a clean project.

Run TypeScript/Vitest from `control-center-web`, and the package Node tests with `PAW_EARTH_GIS_PYTHON` pointing at the managed environment. Native QGIS and live GEE tests are separate from mocked adapters and local numerical tests.

## Task-led interaction and professional GIS references

The task launcher starts with user goals: find similar features, study changes,
research an area, spatial analysis, animation, and deliver results. Choosing an
entry does not execute a model or classify the current selection automatically.
The workflow explicitly assigns the study region and training samples; export
continues to distinguish whole-layer and selected-feature scopes.

The workbench borrows the layer/source/selection distinction from the
[ArcGIS Pro Contents pane](https://doc.esri.com/en/arcgis-pro/latest/help/mapping/map-authoring/contents-pane.html),
the file/database organization from the
[QGIS Browser](https://doc.qgis.org/3.44/en/docs/user_manual/introduction/browser.html),
and the map/code/inspection/task relationship from the
[Earth Engine Code Editor](https://developers.google.com/earth-engine/guides/playground).
These are interaction references, not embedded copies of those applications.

The current siting form performs distance-based exclusion and versioned
comparison. It does **not** certify continuous minimum-area constraints, road
travel times, facility capacity allocation, ownership, or engineering suitability.
The classifier produces pixel classes, not automatically verified fissure
centerlines. Brush labeling, independent geographic validation, interactive chart
brushing, and editable print atlases remain separate work. A cloud workflow plan
or a visible button is not evidence that a particular dataset ran successfully.

A short local acceptance task: **选择两块地并导出；把避让距离从 200 米改成
300 米，对比结果，再生成两版报告。** The supplied automated fixtures are
synthetic and must not be represented as real land parcels or engineering data.
