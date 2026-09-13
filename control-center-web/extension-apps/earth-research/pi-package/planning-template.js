// End-to-end verification: REAL Google terrain/land-cover; SYNTHETIC parcels,
// footprints, connection point and exclusion. This is not a planning approval.
// Edit these inputs for the actual project. Thresholds below are demonstration values.
var rules = { minContinuousAreaM2: 20000, maxSiteSlopeDegrees: 15, corridorWidthM: 30, gridScaleM: 30 };
var region = ee.Geometry.Rectangle([120.10, 30.29, 120.13, 30.32]);
var exclusion = ee.Geometry.Rectangle([120.111, 30.300, 120.118, 30.307]);
var connection = [120.128, 30.296];
var candidates = [
  { id: 'A', box: [120.103, 30.293, 120.106, 30.296], footprint: [120.104, 30.294, 120.105, 30.295], point: [120.1045, 30.2945] },
  { id: 'B', box: [120.112, 30.302, 120.115, 30.305], footprint: [120.113, 30.303, 120.114, 30.304], point: [120.1135, 30.3035] },
  { id: 'C', box: [120.122, 30.312, 120.125, 30.315], footprint: [120.123, 30.313, 120.124, 30.314], point: [120.1235, 30.3135] }
];
var projection = ee.Projection('EPSG:32651').atScale(30);
var gridProjection = projection.atScale(rules.gridScaleM);
var dem = ee.ImageCollection('COPERNICUS/DEM/GLO30_2024_1').filterBounds(region).select('DEM').mosaic().setDefaultProjection(projection);
var slope = ee.Terrain.slope(dem);
var land = ee.ImageCollection('ESA/WorldCover/v200').first().select('Map');
var known = slope.mask().unmask(0).and(land.mask().unmask(0)).rename('known');
var forbidden = ee.Image(0).byte().paint(ee.FeatureCollection([ee.Feature(exclusion)]), 1).reproject(projection);
var allowed = slope.lte(rules.maxSiteSlopeDegrees).and(land.neq(80)).and(forbidden.not()).and(known).rename('allowed');
var features = candidates.map(function(candidate) {
  var site = ee.Geometry.Rectangle(candidate.box);
  var footprint = ee.Geometry.Rectangle(candidate.footprint);
  var usable = allowed.clip(site).selfMask().toInt().reduceToVectors({geometry: site, scale: 30, crs: projection, geometryType: 'polygon', eightConnected: false, maxPixels: 100000});
  usable = usable.map(function(part) { return part.set('area_m2', part.geometry().intersection(site, 1).area(1)); });
  var continuous = ee.Algorithms.If(usable.size().gt(0), usable.aggregate_max('area_m2'), 0);
  var maxSlope = slope.reduceRegion({reducer: ee.Reducer.max(), geometry: footprint, scale: 30, crs: projection, maxPixels: 100000}).get('slope');
  var footprintAllowed = allowed.unmask(0).reduceRegion({reducer: ee.Reducer.min(), geometry: footprint, scale: 30, crs: projection, maxPixels: 100000}).get('allowed');
  var coverage = known.reduceRegion({reducer: ee.Reducer.min(), geometry: site, scale: 30, crs: projection, maxPixels: 100000}).get('known');
  return ee.Feature(site, {id: candidate.id, name: '候选 ' + candidate.id, input_origin: 'synthetic_verification', continuous_area_m2: continuous,
    footprint_contained: site.contains(footprint, 1), footprint_exclusion_m2: footprint.intersection(exclusion, 1).area(1),
    footprint_max_slope: maxSlope, footprint_allowed: footprintAllowed, coverage_min: coverage});
});
var siteResults = await Earth.evaluate(ee.FeatureCollection(features));
siteResults.features.forEach(function(feature) {
  var p = feature.properties;
  p.assessment = p.coverage_min !== 1 || !Number.isFinite(p.footprint_max_slope) ? 'unknown_data'
    : p.footprint_contained && p.footprint_exclusion_m2 === 0 && p.footprint_allowed === 1 && p.continuous_area_m2 >= rules.minContinuousAreaM2 ? 'passes_demo_constraints' : 'excluded';
});
print('数据和参数', {rules: rules, data: ['COPERNICUS/DEM/GLO30_2024_1 (DSM, 30m)', 'ESA/WorldCover/v200 (2021, 10m)'], inputs: '人工测试地块和禁区；不代表真实土地或电网边界', electrical_capacity: 'not_evaluated'});
print('候选地块评估', siteResults);
print('测试禁区', ee.FeatureCollection([ee.Feature(exclusion, {name: '人工测试禁区', input_origin: 'synthetic_verification'})]));
Map.setCenter(120.115, 30.305, 13);
Map.addLayer(slope.clip(region), {min: 0, max: 30, palette: ['edf4df','9ac984','c48b50']}, '实际坡度', false);
Map.addLayer(land.clip(region), {}, 'WorldCover 2021', false);

