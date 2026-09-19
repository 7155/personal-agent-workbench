import fs from 'node:fs';
import path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';

const COLLECTION = 'COPERNICUS/S2_SR_HARMONIZED';
const kinds = new Set(['ndvi', 'change', 'animation', 'research']);
const fields = new Set(['kind', 'region', 'dateFrom', 'dateTo', 'collection', 'scale', 'bands', 'interval', 'splitDate', 'dimensions', 'framesPerSecond', 'question']);
const references = [
  { title: 'Sentinel-2 surface reflectance and SCL definitions', url: 'https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S2_SR_HARMONIZED' },
  { title: 'Earth Engine regional reduction and pixel inclusion', url: 'https://developers.google.com/earth-engine/guides/reducers_reduce_region' },
  { title: 'Earth Engine GIF thumbnail API', url: 'https://developers.google.com/earth-engine/apidocs/ee-imagecollection-getvideothumburl' },
];

function date(value, field) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) throw new TypeError(`${field} must be an ISO calendar date.`);
  const result = new Date(`${value}T00:00:00Z`);
  if (!Number.isFinite(result.getTime()) || result.toISOString().slice(0, 10) !== value) throw new TypeError(`${field} is not a valid calendar date.`);
  return value;
}

function normalizeRegion(value) {
  const geometry = value?.type === 'Feature' ? value.geometry : value;
  if (!geometry || !['Polygon', 'MultiPolygon'].includes(geometry.type) || !Array.isArray(geometry.coordinates)) throw new TypeError('Cloud region must be a WGS84 Polygon or MultiPolygon.');
  const polygons = geometry.type === 'Polygon' ? [geometry.coordinates] : geometry.coordinates;
  let vertices = 0;
  if (!polygons.length) throw new TypeError('Cloud region is empty.');
  const normalized = polygons.map(polygon => {
    if (!Array.isArray(polygon) || !polygon.length) throw new TypeError('Cloud polygon is empty.');
    return polygon.map(ring => {
      if (!Array.isArray(ring) || ring.length < 4) throw new TypeError('Cloud region rings need at least four positions.');
      const positions = ring.map(position => {
        if (!Array.isArray(position) || position.length < 2 || position.length > 3 || !position.every(Number.isFinite) || Math.abs(position[0]) > 180 || Math.abs(position[1]) > 90) throw new TypeError('Cloud region coordinates must be finite WGS84 positions.');
        if (++vertices > 20_000) throw new TypeError('Cloud region exceeds 20,000 vertices.');
        return position.slice(0, 2);
      });
      if (positions[0][0] !== positions.at(-1)[0] || positions[0][1] !== positions.at(-1)[1]) throw new TypeError('Cloud region rings must be closed.');
      if (new Set(positions.slice(0, -1).map(position => position.join(','))).size < 3) throw new TypeError('Cloud region rings need three distinct vertices.');
      return positions;
    });
  });
  return { type: geometry.type, coordinates: geometry.type === 'Polygon' ? normalized[0] : normalized };
}

function periodsBetween(start, end, interval) {
  const periods = [];
  let cursor = start;
  while (cursor < end) {
    const current = new Date(`${cursor}T00:00:00Z`);
    const boundary = interval === 'annual' ? new Date(Date.UTC(current.getUTCFullYear() + 1, 0, 1)) : new Date(Date.UTC(current.getUTCFullYear(), current.getUTCMonth() + 1, 1));
    const next = boundary.toISOString().slice(0, 10) < end ? boundary.toISOString().slice(0, 10) : end;
    periods.push({ label: cursor.slice(0, interval === 'annual' ? 4 : 7), start: cursor, end: next });
    if (periods.length > 120) throw new TypeError('Cloud workflow exceeds 120 periods; use an annual interval or a shorter date range.');
    cursor = next;
  }
  return periods;
}

