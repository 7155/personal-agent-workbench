const COUNT_KEYS = ['running', 'attention', 'error', 'paused', 'idle', 'terminal', 'unknown'];
const emptyCounts = () => Object.fromEntries(COUNT_KEYS.map((key) => [key, 0]));
const exact = (value, keys) => value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key));
const integer = (value) => Number.isSafeInteger(value) && value >= 0;
const identity = (value) => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/.test(value);

function bounded(value) {
  try { if (Buffer.byteLength(JSON.stringify(value)) <= 4096) return; } catch { /* Reject unserializable input below. */ }
  throw new Error('Invalid desktop pet snapshot payload');
}

const opaque = (value, max) => typeof value === 'string' && value.length <= max && /^[A-Za-z0-9][A-Za-z0-9._:-]*$/.test(value);
function canonicalFacts(value) {
  if (!Array.isArray(value.facts) || value.facts.length > 8) throw new Error('Invalid desktop pet facts');
  const ids = new Set(value.conversations.map(row => row.id));
  const seen = new Set();
  return value.facts.map(fact => {
    if (!fact || typeof fact !== 'object' || Array.isArray(fact) || !ids.has(fact.id) || seen.has(fact.id)
      || Object.keys(fact).some(key => !['id', 'activeTurnId', 'waiting', 'terminal'].includes(key))
      || Object.keys(fact).length < 2) throw new Error('Invalid desktop pet fact identity');
    seen.add(fact.id);
    const result = { id: fact.id };
    if (Object.hasOwn(fact, 'activeTurnId')) {
      if (fact.activeTurnId !== '' && !opaque(fact.activeTurnId, 240)) throw new Error('Invalid pet active turn');
      result.activeTurnId = fact.activeTurnId;
    }
    if (Object.hasOwn(fact, 'waiting')) {
      if (!Object.hasOwn(fact, 'activeTurnId') || !Array.isArray(fact.waiting) || fact.waiting.length > 8
        || !fact.waiting.every(request => exact(request, ['turnId', 'requestId', 'kind'])
          && opaque(request.turnId, 240) && request.turnId === fact.activeTurnId && opaque(request.requestId, 240)
          && ['input', 'approval', 'review'].includes(request.kind))
        || new Set(fact.waiting.map(request => request.requestId)).size !== fact.waiting.length) throw new Error('Invalid pet waiting binding');
      result.waiting = fact.waiting.map(request => ({ ...request }));
    }
    if (Object.hasOwn(fact, 'terminal')) {
      const terminal = fact.terminal;
      if (!exact(terminal, ['eventId', 'turnId', 'sequence', 'outcome']) || !opaque(terminal.eventId, 512)
        || !opaque(terminal.turnId, 240) || !integer(terminal.sequence) || terminal.sequence < 1
        || !['completed', 'aborted', 'failed'].includes(terminal.outcome)) throw new Error('Invalid pet terminal');
      result.terminal = { ...terminal };
    }
    return result;
  });
}
function projectVisual(value, facts, previous, producer) {
  const continuous = value.freshness === 'synced' && previous.freshness === 'synced' && Boolean(previous.visual);
  const observed = continuous ? new Map(producer.observed) : new Map();
  const terminals = continuous ? new Map(producer.terminals) : new Map();
  const rows = new Map(value.conversations.map(row => [row.id, row]));
  const admitted = new Set(facts.map(fact => fact.id));
  for (const id of observed.keys()) if (!admitted.has(id)) observed.delete(id);
  const completed = [];
  for (const fact of facts) {
    const terminal = fact.terminal;
    if (terminal) {
      const prior = terminals.get(fact.id);
      const key = JSON.stringify([terminal.eventId, terminal.turnId, terminal.sequence, terminal.outcome]);
      if (prior && (terminal.sequence < prior.sequence || (terminal.sequence === prior.sequence && key !== prior.key))) {
        throw new Error('Regressing desktop pet terminal');
      }
      const eligible = continuous && observed.get(fact.id) === terminal.turnId && fact.activeTurnId === ''
        && terminal.outcome === 'completed';
      const consumed = prior?.key === key ? prior.consumed : !eligible;
      terminals.set(fact.id, { key, sequence: terminal.sequence, consumed });
      if (eligible && !consumed) completed.push({ id: fact.id, key });
      if (terminal.outcome !== 'completed') observed.delete(fact.id);
    }
    if (value.freshness === 'synced' && (rows.get(fact.id)?.state === 'running' || fact.waiting?.length) && fact.activeTurnId) observed.set(fact.id, fact.activeTurnId);
  }
  const waiting = facts.find(fact => fact.waiting?.length && fact.activeTurnId);
  let signal = 'idle', motion = 'static', label = '当前没有运行中的对话', cause = 'idle';
  if (value.freshness !== 'synced') label = value.freshness === 'recovering' ? '正在重新同步' : '状态未同步';
  else if (value.counts.error) { signal = 'error'; motion = 'full'; label = `${value.counts.error} 个对话出错`; cause = 'error'; }
  else if (waiting) { signal = 'waiting'; motion = 'full'; label = '有对话等回复'; cause = JSON.stringify(['waiting', waiting.id, waiting.activeTurnId, waiting.waiting.map(request => request.requestId)]); }
  else if (value.counts.running) { signal = 'working'; motion = 'full'; label = `${value.counts.running} 个对话进行中`; cause = 'working'; }
  else if (value.counts.attention) label = `${value.counts.attention} 个对话待查看`;
  else if (value.counts.paused) label = `${value.counts.paused} 个对话已暂停`;
  else if (value.counts.unknown) label = '部分状态未同步';
  else {
    const oldDone = continuous && previous.visual.signal === 'done'
      && facts.some(fact => fact.activeTurnId === '' && fact.terminal?.outcome === 'completed' && JSON.stringify(['done', fact.id, JSON.stringify([
        fact.terminal.eventId, fact.terminal.turnId, fact.terminal.sequence, fact.terminal.outcome])]) === producer.cause);
    if (completed.length || oldDone) {
      signal = 'done'; motion = 'full'; label = '有对话已完成';
      if (completed.length) {
        const done = completed[0]; cause = JSON.stringify(['done', done.id, done.key]);
        terminals.set(done.id, { ...terminals.get(done.id), consumed: true }); observed.delete(done.id);
      } else cause = producer.cause;
    } else if (!value.counts.terminal) motion = 'full';
  }
  while (terminals.size > 100) {
    const id = [...terminals.keys()].find(id => !admitted.has(id)) ?? terminals.keys().next().value;
    terminals.delete(id);
  }
  const same = continuous && signal === previous.visual.signal && cause === producer.cause;
  const arrivalKey = same ? previous.visual.arrivalKey
    : continuous && previous.counts.unknown === 0 && ['waiting', 'done', 'error'].includes(signal)
      ? `${producer.epoch}:${value.revision}` : null;
  return { visual: { signal, motion, label, arrivalKey }, observed, terminals, cause };
}

