import { useId, useRef, type CSSProperties, type PointerEvent } from 'react';
import { optimizationObjectives, type OptimizationPreference } from './optimization-parameters';
import { adjustWeight, pointWeights, preferenceWeights, triangleVertices, weightAxes, weightObjective, weightPoint, weightPresets, type OptimizationWeights } from './optimization-weights';

const labels = { quality: '效果', cost: '成本', latency: '速度' };
const descriptions = { quality: '提高正确率与证据完整性', cost: '降低每个任务的成本', latency: '缩短完整任务的耗时' };

export function OptimizationObjectivePicker({ preference, onChange }: { preference: OptimizationPreference; onChange: (value: OptimizationPreference) => void }) {
  const hintId = useId();
  const pointer = useRef<number | null>(null);
  const weights = preferenceWeights(preference.objective, preference.weights);
  const point = weightPoint(weights);
  const objective = optimizationObjectives.find((item) => item.id === preference.objective)!;
  const update = (next: OptimizationWeights) => onChange({ ...preference, objective: weightObjective(next), weights: next });
  const move = (event: PointerEvent<SVGSVGElement>) => {
    const matrix = event.currentTarget.getScreenCTM();
    if (!matrix) return;
    const position = new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse());
    update(pointWeights(position.x, position.y));
  };
  return <section className="lab-objectives" aria-label="优化目标与倾向">
    <div className="lab-objectives-options" role="group" aria-label="优化倾向"><span>优化倾向</span>{optimizationObjectives.map((item) => <button key={item.id} aria-pressed={weightAxes.every((axis) => weights[axis] === weightPresets[item.id][axis])} onClick={() => onChange({ ...preference, objective: item.id, weights: { ...weightPresets[item.id] } })} title={item.measure}>{item.title}</button>)}</div>
    <div className="lab-objective-mixer">
      <div className="lab-objective-pad">
        <svg viewBox="0 0 320 258" aria-hidden="true"
          onPointerDown={(event) => { if (!event.isPrimary || event.button !== 0) return; pointer.current = event.pointerId; event.currentTarget.setPointerCapture(event.pointerId); move(event); }}
          onPointerMove={(event) => { if (pointer.current === event.pointerId) move(event); }}
          onPointerUp={(event) => { if (pointer.current === event.pointerId) { move(event); pointer.current = null; event.currentTarget.releasePointerCapture(event.pointerId); } }}
          onPointerCancel={() => { pointer.current = null; }} onLostPointerCapture={() => { pointer.current = null; }}>
          <path className="lab-objective-pad__area" d="M160 38 42 224H278Z" />
          {[.25, .5, .75].map((t) => <g className="lab-objective-pad__grid" key={t}>
            <path d={`M${42 + 118 * t} ${224 - 186 * t}H${278 - 118 * t}`} />
            <path d={`M${160 + 118 * t} ${38 + 186 * t}L${42 + 236 * t} 224`} />
            <path d={`M${160 - 118 * t} ${38 + 186 * t}L${278 - 236 * t} 224`} />
          </g>)}
          {triangleVertices.map((vertex, i) => <line key={i} className="lab-objective-pad__guide" x1={point.x} y1={point.y} x2={vertex.x} y2={vertex.y} />)}
          <text x="160" y="19" textAnchor="middle">效果</text><text x="42" y="249" textAnchor="middle">成本</text><text x="278" y="249" textAnchor="middle">速度</text>
          <circle className="lab-objective-pad__hit" cx={point.x} cy={point.y} r="22" />
          <circle className="lab-objective-pad__point" cx={point.x} cy={point.y} r="8" />
        </svg>
        <p id={hintId}>拖动圆点，或用滑条微调</p>
      </div>
      <div className="lab-objective-sliders" role="group" aria-label="优化倾向权重" aria-describedby={hintId}>
        {weightAxes.map((axis) => <label className="lab-objective-slider" key={axis}><span><strong>{labels[axis]}</strong><output>{weights[axis]}<small>%</small></output></span><input type="range" min="0" max="100" step="1" style={{ '--weight': `${weights[axis]}%` } as CSSProperties} value={weights[axis]} aria-label={`${labels[axis]}权重`} aria-valuetext={`${weights[axis]}%，${descriptions[axis]}`} onChange={(event) => update(adjustWeight(weights, axis, Number(event.target.value)))} /><small>{descriptions[axis]}</small></label>)}
        <p>总计 100% · 权重代表下一轮的优化偏好，质量底线仍须满足。</p>
      </div>
    </div>
    <div className="lab-objectives-contract"><div className="lab-objectives-priority" aria-label={`${objective.title}的取舍`}><span>目标</span><strong>{preference.target.trim() || objective.target}</strong><span>约束</span><strong>{preference.guardrail.trim() || objective.guardrail}</strong></div><details><summary>设置本轮目标与约束</summary><p>{objective.tradeoff}。仅用于下一轮草稿，不改写历史判定。</p><label>本轮目标<input value={preference.target} onChange={(event) => onChange({ ...preference, target: event.target.value })} placeholder={objective.target} /></label><label>必须保持<input value={preference.guardrail} onChange={(event) => onChange({ ...preference, guardrail: event.target.value })} placeholder={objective.guardrail} /></label></details></div>
  </section>;
}
