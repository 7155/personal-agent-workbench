import { useEffect, useRef, useState } from 'react';
import './cloud-tasks.css';
export type CloudTask = { id?: string; name?: string; description?: string; state?: string; error_message?: string };
export type CloudTaskSnapshot = { project?: string; checkedAt?: string; tasks: CloudTask[] };
const labels: Record<string, string> = { READY: '排队中', RUNNING: '运行中', COMPLETED: '已完成', FAILED: '失败', CANCEL_REQUESTED: '正在取消', CANCELLED: '已取消', UNSUBMITTED: '未提交' };
export function CloudTasksPanel({ onRefresh, onCancel, active = true }: {
  onRefresh: () => Promise<CloudTaskSnapshot>; onCancel: (id: string) => Promise<unknown>; active?: boolean;
}) {
  const [snapshot, setSnapshot] = useState<CloudTaskSnapshot>(), [error, setError] = useState('');
  const [busy, setBusy] = useState(false), [automatic, setAutomatic] = useState(true), [cancelId, setCancelId] = useState('');
  const [requested, setRequested] = useState<Record<string, number>>({});
  const alive = useRef(true), loading = useRef(false), cancelling = useRef(false), generation = useRef(0);
  const refreshRef = useRef(onRefresh); refreshRef.current = onRefresh;
  async function refresh() {
    if (loading.current) return;
    const version = generation.current; loading.current = true; setBusy(true);
    try {
      const next = await refreshRef.current();
      if (!next || !Array.isArray(next.tasks) || next.tasks.some(task => !task || typeof task !== 'object')) throw new Error('云端没有返回有效任务列表。');
      if (alive.current && generation.current === version) {
        setSnapshot(next); setError('');
        const waiting = new Set(next.tasks.filter(task => ['READY', 'RUNNING'].includes(task.state || '')).map(task => task.id || task.name));
        setRequested(current => Object.fromEntries(Object.entries(current).filter(([id, at]) => waiting.has(id) && Date.now() - at < 30000)));
      }
    } catch (reason) { if (alive.current && generation.current === version) setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { loading.current = false; if (alive.current) setBusy(false); }
  }
  useEffect(() => { alive.current = true; return () => { alive.current = false; generation.current += 1; }; }, []);
  useEffect(() => {
    if (!active) return;
    void refresh();
    if (!automatic) return;
    const timer = setInterval(() => { if (!document.hidden) void refresh(); }, 10000);
    return () => clearInterval(timer);
  }, [active, automatic]);
  async function cancel(id: string) {
    if (cancelling.current) return;
    cancelling.current = true; setCancelId(id); setError('');
    try {
      const receipt = await onCancel(id) as { status?: string; cancelledTaskIds?: string[] } | undefined;
      if (!receipt || !['submitted', 'completed'].includes(receipt.status || '') || receipt.cancelledTaskIds && !receipt.cancelledTaskIds.includes(id)) throw new Error('取消请求尚未得到匹配回执，请刷新核对状态。');
      if (alive.current) { setRequested(items => ({ ...items, [id]: Date.now() })); await refresh(); }
    } catch (reason) { if (alive.current) setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { cancelling.current = false; if (alive.current) setCancelId(''); }
  }
  const count = (state: string) => snapshot?.tasks.filter(task => task.state === state).length ?? 0;
  return <section className="earth-cloud-tasks" aria-label="实时云端任务">
    <header><div><h2>Earth Engine 云端任务</h2><p>读取真实任务状态；关闭面板后停止自动轮询。</p></div><button type="button" disabled={busy} onClick={() => void refresh()}>{busy ? '正在读取…' : '刷新'}</button></header>
    <label><input type="checkbox" checked={automatic} onChange={event => setAutomatic(event.target.checked)} />每 10 秒刷新</label>
    <div className="earth-cloud-tasks__summary"><span>排队 {count('READY')}</span><span>运行 {count('RUNNING')}</span><span>完成 {count('COMPLETED')}</span><span>失败 {count('FAILED')}</span></div>
    {snapshot ? <p className="earth-cloud-tasks__stamp">{snapshot.project} · 更新于 {snapshot.checkedAt ? new Date(snapshot.checkedAt).toLocaleTimeString() : '未知'}</p> : null}
    {error ? <p role="alert">{error}{snapshot ? ' 上次状态已保留，当前状态尚未确认。' : ''}</p> : null}
    {snapshot?.tasks.length === 0 ? <p>当前没有云端批处理任务。地图预览和即时统计不会生成批处理任务。</p> : null}
    <ul>{snapshot?.tasks.map((task, index) => { const id = task.id || task.name; return <li key={id || index} data-state={task.state}>
      <div><strong>{task.description || id || '未命名任务'}</strong><span>{labels[task.state || ''] || task.state || '状态未知'}</span></div>
      {task.error_message ? <p role="alert">{task.error_message}</p> : null}
      {id ? <details><summary>任务标识</summary><code>{id}</code></details> : null}
      {id && ['READY', 'RUNNING'].includes(task.state || '') ? <button type="button" disabled={Boolean(cancelId) || Boolean(requested[id])} onClick={() => void cancel(id)}>{requested[id] ? '已请求取消' : cancelId === id ? '正在请求…' : '取消这个任务'}</button> : null}
    </li>; })}</ul>
    <p>云端任务完成后，仍需确认成果已下载；“已完成”不表示本地文件已就绪。</p>
  </section>;
}