function normalizePlan(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input) || !kinds.has(input.kind)) throw new TypeError('Unsupported cloud workflow kind.');
  if (Object.keys(input).some(key => !fields.has(key))) throw new TypeError('Cloud workflow contains an unsupported plan field; JavaScript is not a plan parameter.');
  const region = normalizeRegion(input.region);
  const dateFrom = date(input.dateFrom, 'dateFrom'), dateTo = date(input.dateTo, 'dateTo');
  if (dateFrom >= dateTo) throw new TypeError('dateTo must follow dateFrom; the end date is exclusive.');
  if (input.collection !== undefined && input.collection !== COLLECTION) throw new TypeError(`This cloud adapter supports ${COLLECTION}.`);
  const requestedBands = Array.isArray(input.bands) ? { red: input.bands[0], nir: input.bands[1] } : input.bands;
  const bands = requestedBands ?? { red: 'B4', nir: 'B8' };
  if (!bands || bands.red !== 'B4' || !['B8', 'B8A'].includes(bands.nir) || Object.keys(bands).some(key => !['red', 'nir'].includes(key)) || Array.isArray(input.bands) && input.bands.length !== 2) throw new TypeError('Sentinel-2 NDVI bands must be red B4 and near-infrared B8 or B8A.');
  const scale = input.scale ?? (bands.nir === 'B8A' ? 20 : 10);
  if (!Number.isFinite(scale) || scale < 10 || scale > 10_000) throw new TypeError('Cloud scale must be between 10 and 10,000 meters.');
  const interval = input.interval ?? 'monthly';
  if (!['monthly', 'annual'].includes(interval)) throw new TypeError('Cloud interval must be monthly or annual.');
  const plan = { kind: input.kind, region, dateFrom, dateTo, collection: COLLECTION, scale, bands: { red: bands.red, nir: bands.nir }, interval, reductionCrs: 'EPSG:4326', maxPixels: 10_000_000, sclClasses: [4, 5, 6] };
  if (input.kind === 'change') {
    const start = Date.parse(`${dateFrom}T00:00:00Z`), days = (Date.parse(`${dateTo}T00:00:00Z`) - start) / 86_400_000;
    if (days < 2) throw new TypeError('Change detection requires at least two days and two non-empty date intervals.');
    const splitDate = input.splitDate === undefined ? new Date(start + Math.floor(days / 2) * 86_400_000).toISOString().slice(0, 10) : date(input.splitDate, 'splitDate');
    if (splitDate <= dateFrom || splitDate >= dateTo) throw new TypeError('splitDate must fall strictly inside the date range.');
    Object.assign(plan, { splitDate, splitStrategy: input.splitDate === undefined ? 'midpoint' : 'explicit', periods: [{ label: 'before', start: dateFrom, end: splitDate }, { label: 'after', start: splitDate, end: dateTo }] });
  } else {
    if (input.splitDate !== undefined) throw new TypeError('splitDate is only supported for change detection.');
    plan.periods = periodsBetween(dateFrom, dateTo, interval);
  }
  if (input.kind === 'animation') {
    const dimensions = input.dimensions ?? 512, framesPerSecond = input.framesPerSecond ?? 2;
    if (!Number.isInteger(dimensions) || dimensions < 64 || dimensions > 1024 || !Number.isFinite(framesPerSecond) || framesPerSecond < 1 || framesPerSecond > 30) throw new TypeError('Animation dimensions must be 64–1024 pixels and frame rate 1–30.');
    Object.assign(plan, { dimensions, framesPerSecond });
  } else if (input.dimensions !== undefined || input.framesPerSecond !== undefined) throw new TypeError('Animation options are only supported for animation plans.');
  if (input.question !== undefined && (input.kind !== 'research' || typeof input.question !== 'string' || !input.question.trim() || input.question.length > 4_000)) throw new TypeError('Research question must be non-empty text of at most 4,000 characters.');
  if (input.kind === 'research') plan.question = input.question?.trim() || 'Assess data coverage, NDVI methods, uncertainty and evidence for this region and date range.';
  return plan;
}

