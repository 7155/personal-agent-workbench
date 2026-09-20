import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { connectSpatialSource, listSpatialSources, loadSpatialLayer } from './gis-operations.mjs';

const python = process.env.PAW_EARTH_GIS_PYTHON || 'python3';
const runner = fileURLToPath(new URL('./gis-runner.py', import.meta.url));
const livePostGIS = { skip: !process.env.PAW_POSTGIS_TEST_URL ? 'set PAW_POSTGIS_TEST_URL to an isolated PostGIS test database and use a Python runtime with psycopg' : false };

// A DB-API fixture checks the execution contract offline. It is deliberately
// separate from the opt-in real PostGIS acceptance below.
const driverFixture = String.raw`
import json, os
from pathlib import Path

class SQL:
    def __init__(self, value): self.value = value
    def __str__(self): return self.value
    def format(self, *args, **kwargs):
        return SQL(self.value.format(*[str(item) for item in args], **{key: str(value) for key, value in kwargs.items()}))
    def join(self, values): return SQL(self.value.join(str(item) for item in values))

class Identifier(SQL):
    def __init__(self, *parts): super().__init__('.'.join('"' + part.replace('"', '""') + '"' for part in parts))

class SqlModule: pass
sql = SqlModule(); sql.SQL = SQL; sql.Identifier = Identifier

def log(value):
    with open(os.environ['PAW_POSTGIS_TEST_LOG'], 'a') as handle: handle.write(json.dumps(value) + '\n')

class DriverError(Exception):
    def __init__(self, state):
        super().__init__('unsafe driver exception: ' + os.environ['PAW_POSTGIS_TEST_URL'])
        self.sqlstate = state

class Cursor:
    def __init__(self, name=None): self.name = name; self.rows = []; self.position = 0
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, query, params=()):
        query = str(query)
        log({'sql': query, 'params': list(params), 'serverSide': bool(self.name)})
        case = os.environ.get('PAW_POSTGIS_TEST_CASE', '')
        self.position = 0
        if 'pg_catalog.pg_extension' in query:
            self.rows = [] if case == 'no_extension' else [('postgis schema', 123, '3.5.2')]
        elif 'postgis_typmod_srid' in query:
            self.rows = [('survey data', 'roads"; DROP TABLE sentinel;--', 'shape', 'geography' if case == 'geography' else 'geometry', 3857, 'POINT', 456)]
        elif 'pg_catalog.pg_type' in query:
            self.rows = [('id', 'int8', 11), ('name"; SELECT private--', 'text', 11), ('shape', 'geometry', 123)]
        elif 'pg_catalog.pg_index' in query:
            self.rows = [('id',)]
        elif 'spatial_ref_sys' in query:
            self.rows = [('EPSG', 3857, '')]
        elif self.name:
            if case == 'permission': raise DriverError('42501')
            if case == 'timeout': raise DriverError('57014')
            value = 'x' * (1024 * 1024) if case == 'bytes' else 'A'
            srid = 0 if case == 'unknown_crs' else 3857
            row = (srid, None if srid == 0 else '{"type":"Point","coordinates":[1,2]}', json.dumps({'id': 9007199254740993, 'name"; SELECT private--': value}), False)
            if case == 'feature_bytes': row = (srid, None, None, True)
            self.rows = [row] * (10001 if case == 'overflow' else 34 if case == 'bytes' else 1)
        else:
            self.rows = []
    def fetchone(self): return self.rows[0] if self.rows else None
    def fetchall(self): return self.rows
    def fetchmany(self, count):
        value = self.rows[self.position:self.position+count]; self.position += count; return value

class Connection:
    def cursor(self, name=None): return Cursor(name)
    def close(self): log({'closed': True})

def connect(secret, **kwargs):
    assert secret == os.environ['PAW_POSTGIS_TEST_URL']
    assert kwargs['connect_timeout'] == 5
    assert 'default_transaction_read_only=on' in kwargs['options']
    assert 'statement_timeout=15000' in kwargs['options']
    assert 'lock_timeout=3000' in kwargs['options']
    if os.environ.get('PAW_POSTGIS_TEST_CASE') == 'authentication': raise DriverError('28P01')
    log({'connected': True})
    return Connection()
`;

