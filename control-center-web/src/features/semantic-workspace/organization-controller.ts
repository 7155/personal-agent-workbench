import { loadOrganizationCatalog } from './organization-catalog';
import { OrganizationJournal, type PendingWrite } from './organization-journal';
import { requestOrganization } from './organization-request';
import { EMPTY_CATALOG, ok, parseProposal, spaceKeys, type Catalog, type Change, type Proposal,
  type Receipt, type RequestOrganization, type Space } from './organization-model';

export interface OrganizationState {
  catalog: Catalog; phase: 'idle' | 'loading' | 'ready' | 'partial' | 'error';
  error: string; notice: string; proposals: Readonly<Record<string, Proposal>>;
  suggestingKey: string | null; pending: PendingWrite | null; writing: boolean;
}
function statusOf(error: unknown): number | undefined {
  if (!error || typeof error !== 'object') return undefined;
  const e = error as { status?: unknown; httpStatus?: unknown };
  const value = e.status ?? e.httpStatus;
  return typeof value === 'number' ? value : undefined;
}
const message = (error: unknown) => error instanceof Error ? error.message : '操作结果未确认，请重新核实。';

/** Framework-free controller: catalog reads can be cancelled; a cancelled write
 * remains an unresolved intent until the server confirms its idempotency key.
 */
