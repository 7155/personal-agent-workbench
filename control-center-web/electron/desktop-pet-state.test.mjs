import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { test } from 'node:test';
import { installDesktopPetState } from './desktop-pet-state.mjs';

function harness() {
  const handlers = new Map(); const snapshots = [];
  const sender = Object.assign(new EventEmitter(), { mainFrame: {}, getURL: () => 'http://127.0.0.1:7777/', isDestroyed: () => false });
  let source = sender;
  const state = installDesktopPetState({
    ipcMain: { handle: (name, handler) => handlers.set(name, handler), removeHandler: (name) => handlers.delete(name) },
    getSource: () => source, origin: 'http://127.0.0.1:7777', onSnapshot: (snapshot) => snapshots.push(snapshot),
  });
  return { state, sender, snapshots, handlers, replaceSource: (next) => { source = next; },
    invoke: (name, value, event = { sender, senderFrame: sender.mainFrame }) => handlers.get(`paw-pet-state:${name}`)(event, value) };
}
const identity = { schemaVersion: 1, sourceId: 'work-directory', scopeId: 'primary-1' };
const counts = { running: 1, attention: 0, error: 0, paused: 0, idle: 0, terminal: 0, unknown: 0 };
const conversations = [{ id: 'session-one', label: 'Conversation one', state: 'running' }];
const value = (producerEpoch, revision = 1) => ({ schemaVersion: 1, producerEpoch, revision, freshness: 'synced', counts, conversations });

test('retains only a validated small snapshot with host-issued identity and revision', () => {
  const h = harness(); const { producerEpoch } = h.invoke('begin', identity);
  assert.equal(h.state.snapshot().freshness, 'unavailable');
  assert.equal(h.invoke('publish', value(producerEpoch)), true);
  assert.deepEqual(h.state.snapshot(), { ...identity, producerEpoch, revision: 1, freshness: 'synced', counts, conversations });
  assert.equal(h.invoke('publish', value(producerEpoch)), false);
  assert.equal(h.snapshots.length, 2);
});

test('replaces producer identity and refuses delayed publish/release from its old owner', () => {
  const h = harness(); const first = h.invoke('begin', identity).producerEpoch;
  h.invoke('publish', value(first));
  const next = h.invoke('begin', { ...identity, scopeId: 'primary-2' }).producerEpoch;
  assert.equal(h.state.snapshot().scopeId, 'primary-2');
  assert.equal(h.state.snapshot().counts.running, 0);
  assert.equal(h.invoke('publish', value(first, 99)), false);
  assert.equal(h.invoke('release', { producerEpoch: first }), false);
  assert.equal(h.invoke('publish', value(next)), true);
});

test('main unload, navigation and process loss clear retained facts and revoke callbacks', () => {
  for (const reason of ['destroyed', 'render-process-gone', 'did-start-navigation']) {
    const h = harness(); const { producerEpoch } = h.invoke('begin', identity);
    h.invoke('publish', value(producerEpoch));
    h.sender.emit(reason, {}, 'http://127.0.0.1:7777/', false, true);
    assert.equal(h.state.snapshot().freshness, 'unavailable');
    assert.equal(h.state.snapshot().sourceId, null);
    assert.equal(h.state.snapshot().counts.running, 0);
    assert.equal(h.invoke('publish', value(producerEpoch, 2)), false);
  }
});

test('in-page/subframe navigation does not revoke the current producer', () => {
  const h = harness(); const { producerEpoch } = h.invoke('begin', identity);
  h.sender.emit('did-start-navigation', {}, 'http://127.0.0.1:7777/#agent', true, true);
  h.sender.emit('did-start-navigation', {}, 'https://example.com', false, false);
  assert.equal(h.invoke('publish', value(producerEpoch)), true);
});

test('connection loss keeps no misleading active counts and release is owner-bound', () => {
  const h = harness(); const { producerEpoch } = h.invoke('begin', identity);
  h.invoke('publish', value(producerEpoch));
  h.invoke('publish', { ...value(producerEpoch, 2), freshness: 'recovering' });
  assert.equal(h.state.snapshot().counts.running, 0);
  assert.equal(h.invoke('release', { producerEpoch }), true);
  assert.equal(h.state.snapshot().scopeId, null);
});

test('only the current main frame may publish; no plugin/foreign sender escalation', () => {
  const h = harness();
  assert.throws(() => h.invoke('begin', identity, { sender: {}, senderFrame: {} }), /rejected/);
  assert.throws(() => h.invoke('begin', identity, { sender: h.sender, senderFrame: {} }), /rejected/);
  h.sender.getURL = () => 'https://example.com';
  assert.throws(() => h.invoke('begin', identity), /rejected/);
});