async function withDriverFixture(callback) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'paw-postgis-dbapi-'));
  const env = {
    PYTHONPATH: root, PAW_POSTGIS_TEST_URL: 'postgresql://private-user:private-password@private-host/private-db',
    PAW_POSTGIS_TEST_LOG: path.join(root, 'queries.jsonl'), PAW_POSTGIS_TEST_CASE: '',
  };
  const previous = Object.fromEntries(Object.keys(env).map(key => [key, process.env[key]]));
  fs.writeFileSync(path.join(root, 'psycopg.py'), driverFixture);
  Object.assign(process.env, env);
  try { return await callback(root); } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
    fs.rmSync(root, { recursive: true, force: true });
  }
}

const fixtureSource = { name: 'PostGIS fixture', kind: 'postgis', secretReference: 'PAW_POSTGIS_TEST_URL', schema: 'survey data' };

test('PostGIS connects only after a real probe and records a missing secret instead of configured_pending', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'paw-postgis-missing-'));
  const secretReference = 'PAW_POSTGIS_TEST_UNCONFIGURED';
  const previous = process.env[secretReference];
  delete process.env[secretReference];
  try {
    const result = await connectSpatialSource({ root, python, source: { name: 'PostGIS', kind: 'postgis', secretReference } });
    assert.equal(result.status, 'missing_secret');
    assert.equal(result.code, 'postgis_secret_missing');
    assert.equal(result.readOnly, true);
    assert.deepEqual(result.layers, []);
    assert.deepEqual(listSpatialSources({ root }).sources, [result]);
    await assert.rejects(loadSpatialLayer({ root, python, sourceId: result.id, layer: 'public.roads.geom' }), error => error.code === 'postgis_secret_missing');
  } finally {
    if (previous === undefined) delete process.env[secretReference]; else process.env[secretReference] = previous;
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('PostGIS rejects embedded credentials and writable requests before persisting a connection', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'paw-postgis-credentials-'));
  const source = { name: 'PostGIS', kind: 'postgis', secretReference: 'PAW_POSTGIS_TEST_URL' };
  try {
    for (const field of ['url', 'connectionString', 'password', 'dsn']) {
      await assert.rejects(connectSpatialSource({ root, python, source: { ...source, [field]: 'secret-that-must-not-be-saved' } }), /credential|secret/i);
    }
    await assert.rejects(connectSpatialSource({ root, python, source: { ...source, readOnly: false } }), /read.only/i);
    assert.equal(listSpatialSources({ root }).sources.length, 0);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('runner reports a missing driver without printing a connection value', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'paw-postgis-driver-'));
  const secret = 'postgresql://private-user:private-password@private-host/private-database';
  try {
    const result = spawnSync(python, ['-S', runner], {
      encoding: 'utf8', env: { ...process.env, PAW_POSTGIS_TEST_URL: secret },
      input: JSON.stringify({ operation: 'catalog_postgis', root, secretReference: 'PAW_POSTGIS_TEST_URL' }),
    });
    const receipt = JSON.parse(result.stdout.trim().split(/\r?\n/).at(-1));
    assert.equal(receipt.code, 'postgis_dependency_missing');
    assert.equal(receipt.status, 'failed');
    assert.doesNotMatch(result.stdout + result.stderr, /private-(user|password|host|database)/);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('PostGIS DB-API contract uses qualified identifiers, bounded reads and a read-only transaction; persists only WGS84 data and redacted lineage', async () => {
  await withDriverFixture(async root => {
    const source = await connectSpatialSource({ root, python, source: fixtureSource });
    assert.equal(source.status, 'ready', source.error);
    assert.equal(source.postgisVersion, '3.5.2');
    assert.equal(source.featureLimit, 10_000);
    assert.equal(source.catalogTruncated, false);
    assert.deepEqual(source.layers, ['"survey data"."roads""; DROP TABLE sentinel;--".shape']);
    const result = await loadSpatialLayer({ root, python, sourceId: source.id, layer: source.layers[0] });
    assert.equal(result.status, 'completed');
    assert.equal(result.featureCount, 1);
    assert.equal(result.sourceCrs, 'EPSG:3857');
    assert.equal(result.crs, 'EPSG:4326');
    assert.equal(result.readOnly, true);
    assert.equal(result.truncated, false);
    assert.deepEqual(result.bounds, [1, 2, 1, 2]);
    assert.equal(result.geojson.features[0].id, '9007199254740993');
    assert.equal(result.sourceLineage.identityBasis, 'primary-key');
    assert.equal(result.sourceLineage.sourceSha256, '');
    assert.match(result.sourceLineage.snapshotSha256, /^[a-f0-9]{64}$/);
    const geojson = JSON.parse(fs.readFileSync(path.join(root, result.path), 'utf8'));
    assert.deepEqual(geojson, result.geojson);
    const lineage = JSON.parse(fs.readFileSync(path.join(root, result.lineagePath), 'utf8'));
    assert.equal(lineage.snapshotSha256, result.snapshotSha256);
    const queries = fs.readFileSync(process.env.PAW_POSTGIS_TEST_LOG, 'utf8').trim().split('\n').map(JSON.parse);
    const transaction = queries.findIndex(row => row.sql?.includes('READ ONLY'));
    const catalog = queries.findIndex(row => row.sql?.includes('postgis_typmod_srid'));
    assert.ok(transaction >= 0 && transaction < catalog);
    const read = queries.find(row => row.serverSide);
    assert.match(read.sql, /FROM "survey data"\."roads""; DROP TABLE sentinel;--" AS src/);
    assert.match(read.sql, /"src"\."name""; SELECT private--"/);
    assert.match(read.sql, /"postgis schema"\."st_transform"/);
    assert.match(read.sql, /LIMIT %s/);
    assert.match(read.sql, /payload_bytes <= 2097152/);
    assert.deepEqual(read.params, [10_001]);
    assert.ok(queries.some(row => row.closed));
    const persisted = JSON.stringify(source) + JSON.stringify(geojson) + JSON.stringify(lineage) + fs.readFileSync(path.join(root, '.earth/gis/databases.json'), 'utf8');
    assert.doesNotMatch(persisted, /private-(user|password|host|db)|postgresql:\/\//);
    await assert.rejects(loadSpatialLayer({ root, python, sourceId: source.id, layer: 'public.roads; DROP TABLE sentinel' }), error => error.code === 'layer_not_found');
  });
});

test('PostGIS preserves an existing materialization when row/byte/CRS bounds or permissions reject a read', async () => {
  await withDriverFixture(async root => {
    const source = await connectSpatialSource({ root, python, source: fixtureSource });
    const loaded = await loadSpatialLayer({ root, python, sourceId: source.id, layer: source.layers[0] });
    const baseline = fs.readFileSync(path.join(root, loaded.path), 'utf8');
    const baselineReceipt = fs.readFileSync(path.join(root, loaded.lineagePath), 'utf8');
    for (const [scenario, code] of [['overflow', 'postgis_feature_limit'], ['bytes', 'postgis_byte_limit'], ['feature_bytes', 'postgis_feature_byte_limit'], ['unknown_crs', 'unknown_crs'], ['permission', 'postgis_permission_denied'], ['timeout', 'postgis_query_timeout']]) {
      process.env.PAW_POSTGIS_TEST_CASE = scenario;
      await assert.rejects(loadSpatialLayer({ root, python, sourceId: source.id, layer: source.layers[0] }), error => {
        assert.doesNotMatch(error.message, /private-(user|password|host|db)|postgresql:\/\//);
        return error.code === code;
      });
      assert.equal(fs.readFileSync(path.join(root, loaded.path), 'utf8'), baseline);
      assert.equal(fs.readFileSync(path.join(root, loaded.lineagePath), 'utf8'), baselineReceipt);
    }
  });
});

test('PostGIS connection failures are classified without persisting raw driver exceptions', async () => {
  await withDriverFixture(async root => {
    for (const [scenario, status, code] of [['authentication', 'authentication_failed', 'postgis_authentication_failed'], ['no_extension', 'postgis_missing', 'postgis_extension_missing']]) {
      process.env.PAW_POSTGIS_TEST_CASE = scenario;
      const result = await connectSpatialSource({ root, python, source: fixtureSource });
      assert.equal(result.status, status);
      assert.equal(result.code, code);
      assert.deepEqual(result.layers, []);
      assert.doesNotMatch(JSON.stringify(result) + fs.readFileSync(path.join(root, '.earth/gis/databases.json'), 'utf8'), /private-(user|password|host|db)|postgresql:\/\//);
    }
  });
});

test('PostGIS also redacts a child process failure after it emits a partial receipt', async () => {
  await withDriverFixture(async root => {
    const brokenPython = path.join(root, 'broken-python');
    fs.writeFileSync(brokenPython, `#!/usr/bin/env python3\nimport json, os, sys\nprint(json.dumps({'status': 'completed'}))\nprint(os.environ['PAW_POSTGIS_TEST_URL'], file=sys.stderr)\nsys.exit(1)\n`, { mode: 0o700 });
    const result = await connectSpatialSource({ root, python: brokenPython, source: fixtureSource });
    assert.equal(result.status, 'runtime_unavailable');
    assert.equal(result.code, 'postgis_runner_failed');
    assert.doesNotMatch(JSON.stringify(result) + fs.readFileSync(path.join(root, '.earth/gis/databases.json'), 'utf8'), /private-(user|password|host|db)|postgresql:\/\//);
  });
});

function liveDatabase(script, schema) {
  const result = spawnSync(python, ['-c', String.raw`
import json, os, sys
try:
    try:
        import psycopg as driver
        from psycopg import sql
    except ImportError:
        import psycopg2 as driver
        from psycopg2 import sql
    connection = driver.connect(os.environ['PAW_POSTGIS_TEST_URL'], connect_timeout=5)
    connection.autocommit = False
    try:
        with connection.cursor() as cursor:
            schema = sys.argv[1]
            cursor.execute("SELECT n.nspname FROM pg_catalog.pg_extension e JOIN pg_catalog.pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='postgis'")
            extension = cursor.fetchone()
            if extension is None: raise RuntimeError('PostGIS extension must already be enabled in the fixture database')
            namespace = extension[0]
` + script.split('\n').map(line => `            ${line}`).join('\n') + String.raw`
        connection.commit()
    finally:
        connection.close()
except Exception as exc:
    # Even fixture setup failures must not dump a credential-bearing driver error.
    print(json.dumps({'status': 'failed', 'exception': type(exc).__name__, 'sqlstate': getattr(exc, 'sqlstate', None) or getattr(exc, 'pgcode', None)}))
    sys.exit(1)
`, schema], { encoding: 'utf8', env: process.env, maxBuffer: 1024 * 1024 });
  assert.equal(result.status, 0, result.stdout || 'PostGIS fixture runtime failed; check the configured test database and psycopg dependency.');
  return result.stdout.trim() ? JSON.parse(result.stdout.trim().split(/\r?\n/).at(-1)) : null;
}

test('live PostGIS reads the chosen projected/geography layers, quotes identifiers and enforces read-only transactions and row limits', livePostGIS, async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'paw-postgis-native-'));
  const schema = `paw_test_${randomUUID().replaceAll('-', '')}`;
  let created = false;
  try {
    liveDatabase(String.raw`
cursor.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
cursor.execute(sql.SQL('CREATE TABLE {} (id bigint PRIMARY KEY, name text, shape {}(Point,3857))').format(sql.Identifier(schema, 'projected'), sql.Identifier(namespace, 'geometry')))
cursor.execute(sql.SQL('INSERT INTO {} VALUES (%s, %s, {}({}(%s, %s),3857))').format(sql.Identifier(schema, 'projected'), sql.Identifier(namespace, 'st_setsrid'), sql.Identifier(namespace, 'st_makepoint')), (9007199254740993, 'native fixture', 111319.49079327357, 0))
cursor.execute(sql.SQL('CREATE TABLE {} (id text PRIMARY KEY, place {}(Point,4326))').format(sql.Identifier(schema, 'geography'), sql.Identifier(namespace, 'geography')))
cursor.execute(sql.SQL('INSERT INTO {} VALUES (%s, {}({}(%s, %s),4326)::{} )').format(sql.Identifier(schema, 'geography'), sql.Identifier(namespace, 'st_setsrid'), sql.Identifier(namespace, 'st_makepoint'), sql.Identifier(namespace, 'geography')), ('geo-1', 120, 30))
cursor.execute(sql.SQL('CREATE TABLE {} (id integer PRIMARY KEY, shape {}(Point,4326))').format(sql.Identifier(schema, 'large_layer'), sql.Identifier(namespace, 'geometry')))
cursor.execute(sql.SQL('INSERT INTO {} SELECT n, {}({}(0,0),4326) FROM generate_series(1,10001) n').format(sql.Identifier(schema, 'large_layer'), sql.Identifier(namespace, 'st_setsrid'), sql.Identifier(namespace, 'st_makepoint')))
cursor.execute(sql.SQL('CREATE TABLE {} (id integer PRIMARY KEY, payload text, shape {}(Point,4326))').format(sql.Identifier(schema, 'large_feature'), sql.Identifier(namespace, 'geometry')))
cursor.execute(sql.SQL("INSERT INTO {} VALUES (1, repeat('x', 3145728), {}({}(0,0),4326))").format(sql.Identifier(schema, 'large_feature'), sql.Identifier(namespace, 'st_setsrid'), sql.Identifier(namespace, 'st_makepoint')))
cursor.execute(sql.SQL('CREATE TABLE {} (id integer PRIMARY KEY, shape {})').format(sql.Identifier(schema, 'unknown_crs'), sql.Identifier(namespace, 'geometry')))
cursor.execute(sql.SQL('INSERT INTO {} VALUES (1, {}(0,0))').format(sql.Identifier(schema, 'unknown_crs'), sql.Identifier(namespace, 'st_makepoint')))
cursor.execute(sql.SQL('CREATE VIEW {} AS SELECT * FROM {}').format(sql.Identifier(schema, 'roads"; DROP TABLE sentinel;--'), sql.Identifier(schema, 'projected')))
cursor.execute(sql.SQL('CREATE TABLE {} (id integer)').format(sql.Identifier(schema, 'sentinel')))
body = sql.SQL('BEGIN INSERT INTO {} VALUES (1); RETURN 1; END;').format(sql.Identifier(schema, 'sentinel')).as_string(connection)
cursor.execute(sql.SQL('CREATE FUNCTION {}() RETURNS integer LANGUAGE plpgsql AS {}').format(sql.Identifier(schema, 'write_on_read'), sql.Literal(body)))
cursor.execute(sql.SQL('CREATE VIEW {} AS SELECT {}() AS attempt, shape FROM {}').format(sql.Identifier(schema, 'writing_view'), sql.Identifier(schema, 'write_on_read'), sql.Identifier(schema, 'projected')))
`, schema);
    created = true;
    const source = await connectSpatialSource({ root, python, source: { name: 'Live PostGIS fixture', kind: 'postgis', secretReference: 'PAW_POSTGIS_TEST_URL', schema } });
    assert.equal(source.status, 'ready', source.error);
    assert.equal(source.layers.length, 7);
    const layer = table => source.layerDetails.find(item => item.table === table)?.name;
    const projected = await loadSpatialLayer({ root, python, sourceId: source.id, layer: layer('projected') });
    assert.equal(projected.featureCount, 1);
    assert.equal(projected.sourceCrs, 'EPSG:3857');
    assert.equal(projected.geojson.features[0].id, '9007199254740993');
    assert.ok(Math.abs(projected.geojson.features[0].geometry.coordinates[0] - 1) < 1e-8);
    assert.ok(Math.abs(projected.geojson.features[0].geometry.coordinates[1]) < 1e-8);
    assert.equal(projected.sourceLineage.table, 'projected');
    const geography = await loadSpatialLayer({ root, python, sourceId: source.id, layer: layer('geography') });
    assert.equal(geography.sourceCrs, 'EPSG:4326');
    assert.deepEqual(geography.geojson.features[0].geometry.coordinates, [120, 30]);
    const quoted = await loadSpatialLayer({ root, python, sourceId: source.id, layer: layer('roads"; DROP TABLE sentinel;--') });
    assert.equal(quoted.featureCount, 1);
    await assert.rejects(loadSpatialLayer({ root, python, sourceId: source.id, layer: layer('large_layer') }), error => error.code === 'postgis_feature_limit');
    await assert.rejects(loadSpatialLayer({ root, python, sourceId: source.id, layer: layer('large_feature') }), error => error.code === 'postgis_feature_byte_limit');
    await assert.rejects(loadSpatialLayer({ root, python, sourceId: source.id, layer: layer('unknown_crs') }), error => error.code === 'unknown_crs');
    await assert.rejects(loadSpatialLayer({ root, python, sourceId: source.id, layer: layer('writing_view') }), error => error.code === 'postgis_read_only_violation');
    const sentinel = liveDatabase(String.raw`
cursor.execute(sql.SQL('SELECT count(*) FROM {}').format(sql.Identifier(schema, 'sentinel')))
print(json.dumps({'count': cursor.fetchone()[0]}))
`, schema);
    assert.equal(sentinel.count, 0);
    const filtered = await connectSpatialSource({ root, python, source: { name: 'Filtered PostGIS', kind: 'postgis', secretReference: 'PAW_POSTGIS_TEST_URL', schema, table: 'projected' } });
    assert.deepEqual(filtered.layers, [layer('projected')]);
    const persisted = fs.readFileSync(path.join(root, '.earth/gis/databases.json'), 'utf8') + fs.readFileSync(path.join(root, projected.path), 'utf8');
    assert.equal(persisted.includes(process.env.PAW_POSTGIS_TEST_URL), false);
  } finally {
    if (created) liveDatabase("cursor.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))", schema);
    fs.rmSync(root, { recursive: true, force: true });
  }
});
