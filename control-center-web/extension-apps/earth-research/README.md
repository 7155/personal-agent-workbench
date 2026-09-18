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

## Acceptance task

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
