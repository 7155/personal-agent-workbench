import { useId } from 'react';
import { ChevronRight, Film, MapPin, Printer, Route, Search, TrendingUp } from 'lucide-react';

export type MapTaskAction = 'classification' | 'change' | 'research' | 'spatial' | 'animation' | 'delivery';

export type MapTaskLauncherProps = {
  selectedFeatures: GeoJSON.Feature[];
  workspaceReady: boolean;
  onChoose: (action: MapTaskAction) => void;
};

const TASKS = [
  { action: 'classification', title: '寻找类似地物', description: '标注目标与非目标样本，在小范围内试算，再检查识别结果。', Icon: Search },
  { action: 'change', title: '研究变化', description: '查看植被状况，或比较两个时段；先核对影像和有效观测。', Icon: TrendingUp },
  { action: 'research', title: '区域调研', description: '带着地图范围和问题，让 Agent 整理资料、数据与分析计划。', Icon: MapPin },
  { action: 'spatial', title: '选址与空间分析', description: '筛选候选地块、设置避让条件；复杂路线与设施方案交给 Agent 规划。', Icon: Route },
  { action: 'animation', title: '制作动画', description: '选择区域与日期，用真实时序影像准备变化动画。', Icon: Film },
  { action: 'delivery', title: '制图交付', description: '选择已完成的分析版本，准备地图、空间数据与交付说明。', Icon: Printer },
] as const;

export function MapTaskLauncher({ selectedFeatures, workspaceReady, onChoose }: MapTaskLauncherProps) {
  const launcherId = useId();
  const regionCount = selectedFeatures.filter(feature => feature.geometry?.type === 'Polygon' || feature.geometry?.type === 'MultiPolygon').length;
  const selectionHint = selectedFeatures.length === 0
    ? '还没有地图选择。可以先选任务，再绘制范围或选择已有图层。'
    : regionCount > 0
      ? `已选 ${selectedFeatures.length} 个地图要素，其中 ${regionCount} 个面可用作范围；进入任务后再确认用途。`
      : `已选 ${selectedFeatures.length} 个地图要素。区域任务还需要绘制或选择一个面。`;

  return <section className="earth-task-launcher" aria-label="地图任务">
    <header className="earth-task-launcher__header">
      <h2>用这片地图做什么</h2>
      <p>从要解决的问题开始，逐步确认范围、材料和成果。</p>
    </header>
    <p className="earth-task-launcher__context" data-has-selection={selectedFeatures.length > 0}>
      {selectionHint}{!workspaceReady ? ' 请先关联工作区，以保存输入、分析版本和成果。' : ''}
    </p>
    <ul className="earth-task-launcher__list">
      {TASKS.map(({ action, title, description, Icon }) => <li key={action}>
        <button type="button" className="earth-task-launcher__action" aria-label={title} aria-describedby={`${launcherId}-${action}`} onClick={() => onChoose(action)}>
          <Icon className="earth-task-launcher__icon" size={19} aria-hidden="true" />
          <span className="earth-task-launcher__copy">
            <strong className="earth-task-launcher__title">{title}</strong>
            <span id={`${launcherId}-${action}`} className="earth-task-launcher__description">{description}</span>
          </span>
          <ChevronRight className="earth-task-launcher__chevron" size={16} aria-hidden="true" />
        </button>
      </li>)}
    </ul>
    <p className="earth-task-launcher__boundary">路线、设施容量与服务覆盖需先确认路网、需求和约束数据。当前入口可交给 Agent 制定方案，不代表这些计算已经完成。</p>
  </section>;
}
