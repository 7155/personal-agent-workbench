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
