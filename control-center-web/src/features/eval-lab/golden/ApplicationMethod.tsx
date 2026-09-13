import { useId } from 'react';
import { Field, Input, TextArea } from '@/components/primitives';
import type { ModelConfig } from './types';
import type { ApplicationMethodComparison } from './application-method';
import './application-method.css';

export function ApplicationMethodEditor({ label, value, onChange, disabled }: { label: string; value: ModelConfig; onChange: (value: ModelConfig) => void; disabled: boolean }) {
  const id = useId(); const method = value.applicationMethod;
  const inline = method && 'body' in method ? method : undefined;
  const reference = method && 'artifactId' in method ? method : undefined;
  return <div className="golden-application-method"><label className="golden-check"><input type="checkbox" checked={Boolean(method)} disabled={disabled} onChange={(event) => {
    const { applicationMethod: _method, ...model } = value; onChange(event.target.checked ? { ...model, applicationMethod: { body: '', title: `${label}应用方法` } } : model);
  }} />{label}使用应用 Skill 方法</label>{method ? <><p className="golden-note">只对照实际应用方法正文；不启用或评测 Pi 的原生 Skill 路由。</p>{inline ? <><Field label={`${label}方法名称`} htmlFor={`${id}-title`}><Input id={`${id}-title`} value={inline.title ?? ''} disabled={disabled} onChange={(event) => onChange({ ...value, applicationMethod: { body: inline.body, title: event.target.value } })} /></Field><Field label={`${label}应用方法正文`} htmlFor={`${id}-body`}><TextArea id={`${id}-body`} value={inline.body} rows={7} disabled={disabled} placeholder="写明阅读、验证、引用和报告的方法。执行时冻结此正文。" onChange={(event) => onChange({ ...value, applicationMethod: { body: event.target.value, title: inline.title } })} /></Field></> : <p className="golden-note">方法取自项目成果 {reference?.artifactId} · v{reference?.artifactRevision}</p>}</> : <p className="golden-note">本方案没有附加应用方法正文。</p>}</div>;
}

export function ApplicationMethodDiff({ comparison }: { comparison: ApplicationMethodComparison }) {
  return <section className="golden-method-diff" aria-label="应用 Skill 方法对照"><header><h4>应用 Skill 方法</h4><span>{comparison.changed ? '方法正文有变化' : '方法正文相同'}</span></header><div className="golden-method-diff__identities">{(['baseline', 'candidate'] as const).map((role) => { const method = comparison[role]; return <div key={role}><small>{role === 'baseline' ? '基线' : '候选'}</small><strong>{method?.title || '未附加应用方法'}</strong>{method ? <><span title={method.sha256}>SHA {method.sha256.slice(0, 12)}</span><span>{method.source.kind === 'project_artifact' ? `项目成果 · v${method.source.artifactRevision}` : '本轮填写并冻结'}</span></> : null}</div>; })}</div><p>比较的是随本次回答使用的方法正文，未评测 Pi Skill 自动路由。</p>{comparison.diff ? <details open><summary>查看精确正文差异</summary><pre>{comparison.diff.split('\n').map((line, index) => <span key={index} data-change={line.startsWith('+') ? 'added' : line.startsWith('-') ? 'removed' : undefined}>{line}{'\n'}</span>)}</pre></details> : null}</section>;
}
