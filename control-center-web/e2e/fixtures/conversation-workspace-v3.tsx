import { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ControlTransportProvider } from '../../src/app/control-transport';
import { createPreviewTransport } from '../../src/app/preview-control-transport';
import { TooltipProvider } from '../../src/components/primitives';
import { MarkdownBody } from '../../src/features/agent/timeline/MarkdownRenderer';
import { CodeContentBlock } from '../../src/features/agent/timeline/CodeDiffRenderers';
import { CopyAction, DownloadAction } from '../../src/features/agent/timeline/rich/RichBlockTools';
import { RoomPlanetAvatar } from '../../src/features/rooms/RoomPlanetAvatar';
import { useReceiptHighlight } from '../../src/features/semantic-workspace/use-receipt-highlight';
import { parseJevSnapshot } from '../../src/features/semantic-workspace/jev-execution';
import { jevMission } from '../../src/features/semantic-workspace/jev-mission';
import { JevRailFilters, matchesJevRailFilter, type JevRailFilter } from '../../src/paw-os/apps/JevRailFilters';
import { updateReadingPreferences, useReadingPreferences } from '../../src/features/conversation-ui/reading/reading-preferences';
import '../../src/design/tokens.css';
import '../../src/design/typography.css';
import '../../src/components/primitives/primitives.css';
import '../../src/features/agent/agent.css';
import '../../src/features/conversation-ui/conversation-ui.css';
import '../../src/features/agent/timeline/rich/rich-conversation.css';
import '../../src/paw-os/apps/paw-jev-mission.css';
import './conversation-workspace-v3.css';

const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
const transport = createPreviewTransport();
const source = `type ReadingMode = 'standard' | 'large';\n\nexport function readingSize(mode: ReadingMode): number {\n  return mode === 'large' ? 18 : 16;\n}`;
const prose = `先读结论，需要时再核对过程。**这是合成输入，未调用模型。**

## 1. 把当前工作说清楚

任务标题说明要完成什么，状态解释目前到了哪里。工具次数不代表完成百分比，已经提交的结果也不自动等于通过验收。

## 2. 让不同内容各得其所

| 内容类型 | 默认展示 | 进一步操作 | 来源 | 当前状态 | 说明 |
| --- | --- | --- | --- | --- | --- |
| 中文正文 | 连续阅读 | 章节与字号 | 示例材料 | 已返回 | 不替用户改写结论 |
| 代码 | 完整源码 | 复制与保存 | 示例代码 | 已返回 | 不执行代码 |
| 交付文件 | 名称与状态 | 查看来源 | 合成回执 | 待复核 | 不提前标记完成 |

## 3. 需要关注，不一定需要干预

等待依赖是任务安排；失败或回执不明确才需要突出显示。是否允许重试、返修或改派，仍由原来的能力与状态决定。

## 4. 动效只解释变化

阅读位置由原对话组件管理。章节跳转只发生在你点击之后，字号变化不修改消息。新回执可以短暂高亮，恢复历史不补演过去。
`;
function graph(accepted: boolean) {
  return parseJevSnapshot({ ok: true, mode: 'jev', graphId: 'workspace-v3-fixture', rootId: 'root', phase: 'execute',
    tasks: [
      { id: 'root', objective: '阅读体验检查', owner_id: 'earth', state: 'active', parent_id: '', revision: 1 },
      { id: 'read', objective: '核对正文与来源', owner_id: 'mars', state: 'done', parent_id: 'root', revision: 1 },
      { id: 'check', objective: '复核阅读设置', owner_id: 'earth', state: accepted ? 'done' : 'review', parent_id: 'root', revision: 1 },
      { id: 'failed', objective: '有失败记录的示例', owner_id: 'venus', state: 'failed', parent_id: 'root', revision: 1 },
    ], effects: [], edges: [], events: [], stopped: false, snapshotVersion: accepted ? '2' : '1',
    ready: [], running: [], review: accepted ? [] : ['check'], blocked: [], final: {},
  }, 'workspace-v3-fixture');
}
function Fixture() {
  const [dark, setDark] = useState(false);
  const [accepted, setAccepted] = useState(false);
  const [filter, setFilter] = useState<JevRailFilter>('all');
  const preferences = useReadingPreferences();
  const { tasks } = jevMission(graph(accepted));
  const fresh = useReceiptHighlight('workspace-v3-fixture', accepted ? ['read:1', 'check:1'] : ['read:1'], true);
  useEffect(() => { document.documentElement.dataset.theme = dark ? 'dark' : 'light'; }, [dark]);
  return <QueryClientProvider client={client}><ControlTransportProvider transport={transport}><TooltipProvider>
    <div className="v3-fixture">
      <header className="v3-fixture__controls"><strong>V3 · 实际 React 组件 / 合成输入</strong>
        <button type="button" onClick={() => setDark(value => !value)}>{dark ? '浅色' : '深色'}</button>
        <button type="button" aria-pressed={preferences.motion === 'reduced'} onClick={() => updateReadingPreferences({ motion: preferences.motion === 'reduced' ? 'system' : 'reduced' })}>减少动态</button>
      </header>
      <main className="v3-fixture__layout"><article className="v3-fixture__prose">
        <header><RoomPlanetAvatar ordinal={0} size={30} activity="static" /><h1>信息清楚，阅读安静</h1></header>
        <MarkdownBody documentKey="v3-reading-example" text={prose} />
        <CodeContentBlock code={source} language="typescript" fileName="reading-size.ts" />
        <div className="v3-fixture__actions"><CopyAction value={prose} label="复制示例" /><DownloadAction value={prose} fileName="reading-example.md" /></div>
      </article><aside className="v3-fixture__rail"><h2>任务筛选</h2><p>使用已有快照解析与任务投影；没有网络执行。</p>
        <JevRailFilters items={tasks} value={filter} onChange={setFilter} />
        <ul>{tasks.filter(task => matchesJevRailFilter(task, filter)).map(item => <li key={item.task.id} data-fresh={fresh.has(`${item.task.id}:${item.task.revision}`) || undefined}><strong>{item.task.objective}</strong><small>{item.stage}</small></li>)}</ul>
        <button type="button" disabled={accepted} onClick={() => setAccepted(true)}>注入一条合成验收回执</button>
        <p>切到后台或减少动态时只更新内容，不补演高亮。</p>
        <a href="./conversation-rich-v2.html">查看 V2 所有内容类型</a>
      </aside></main>
    </div>
  </TooltipProvider></ControlTransportProvider></QueryClientProvider>;
}
const root = document.getElementById('root');
if (root) createRoot(root).render(<Fixture />);