function literal(value) { return JSON.stringify(value).replace(/\u2028/g, '\\u2028').replace(/\u2029/g, '\\u2029'); }

const reportHelpers = String.raw`
function escapeHtml(value) { return String(value == null ? '' : value).replace(/[&<>"']/g, function(character) { return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[character]; }); }
function finiteOrNull(value) { return typeof value === 'number' && Number.isFinite(value) ? value : null; }
function display(value) { return value == null ? 'No valid pixels' : Number(value).toFixed(4); }
function csvCell(value) { return '"' + String(value == null ? '' : value).replace(/"/g, '""') + '"'; }
function chartSvg(rows) {
  if (!rows.some(function(row) { return row.meanNdvi !== null; })) return '';
  var width = 900, height = 270, left = 64, top = 20, bottom = 224, right = 876;
  var segments = [], active = [], circles = [];
  rows.forEach(function(row, index) {
    if (row.meanNdvi === null) { if (active.length) segments.push(active.join(' ')); active = []; return; }
    var x = left + (right - left) * (rows.length > 1 ? index / (rows.length - 1) : 0.5);
    var y = bottom - (row.meanNdvi + 1) * (bottom - top) / 2;
    active.push((active.length ? 'L' : 'M') + x.toFixed(2) + ' ' + y.toFixed(2));
    circles.push('<circle cx="' + x.toFixed(2) + '" cy="' + y.toFixed(2) + '" r="4"><title>' + escapeHtml(row.label + ': ' + display(row.meanNdvi)) + '</title></circle>');
  });
  if (active.length) segments.push(active.join(' '));
  return '<svg role="img" aria-label="Observed NDVI by period; missing periods are gaps" viewBox="0 0 ' + width + ' ' + height + '"><title>Observed mean of each period median NDVI</title><path d="M64 20V224H876" fill="none" stroke="#9ca3af"/><path d="M64 122H876" stroke="#e5e7eb"/><g fill="#4b5563" font-size="13"><text x="20" y="24">1.0</text><text x="20" y="126">0.0</text><text x="16" y="228">-1.0</text><text x="64" y="254">' + escapeHtml(rows[0].periodStart) + '</text><text x="876" y="254" text-anchor="end">' + escapeHtml(rows[rows.length - 1].periodEnd) + '</text></g><g fill="none" stroke="#15803d" stroke-width="2">' + segments.map(function(segment) { return '<path d="' + segment + '"/>'; }).join('') + '</g><g fill="#15803d">' + circles.join('') + '</g></svg>';
}
async function writeResults(result, animation) {
  result.generatedAt = new Date().toISOString();
  await Earth.writeArtifact('statistics.json', JSON.stringify(result, null, 2));
  var columns = ['label', 'periodStart', 'periodEnd', 'imageCount', 'meanNdvi', 'minNdvi', 'maxNdvi', 'validPixelCount', 'validPixelCoverage'];
  var csv = [columns.join(',')].concat(result.rows.map(function(row) { return columns.map(function(key) { return csvCell(row[key]); }).join(','); })).join('\n');
  await Earth.writeArtifact('statistics.csv', csv);
  var table = result.rows.map(function(row) { return '<tr><th>' + escapeHtml(row.label) + '</th><td>' + escapeHtml(row.periodStart + ' – ' + row.periodEnd) + '</td><td>' + row.imageCount + '</td><td>' + display(row.meanNdvi) + '</td><td>' + row.validPixelCount + '</td><td>' + (row.validPixelCoverage * 100).toFixed(1) + '%</td></tr>'; }).join('');
  var delta = result.change ? '<h2>NDVI difference on common valid pixels</h2><p>After minus before: ' + display(result.change.meanDeltaNdvi) + '. Common valid cells: ' + result.change.commonValidPixelCount + ' (' + (result.change.validPixelCoverage * 100).toFixed(1) + '%).</p><p>Intervals may not be seasonally matched. An observed NDVI difference is not causal attribution or land-cover classification.</p>' : '';
  var animationHtml = animation ? '<h2>Observed temporal composites</h2><img src="animation.gif" alt="NDVI animation of periods with valid observations"><p>' + result.frameCount + ' frames, ' + plan.framesPerSecond + ' frames per second. Missing periods are excluded and remain listed in the table.</p>' : '';
  var html = '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Earth workflow results</title><style>body{font:16px/1.6 system-ui,sans-serif;margin:40px auto;max-width:1040px;padding:0 24px;color:#172b25;background:#fff}h1{font-size:32px;line-height:1.2}h2{margin-top:32px}svg{width:100%;height:auto}img{max-width:100%}table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;border-bottom:1px solid #ddd;padding:10px}code{overflow-wrap:anywhere}a{color:#166534}.muted{color:#52635a}</style><main><h1>' + escapeHtml(plan.kind.toUpperCase()) + ' · Earth Engine observations</h1><p>Status: <strong>' + escapeHtml(result.status) + '</strong>. ' + escapeHtml(result.message || '') + '</p><p class="muted">' + escapeHtml(plan.dateFrom + ' to ' + plan.dateTo) + ' (end exclusive) · ' + plan.scale + ' m reduction scale · ' + result.sourceImageCount + ' source scenes</p>' + chartSvg(result.rows) + animationHtml + delta + '<h2>Period statistics</h2><table><thead><tr><th>Period</th><th>Date interval</th><th>Scenes</th><th>Mean NDVI</th><th>Valid cells</th><th>Coverage</th></tr></thead><tbody>' + table + '</tbody></table><h2>Method and limits</h2><p>Median NDVI composite per period, then an unweighted pixel mean on a fixed EPSG:4326 grid at the stated scale. SCL classes 4, 5 and 6 are retained; cloud, shadow, snow, defective and unclassified pixels are excluded. Missing observations remain missing. Coverage is valid composite cells divided by all region grid cells. No best-effort scale adjustment is used.</p><p>NDVI = (NIR − red) / (NIR + red), using ' + escapeHtml(plan.bands.nir + ' and ' + plan.bands.red) + '. SCL is a 20 m classification; cloud-mask and compositing uncertainty remain. Early Sentinel-2 L2A coverage is incomplete in some regions.</p><p><code>' + escapeHtml(plan.collection) + '</code> · Plan <code>' + escapeHtml(planId) + '</code></p><p><a href="statistics.json">Raw evaluated statistics and source scene IDs</a> · <a href="statistics.csv">CSV</a></p><ul>' + references.map(function(reference) { return '<li><a href="' + escapeHtml(reference.url) + '">' + escapeHtml(reference.title) + '</a></li>'; }).join('') + '</ul></main></html>';
  await Earth.writeArtifact('report.html', html);
  print(result);
}
`;

