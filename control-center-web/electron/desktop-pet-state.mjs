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
    retained = Object.freeze({ ...value, counts: Object.freeze({ ...value.counts }), conversations: Object.freeze(value.conversations.map(item => Object.freeze({ ...item }))) });
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
    producer = { source, sourceId: value.sourceId, scopeId: value.scopeId, epoch: ++generation, revision: 0 };
    const revoke = () => { if (producer?.source === source) invalidate(); };
    const navigate = (_event, _url, isInPlace, isMainFrame) => { if (isMainFrame && !isInPlace) revoke(); };
    source.on('destroyed', revoke); source.on('render-process-gone', revoke); source.on('did-start-navigation', navigate);
    releaseSource = () => {
      source.removeListener('destroyed', revoke); source.removeListener('render-process-gone', revoke);
      source.removeListener('did-start-navigation', navigate);
    };
    emit({ ...unavailable(), sourceId: producer.sourceId, scopeId: producer.scopeId });
    return { producerEpoch: producer.epoch };
  });
  ipcMain.handle('paw-pet-state:publish', (event, value) => {
    const source = trusted(event); bounded(value);
    if (!exact(value, ['schemaVersion', 'producerEpoch', 'revision', 'freshness', 'counts', 'conversations']) || value.schemaVersion !== 1
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
    producer.revision = value.revision;
    emit({ schemaVersion: 1, producerEpoch: producer.epoch, revision: producer.revision,
      sourceId: producer.sourceId, scopeId: producer.scopeId,
      freshness: value.freshness, counts: value.freshness === 'synced' ? value.counts : { ...emptyCounts(), unknown: COUNT_KEYS.reduce((total, key) => total + value.counts[key], 0) },
      conversations: value.conversations.map(item => ({ ...item, state: value.freshness === 'synced' ? item.state : 'unknown' })) });
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
