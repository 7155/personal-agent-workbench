import vm from 'node:vm';
import { routeGrid } from './route-grid.mjs';
import https from 'node:https';

/** Code Editor compatibility for the supported Map/print operations. This is
 * an execution adapter for authorized workspace scripts, not a security sandbox. */
export async function executeScript({ ee, script, filename = 'analysis.js', onChange = () => {}, downloadDir = null }) {
  const result = { status: 'running', code: script, filename, layers: [], console: [], view: null, tasks: [], artifacts: [], error: null };
  const jobs = [];
  const publish = () => onChange(structuredClone(result));
  const evaluate = value => value && typeof value.evaluate === 'function'
    ? new Promise((resolve, reject) => value.evaluate((data, error) => error ? reject(new Error(String(error))) : resolve(data)))
    : Promise.resolve(value);
  const track = task => { jobs.push(task); task.catch(() => {}); };
  const print = (...values) => {
    const row = { values: [], pending: true };
    result.console.push(row); publish();
    track(Promise.all(values.map(evaluate)).then(data => { row.values = data; row.pending = false; publish(); }));
  };
  const map = {
    addLayer(object, style = {}, name = '图层', shown = true, opacity = 1) {
      const index = result.layers.length;
      const layer = { id: `layer-${index}`, name, style, shown, opacity, status: 'loading' };
      result.layers.push(layer); publish();
      // Raster and vector rendering both use the SDK's actual getMap result.
      track(new Promise((resolve, reject) => {
        if (!object || typeof object.getMap !== 'function') return reject(new Error('Map.addLayer requires an Earth Engine image or collection'));
        object.getMap(style, (data, error) => {
          if (error || !data) { result.layers = result.layers.filter(x => x !== layer); publish(); reject(new Error(String(error || 'Map result missing'))); return; }
          const tileUrl = data.urlFormat ?? data.tile_fetcher?.url_format;
          if (!tileUrl) { reject(new Error('Earth Engine returned no tile URL')); return; }
          Object.assign(layer, { status: 'ready', tileUrl }); publish(); resolve();
        });
      }));
      return layer;
    },
    setCenter(lon, lat, zoom = 10) {
      if (![lon, lat, zoom].every(Number.isFinite) || Math.abs(lon) > 180 || Math.abs(lat) > 90) throw new Error('Invalid map center');
      result.view = { center: [lon, lat], zoom }; publish();
    },
    centerObject(object, zoom = 10) {
      const geometry = typeof object.geometry === 'function' ? object.geometry() : object;
      track(evaluate(geometry.centroid(1)).then(data => map.setCenter(...data.coordinates, zoom)));
    },
  };
  const exportApi = {};
  for (const kind of ['image', 'table', 'map', 'video', 'classifier']) {
    if (!ee?.batch?.Export?.[kind]) continue;
    exportApi[kind] = {};
    for (const destination of ['toDrive', 'toCloudStorage', 'toAsset', 'toFeatureView']) {
      if (typeof ee.batch.Export[kind][destination] !== 'function') continue;
      exportApi[kind][destination] = (...args) => {
        const task = ee.batch.Export[kind][destination](...args);
        const descriptor = { id: null, kind, destination, status: 'starting', submittedAt: new Date().toISOString() };
        result.tasks.push(descriptor); publish();
        try {
          task.start();
          descriptor.id = task.id || null;
          descriptor.status = descriptor.id ? 'submitted' : 'unknown';
          publish();
        } catch (error) {
          descriptor.status = 'failed'; descriptor.error = String(error?.message || error); publish(); throw error;
        }
        return task;
      };
    }
  }
  const downloadImage = async (object, params = {}, filename = 'earth-image.tif') => {
    if (!object || typeof object.getDownloadURL !== 'function') throw new Error('Earth.downloadImage requires an Earth Engine image.');
    if (!/^[-\w.]+$/.test(filename) || !filename.toLowerCase().endsWith('.tif')) throw new Error('Downloaded image name must be a safe .tif filename.');
    const url = await new Promise((resolve, reject) => object.getDownloadURL(params, (value, error) => error ? reject(new Error(String(error))) : resolve(value)));
    const parsed = new URL(String(url));
    if (parsed.protocol !== 'https:' || parsed.hostname !== 'earthengine.googleapis.com') throw new Error('Earth Engine returned an unexpected download host.');
    const bytes = await new Promise((resolve, reject) => {
      const request = https.get(parsed, response => {
        if ((response.statusCode || 0) < 200 || (response.statusCode || 0) >= 300) { response.resume(); reject(new Error(`Earth Engine download failed: HTTP ${response.statusCode}`)); return; }
        const chunks = []; let total = 0;
        response.on('data', chunk => { total += chunk.length; if (total <= 128 * 1024 * 1024) chunks.push(chunk); else { response.destroy(); reject(new Error('Direct image download exceeds 128 MiB; use an Export task.')); } });
        response.on('end', () => resolve(Buffer.concat(chunks)));
        response.on('error', reject);
      });
      request.on('error', reject);
    });
    if (bytes.byteLength > 128 * 1024 * 1024) throw new Error('Direct image download exceeds 128 MiB; use an Export task.');
    if (!downloadImage.directory) throw new Error('Download directory is not configured.');
    const file = `${downloadImage.directory}/${filename}`;
    const fs = await import('node:fs/promises');
    await fs.mkdir(downloadImage.directory, { recursive: true });
    await fs.writeFile(file, bytes, { mode: 0o600 });
    const artifact = { kind: 'geotiff', path: file, bytes: bytes.byteLength, source: 'earthengine.getDownloadURL', createdAt: new Date().toISOString() };
    result.artifacts.push(artifact); publish();
    return artifact;
  };
  downloadImage.directory = downloadDir;
  publish();
  try {
    await new vm.Script(`(async () => {\n${script}\n})()`, { filename, lineOffset: -1 }).runInNewContext({ ee, Map: map, Export: exportApi, print, Earth: { evaluate, routeGrid, downloadImage }, console: { log: print, warn: print, error: print } }, { timeout: 10000 });
    // A callback may enqueue another visible operation while jobs settle.
    let settled = 0;
    while (settled < jobs.length) {
      const batch = jobs.slice(settled); settled = jobs.length;
      const outcomes = await Promise.allSettled(batch);
      const failure = outcomes.find(x => x.status === 'rejected');
      if (failure) throw failure.reason;
    }
    result.status = 'completed';
  } catch (error) {
    result.status = 'failed'; result.error = String(error?.stack || error);
  }
  publish(); return result;
}
