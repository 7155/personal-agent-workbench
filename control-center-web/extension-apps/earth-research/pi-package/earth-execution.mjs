import vm from 'node:vm';
import { routeGrid } from './route-grid.mjs';

/** Code Editor compatibility for the supported Map/print operations. This is
 * an execution adapter for authorized workspace scripts, not a security sandbox. */
export async function executeScript({ ee, script, filename = 'analysis.js', onChange = () => {} }) {
  const result = { status: 'running', code: script, filename, layers: [], console: [], view: null, error: null };
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
  publish();
  try {
    await new vm.Script(`(async () => {\n${script}\n})()`, { filename, lineOffset: -1 }).runInNewContext({ ee, Map: map, print, Earth: { evaluate, routeGrid }, console: { log: print, warn: print, error: print } }, { timeout: 10000 });
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