/** Retains presentation data only; it does not fetch, execute, or own a Session. */
export function installDesktopPetState({ ipcMain, getSource, origin, onSnapshot }) {
  let generation = 0;
  let producer = null;
  let releaseSource = () => {};
  let retained = unavailable();
  function unavailable() {
    return { schemaVersion: 1, producerEpoch: generation, revision: 0, sourceId: null,
      scopeId: null, freshness: 'unavailable', counts: emptyCounts(), conversations: [] };
  }
  function emit(value) {
    const extras = value.facts ? { facts: Object.freeze(value.facts.map(fact => Object.freeze({ ...fact,
      ...(fact.waiting ? { waiting: Object.freeze(fact.waiting.map(request => Object.freeze(request))) } : {}),
      ...(fact.terminal ? { terminal: Object.freeze(fact.terminal) } : {}) }))), visual: Object.freeze(value.visual) } : {};
    retained = Object.freeze({ ...value, ...extras, counts: Object.freeze({ ...value.counts }), conversations: Object.freeze(value.conversations.map(item => Object.freeze({ ...item }))) });
    onSnapshot(retained);
  }
  function invalidate() {
    releaseSource(); releaseSource = () => {};
    producer = null; generation += 1; emit(unavailable());
  }
  function trusted(event) {
    const source = getSource();
    let sameOrigin = false;
    try { sameOrigin = source && new URL(source.getURL()).origin === origin; } catch { /* Invalid URL is untrusted. */ }
    if (!source || source.isDestroyed() || event.sender !== source || event.senderFrame !== source.mainFrame || !sameOrigin) {
      throw new Error('Desktop pet producer rejected');
    }
    return source;
  }
  ipcMain.handle('paw-pet-state:begin', (event, value) => {
    const source = trusted(event); bounded(value);
    if (!exact(value, ['schemaVersion', 'sourceId', 'scopeId']) || value.schemaVersion !== 1
      || value.sourceId !== 'work-directory' || !identity(value.scopeId)) throw new Error('Invalid desktop pet identity');
    releaseSource();
    producer = { source, sourceId: value.sourceId, scopeId: value.scopeId, epoch: ++generation, revision: 0, observed: new Map(), terminals: new Map(), cause: '' };
    const revoke = () => { if (producer?.source === source) invalidate(); };
    const navigate = (_event, _url, isInPlace, isMainFrame) => { if (isMainFrame && !isInPlace) revoke(); };
    source.on('destroyed', revoke); source.on('render-process-gone', revoke); source.on('did-start-navigation', navigate);
    releaseSource = () => {
      source.removeListener('destroyed', revoke); source.removeListener('render-process-gone', revoke);
      source.removeListener('did-start-navigation', navigate);
    };
    emit({ ...unavailable(), sourceId: producer.sourceId, scopeId: producer.scopeId });
    return { producerEpoch: producer.epoch, factsVersion: 1 };
  });
  ipcMain.handle('paw-pet-state:publish', (event, value) => {
    const source = trusted(event); bounded(value);
    const fields = ['schemaVersion', 'producerEpoch', 'revision', 'freshness', 'counts', 'conversations'];
    if (value && Object.hasOwn(value, 'facts')) fields.push('facts');
    if (!exact(value, fields) || value.schemaVersion !== 1
      || !integer(value.producerEpoch) || !integer(value.revision) || value.revision < 1
      || !['synced', 'recovering', 'unavailable'].includes(value.freshness)
      || !exact(value.counts, COUNT_KEYS) || !COUNT_KEYS.every((key) => integer(value.counts[key]) && value.counts[key] <= 100)
      || COUNT_KEYS.reduce((total, key) => total + value.counts[key], 0) > 100) throw new Error('Invalid desktop pet state');
    if (!Array.isArray(value.conversations) || value.conversations.length > 8
      || !value.conversations.every(item => exact(item, ['id', 'label', 'state']) && identity(item.id)
        && typeof item.label === 'string' && [...item.label].length >= 1 && [...item.label].length <= 48
        && !/[\x00-\x1f\x7f]/.test(item.label) && COUNT_KEYS.includes(item.state))
      || new Set(value.conversations.map(item => item.id)).size !== value.conversations.length
      || COUNT_KEYS.some(key => value.conversations.filter(item => item.state === key).length > value.counts[key])) throw new Error('Invalid desktop pet conversations');
    if (!producer || producer.source !== source || value.producerEpoch !== producer.epoch || value.revision <= producer.revision) return false;
    const facts = Object.hasOwn(value, 'facts') ? canonicalFacts(value) : undefined;
    const projection = facts ? projectVisual(value, facts, retained, producer) : undefined;
    const snapshot = { schemaVersion: 1, producerEpoch: producer.epoch, revision: value.revision,
      sourceId: producer.sourceId, scopeId: producer.scopeId,
      freshness: value.freshness, counts: value.freshness === 'synced' ? value.counts : { ...emptyCounts(), unknown: COUNT_KEYS.reduce((total, key) => total + value.counts[key], 0) },
      conversations: value.conversations.map(item => ({ ...item, state: value.freshness === 'synced' ? item.state : 'unknown' })),
      ...(projection ? { facts: value.freshness === 'synced' ? facts : [], visual: projection.visual } : {}) };
    if (projection) bounded(snapshot);
    producer.revision = value.revision;
    producer.observed = projection?.observed ?? new Map(); producer.terminals = projection?.terminals ?? new Map(); producer.cause = projection?.cause ?? '';
    emit(snapshot);
    return true;
  });
  ipcMain.handle('paw-pet-state:release', (event, value) => {
    const source = trusted(event); bounded(value);
    if (!exact(value, ['producerEpoch']) || !integer(value.producerEpoch)) throw new Error('Invalid desktop pet release');
    if (!producer || producer.source !== source || value.producerEpoch !== producer.epoch) return false;
    invalidate(); return true;
  });
  return {
    snapshot: () => retained,
    canOpenConversation(target) {
      if (!exact(target, ['id', 'producerEpoch', 'sourceId', 'scopeId']) || !producer
        || !identity(target.id) || target.producerEpoch !== producer.epoch
        || target.sourceId !== producer.sourceId || target.scopeId !== producer.scopeId
        || producer.source !== getSource() || producer.source.isDestroyed()) return false;
      try { if (new URL(producer.source.getURL()).origin !== origin) return false; } catch { return false; }
      return retained.conversations.some(item => item.id === target.id);
    },
    dispose() {
      invalidate();
      for (const action of ['begin', 'publish', 'release']) ipcMain.removeHandler(`paw-pet-state:${action}`);
    },
  };
}
