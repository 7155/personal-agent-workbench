import { useState } from 'react';
import { ArrowRight, FolderOpen, Plus, Search, SlidersHorizontal } from 'lucide-react';
import { Button } from '@/components/primitives';
import type { LabProjectSummary } from './types';
import type { ProjectPage } from './views';
import { needsProjectAttention, projectHomeProjection } from './project-home-model';
import './lab-project-home.css';

export function LabProjectHome({ items, onOpen, onCreate }: {
  items: LabProjectSummary[]; onOpen: (id: string, page?: ProjectPage, artifactId?: string) => void; onCreate: () => void;
}) {
  const [query, setQuery] = useState(''); const [filter, setFilter] = useState<'all' | 'attention' | 'active'>('all');
  const attention = items.filter(needsProjectAttention);
  const active = items.filter((item) => !needsProjectAttention(item));
  const filtered = (filter === 'attention' ? attention : filter === 'active' ? active : items)
    .filter((item) => `${item.title} ${item.latestRecord?.title ?? ''}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  return <div className="lab-home">
    <section className="lab-home__intro"><div><h2>从一个想法，到可用的应用。</h2><p>准备材料、验证方案、比较结果，再把有效的方案交付出去。</p></div><Button variant="primary" onClick={onCreate}><Plus size={16} />新建项目</Button></section>
    {items.length ? <section className="lab-home__attention" aria-labelledby="lab-home-attention"><div><SlidersHorizontal size={17} aria-hidden="true" /><h3 id="lab-home-attention">需要处理</h3><span>{attention.length}</span></div><p>{attention.length ? `${attention.length} 个项目需要补齐材料或执行条件。已有结果随时可以查看。` : '当前项目都有继续入口，可以查看最近成果或开始下一轮。'}</p><button onClick={() => setFilter(filter === 'attention' ? 'all' : 'attention')}>{filter === 'attention' ? '查看全部项目' : '查看待处理项目'}<ArrowRight size={15} /></button></section> : null}
    {items.length ? <>
    <section className="lab-home__projects" aria-label="最近的优化项目">
      <header><div className="lab-home__filters" aria-label="筛选项目">{([['all', '全部项目', items.length], ['attention', '需要处理', attention.length], ['active', '可继续', active.length]] as const).map(([key, title, count]) => <button key={key} aria-pressed={filter === key} onClick={() => setFilter(key)}>{title}<span>{count}</span></button>)}</div><label className="lab-home__search"><Search size={15} aria-hidden="true" /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索项目" aria-label="搜索 Lab 项目" /></label></header>
      <div className="lab-home__columns" aria-hidden="true"><span>项目</span><span>最近成果</span><span>状态</span><span>下一步</span></div>
      <div className="lab-home__rows">{filtered.map((item) => {
        const projection = projectHomeProjection(item);
        const page: ProjectPage = ['add_materials', 'fix_materials'].includes(projection.nextAction.kind) ? 'materials' : projection.nextAction.kind === 'review_latest' ? 'artifact' : 'lifecycle';
        return <article key={item.projectId} className="lab-home__row">
          <button className="lab-home__project" aria-label={`${item.title}，${projection.workState.label}`} onClick={() => onOpen(item.projectId)}><span className="lab-home__project-icon"><FolderOpen size={18} /></span><span><strong>{item.title}</strong><small>{item.knowledgeResources?.documentCount ? `知识库 ${item.knowledgeResources.documentCount} 篇 · ` : `${item.materialCount} 份材料 · `}{item.artifactCount} 份成果</small></span></button>
          <button className="lab-home__latest" disabled={!projection.latestRecord} onClick={() => onOpen(item.projectId, 'artifact', projection.latestRecord?.artifactId || undefined)}><span>{projection.latestRecord?.title ?? '等待首份成果'}</span><small>{projection.latestRecord?.kind === 'history' ? '历史实验已保留' : projection.latestRecord ? '查看成果与原始记录' : '从材料与目标开始'}</small></button>
          <span className="lab-home__status" data-status={projection.workState.status} title={projection.workState.reason}>{projection.workState.label}</span>
          <button className="lab-home__continue" title={projection.nextAction.reason} onClick={() => page === 'artifact' ? onOpen(item.projectId, page, projection.latestRecord?.artifactId || undefined) : onOpen(item.projectId, page)}>{projection.nextAction.label}<ArrowRight size={15} /></button>
        </article>;
      })}</div>
      {!filtered.length ? <div className="lab-home__empty"><Search size={24} /><h3>没有匹配的项目</h3><p>试试其他名称，或查看全部项目。</p><Button size="small" onClick={() => { setQuery(''); setFilter('all'); }}>清除筛选</Button></div> : null}
      <footer>{filtered.length} / {items.length} 个项目<span>实验结果和交付版本会随项目保存</span></footer>
    </section>
    </> : <section className="lab-home__first-project"><FolderOpen size={28} aria-hidden="true" /><h3>让第一个项目开始工作</h3><p>描述你要完成的任务，可以同时添加资料或项目目录。Agent 会检查已有条件，带你完成第一轮验证。</p></section>}
    <section className="lab-home__path" aria-label="项目完成路径"><div><span>01</span><h3>定义任务</h3><p>目标、资料和通过标准</p></div><ArrowRight size={17} aria-hidden="true" /><div><span>02</span><h3>验证与优化</h3><p>运行、对照与改进依据</p></div><ArrowRight size={17} aria-hidden="true" /><div><span>03</span><h3>交付应用</h3><p>试用、导出与部署</p></div></section>
  </div>;
}
