import { Archive, Layers, MessageCircle, MoreHorizontal, FileText, Play } from 'lucide-react';
import { DisclosureMenu } from './DisclosureMenu';
import type { DockTab } from './EarthDataDock';

export type WorkbenchView = 'split' | 'map' | 'code';
export type WorkbenchPanel = 'tasks' | 'delivery' | 'cloud' | 'console' | 'sources' | 'gis' | 'knowledge' | 'remote' | 'raster';
export const PANEL_TITLES: Record<WorkbenchPanel, string> = {
  tasks: '开始分析', delivery: '报告与交付', remote: '遥感分析', raster: '查询栅格',
  gis: '避让筛选', knowledge: '方法参考', sources: '数据来源', console: '云端运行详情', cloud: '云端任务',
};
export function WorkbenchToolbar({ view, panel, onView, onPanel, agentVisible = false, onAgent, dockView, onData, onRefresh, workspaceReady = true }: {
  view: WorkbenchView; panel: WorkbenchPanel | null;
  onView: (view: WorkbenchView) => void; onPanel: (panel: WorkbenchPanel | null) => void;
  agentVisible?: boolean; onAgent?: () => void; dockView?: DockTab | null;
  onData?: (tab: DockTab | null) => void; onRefresh?: () => void; workspaceReady?: boolean;
}) {
  const dataOpen = Boolean(dockView && dockView !== 'runs');
  return <nav className="earth-simple-toolbar" aria-label="地图工作区">
    <button type="button" className="earth-simple-toolbar__analyze" aria-expanded={panel === 'tasks' || panel === 'remote' || panel === 'gis'} onClick={() => onPanel(panel === 'tasks' ? null : 'tasks')}><Play size={15} aria-hidden="true" />分析</button>
    <button type="button" className="earth-simple-toolbar__ask" aria-expanded={agentVisible} aria-controls="earth-agent-panel" onClick={onAgent}>
      <MessageCircle size={16} aria-hidden="true" />Agent
    </button>
    <button type="button" aria-expanded={dataOpen} onClick={() => onData?.(dataOpen ? null : 'layers')}>
      <Layers size={16} aria-hidden="true" />数据
    </button>
    <button type="button" aria-expanded={dockView === 'runs'} onClick={() => onData?.(dockView === 'runs' ? null : 'runs')}>
      <Archive size={16} aria-hidden="true" />结果
    </button>
    <button type="button" aria-expanded={panel === 'delivery'} onClick={() => onPanel(panel === 'delivery' ? null : 'delivery')}><FileText size={16} aria-hidden="true" />报告</button>
    <DisclosureMenu label={<><MoreHorizontal size={16} aria-hidden="true" /><span>更多</span></>} className="earth-simple-toolbar__more">
      {close => <>
        <button type="button" onClick={() => { close(); onPanel('delivery'); }}>导出成果</button>
        <button type="button" onClick={() => { close(); onData?.('databases'); }}>连接数据库</button>
        <div className="earth-disclosure__group" role="group" aria-label="视图布局">
          {([['map', '地图'], ['split', '地图＋代码'], ['code', '代码']] as const).map(([key, label]) =>
            <button type="button" key={key} aria-pressed={view === key} onClick={() => { close(); onView(key); }}>{label}</button>)}
        </div>
        <div className="earth-disclosure__group">
          <button type="button" onClick={() => { close(); onPanel('cloud'); }}>云端任务</button>
          <button type="button" onClick={() => { close(); onPanel('console'); }}>云端运行详情</button>
          <button type="button" onClick={() => { close(); onPanel('raster'); }}>查询栅格</button>
          <button type="button" onClick={() => { close(); onPanel('knowledge'); }}>方法参考</button>
          <button type="button" onClick={() => { close(); onPanel('sources'); }}>数据来源</button>
          <button type="button" disabled={!workspaceReady} onClick={() => { close(); onRefresh?.(); }}>刷新数据</button>
        </div>
      </>}
    </DisclosureMenu>
  </nav>;
}