test('rejects unknown schema, unsafe identities, extra data, malformed counts and payloads', () => {
  const h = harness();
  for (const input of [{ ...identity, schemaVersion: 2 }, { ...identity, sourceId: '' }, { ...identity, path: '/private' }, { ...identity, scopeId: 'x'.repeat(3000) }]) {
    assert.throws(() => h.invoke('begin', input), /Invalid/);
  }
  const { producerEpoch } = h.invoke('begin', identity);
  for (const input of [{ ...value(producerEpoch), schemaVersion: 2 }, { ...value(producerEpoch), history: [] }, { ...value(producerEpoch), counts: { ...counts, running: -1 } }, { ...value(producerEpoch), counts: { ...counts, running: 1.5 } }, { ...value(producerEpoch), counts: { ...counts, running: 101 } }, { ...value(producerEpoch), counts: { ...counts, attention: 100 } }, { ...value(producerEpoch), counts: { ...counts, tool: 'secret' } }]) {
    assert.throws(() => h.invoke('publish', input), /Invalid/);
  }
});

test('disposal releases sender listeners and all IPC handlers', () => {
  const h = harness(); h.invoke('begin', identity);
  h.state.dispose();
  assert.equal(h.handlers.size, 0);
  assert.equal(h.sender.listenerCount('destroyed'), 0);
  assert.equal(h.sender.listenerCount('did-start-navigation'), 0);
});

test('opening a reused conversation id is bound to the current producer identity and source', () => {
  const h = harness(); const first = h.invoke('begin', identity).producerEpoch;
  h.invoke('publish', value(first));
  const target = { id: 'session-one', producerEpoch: first, sourceId: identity.sourceId, scopeId: identity.scopeId };
  assert.equal(h.state.canOpenConversation(target), true);
  const next = h.invoke('begin', { ...identity, scopeId: 'other-connection' }).producerEpoch;
  h.invoke('publish', value(next));
  assert.equal(h.state.canOpenConversation(target), false);
  const current = { ...target, producerEpoch: next, scopeId: 'other-connection' };
  assert.equal(h.state.canOpenConversation(current), true);
  assert.equal(h.state.canOpenConversation({ ...current, scopeId: identity.scopeId }), false);
  assert.equal(h.state.canOpenConversation({ ...current, sourceId: 'untrusted' }), false);
  assert.equal(h.state.canOpenConversation({ ...current, extra: true }), false);
  h.replaceSource({ isDestroyed: () => false });
  assert.equal(h.state.canOpenConversation(current), false);
});

test('retains explicit error separately from attention and clears both when stale', () => {
  const h = harness(); const { producerEpoch } = h.invoke('begin', identity);
  const failure = { ...value(producerEpoch), counts: { ...counts, running: 0, error: 1 },
    conversations: [{ id: 'failed', label: 'Failed conversation', state: 'error' }] };
  assert.equal(h.invoke('publish', failure), true);
  assert.equal(h.state.snapshot().counts.error, 1);
  assert.equal(h.state.snapshot().counts.attention, 0);
  assert.equal(h.state.snapshot().conversations[0].state, 'error');
  assert.throws(() => h.invoke('publish', { ...failure, revision: 2, counts: { ...failure.counts, error: 0 } }), /Invalid/);
  assert.throws(() => h.invoke('publish', { ...failure, revision: 2, counts: { ...failure.counts, error: -1 } }), /Invalid/);
  assert.equal(h.invoke('publish', { ...failure, revision: 2, freshness: 'recovering' }), true);
  assert.equal(h.state.snapshot().counts.error, 0);
  assert.equal(h.state.snapshot().counts.unknown, 1);
  assert.equal(h.state.snapshot().conversations[0].state, 'unknown');
});

const activeFact = { id: 'session-one', activeTurnId: 'turn:one', waiting: [] };
const doneFact = { id: 'session-one', activeTurnId: '', waiting: [], terminal: {
  eventId: 'event:done', turnId: 'turn:one', sequence: 3, outcome: 'completed' } };
const idleValue = (epoch, revision, facts) => ({ ...value(epoch, revision), counts: { ...emptyTestCounts(), idle: 1 },
  conversations: [{ id: 'session-one', label: 'Conversation one', state: 'idle' }], facts });
