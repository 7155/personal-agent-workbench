import { useEffect, useRef, useState } from 'react';
import { LoaderCircle, RefreshCw } from 'lucide-react';
import { useControlTransport } from '@/app/control-transport';
import type { SpaceFacts } from './continuity-model';

type Judgment = { choice: string; label: string; confidence: number; abstained: boolean };
type Analysis = { spaceKey: string; revision: string; model: string; observedAtMs: number;
  answers: Record<string, Judgment>; tasks: { id: string; text: string; judgment: Judgment }[]; boundary: string };
const label = (value?: Judgment) => !value || value.abstained ? '待核实' : value.label;

export function JevProgress({ facts }: { facts: SpaceFacts }) {
  const transport = useControlTransport();
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const epoch = useRef(0);
  useEffect(() => { epoch.current++; setAnalysis(null); setError(''); setBusy(false); return () => { epoch.current++; }; }, [facts.key, facts.revision, transport]);
  const analyze = async () => {
    const requestEpoch = ++epoch.current; setBusy(true); setError('');
    try {
      const result = await transport.request<{ ok: boolean; analysis: Analysis }>({ pathId: 'agent.continuity.analyze',
        body: { spaceKey: facts.key, expectedRevision: facts.revision }, timeoutMs: 25_000 });
      if (epoch.current !== requestEpoch) return;
      if (!result.ok || result.analysis?.revision !== facts.revision || result.analysis.spaceKey !== facts.key) throw new Error('分析依据已变化，请刷新后重试。');
      setAnalysis(result.analysis);
    } catch (reason) { if (epoch.current === requestEpoch) setError(reason instanceof Error ? reason.message : '进度分析暂时不可用。'); }
    finally { if (epoch.current === requestEpoch) setBusy(false); }
  };
  return <section className="jev-progress" aria-label="Jev 工作进度" aria-busy={busy}>
    <div className="jev-progress-heading"><strong>工作进度</strong><button type="button" disabled={busy} onClick={() => void analyze()}>{busy ? <LoaderCircle size={14} className="jev-progress-spinner" /> : <RefreshCw size={14} />}{busy ? 'Jev 正在分析' : analysis ? '更新 Jev 进度' : '用 Jev 梳理进度'}</button></div>
    {error ? <p role="alert">{error}</p> : null}
    {analysis ? <div className="jev-progress-result" key={analysis.observedAtMs} aria-live="polite"><p><strong>{label(analysis.answers.stage)}</strong><span> · {label(analysis.answers.attention)}</span></p>
      <ul>{analysis.tasks.map(task => <li key={task.id}><span>{task.text}</span><small>{label(task.judgment)}</small></li>)}</ul>
      <details><summary>分析依据</summary><p>{analysis.boundary}</p><small>{analysis.model} · {new Date(analysis.observedAtMs).toLocaleString()} · 本次空间版本 {analysis.revision.slice(0, 10)}</small></details>
    </div> : <p className="continuity-scope">按当前目标、最近对话和成果判断阶段与待办；点击后将这些材料发送给 Jev。</p>}
  </section>;
}
