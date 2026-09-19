import vm from 'node:vm';
import { routeGrid } from './route-grid.mjs';
import https from 'node:https';
import fs from 'node:fs/promises';
import path from 'node:path';
import { createHash } from 'node:crypto';

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
  const artifactKinds = { '.json': 'json', '.csv': 'csv', '.html': 'html', '.md': 'markdown', '.svg': 'svg', '.gif': 'gif' };
  const persistArtifact = async (filename, bytes, source) => {
    if (!downloadDir) throw new Error('Artifact download directory is not configured.');
    const directory = path.resolve(downloadDir);
    await fs.mkdir(directory, { recursive: true, mode: 0o700 });
    const file = path.join(directory, filename);
    const handle = await fs.open(file, 'wx', 0o600);
    try {
      await handle.writeFile(bytes);
    } catch (error) {
      await handle.close();
      await fs.unlink(file).catch(() => {});
      throw error;
    }
    await handle.close();
    const artifact = { kind: artifactKinds[path.extname(filename).toLowerCase()], name: filename, path: file, bytes: bytes.byteLength, sha256: createHash('sha256').update(bytes).digest('hex'), source, createdAt: new Date().toISOString() };
    result.artifacts.push(artifact); publish();
    return artifact;
  };
  const validArtifactName = filename => typeof filename === 'string' && /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(filename);
  const writeArtifact = (filename, content) => {
    const job = (async () => {
      if (!validArtifactName(filename) || !['.json', '.csv', '.html', '.md', '.svg'].includes(path.extname(filename).toLowerCase())) throw new Error('Artifact filename must be a safe base filename ending in .json, .csv, .html, .md or .svg.');
      if (typeof content !== 'string') throw new TypeError('Artifact content must be a string.');
      const bytes = Buffer.from(content, 'utf8');
      if (bytes.byteLength > 4 * 1024 * 1024) throw new Error('Report artifact exceeds 4 MiB.');
      return persistArtifact(filename, bytes, 'earth.writeArtifact');
    })();
    track(job); return job;
  };
  const downloadAnimation = (collection, params = {}, filename = 'earth-animation.gif') => {
    const job = (async () => {
      if (!collection || typeof collection.getVideoThumbURL !== 'function') throw new Error('Earth.downloadAnimation requires an Earth Engine ImageCollection.');
      if (!validArtifactName(filename) || path.extname(filename).toLowerCase() !== '.gif') throw new Error('Animation filename must be a safe .gif base filename.');
      if (!downloadDir) throw new Error('Animation download directory is not configured.');
      if (!params || typeof params !== 'object' || Array.isArray(params) || !params.region) throw new TypeError('Animation parameters require a region.');
      const dimensions = params.dimensions ?? 512, framesPerSecond = params.framesPerSecond ?? 2;
      if (!Number.isInteger(dimensions) || dimensions < 1 || dimensions > 1024 || !Number.isFinite(framesPerSecond) || framesPerSecond < 1 || framesPerSecond > 30) throw new TypeError('Animation dimensions must be 1–1024 pixels and frame rate 1–30.');
      if (Object.keys(params).some(key => !['region', 'dimensions', 'framesPerSecond', 'crs', 'format'].includes(key)) || params.format && params.format !== 'gif') throw new TypeError('Animation parameters contain an unsupported option.');
      const url = await new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error('Earth Engine animation URL request timed out.')), 30_000);
        try {
          collection.getVideoThumbURL({ ...params, dimensions, framesPerSecond, format: 'gif' }, (value, error) => {
            clearTimeout(timer);
            if (error || !value) reject(new Error(String(error || 'Earth Engine returned no animation URL.')));
            else resolve(value);
          });
        } catch (error) { clearTimeout(timer); reject(error); }
      });
      const parsed = new URL(String(url));
      if (parsed.protocol !== 'https:' || parsed.hostname !== 'earthengine.googleapis.com' || parsed.username || parsed.password || parsed.port && parsed.port !== '443') throw new Error('Earth Engine returned an unexpected animation host.');
      const bytes = await new Promise((resolve, reject) => {
        const limit = 64 * 1024 * 1024;
        let settled = false;
        let request;
        const finish = (error, value) => { if (settled) return; settled = true; clearTimeout(timer); if (error) reject(error); else resolve(value); };
        const timer = setTimeout(() => { const error = new Error('Animation download timed out.'); finish(error); request?.destroy(error); }, 60_000);
        try { request = https.get(parsed, response => {
          if ((response.statusCode || 0) < 200 || (response.statusCode || 0) >= 300) { response.resume(); finish(new Error(`Animation download failed: HTTP ${response.statusCode}`)); return; }
          if (Number(response.headers['content-length']) > limit) { response.resume(); finish(new Error('Animation download exceeds 64 MiB.')); request.destroy(); return; }
          const chunks = []; let total = 0;
          response.on('data', chunk => {
            total += chunk.length;
            if (total > limit) { finish(new Error('Animation download exceeds 64 MiB.')); response.destroy(); request.destroy(); }
            else chunks.push(chunk);
          });
          response.on('end', () => finish(null, Buffer.concat(chunks)));
          response.on('error', error => finish(error));
          response.on('aborted', () => finish(new Error('Animation response was interrupted.')));
        }); } catch (error) { finish(error); return; }
        request.setTimeout(30_000, () => { const error = new Error('Animation download request timed out.'); finish(error); request.destroy(error); });
        request.on('error', error => finish(error));
      });
      if (bytes.length < 13 || !['GIF87a', 'GIF89a'].includes(bytes.subarray(0, 6).toString('ascii')) || bytes.readUInt16LE(6) < 1 || bytes.readUInt16LE(8) < 1 || bytes.at(-1) !== 0x3b) throw new Error('Earth Engine response is not a valid GIF animation.');
      return persistArtifact(filename, bytes, 'earthengine.getVideoThumbURL');
    })();
    track(job); return job;
  };
  publish();
  try {
    await new vm.Script(`(async () => {\n${script}\n})()`, { filename, lineOffset: -1 }).runInNewContext({ ee, Map: map, Export: exportApi, print, Earth: { evaluate, routeGrid, downloadImage, writeArtifact, downloadAnimation }, console: { log: print, warn: print, error: print } }, { timeout: 10000 });
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