export class OrganizationController {
  private state: OrganizationState;
  private listeners = new Set<() => void>();
  private keys: string[] = [];
  private epoch = 0;
  private suggestionEpoch = 0;
  private readAbort?: AbortController;
  private suggestionAbort?: AbortController;
  private writeAbort?: AbortController;
  private disposed = false;
  private unsubscribeJournal: () => void;
  constructor(private request: RequestOrganization, readonly journal: OrganizationJournal,
              private now: () => number = Date.now, private newId: () => string = () => crypto.randomUUID()) {
    this.state = { catalog: EMPTY_CATALOG, phase: 'idle', error: '', notice: '', proposals: {},
      suggestingKey: null, writing: false, pending: journal.getSnapshot() };
    this.unsubscribeJournal = journal.subscribe(() => this.update({ pending: journal.getSnapshot() }));
  }
  getSnapshot = () => this.state;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  private update(patch: Partial<OrganizationState>) {
    if (this.disposed) return;
    this.state = { ...this.state, ...patch }; for (const listener of this.listeners) listener();
  }
  async setKeys(input: readonly string[]) {
    this.keys = spaceKeys(input);
    this.cancelSuggestion();
    this.update({ proposals: {} });
    await this.refresh();
  }
  refresh = async (): Promise<void> => {
    if (this.disposed) return;
    this.readAbort?.abort(); const epoch = ++this.epoch;
    const controller = new AbortController(); this.readAbort = controller;
    // Keep existing rows only as a stale read view. Writes are disabled while loading.
    // A change in scope removes rows not present in that scope immediately.
    const allowed = new Set(this.keys);
    const previous = this.state.catalog;
    this.update({ phase: 'loading', error: '', notice: '', catalog: { ...previous,
      items: previous.items.filter(item => allowed.has(item.key)),
      receipts: previous.receipts.filter(item => allowed.has(item.spaceKey)), requestedCount: this.keys.length } });
    try {
      const catalog = await loadOrganizationCatalog(this.request, this.keys, controller.signal);
      if (this.disposed || controller.signal.aborted || epoch !== this.epoch) return;
      const byKey = new Map(catalog.items.map(item => [item.key, item]));
      const proposals = Object.fromEntries(Object.entries(this.state.proposals).filter(([key, proposal]) => {
        const item = byKey.get(key);
        return item && proposal.expiresAtMs > this.now()
          && (proposal.expectedRevision === undefined || proposal.expectedRevision === item.revision)
          && (proposal.sourceRevision === undefined || item.sourceRevision === undefined || proposal.sourceRevision === item.sourceRevision);
      }));
      const phase = catalog.failedKeys.length ? (catalog.items.length ? 'partial' : 'error') : 'ready';
      this.update({ catalog, proposals, phase, error: catalog.failedKeys.length
        ? `${catalog.failedKeys.length} 个空间读取失败，当前搜索结果不完整。可以重试；没有显示旧数据为新结果。` : '' });
    } catch (error) {
      if (this.disposed || controller.signal.aborted || epoch !== this.epoch) return;
      this.update({ catalog: { ...EMPTY_CATALOG, requestedCount: this.keys.length }, phase: 'error', error: message(error) });
    }
  };
  private async refreshAffected(key: string): Promise<void> {
    if (this.disposed || !this.keys.includes(key)) return;
    // A user-initiated full refresh is allowed to finish against a new epoch.
    if (this.state.phase === 'loading') { await this.refresh(); return; }
    this.readAbort?.abort(); const epoch = ++this.epoch;
    const controller = new AbortController(); this.readAbort = controller;
    this.update({ phase: 'loading', error: '' });
    try {
      const fresh = await loadOrganizationCatalog(this.request, [key], controller.signal);
      if (this.disposed || controller.signal.aborted || epoch !== this.epoch) return;
      const old = this.state.catalog;
      const catalog: Catalog = {
        ...old,
        items: [...old.items.filter(item => item.key !== key), ...fresh.items],
        receipts: [...old.receipts.filter(item => item.spaceKey !== key), ...fresh.receipts],
        unavailable: [...old.unavailable.filter(item => item !== key), ...fresh.unavailable],
        failedKeys: [...old.failedKeys.filter(item => item !== key), ...fresh.failedKeys],
      };
      const phase = catalog.failedKeys.length ? (catalog.items.length ? 'partial' : 'error') : 'ready';
      this.update({ catalog, phase, error: catalog.failedKeys.length ? '部分空间读取失败，当前搜索范围不完整。' : '' });
    } catch (error) {
      if (!this.disposed && !controller.signal.aborted && epoch === this.epoch) {
        const old = this.state.catalog;
        const catalog = { ...old, items: old.items.filter(item => item.key !== key),
          receipts: old.receipts.filter(item => item.spaceKey !== key), failedKeys: [...new Set([...old.failedKeys, key])] };
        this.update({ catalog, phase: catalog.items.length ? 'partial' : 'error', error: message(error) });
      }
    }
  }
  private currentItem(key: string): Space {
    if (this.state.catalog.readOnlyReason) throw new Error(this.state.catalog.readOnlyReason);
    if (this.state.phase !== 'ready' && this.state.phase !== 'partial') throw new Error('目录正在刷新，请读取当前状态后再修改。');
    const item = this.state.catalog.items.find(row => row.key === key);
    if (!item) throw new Error('当前目录无法核实这个空间。');
    return item;
  }
  change = async (key: string, change: Change): Promise<void> => {
    if (this.disposed) return;
    try {
      const item = this.currentItem(key);
      if (change.operation === 'placement' && change.value === 'shelf' && item.pinned) throw new Error('请先取消固定。');
      if (change.operation === 'proposal') {
        const p = this.state.proposals[key];
        if (!p || p.id !== change.value || p.expiresAtMs <= this.now()) throw new Error('建议已过期，请重新获取。');
      }
      const id = this.newId();
      const pending: PendingWrite = { id, spaceKey: key, createdAtMs: this.now(), uncertain: false,
        pathId: 'agent.organization.command', body: { commandId: id, spaceKey: key, expectedRevision: item.revision, ...change } };
      this.journal.begin(pending); await this.send(pending, false);
    } catch (error) { this.update({ error: message(error) }); }
  };
  undo = async (receipt: Receipt): Promise<void> => {
    if (this.disposed) return;
    try {
      this.currentItem(receipt.spaceKey);
      if (!this.state.catalog.receipts.some(r => r.id === receipt.id && r.spaceKey === receipt.spaceKey)) throw new Error('请刷新可撤销记录。');
      const pending: PendingWrite = { id: receipt.id, spaceKey: receipt.spaceKey, createdAtMs: this.now(), uncertain: false,
        pathId: 'agent.organization.undo', body: { receiptId: receipt.id } };
      this.journal.begin(pending); await this.send(pending, false);
    } catch (error) { this.update({ error: message(error) }); }
  };
  retryPending = async (): Promise<void> => {
    const pending = this.journal.getSnapshot();
    if (this.disposed || this.state.writing || !pending) return;
    // Positive server confirmation is required; matching local metadata is not proof.
    await this.send(pending, true);
  };
  private async send(pending: PendingWrite, retried: boolean) {
    this.cancelSuggestion(); const controller = new AbortController(); this.writeAbort = controller;
    this.update({ writing: true, error: '', notice: '', proposals: {} });
    let confirmed = false;
    try {
      const result = ok(await requestOrganization(this.request, { pathId: pending.pathId, body: pending.body,
        signal: controller.signal, timeoutMs: 25_000 } as Parameters<RequestOrganization>[0]));
      // v1 undo did not return receiptId; command always did.
      if ((pending.pathId === 'agent.organization.command' && result.receiptId !== pending.id)
        || (result.receiptId !== undefined && result.receiptId !== pending.id)) throw new Error('整理回执与请求不符，尚不能确认结果。');
      confirmed = true;
      this.journal.resolve(pending.id);
      const notice = result.undone === true ? '服务器确认这项操作曾成功，之后已被撤销；没有重复应用。'
        : result.replayed === true ? '服务器确认原操作已处理；当前状态以下方重新读取的结果为准。'
        : result.noChange === true ? '当前状态已经一致，无需重复修改。'
        : pending.pathId === 'agent.organization.undo' ? '已撤销；原始对话未修改。' : '已保存，可在整理记录中撤销。';
      this.update({ notice });
      if (!this.disposed) {
        await this.refreshAffected(pending.spaceKey);
        if (this.state.phase === 'error' || this.state.phase === 'partial') {
          this.update({ notice: `${notice} 目录刷新不完整，请只重试读取，不要重复提交修改。` });
        } else this.update({ notice });
      }
    } catch (error) {
      const status = statusOf(error);
      // An explicit 4xx on the first attempt confirms rejection. After an
      // uncertain prior attempt even a 4xx cannot prove that prior write failed.
      if (!retried && !confirmed && [400, 401, 403, 404, 409, 422].includes(status ?? 0)) {
        this.journal.resolve(pending.id);
        this.update({ error: message(error) });
        if (!this.disposed) { await this.refresh(); this.update({ error: message(error) }); }
      } else {
        this.journal.markUncertain(pending.id);
        this.update({ error: '操作可能已经保存，但尚未收到可靠确认。请使用“核实上次操作”，不会生成新的操作ID。' });
      }
    } finally {
      this.update({ writing: false });
      if (this.writeAbort === controller) this.writeAbort = undefined;
    }
  }
  suggest = async (key: string): Promise<void> => {
    if (this.disposed || this.state.suggestingKey || this.journal.getSnapshot()) return;
    let item: Space;
    try { item = this.currentItem(key); } catch (error) { this.update({ error: message(error) }); return; }
    const epoch = ++this.suggestionEpoch; const controller = new AbortController(); this.suggestionAbort = controller;
    this.update({ suggestingKey: key, error: '', notice: '' });
    try {
      const result = await requestOrganization(this.request, { pathId: 'agent.organization.suggest', body: { spaceKey: key }, signal: controller.signal, timeoutMs: 25_000 });
      if (this.disposed || controller.signal.aborted || epoch !== this.suggestionEpoch) return;
      const latest = this.currentItem(key);
      if (item.revision !== latest.revision || item.sourceRevision !== latest.sourceRevision) throw new Error('空间已变化，未展示旧建议。');
      const proposal = parseProposal(result, latest, this.now());
      this.update(proposal ? { proposals: { ...this.state.proposals, [key]: proposal } }
        : { notice: typeof ok(result).message === 'string' ? String(ok(result).message) : '依据不足，保留当前用途。' });
    } catch (error) {
      if (!this.disposed && !controller.signal.aborted && epoch === this.suggestionEpoch) this.update({ error: message(error) });
    } finally {
      if (epoch === this.suggestionEpoch) this.update({ suggestingKey: null });
    }
  };
  dismiss = (key: string) => {
    const proposals = { ...this.state.proposals }; delete proposals[key]; this.update({ proposals });
  };
  private cancelSuggestion() {
    this.suggestionEpoch++; this.suggestionAbort?.abort(); this.update({ suggestingKey: null });
  }
  dispose() {
    this.disposed = true; this.epoch++; this.suggestionEpoch++;
    this.readAbort?.abort(); this.suggestionAbort?.abort();
    if (this.writeAbort) {
      const pending = this.journal.getSnapshot();
      if (pending) this.journal.markUncertain(pending.id);
      this.writeAbort.abort();
    }
    this.unsubscribeJournal(); this.listeners.clear();
  }
}