const cloudPreparation = String.raw`
var region = ee.Geometry(plan.region, null, false);
var source = ee.ImageCollection(plan.collection).filterBounds(region).filterDate(plan.dateFrom, plan.dateTo).sort('system:time_start');
var sourceImageCount = await Earth.evaluate(source.size());
if (!Number.isInteger(sourceImageCount) || sourceImageCount < 0) throw new Error('Earth Engine returned an invalid source scene count.');
var result = { schemaVersion: 'earth.cloud-workflow-result.v1', planId: planId, plan: plan, backend: 'gee', kind: plan.kind, status: 'completed', collection: plan.collection, dateFrom: plan.dateFrom, dateTo: plan.dateTo, scale: plan.scale, reductionCrs: plan.reductionCrs, sourceImageCount: sourceImageCount, sourceSceneIds: [], sourceSceneIdsTruncated: sourceImageCount > 500, rows: [] };
if (sourceImageCount === 0) { result.status = 'no_data'; result.message = 'No source images intersect this region and date range.'; await writeResults(result, false); return; }
result.sourceSceneIds = await Earth.evaluate(source.limit(500).aggregate_array('system:index'));
if (!Array.isArray(result.sourceSceneIds) || !result.sourceSceneIds.every(function(id) { return typeof id === 'string'; })) throw new Error('Earth Engine returned invalid scene identities.');
function maskedNdvi(image) {
  var scl = image.select('SCL');
  var clear = scl.eq(4).or(scl.eq(5)).or(scl.eq(6));
  var red = image.select(plan.bands.red).multiply(0.0001), nir = image.select(plan.bands.nir).multiply(0.0001);
  var denominator = nir.add(red);
  return nir.subtract(red).divide(denominator).rename('NDVI').updateMask(clear).updateMask(denominator.neq(0)).copyProperties(image, ['system:time_start', 'system:index']);
}
var observations = source.map(maskedNdvi);
var reduction = { geometry: region, scale: plan.scale, crs: plan.reductionCrs, maxPixels: plan.maxPixels, bestEffort: false, tileScale: 2 };
var totalRegionPixels = await Earth.evaluate(ee.Image.constant(1).rename('coverage').reduceRegion(Object.assign({}, reduction, { reducer: ee.Reducer.count() })).get('coverage'));
if (!Number.isInteger(totalRegionPixels) || totalRegionPixels < 1) throw new Error('The requested region contains no grid cells at this scale.');
result.totalRegionPixels = totalRegionPixels;
var statisticsReducer = ee.Reducer.mean().unweighted().combine({ reducer2: ee.Reducer.minMax().unweighted(), sharedInputs: true }).combine({ reducer2: ee.Reducer.count(), sharedInputs: true });
function composite(start, end) {
  var subset = observations.filterDate(start, end);
  var empty = ee.Image.constant(0).rename('NDVI').updateMask(ee.Image.constant(0));
  return ee.Image(ee.Algorithms.If(subset.size().gt(0), subset.median(), empty)).clip(region);
}
function periodFeature(period) {
  var subset = observations.filterDate(period.start, period.end);
  var stats = composite(period.start, period.end).reduceRegion(Object.assign({}, reduction, { reducer: statisticsReducer }));
  return ee.Feature(null, { label: period.label, periodStart: period.start, periodEnd: period.end, imageCount: subset.size(), meanNdvi: stats.get('NDVI_mean', null), minNdvi: stats.get('NDVI_min', null), maxNdvi: stats.get('NDVI_max', null), validPixelCount: stats.get('NDVI_count', 0) });
}
var evaluated = await Earth.evaluate(ee.FeatureCollection(plan.periods.map(periodFeature)));
if (!evaluated || !Array.isArray(evaluated.features) || evaluated.features.length !== plan.periods.length) throw new Error('Earth Engine returned an incomplete period table.');
result.rows = evaluated.features.map(function(feature, index) {
  var properties = feature.properties || {}, period = plan.periods[index];
  if (properties.periodStart !== period.start || properties.periodEnd !== period.end) throw new Error('Earth Engine returned mismatched period identities.');
  var valid = properties.validPixelCount == null ? 0 : properties.validPixelCount;
  if (!Number.isInteger(valid) || valid < 0 || valid > totalRegionPixels || !Number.isInteger(properties.imageCount) || properties.imageCount < 0) throw new Error('Earth Engine returned invalid observation counts.');
  var mean = finiteOrNull(properties.meanNdvi), minimum = finiteOrNull(properties.minNdvi), maximum = finiteOrNull(properties.maxNdvi);
  if (valid > 0 && (mean === null || minimum === null || maximum === null)) throw new Error('Valid pixels are missing their observed NDVI statistics.');
  return { label: period.label, periodStart: period.start, periodEnd: period.end, imageCount: properties.imageCount, meanNdvi: valid ? mean : null, minNdvi: valid ? minimum : null, maxNdvi: valid ? maximum : null, validPixelCount: valid, validPixelCoverage: valid / totalRegionPixels };
});
if (!result.rows.some(function(row) { return row.validPixelCount > 0; })) { result.status = 'no_data'; result.message = 'All source pixels were masked or outside the region.'; await writeResults(result, false); return; }
Map.centerObject(region, 10);
`;

