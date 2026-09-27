import { useState } from 'react';
import type { RoomRouteVisibility } from './room-route-visibility';

export function PawRoomRoutingEvidence({ routes, live, onOpenParticipant }: {
  routes: readonly RoomRouteVisibility[];
  live: boolean;
  onOpenParticipant: (id: string) => void;
}) {
  const [showAll, setShowAll] = useState(false);
  const display = showAll ? routes : routes.slice(-8);
  return <details className="paw-room-routing" aria-label="分派依据与 Jev 回执">
    <summary>分派依据{routes.length ? ` · ${routes.length} 条记录` : ''}<span>{routes.some((route) => route.source === 'jev') ? '含 Jev 决策回执' : 'Room 现有路由'}</span></summary>
    <p>选择接收者 ≠ 开始执行 ≠ 通过验收。这里仅展示已有回执，不发起路由或模型调用。{!live ? ' 当前显示上次记录。' : ''}</p>
    {!routes.some((route) => route.source === 'jev') ? <p>本轮没有 Jev 决策回执；不能据此判断账号配置或 Provider 是否可用。</p> : null}
    {display.map((route) => <article key={route.id} data-decision={route.decision}>
      <header><strong>{route.decisionLabel}</strong><button type="button" disabled={!route.canOpen} onClick={() => onOpenParticipant(route.targetId)}>{route.targetName} · 会话</button></header>
      <p className="paw-room-routing__execution" data-execution={route.execution}>{route.executionLabel}</p>
      {route.confidence !== undefined ? <small>决策置信度 {route.confidence.toFixed(2)}，不是任务完成度或准确率。</small> : null}
      <details><summary>查看路由与执行依据</summary>
        <dl><dt>Root</dt><dd>{route.rootId}</dd><dt>Dispatch</dt><dd>{route.dispatchId || '未记录'}</dd>
          <dt>路由记录</dt><dd>{route.id}</dd><dt>开始回执</dt><dd>{route.startedReceiptId || '当前窗口未包含'}</dd>
          <dt>结束依据</dt><dd>{route.executionReceiptId || (['returned', 'failed', 'stopped'].includes(route.execution) ? '当前 Root 的精确 dispatch 终态记录' : '尚无') }</dd>
          <dt>决策来源版本</dt><dd>{route.sourceRevision || '未提供'}（历史版本，不代表当前候选仍可再次应用）</dd></dl>
        <pre>{JSON.stringify(route.raw, null, 2)}</pre>
      </details>
    </article>)}
    {!routes.length ? <p>当前事件窗口没有分派回执。任务责任关系仍可查看，不从责任连线猜测实际执行。</p> : null}
    {!showAll && routes.length > display.length ? <button type="button" onClick={() => setShowAll(true)}>再显示 {routes.length - display.length} 条本轮记录</button> : null}
  </details>;
}