var routes = [], signatures = new Set(), outcomes = [];
var profiles = [{name:'距离与地形', slopeWeight:0.1, forestWeight:1}, {name:'减少林地穿越', slopeWeight:0.1, forestWeight:8}];
for (var profile of profiles) {
  var forestFraction = land.eq(10).reduceResolution({reducer: ee.Reducer.mean(), maxPixels: 1024}).reproject(gridProjection);
  var waterFraction = land.eq(80).reduceResolution({reducer: ee.Reducer.mean(), maxPixels: 1024}).reproject(gridProjection);
  var cost = slope.multiply(profile.slopeWeight).add(1).add(forestFraction.multiply(profile.forestWeight)).add(waterFraction.multiply(2))
    .updateMask(forbidden.not()).updateMask(known).rename('cost').reproject(gridProjection).clip(region);
  var grid = await Earth.evaluate(cost.toDouble().unmask(-9999).addBands(ee.Image.pixelLonLat().reproject(gridProjection)).sampleRectangle({region: region, defaultValue: -9999}));
  var costs = grid.properties.cost, longitude = grid.properties.longitude, latitude = grid.properties.latitude;
  function closest(point) {
    var best = null, distance = Infinity;
    for (var r = 0; r < longitude.length; r++) for (var c = 0; c < longitude[r].length; c++) {
      var d = Math.pow((longitude[r][c] - point[0]) * Math.cos(point[1] * Math.PI / 180), 2) + Math.pow(latitude[r][c] - point[1], 2);
      if (d < distance) { distance = d; best = [r,c]; }
    }
    return best;
  }
  for (var candidate of candidates) {
    var siteResult = siteResults.features.find(function(f) { return f.properties.id === candidate.id; });
    if (siteResult.properties.assessment !== 'passes_demo_constraints') { outcomes.push({site: candidate.id, profile: profile.name, status: 'site_not_eligible'}); continue; }
    var recovered = Earth.routeGrid(costs, closest(candidate.point), closest(connection), {clearanceCells: Math.ceil(rules.corridorWidthM / 2 / rules.gridScaleM) + 1});
    if (recovered.status !== 'completed') { outcomes.push({site: candidate.id, profile: profile.name, status: recovered.status}); continue; }
    var coordinates = [candidate.point].concat(recovered.cells.map(function(cell) { return [longitude[cell[0]][cell[1]], latitude[cell[0]][cell[1]]]; })).concat([connection]);
    var signature = JSON.stringify(coordinates);
    if (signatures.has(signature)) { outcomes.push({site: candidate.id, profile: profile.name, status: 'same_route_as_existing'}); continue; }
    var line = ee.Geometry.LineString(coordinates);
    var corridor = line.buffer(rules.corridorWidthM / 2, 1);
    var area = ee.Image.pixelArea();
    var checks = await Earth.evaluate(ee.Dictionary({exclusion_overlap_m2: corridor.intersection(exclusion, 1).area(1), outside_region_m2: corridor.difference(region, 1).area(1), length_m: line.length(1),
      coverage_min: known.reduceRegion({reducer: ee.Reducer.min(), geometry: corridor, scale: 30, crs: projection, maxPixels: 1000000}).get('known'),
      forest_area_m2: area.multiply(land.eq(10)).reduceRegion({reducer: ee.Reducer.sum(), geometry: corridor, scale: 10, crs: projection, maxPixels: 1000000}).get('area'),
      water_area_m2: area.multiply(land.eq(80)).reduceRegion({reducer: ee.Reducer.sum(), geometry: corridor, scale: 10, crs: projection, maxPixels: 1000000}).get('area'),
      slope_mean: slope.reduceRegion({reducer: ee.Reducer.mean(), geometry: corridor, scale: 30, crs: projection, maxPixels: 1000000}).get('slope')}));
    if (checks.coverage_min !== 1 || checks.exclusion_overlap_m2 > 0 || checks.outside_region_m2 > 0) { outcomes.push({site: candidate.id, profile: profile.name, status: 'corridor_failed_exact_check', checks: checks}); continue; }
    signatures.add(signature);
    routes.push(ee.Feature(line, {id: candidate.id + '-' + profile.name, name: candidate.id + ' · ' + profile.name, site: candidate.id, profile: profile.name,
      length_m: checks.length_m, exclusion_overlap_m2: checks.exclusion_overlap_m2, forest_area_m2: checks.forest_area_m2, water_area_m2: checks.water_area_m2, slope_mean: checks.slope_mean, coverage_min: checks.coverage_min,
      corridor_width_m: rules.corridorWidthM, grid_scale_m: rules.gridScaleM, relative_grid_cost: recovered.gridCost, input_origin: 'synthetic_constraints_real_terrain', electrical_capacity: 'not_evaluated'}));
    outcomes.push({site: candidate.id, profile: profile.name, status: 'completed', length_m: checks.length_m});
  }
}
print('接入线路', ee.FeatureCollection(routes));
print('比选与失败原因', outcomes);