const ndviExecution = String.raw`
Map.addLayer(observations.median().clip(region), { min: -1, max: 1, palette: ['8c510a', 'f6e8c3', 'c7eae5', '01665e'] }, 'Observed median NDVI: ' + plan.dateFrom + ' to ' + plan.dateTo);
await writeResults(result, false);
`;

const changeExecution = String.raw`
var before = composite(plan.periods[0].start, plan.periods[0].end);
var after = composite(plan.periods[1].start, plan.periods[1].end);
var difference = after.subtract(before).rename('NDVI');
var changeStats = await Earth.evaluate(difference.reduceRegion(Object.assign({}, reduction, { reducer: statisticsReducer })));
var commonValidPixelCount = changeStats.NDVI_count == null ? 0 : changeStats.NDVI_count;
if (!Number.isInteger(commonValidPixelCount) || commonValidPixelCount < 0 || commonValidPixelCount > totalRegionPixels) throw new Error('Earth Engine returned an invalid common-valid pixel count.');
result.change = { method: 'after-minus-before period median NDVI on common valid pixels', splitDate: plan.splitDate, splitStrategy: plan.splitStrategy, commonValidPixelCount: commonValidPixelCount, validPixelCoverage: commonValidPixelCount / totalRegionPixels, meanDeltaNdvi: finiteOrNull(changeStats.NDVI_mean), minDeltaNdvi: finiteOrNull(changeStats.NDVI_min), maxDeltaNdvi: finiteOrNull(changeStats.NDVI_max) };
if (commonValidPixelCount > 0 && result.change.meanDeltaNdvi === null) throw new Error('Common valid pixels are missing their change statistics.');
if (!commonValidPixelCount) { result.status = 'no_data'; result.message = 'The before and after periods have no common valid pixels.'; }
else Map.addLayer(difference, { min: -0.5, max: 0.5, palette: ['b2182b', 'f7f7f7', '2166ac'] }, 'Observed NDVI change: after minus before');
await writeResults(result, false);
`;