function emptyTestCounts() { return { running: 0, attention: 0, error: 0, paused: 0, idle: 0, terminal: 0, unknown: 0 }; }
test('advertises optional facts and seeds historical success without global completion', () => {
  const h = harness(); const begin = h.invoke('begin', identity);
  assert.equal(begin.factsVersion, 1);
  h.invoke('publish', idleValue(begin.producerEpoch, 1, [doneFact]));
  assert.equal(h.state.snapshot().visual.signal, 'idle');
  assert.equal(h.state.snapshot().visual.arrivalKey, null);
});
test('observes exact original running turn before done and retains consumed identity', () => {
  const h = harness(); const { producerEpoch } = h.invoke('begin', identity);
  h.invoke('publish', { ...value(producerEpoch), facts: [activeFact] });
  h.invoke('publish', idleValue(producerEpoch, 2, [doneFact]));
  assert.equal(h.state.snapshot().visual.signal, 'done');
  const key = h.state.snapshot().visual.arrivalKey;
  assert.ok(key);
  h.invoke('publish', idleValue(producerEpoch, 3, [doneFact]));
  assert.equal(h.state.snapshot().visual.arrivalKey, key);
});

test('keeps legacy publish exact and does not upgrade generic diagnostics into waiting or done', () => {
  const h = harness(); const { producerEpoch } = h.invoke('begin', identity);
  h.invoke('publish', value(producerEpoch));
  assert.equal(Object.hasOwn(h.state.snapshot(), 'facts'), false);
  assert.equal(Object.hasOwn(h.state.snapshot(), 'visual'), false);
  h.invoke('publish', { ...idleValue(producerEpoch, 2, []), counts: { ...emptyTestCounts(), attention: 1 },
    conversations: [{ id: 'session-one', label: 'Conversation one', state: 'attention' }] });
  assert.equal(h.state.snapshot().visual.signal, 'idle');
});
test('only exact bound input becomes waiting, seed static then one new transition', () => {
  const h = harness(); const { producerEpoch } = h.invoke('begin', identity);
  const request = { turnId: 'turn:one', requestId: 'request:one', kind: 'input' };
  h.invoke('publish', { ...value(producerEpoch), facts: [activeFact] });
  h.invoke('publish', { ...value(producerEpoch, 2), facts: [{ ...activeFact, waiting: [request] }] });
  assert.equal(h.state.snapshot().visual.signal, 'waiting');
  const key = h.state.snapshot().visual.arrivalKey; assert.ok(key);
  h.invoke('publish', { ...value(producerEpoch, 3), facts: [{ ...activeFact, waiting: [request] }] });
  assert.equal(h.state.snapshot().visual.arrivalKey, key);
  h.invoke('publish', { ...value(producerEpoch, 4), facts: [activeFact] });
  assert.equal(h.state.snapshot().visual.signal, 'working');
  assert.throws(() => h.invoke('publish', { ...value(producerEpoch, 5), facts: [{ ...activeFact,
    waiting: [{ ...request, turnId: 'turn:foreign' }] }] }), /Invalid/);
  const other = harness(); const epoch = other.invoke('begin', identity).producerEpoch;
  other.invoke('publish', { ...value(epoch), facts: [{ ...activeFact, waiting: [request] }] });
  assert.equal(other.state.snapshot().visual.signal, 'waiting');
  assert.equal(other.state.snapshot().visual.arrivalKey, null);
});
test('Stop, failed, foreign turn and newer work cannot be celebrated', () => {
  for (const outcome of ['aborted', 'failed']) {
    const h = harness(); const { producerEpoch } = h.invoke('begin', identity);
    h.invoke('publish', { ...value(producerEpoch), facts: [activeFact] });
    h.invoke('publish', idleValue(producerEpoch, 2, [{ ...doneFact, terminal: { ...doneFact.terminal, outcome } }]));
    assert.equal(h.state.snapshot().visual.signal, 'idle');
    assert.equal(h.state.snapshot().visual.arrivalKey, null);
  }
  const h = harness(); const { producerEpoch } = h.invoke('begin', identity);
  h.invoke('publish', { ...value(producerEpoch), facts: [activeFact] });
  h.invoke('publish', idleValue(producerEpoch, 2, [{ ...doneFact, terminal: { ...doneFact.terminal, turnId: 'turn:other' } }]));
  assert.equal(h.state.snapshot().visual.signal, 'idle');
  h.invoke('publish', { ...value(producerEpoch, 3), facts: [{ ...doneFact, activeTurnId: 'turn:successor', terminal: { ...doneFact.terminal, turnId: 'turn:other' } }] });
  assert.equal(h.state.snapshot().visual.signal, 'working');
});
test('recovery and new epoch seed successful history and preserve unknown offline', () => {
  const h = harness(); const epoch = h.invoke('begin', identity).producerEpoch;
  h.invoke('publish', { ...value(epoch), facts: [activeFact] });
  h.invoke('publish', { ...value(epoch, 2), freshness: 'recovering', facts: [activeFact] });
  assert.equal(h.state.snapshot().visual.signal, 'idle');
  assert.equal(h.state.snapshot().visual.motion, 'static');
  h.invoke('publish', idleValue(epoch, 3, [doneFact]));
  assert.equal(h.state.snapshot().visual.signal, 'idle');
  const next = h.invoke('begin', identity).producerEpoch;
  h.invoke('publish', idleValue(next, 1, [doneFact]));
  assert.equal(h.state.snapshot().visual.signal, 'idle');
  assert.equal(h.invoke('publish', { ...value(epoch, 99), facts: [activeFact] }), false);
});
test('new waiting/error/running take precedence over an old completion', () => {
  const h = harness(); const { producerEpoch } = h.invoke('begin', identity);
  h.invoke('publish', { ...value(producerEpoch), facts: [activeFact] });
  h.invoke('publish', idleValue(producerEpoch, 2, [doneFact]));
  assert.equal(h.state.snapshot().visual.signal, 'done');
  h.invoke('publish', { ...value(producerEpoch, 3), facts: [{ ...activeFact, activeTurnId: 'turn:new', waiting: [
    { requestId: 'request:new', turnId: 'turn:new', kind: 'review' }] }] });
  assert.equal(h.state.snapshot().visual.signal, 'waiting');
  h.invoke('publish', { ...value(producerEpoch, 4), counts: { ...emptyTestCounts(), error: 1 },
    conversations: [{ id: 'session-one', label: 'Conversation one', state: 'error' }], facts: [doneFact] });
  assert.equal(h.state.snapshot().visual.signal, 'error');
});
test('rejects content, unbound rows, duplicate or oversized facts and regressing exact terminal', () => {
  const h = harness(); const epoch = h.invoke('begin', identity).producerEpoch;
  for (const facts of [[{ ...activeFact, prompt: 'private' }], [{ ...activeFact, id: 'foreign' }],
    [activeFact, activeFact], [{ ...activeFact, waiting: [{ turnId: 'turn:one', requestId: 'request:one', kind: 'attention' }] }],
    [{ ...doneFact, terminal: { ...doneFact.terminal, eventId: 'x'.repeat(513) } }]]) {
    assert.throws(() => h.invoke('publish', { ...value(epoch), facts }), /Invalid/);
  }
  h.invoke('publish', idleValue(epoch, 1, [doneFact]));
  assert.throws(() => h.invoke('publish', idleValue(epoch, 2, [{ ...doneFact, terminal: { ...doneFact.terminal, sequence: 2 } }])), /Regressing/);
  assert.equal(h.state.snapshot().revision, 1);
  assert.throws(() => h.invoke('publish', { ...value(epoch, 2), history: 'x'.repeat(4096) }), /Invalid/);
  assert.equal(h.state.snapshot().revision, 1);
});


