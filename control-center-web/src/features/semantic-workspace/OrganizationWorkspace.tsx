import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ArchiveRestore, ChevronLeft, ChevronRight, Pin, RefreshCw, Search, Undo2, X } from 'lucide-react';
import { useControlTransport } from '@/app/control-transport';
import { OrganizationController, type OrganizationState } from './organization-controller';
import { journalForTransport } from './organization-journal';
import { CATEGORY_LABELS, EMPTY_CATALOG, pageItems, selectSpaces, spaceKeys as normalizeKeys,
  type Category, type Change, type Placement, type RequestOrganization, type Space } from './organization-model';
import { OrganizationSurface } from './OrganizationSurface';

const INITIAL: OrganizationState = { catalog: EMPTY_CATALOG, phase: 'idle', error: '', notice: '',
  proposals: {}, suggestingKey: null, writing: false, pending: null };

/** Drop-in replacement. Existing App/Workspace identity and Runtime remain intact. */
export function OrganizationWorkspace({ spaceKeys, selectedKey, onOpen, onClose }: {
  spaceKeys: readonly string[]; selectedKey: string; onOpen: (key: string) => void; onClose: () => void;
}) {
  const transport = useControlTransport();
  const request = useCallback<RequestOrganization>(input => transport.request(input), [transport]);
  const journal = useMemo(() => journalForTransport(transport), [transport]);
  const controller = useRef<OrganizationController | null>(null);
  const [state, setState] = useState<OrganizationState>(INITIAL);
  const [query, setQuery] = useState(''); const [placement, setPlacement] = useState<Placement | 'all'>('desk');
  const [category, setCategory] = useState<Category | 'all'>('all');
  const [sort, setSort] = useState<'recent' | 'title'>('recent'); const [page, setPage] = useState(0);
  const signature = JSON.stringify(normalizeKeys(spaceKeys));
  // Effect-owned instance avoids reusing a disposed controller after StrictMode's
  // setup/cleanup/setup probe. Journal survives view changes, controller does not.
  useEffect(() => {
    const next = new OrganizationController(request, journal); controller.current = next;
    setState(next.getSnapshot());
    const unsubscribe = next.subscribe(() => setState(next.getSnapshot()));
    return () => { unsubscribe(); next.dispose(); if (controller.current === next) controller.current = null; };
  }, [request, journal]);
  useEffect(() => { void controller.current?.setKeys(JSON.parse(signature) as string[]); }, [signature, request, journal]);
  const { catalog, phase, pending } = state;
  const reading = phase === 'idle' || phase === 'loading';
  const canWrite = (phase === 'ready' || phase === 'partial') && !pending && !state.writing && !catalog.readOnlyReason;
  const filtered = useMemo(() => selectSpaces(catalog.items, { query, placement, category, sort }),
    [catalog.items, query, placement, category, sort]);
  const paged = pageItems(filtered, page);
  const deskCount = catalog.items.filter(item => item.placement === 'desk').length;
  const shelfCount = catalog.items.length - deskCount;
  const change = (item: Space, value: Change) => { void controller.current?.change(item.key, value); };
  const clearFilters = () => { setQuery(''); setCategory('all'); setPlacement('all'); setPage(0); };

  return <OrganizationSurface onClose={onClose}>
    {close => <aside aria-label="Jev 工作空间目录" className="jev-workspace">
      <header className="jev-workspace__heading">
        <div><h2>工作空间</h2><p>找回已有工作，接着往前走。</p></div>
        <button aria-label="关闭工作空间" onClick={close} type="button"><X size={18} /></button>
      </header>
      <label className="jev-workspace__search"><Search aria-hidden size={16} />
        <input aria-label="搜索已载入的全部空间" autoComplete="off" placeholder="搜索标题、用途或分组"
          value={query} onChange={event => { setQuery(event.target.value); setPage(0); }} />
        {query ? <button aria-label="清空空间搜索" onClick={() => { setQuery(''); setPage(0); }} type="button"><X size={14} /></button> : null}
      </label>
      <div aria-label="空间位置" className="jev-workspace__tabs" role="group">
        {([['desk', `当前工作 ${deskCount}`], ['shelf', `已收起 ${shelfCount}`], ['all', '全部']] as const).map(([value, label]) =>
          <button aria-pressed={placement === value} key={value} onClick={() => { setPlacement(value); setPage(0); }} type="button">{label}</button>)}
        <button aria-label="刷新工作目录" disabled={reading} onClick={() => void controller.current?.refresh()} type="button"><RefreshCw aria-hidden size={14} /></button>
      </div>
      <div className="jev-workspace__filters">
        <label><span className="jev-sr-only">筛选空间用途</span><select aria-label="筛选空间用途" value={category}
          onChange={event => { setCategory(event.target.value as Category | 'all'); setPage(0); }}>
          <option value="all">全部用途</option>{Object.entries(CATEGORY_LABELS).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
        </select></label>
        <label><span className="jev-sr-only">空间排序</span><select aria-label="空间排序" value={sort}
          onChange={event => { setSort(event.target.value as 'recent' | 'title'); setPage(0); }}>
          <option value="recent">记录更新优先</option><option value="title">按标题</option>
        </select></label>
      </div>
      <p className="jev-workspace__scope">分类建议仅发送你选中的标题；搜索与手动整理不调用模型。</p>
      {catalog.readOnlyReason ? <p className="jev-workspace__scope" role="status">{catalog.readOnlyReason}</p> : null}
      {state.error ? <p className="jev-workspace__error" role="alert">{state.error}</p> : null}
      {pending ? <div className="jev-workspace__recovery" role="status">
        <strong>{state.writing ? '正在确认整理结果' : '有一项整理结果待确认'}</strong>
        <p>切换界面不会取消已提交的修改。核实时复用原操作ID，不重复新建操作。</p>
        <button disabled={state.writing || !pending.uncertain} onClick={() => void controller.current?.retryPending()} type="button">核实上次操作</button>
        {journal.getPersistenceError() ? <p>浏览器未能保存待确认记录，请在关闭此标签页前完成核实。</p> : null}
      </div> : null}
      <p aria-live="polite" className="jev-workspace__notice">{reading ? '正在读取已载入目录…' : state.notice}</p>
      <div aria-busy={reading} className="jev-workspace__list">
        {!reading && !paged.items.length ? <div className="jev-workspace__empty">
          <h3>{phase === 'error' ? '目录暂时无法读取' : query || category !== 'all' ? '当前筛选没有匹配项' : placement === 'shelf' ? '这里保留暂时收起的工作' : '从一段真实工作开始'}</h3>
          <p>{phase === 'error' ? '你的对话仍在原处，重试读取即可，不需要重新创建。'
            : phase === 'partial' ? '部分空间尚未读取，不能据此判断整个目录没有结果。'
            : query || category !== 'all' ? '搜索覆盖已读取目录的所有页，可以换个关键词或清除筛选。'
            : placement === 'shelf' ? '收起不是删除，重新打开只恢复阅读，不启动 Agent。' : '开始对话后，就能在这里查找、分组和继续阅读。'}</p>
          {phase === 'error' || phase === 'partial' ? <button onClick={() => void controller.current?.refresh()} type="button">重新读取目录</button>
            : query || category !== 'all' ? <button onClick={clearFilters} type="button">清除筛选</button> : null}
        </div> : null}
        {paged.items.map(item => <article className="jev-space" data-selected={selectedKey === item.key} key={item.key}>
          <div className="jev-space__title"><button aria-current={selectedKey === item.key ? 'page' : undefined}
            onClick={() => onOpen(item.key)} type="button">{item.title}</button>
            <button aria-label={`${item.pinned ? '取消固定' : '固定'} ${item.title}`} aria-pressed={item.pinned} disabled={!canWrite}
              onClick={() => change(item, { operation: 'pinned', value: !item.pinned })} type="button"><Pin aria-hidden size={14} /></button>
          </div>
          <div className="jev-space__metadata"><select aria-label={`${item.title}的用途`} disabled={!canWrite} value={item.category}
            onChange={event => change(item, { operation: 'category', value: event.target.value as Category })}>
            {Object.entries(CATEGORY_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select><span>{item.key.startsWith('room:') ? '协作空间' : '对话'}{selectedKey === item.key ? ' · 当前打开' : ''}</span></div>
          <GroupEditor item={item} disabled={!canWrite} onSave={value => change(item, { operation: 'group', value })} />
          <div className="jev-space__actions">
            <button disabled={!canWrite || state.suggestingKey !== null} onClick={() => void controller.current?.suggest(item.key)} type="button">
              {state.suggestingKey === item.key ? '正在判断…' : 'Jev 分类建议'}</button>
            <button disabled={!canWrite || (item.placement === 'desk' && (item.pinned || selectedKey === item.key))}
              onClick={() => change(item, { operation: 'placement', value: item.placement === 'shelf' ? 'desk' : 'shelf' })} type="button">
              {item.placement === 'shelf' ? <><ArchiveRestore aria-hidden size={13} />放回当前工作</> : '收起入口'}</button>
          </div>
          {state.proposals[item.key] ? <div className="jev-space__proposal">
            <strong>建议归为「{CATEGORY_LABELS[state.proposals[item.key].category]}」</strong><p>{state.proposals[item.key].basis}</p>
            <button disabled={!canWrite} onClick={() => change(item, { operation: 'proposal', value: state.proposals[item.key].id })} type="button">采用</button>
            <button onClick={() => controller.current?.dismiss(item.key)} type="button">忽略本次</button>
          </div> : null}
        </article>)}
      </div>
      {catalog.receipts.length ? <details className="jev-workspace__receipts"><summary>可撤销的整理 · {catalog.receipts.length}</summary>
        {catalog.receipts.map(receipt => <button disabled={!canWrite} key={receipt.id} onClick={() => void controller.current?.undo(receipt)} type="button">
          <Undo2 aria-hidden size={13} />撤销「{catalog.items.find(item => item.key === receipt.spaceKey)?.title ?? '工作空间'}」的上次整理
        </button>)}
      </details> : null}
      <footer><span>已读取 {catalog.items.length}/{catalog.requestedCount} 项 · 匹配 {filtered.length} 项
        {catalog.unavailable.length ? ` · ${catalog.unavailable.length} 项不支持整理` : ''}
        {catalog.failedKeys.length ? ` · ${catalog.failedKeys.length} 项失败` : ''}</span>
        <p>范围为 Agent 已载入目录；收起不改变运行状态，也不会删除历史。</p>
        {paged.pages > 1 ? <nav aria-label="搜索结果分页"><button aria-label="上一页空间" disabled={paged.page === 0}
          onClick={() => setPage(paged.page - 1)} type="button"><ChevronLeft aria-hidden size={14} /></button>
          <span>{paged.page + 1} / {paged.pages}</span><button aria-label="下一页空间" disabled={paged.page + 1 >= paged.pages}
          onClick={() => setPage(paged.page + 1)} type="button"><ChevronRight aria-hidden size={14} /></button></nav> : null}
      </footer>
    </aside>}
  </OrganizationSurface>;
}
function GroupEditor({ item, disabled, onSave }: { item: Space; disabled: boolean; onSave: (value: string) => void }) {
  const [value, setValue] = useState(item.group);
  useEffect(() => setValue(item.group), [item.key, item.group]);
  return <details className="jev-space__group"><summary>{item.group ? `分组：${item.group}` : '设置分组'}</summary>
    <form onSubmit={event => { event.preventDefault(); if (!disabled) onSave(value.trim()); }}>
      <input aria-label={`${item.title}的分组`} disabled={disabled} maxLength={80} placeholder="例如：RAG 实验" value={value} onChange={event => setValue(event.target.value)} />
      <button disabled={disabled || value.trim() === item.group} type="submit">保存</button>
    </form>
  </details>;
}
