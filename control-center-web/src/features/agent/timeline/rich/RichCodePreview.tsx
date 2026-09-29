import { useMemo, useState } from 'react';
import { RichDiagram } from './RichDiagram';
import { RichMath } from './RichMath';
import { RichTableFrame } from './RichBlockTools';
import { parseDelimited, type FencedPreviewKind } from './rich-data';

export function RichCodePreview({ kind, source }: { kind: FencedPreviewKind; source: string }) {
  if (kind === 'math') return <RichMath source={source} />;
  if (kind === 'mermaid' || kind === 'svg') return <RichDiagram source={source} svg={kind === 'svg'} />;
  if (kind === 'json') return <JsonPreview source={source} />;
  return <DelimitedPreview source={source} delimiter={kind === 'tsv' ? '\t' : ','} />;
}

function JsonPreview({ source }: { source: string }) {
  const parsed = useMemo(() => {
    if (source.length > 200_000) return { error: 'JSON 较大，请在源码中阅读或保存后打开。', value: null };
    try { return { error: '', value: JSON.parse(source) as unknown }; }
    catch { return { error: 'JSON 尚未闭合或语法无效，原文仍在源码标签中。', value: null }; }
  }, [source]);
  return parsed.error ? <p className="paw-rich-placeholder" role="status">{parsed.error}</p>
    : <div className="paw-rich-json" role="region" aria-label="JSON 结构" tabIndex={0}><JsonNode value={parsed.value} depth={0} /></div>;
}

function JsonNode({ value, label, depth }: { value: unknown; label?: string; depth: number }) {
  const [limit, setLimit] = useState(30);
  if (value === null || typeof value !== 'object') return <div className="paw-rich-json__value">
    {label !== undefined ? <span className="paw-rich-json__key">{label}: </span> : null}
    <span data-type={value === null ? 'null' : typeof value}>{JSON.stringify(value)}</span>
  </div>;
  const entries = Object.entries(value);
  if (depth >= 8) return <div className="paw-rich-json__value">{label}: <em>嵌套较深，请在源码中继续阅读。</em></div>;
  return <details className="paw-rich-json__node" open={depth === 0 ? true : undefined}>
    <summary>{label ? <span className="paw-rich-json__key">{label} </span> : null}
      <span>{Array.isArray(value) ? '[ ]' : '{ }'}</span><small>{entries.length} 项</small>
    </summary>
    <div className="paw-rich-json__children">{entries.slice(0, limit).map(([key, item]) => <JsonNode key={key} label={key} value={item} depth={depth + 1} />)}
      {limit < entries.length ? <button className="paw-rich-text-action" type="button" onClick={() => setLimit(n => n + 30)}>继续显示 · {limit}/{entries.length}</button> : null}
    </div>
  </details>;
}

function DelimitedPreview({ source, delimiter }: { source: string; delimiter: string }) {
  const { rows, error } = useMemo(() => parseDelimited(source, delimiter), [source, delimiter]);
  const [limit, setLimit] = useState(50);
  const [chart, setChart] = useState(false);
  if (error || !rows.length) return <p className="paw-rich-placeholder" role="status">{error || '没有可展示的数据。'}</p>;
  const header = rows[0]!; const records = rows.slice(1);
  const columnCount = Math.max(header.length, ...records.map(row => row.length));
  const columns = Array.from({ length: columnCount }, (_, i) => header[i] || `列 ${i + 1}`);
  const points = records.slice(0, 24).map(row => ({ label: row[0] ?? '', value: Number(row[1]) }));
  const chartable = columns.length === 2 && points.length > 0 && points.every((p, i) => records[i]?.[1]?.trim() && Number.isFinite(p.value) && p.value >= 0);
  return <div className="paw-rich-data">
    {chartable ? <div className="paw-rich-data__view" role="group" aria-label="数据视图">
      <button type="button" aria-pressed={!chart} onClick={() => setChart(false)}>表格</button><button type="button" aria-pressed={chart} onClick={() => setChart(true)}>图表</button>
    </div> : null}
    {chart && chartable ? <SimpleBarChart points={points} label={columns[1]!} /> : <>
      <RichTableFrame label="数据预览" hint={`显示 ${Math.min(limit, records.length)}/${records.length} 行 · ${columns.length} 列`}>
        <table><thead><tr>{columns.map((column, i) => <th key={i} scope="col">{column || `列 ${i + 1}`}</th>)}</tr></thead>
          <tbody>{records.slice(0, limit).map((row, i) => <tr key={i}>{Array.from({ length: Math.max(columns.length, row.length) }, (_, j) => <td key={j}>{row[j] ?? ''}</td>)}</tr>)}</tbody>
        </table>
      </RichTableFrame>
      {records.length > limit ? <button type="button" className="paw-rich-text-action" onClick={() => setLimit(n => n + 50)}>继续显示数据 · {limit}/{records.length}</button> : null}
    </>}
  </div>;
}

function SimpleBarChart({ points, label }: { points: { label: string; value: number }[]; label: string }) {
  const max = Math.max(...points.map(p => p.value), 1);
  return <figure className="paw-rich-chart" aria-label={`${label}柱形图`}>
    <figcaption>{label}<small>按原顺序展示前 {points.length} 行 · 横轴从 0 开始</small></figcaption>
    <div className="paw-rich-chart__bars">{points.map((point, i) => <div className="paw-rich-chart__row" key={i}>
      <span title={point.label}>{point.label}</span><div><i style={{ width: `${point.value / max * 100}%` }} /></div><strong>{point.value.toLocaleString('zh-CN')}</strong>
    </div>)}</div>
    <span className="paw-rich-chart__axis">0<span>{max.toLocaleString('zh-CN')}</span></span>
  </figure>;
}