test('waits for the current running count to clear before consuming a successful exact turn', () => {
  const h = harness(); const epoch = h.invoke('begin', identity).producerEpoch;
  h.invoke('publish', { ...value(epoch), facts: [activeFact] });
  h.invoke('publish', { ...value(epoch, 2), facts: [doneFact] });
  assert.equal(h.state.snapshot().visual.signal, 'working');
  h.invoke('publish', idleValue(epoch, 3, [doneFact]));
  assert.equal(h.state.snapshot().visual.signal, 'done');
  const key = h.state.snapshot().visual.arrivalKey;
  h.invoke('publish', idleValue(epoch, 4, [doneFact]));
  assert.equal(h.state.snapshot().visual.arrivalKey, key);
  h.invoke('publish', idleValue(epoch, 5, [{ ...doneFact, activeTurnId: 'turn:new' }]));
  assert.equal(h.state.snapshot().visual.signal, 'idle');
});


test('an exact active waiting turn observed by this owner may complete, but seed history cannot', () => {
  const h = harness(); const epoch = h.invoke('begin', identity).producerEpoch;
  h.invoke('publish', { ...value(epoch), counts: { ...emptyTestCounts(), attention: 1 },
    conversations: [{ id: 'session-one', label: 'Conversation one', state: 'attention' }],
    facts: [{ ...activeFact, waiting: [{ turnId: activeFact.activeTurnId, requestId: 'request:one', kind: 'input' }] }] });
  assert.equal(h.state.snapshot().visual.signal, 'waiting');
  assert.equal(h.state.snapshot().visual.arrivalKey, null);
  h.invoke('publish', idleValue(epoch, 2, [doneFact]));
  assert.equal(h.state.snapshot().visual.signal, 'done'); assert.ok(h.state.snapshot().visual.arrivalKey);
});