const animationExecution = String.raw`
var validPeriods = result.rows.filter(function(row) { return row.validPixelCount > 0; });
result.frameCount = validPeriods.length;
if (validPeriods.length < 2) { result.status = 'insufficient_frames'; result.message = 'An animation requires at least two periods with valid observations.'; await writeResults(result, false); return; }
var frames = ee.ImageCollection.fromImages(validPeriods.map(function(row) { return composite(row.periodStart, row.periodEnd).visualize({ min: -1, max: 1, palette: ['8c510a', 'f6e8c3', 'c7eae5', '01665e'] }).set('system:time_start', Date.parse(row.periodStart + 'T00:00:00Z')); }));
var animation = await Earth.downloadAnimation(frames, { region: plan.region, dimensions: plan.dimensions, framesPerSecond: plan.framesPerSecond, crs: plan.reductionCrs }, 'animation.gif');
result.animation = { filename: 'animation.gif', bytes: animation.bytes, sha256: animation.sha256, framesPerSecond: plan.framesPerSecond, dimensions: plan.dimensions, framePeriods: validPeriods.map(function(row) { return { start: row.periodStart, end: row.periodEnd }; }) };
await writeResults(result, true);
`;

function ensureWorkspaceDirectory(root, relative) {
  const directory = path.join(root, relative);
  let ancestor = directory;
  while (!fs.existsSync(ancestor)) ancestor = path.dirname(ancestor);
  const actual = fs.realpathSync(ancestor);
  if (actual !== root && !actual.startsWith(root + path.sep)) throw new Error('Cloud workflow path resolves outside the workspace.');
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  return directory;
}

