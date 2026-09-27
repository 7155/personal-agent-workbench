import { useState } from 'react';
import { ArrowRight, Database, FileText, FlaskConical, Library, PackageCheck } from 'lucide-react';
import type { LabWorkflowNode } from './project-workflow-types';
import { workflowNodeStatus } from './LabWorkflowGraph';
import type { LabProject } from './types';

const stages = [
  { id: 'sources', title: '准备资料', note: '原文进入项目', detail: '先确定要研究的问题和可使用的材料。这里查看资料范围与导入记录。', kinds: ['materials', 'corpus'], icon: Library },
  { id: 'index', title: '建立索引', note: '分块与向量化', detail: '把文档转换成能检索的片段。分块、向量模型和索引版本决定后续能找到什么。', kinds: ['index'], icon: Database },
  { id: 'evaluate', title: '比较方案', note: '同题核对效果', detail: '固定问题和标准，比较检索、模型与回答方法。运行完成不等于质量通过；点开实验看前后变化与判定。', kinds: ['dataset', 'calibration', 'experiment'], icon: FlaskConical },
  { id: 'results', title: '核对成果', note: '答案与原文对照', detail: '查看保存的回答、报告和证据。成果展示讲改动过程，实际调用记录说明应用执行了什么。', kinds: ['artifact', 'job'], icon: FileText },
  { id: 'app', title: '交付应用', note: '使用冻结的方案', detail: '把选定模型、方法和知识范围保存为应用版本。先试用核对，再启用；历史版本和失败记录继续保留。', kinds: ['application'], icon: PackageCheck },
] as const;

/** Reading order, not invented execution edges. Actual dependencies remain in the workflow graph. */
export function LabProjectFlow({ project, onSelectNode, onOpenApps, onOpenGraph }: {
  project: LabProject; onSelectNode: (node: LabWorkflowNode) => void; onOpenApps: () => void; onOpenGraph: () => void;
}) {
  const [selected, setSelected] = useState<string>('');
  const nodes = project.workflow?.nodes ?? [];
  const stage = stages.find((item) => item.id === selected);
  const records = stage ? nodes.filter((node) => (stage.kinds as readonly string[]).includes(node.kind) && !node.parentId) : [];
  return <section className="lab-flow-summary" aria-label="项目逻辑">
    <header><div><h2>这个项目怎样工作</h2><p>按流程看输入、改动和结果；实际依赖关系见任务图。</p></div><button onClick={onOpenGraph}>查看任务依赖图<ArrowRight size={14} /></button></header>
    <ol>{stages.map((item, index) => {
      const Icon = item.icon;
      return <li key={item.id}><button aria-expanded={selected === item.id} onClick={() => setSelected(selected === item.id ? '' : item.id)}><Icon size={18} /><span><strong>{item.title}</strong><small>{item.note}</small></span></button>{index < stages.length - 1 ? <ArrowRight className="lab-flow-summary__arrow" size={15} aria-hidden="true" /> : null}</li>;
    })}</ol>
    {stage ? <div className="lab-flow-summary__detail" role="region" aria-label={`${stage.title}的记录`}><p>{stage.detail}</p>{records.length ? <ul>{records.map((node) => <li key={node.id}><button onClick={() => onSelectNode(node)}><span>{node.title}</span><small>{workflowNodeStatus(node)}</small><ArrowRight size={14} /></button></li>)}</ul> : <p>尚未返回这一环节的记录。</p>}{stage.id === 'app' ? <button onClick={onOpenApps}>打开应用版本与试用<ArrowRight size={14} /></button> : null}</div> : null}
  </section>;
}
