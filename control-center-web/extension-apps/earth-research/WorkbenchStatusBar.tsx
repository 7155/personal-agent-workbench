import { Activity, Layers } from 'lucide-react';
import type { GISCommandActivity } from './gis-command-queue';
const states = { queued: '等待执行', running: '正在执行', completed: '回执已读', failed: '未成功', superseded: '尚未启动，已撤回' };
export function WorkbenchStatusBar({ sessionId, layerCount, selectedCount, activities, incomplete }: {
  sessionId?: string; layerCount: number; selectedCount: number; activities: GISCommandActivity[]; incomplete: boolean;
}) {
  const visible = activities.filter(item => item.sessionId === sessionId && item.label !== '读取运行记录');
  const running = visible.find(item => item.status === 'running');
  const latest = visible.at(-1);
  const queued = visible.filter(item => item.status === 'queued').length;
  return <footer className="earth-statusbar" aria-label="GIS 工作区状态">
    <span><Layers size={13} aria-hidden="true" />{layerCount} 个图层{incomplete ? ' · 部分数据待读取' : ''}</span>
    {selectedCount > 0 ? <span>{selectedCount} 个已选</span> : null}
    <details className="earth-statusbar__jobs">
      <summary><Activity size={13} aria-hidden="true" /><span role="status">{running ? running.label : queued ? `${queued} 项等待执行` : latest?.status === 'failed' ? '上次操作未成功 · 查看' : incomplete ? '部分数据待读取' : '操作记录'}</span></summary>
      <section aria-label="工作区操作记录"><h3>本窗口的操作</h3><p>已启动的计算不会因关闭面板而撤销。运行成果以磁盘回执为准。</p>
        {!visible.length ? <p>还没有执行操作。选择任务后，这里会记录进度。</p> : <ol>{visible.slice(-12).reverse().map(item =>
          <li key={item.id} data-state={item.status}><strong>{item.label}</strong><span>{states[item.status]}</span>{item.error ? <p>{item.error}</p> : null}</li>)}</ol>}
      </section>
    </details>
  </footer>;
}
