import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { execFileSync } from 'node:child_process';
import { randomUUID, createHash } from 'node:crypto';
import { executeScript } from './earth-execution.mjs';
import https from 'node:https';

const args = process.argv.slice(2);
const option = name => { const i = args.indexOf(name); return i < 0 ? undefined : args[i + 1]; };
const root = fs.realpathSync(option('--root') || process.cwd());
const project = option('--project');
if (!project || !/^[a-z][a-z0-9-]{4,61}[a-z0-9]$/.test(project)) throw new Error('--project must be your authorized Google Cloud project ID');
const scriptPath = fs.realpathSync(path.resolve(root, option('--script') || 'analysis.js'));
if (!scriptPath.startsWith(root + path.sep)) throw new Error('Script must be inside the workspace');
const source = fs.readFileSync(scriptPath, 'utf8');
const sourceHash = createHash('sha256').update(source).digest('hex');
if (option('--expected-source-hash') && option('--expected-source-hash') !== sourceHash) throw new Error('Script changed after the run request. Nothing was executed.');
if (Buffer.byteLength(source) > 256000) throw new Error('Script exceeds 256 KB');
const runId = randomUUID();
const directory = path.join(root, '.earth', 'runs', runId);
fs.mkdirSync(directory, { recursive: true });
const pointer = path.join(root, '.earth', 'current.json');
const runFile = path.join(directory, 'run.json');
const atomicWrite = (file, value) => {
  const tmp = `${file}.${runId}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(value, null, 2), { mode: 0o600 }); fs.renameSync(tmp, file);
};
const sourceFile = path.join(root, '.earth', 'sources.json');
const sourceRefs = fs.existsSync(sourceFile) ? JSON.parse(fs.readFileSync(sourceFile, 'utf8')) : [];
const base = { schemaVersion: 'earth.run.v1', runId, project, scriptPath, toolCallId: option('--tool-call-id') || null, sourceHash, sourceRefs, startedAt: new Date().toISOString(), code: source };
atomicWrite(pointer, { runId, path: runFile });
let latest = { ...base, status: 'starting', layers: [], console: [] };
let accessToken = '';
function save(update) {
  const normalized = { ...update };
  if (Array.isArray(normalized.artifacts)) normalized.artifacts = normalized.artifacts.map(artifact => ({ ...artifact, path: typeof artifact.path === 'string' && path.isAbsolute(artifact.path) ? path.relative(root, artifact.path) : artifact.path }));
  latest = { ...base, ...normalized, updatedAt: new Date().toISOString() };
  if (accessToken) latest = JSON.parse(JSON.stringify(latest).split(accessToken).join('[REDACTED]'));
  atomicWrite(runFile, latest);
  // Each run has an immutable identity. A late run never steals the active pointer.
  const current = JSON.parse(fs.readFileSync(pointer, 'utf8'));
  if (current.runId === runId) {
    atomicWrite(path.join(root, '.earth', 'workspace.json'), latest);
    if (latest.status === 'completed') atomicWrite(path.join(root, '.earth', 'last-completed.json'), latest);
  }
}
save(latest);
const cancel = () => { save({ ...latest, status: 'cancelled', remoteComputeMayContinue: true }); process.exit(130); };
process.once('SIGTERM', cancel); process.once('SIGINT', cancel);
const timeout = setTimeout(() => { save({ ...latest, status: 'failed', error: 'Execution exceeded 5 minutes; remote computation may continue.' }); process.exit(1); }, 300000);
try {
  const dependencies = option('--dependencies') || path.join(root, '.earth', 'runtime');
  const require = createRequire(path.join(path.resolve(dependencies), 'package.json'));
  const proxy = process.env.HTTPS_PROXY || process.env.https_proxy;
  if (proxy) {
    const { HttpsProxyAgent } = require('https-proxy-agent');
    const agent = new HttpsProxyAgent(proxy);
    // Earth Engine's Node XMLHttpRequest explicitly sets agent:false, bypassing
    // Node's global agent. This dedicated child forwards its HTTPS requests via
    // the user's configured proxy; the parent PAW process is unaffected.
    const request = https.request.bind(https);
    https.request = (options, callback) => request({ ...options, agent }, callback);
  }
  const ee = require('@google/earthengine');
  const helper = path.join(path.dirname(fileURLToPath(import.meta.url)), 'auth-token.py');
  // Never place the access token in command arguments, output, artifacts or UI.
  accessToken = execFileSync(option('--python') || 'python3', [helper], { encoding: 'utf8', timeout: 30000, stdio: ['ignore', 'pipe', 'pipe'] }).trim();
  ee.data.setAuthToken('', 'Bearer', accessToken, 3600, [], undefined, false);
  await new Promise((resolve, reject) => ee.initialize(null, null, resolve, reject, null, project));
  const result = await executeScript({ ee, script: source, filename: path.basename(scriptPath), downloadDir: path.join(directory, 'artifacts'), onChange: save });
  save(result);
  console.log(JSON.stringify({ runId, status: latest.status, artifact: runFile, layers: latest.layers.length, error: latest.error }));
  process.exitCode = result.status === 'completed' ? 0 : 1;
} catch (error) {
  // Child process errors may carry private stdout. Only emit a bounded message.
  const message = error?.status !== undefined ? 'Earth Engine authentication failed; check the configured Python environment and authorization.' : String(error?.message || error);
  save({ ...latest, status: 'failed', error: message });
  console.log(JSON.stringify({ runId, status: 'failed', artifact: runFile, error: message })); process.exitCode = 1;
} finally { clearTimeout(timeout); }
