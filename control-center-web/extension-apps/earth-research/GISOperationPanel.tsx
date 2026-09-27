import { useEffect, useRef, useState } from 'react';
import { CheckCircle2, LoaderCircle } from 'lucide-react';
import type { ProjectLayer } from './layer-catalog';
import type { SitingPreflight } from './pi-package/gis-preflight.mjs';

export type SitingPlanInput = { parcels: string; avoidance: string; distance: number; commandId: string;
  expectedInputs?: Array<{ role: string; path: string; sha256: string; files: Array<{name: string; sha256: string}> }> };
type Props = {
  layers: ProjectLayer[]; onRun: (plan: SitingPlanInput) => Promise<void>;
  onCheck?: (plan: Pick<SitingPlanInput, 'parcels' | 'avoidance' | 'distance'>) => Promise<SitingPreflight>;
  onPlan?: (question: string) => Promise<void>;
  onShowResult?: () => void;
  onDelivery?: () => void;
};
export function GISOperationPanel({ layers, onRun, onCheck, onPlan, onShowResult, onDelivery }: Props) {
  const [question, setQuestion] = useState('');
  const polygons = layers.filter(layer => !layer.loadError && layer.geometryTypes.some(type => type === 'Polygon' || type === 'MultiPolygon'));
  const [parcels, setParcels] = useState(''), [avoidance, setAvoidance] = useState(''), [distance, setDistance] = useState('200');
  const [busy, setBusy] = useState<'checking' | 'running' | 'planning' | null>(null);
  const [error, setError] = useState(''), [completed, setCompleted] = useState(false);
  const [check, setCheck] = useState<{ fingerprint: string; receipt: SitingPreflight }>();
  const command = useRef<{ fingerprint: string; id: string } | undefined>(undefined);
  const active = useRef(true), submitting = useRef(false);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  useEffect(() => { if (!parcels && polygons.length === 1) setParcels(polygons[0].path); }, [parcels, polygons]);
  const parcel = polygons.find(layer => layer.path === parcels), exclusion = layers.find(layer => layer.path === avoidance && !layer.loadError);
  const fingerprint = JSON.stringify([parcels, parcel?.revision, avoidance, exclusion?.revision, distance]);
  const valid = Boolean(parcel && exclusion && parcel.id !== exclusion.id && distance.trim() && Number.isFinite(Number(distance)) && Number(distance) > 0 && Number(distance) <= 100000);
  const checked = check?.fingerprint === fingerprint ? check.receipt : undefined;
  const ready = valid && (!onCheck || checked?.ready);
  const changed = () => { setCompleted(false); setError(''); setCheck(undefined); command.current = undefined; };
  async function act(kind: 'checking' | 'running' | 'planning', action: () => Promise<void>) {
    if (submitting.current) return;
    submitting.current = true; setBusy(kind); setError('');
    try { await action(); } catch (reason) { if (active.current) setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { submitting.current = false; if (active.current) setBusy(null); }
  }
  return <section className="earth-sources earth-spatial-workflow earth-analysis-form" aria-label="选址方案参数" aria-busy={Boolean(busy)}>
    <p className="earth-inline-hint">从候选地块中，扣除指定距离内的避让区。</p>
    <fieldset disabled={Boolean(busy)}><legend>分析输入</legend>
      <label>候选地块<select aria-label="分析地块" value={parcels} onChange={event => { changed(); setParcels(event.target.value); }}><option value="">选择面图层</option>{polygons.map(layer => <option key={layer.id} value={layer.path}>{layer.name}</option>)}</select></label>
      {!polygons.length ? <p className="earth-inline-hint">先从数据库加载地块，或在地图绘制区域并保存为项目图层。</p> : null}
      <label>避让图层<select aria-label="分析避让图层" value={avoidance} onChange={event => { changed(); setAvoidance(event.target.value); }}><option value="">选择河流、设施或限制区</option>{layers.filter(layer => layer.path !== parcels && !layer.loadError).map(layer => <option key={layer.id} value={layer.path}>{layer.name}</option>)}</select></label>
      <label>避让距离（米）<input aria-label="避让距离" type="number" min="0.01" max="100000" step="any" value={distance} onChange={event => { changed(); setDistance(event.target.value); }} /></label>
    </fieldset>
    {checked ? <section className="earth-preflight earth-preflight--quiet" aria-label="输入检查结果" data-ready={checked.ready}>
      <p role="status">{checked.ready ? <CheckCircle2 size={15} aria-hidden="true" /> : null}{checked.ready ? '输入检查通过，可以生成方案' : '请先处理以下问题'}</p>
      {checked.issues.map((issue, index) => issue.level === 'error'
        ? <p role="alert" key={`${issue.code}-${index}`}>{issue.message}</p>
        : <p key={`${issue.code}-${index}`}>{issue.message}</p>)}
      <details><summary>查看检查详情</summary>
        {checked.inputs.map(input => <p key={input.role}><strong>{input.role === 'parcels' ? '候选地块' : '避让图层'}</strong><span>{input.rows ?? '未知'} 个要素 · {input.crs || '坐标系未知'}</span></p>)}
        <small>使用米制投影计算；执行前再次核对输入版本，数据变化时需重新检查。</small>
      </details>
    </section> : null}
    <div className="earth-analysis-form__actions">
      {completed ? <>
        <p className="earth-form-success" role="status">分析已完成，结果已保存。</p>
        {onShowResult ? <button type="button" className="earth-primary-action" onClick={onShowResult}>查看地图</button> : null}
        {onDelivery ? <button type="button" onClick={onDelivery}>导出成果</button> : null}
        <button type="button" className="earth-secondary-action" onClick={changed}>新建另一方案</button>
      </> : onCheck && !checked?.ready ? <button type="button" className="earth-primary-action" disabled={Boolean(busy) || !valid} onClick={() => void act('checking', async () => {
        const receipt = await onCheck({ parcels, avoidance, distance: Number(distance) });
        if (active.current) { setCheck({ fingerprint, receipt }); setCompleted(false); command.current = undefined; }
      })}>{busy === 'checking' ? <LoaderCircle size={15} className="earth-spin" aria-hidden="true" /> : null}{busy === 'checking' ? '正在检查输入…' : checked ? '重新检查输入' : '检查输入'}</button> : <>
        <button type="button" className="earth-primary-action" disabled={Boolean(busy) || !ready} onClick={() => void act('running', async () => {
          if (command.current?.fingerprint !== fingerprint) command.current = { fingerprint, id: crypto.randomUUID() };
          await onRun({ parcels, avoidance, distance: Number(distance), commandId: command.current.id,
            expectedInputs: checked?.inputs.map(({ role, path, sha256, files }) => ({ role, path, sha256, files })) });
          if (active.current) setCompleted(true);
        })}>{busy === 'running' ? <LoaderCircle size={15} className="earth-spin" aria-hidden="true" /> : null}{busy === 'running' ? '正在分析…' : error && command.current ? '核对并重试' : '生成方案'}</button>
        {onCheck && !(error && command.current) ? <button type="button" className="earth-secondary-action" disabled={Boolean(busy)} onClick={changed}>重新检查输入</button> : null}
      </>}
    </div>
    {error ? <p className="earth-form-error" role="alert">{error}</p> : null}
    <p className="earth-inline-hint">仅计算避让距离，坡度、权属及其他建设条件未判断。</p>
    <details className="earth-method-boundary"><summary>计算方法与适用范围</summary><p>只检查填写的几何避让距离，并保存实际输入版本。最小连续面积、坡度、权属、道路通行和设施容量未纳入本流程，不能视为已满足。</p></details>
    {onPlan ? <details className="earth-method-boundary"><summary>添加其他条件</summary><p>让 Agent 先核对目标、数据与缺失条件，再安排工具执行。</p><label>你想比较什么<textarea aria-label="空间分析问题" placeholder="例如：还需要比较接入道路的距离" value={question} disabled={Boolean(busy)} onChange={event => setQuestion(event.target.value)} /></label><button type="button" disabled={Boolean(busy) || !question.trim()} onClick={() => void act('planning', () => onPlan(question.trim()))}>交给 Agent 整理方案</button></details> : null}
  </section>;
}