/** Prepare immutable scripts and a typed plan. This function never initializes or calls Earth Engine. */
export function prepareCloudWorkflow({ root, plan: input }) {
  if (typeof root !== 'string' || !path.isAbsolute(root) || !fs.statSync(root).isDirectory()) throw new TypeError('An existing absolute cloud workspace root is required.');
  root = fs.realpathSync(root);
  const plan = normalizePlan(input), planId = randomUUID();
  const relativeDirectory = `.earth/cloud-workflows/${planId}`;
  const directory = ensureWorkspaceDirectory(root, relativeDirectory);
  const requirements = [
    { key: 'region', label: 'WGS84 region', status: 'provided', detail: 'The exact polygon is stored; Earth Engine validates topology during execution.' },
    { key: 'dates', label: 'Date range', status: 'provided', detail: `${plan.dateFrom} through ${plan.dateTo}, exclusive end.` },
    { key: 'collection', label: 'Sentinel-2 L2A collection', status: 'provided', detail: `${COLLECTION}; keep SCL classes 4, 5, 6; red ${plan.bands.red}, NIR ${plan.bands.nir}.` },
    { key: 'scale', label: 'Fixed reduction grid', status: 'provided', detail: `${plan.scale} m in EPSG:4326, at most ${plan.maxPixels} cells per reduction; no automatic scale change.` },
    { key: 'authentication', label: 'Earth Engine authorization', status: 'preparation_only', detail: 'Authorization, project access and quota are checked at execution. Preparation does not probe them.' },
  ];
  let script, evidenceRequestPath;
  if (plan.kind === 'research') {
    evidenceRequestPath = `${relativeDirectory}/evidence-request.json`;
    const evidence = { schemaVersion: 'earth.research-evidence-request.v1', planId, status: 'awaiting_sources', question: plan.question, sourcesQueried: false, report: null, region: plan.region, dateFrom: plan.dateFrom, dateTo: plan.dateTo, references, requests: [{ kind: 'dataset_catalog', url: references[0].url, fields: ['coverage', 'bands', 'SCL class definitions', 'processing limitations'] }, { kind: 'scene_inventory', collection: plan.collection, region: plan.region, dateFrom: plan.dateFrom, dateTo: plan.dateTo, required: ['scene IDs', 'acquisition dates', 'valid pixel counts', 'cloud-mask method'] }, { kind: 'research_sources', query: plan.question, required: ['source URL or DOI', 'publication date', 'relevant passage', 'claim and uncertainty'] }], completion: 'Write a research report only after source queries return evidence; keep unavailable data and unsupported conclusions explicit.' };
    fs.writeFileSync(path.join(directory, 'evidence-request.json'), JSON.stringify(evidence, null, 2), { flag: 'wx', mode: 0o600 });
    script = `// Evidence request only. No research or cloud result has been produced.\nprint(${literal(evidence)});\n`;
    requirements.push({ key: 'evidence', label: 'Research evidence', status: 'missing', detail: 'Query the recorded data and research sources before writing or claiming a report.' });
  } else {
    script = `// Prepared Earth Engine workflow. Run explicitly through earth_run_script after authorization.\nvar planId = ${literal(planId)};\nvar plan = ${literal(plan)};\nvar references = ${literal(references)};\n${reportHelpers}\n${cloudPreparation}\n${plan.kind === 'change' ? changeExecution : plan.kind === 'animation' ? animationExecution : ndviExecution}`;
  }
  const scriptPath = `${relativeDirectory}/analysis.js`, recordPath = `${relativeDirectory}/plan.json`;
  fs.writeFileSync(path.join(directory, 'analysis.js'), script, { flag: 'wx', mode: 0o600 });
  const record = { schemaVersion: 'earth.cloud-workflow-plan.v1', planId, backend: 'gee', execution: 'gee', status: 'prepared', runnable: plan.kind !== 'research', createdAt: new Date().toISOString(), path: recordPath, scriptPath, scriptSha256: createHash('sha256').update(script).digest('hex'), plan, requirements, references, ...(evidenceRequestPath ? { evidenceRequestPath } : {}) };
  fs.writeFileSync(path.join(directory, 'plan.json'), JSON.stringify(record, null, 2), { flag: 'wx', mode: 0o600 });
  return record;
}
