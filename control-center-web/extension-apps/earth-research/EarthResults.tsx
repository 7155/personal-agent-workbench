import { geoJsonOutputs, runStatus, type EarthRun } from './workspace';
import { useState } from 'react';
import { toKml } from './export';
export function EarthResults({ run }: { run: EarthRun | null }) {
  const [error, setError] = useState('');
  if (!run) return <p className="earth-empty">尚无运行记录。Agent 的工具调用会显示在左侧。</p>;
  const outputs = geoJsonOutputs(run);
  function download() {
    if (!run) return;
    const blob = new Blob([JSON.stringify({ ...run, exportedAt: new Date().toISOString() }, null, 2)], { type: 'application/json' });
    save(blob, `earth-run-${run.runId}.json`);
  }
  return <section className="earth-results" aria-label="运行控制台">
    <header><strong>{runStatus[run.status]}</strong><small>{run.runId.slice(0, 8)} · {run.project}</small><button onClick={download}>下载运行记录</button></header>
    {run.error ? <pre role="alert">{run.error}</pre> : null}
    {run.console.map((row, index) => <section className="earth-output" key={index}><h3>{typeof row.values[0] === 'string' ? row.values[0] : `输出 ${index + 1}`}{row.pending ? ' · 等待 Google 返回' : ''}</h3>{row.values.slice(typeof row.values[0] === 'string' ? 1 : 0).map((value, i) => <StructuredResult key={i} value={value} />)}<details><summary>原始输出</summary><pre>{JSON.stringify(row.values, null, 2)}</pre></details></section>)}
    {error ? <p role="alert">{error}</p> : null}
    {outputs.map((output, index) => <div className="earth-export" key={index}><span>{output.label}</span><button onClick={() => save(new Blob([JSON.stringify(output.geojson, null, 2)], { type: 'application/geo+json' }), `earth-${run.runId}-${index}.geojson`)}>GeoJSON</button><button onClick={() => { try { save(new Blob([toKml(output.geojson, output.label)], { type: 'application/vnd.google-earth.kml+xml' }), `earth-${run.runId}-${index}.kml`); setError(''); } catch (reason) { setError(String(reason)); } }}>KML</button></div>)}
  </section>;
}
function save(blob: Blob, name: string) { const url = URL.createObjectURL(blob); const a = document.createElement('a'); a.href = url; a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
const labels: Record<string, string> = { id:'编号', name:'名称', assessment:'评估', site:'地块', profile:'偏好', status:'状态', continuous_area_m2:'连续可用面积 m²', footprint_max_slope:'占地最大坡度 °', footprint_contained:'占地可放入', footprint_allowed:'占地满足栅格条件', footprint_exclusion_m2:'占地禁区相交 m²', coverage_min:'数据覆盖', length_m:'长度 m', exclusion_overlap_m2:'走廊禁区相交 m²', relative_grid_cost:'相对代价', input_origin:'输入来源', corridor_width_m:'走廊宽 m', grid_scale_m:'网格 m', electrical_capacity:'电气容量', slope_mean:'平均坡度 °', slope_max:'最大坡度 °' };
const values: Record<string, string> = { excluded:'已排除', unknown_data:'数据不足', passes_demo_constraints:'满足演示约束', site_not_eligible:'地块不满足条件', completed:'已完成', no_route:'无可行路线', invalid_endpoint:'端点不可用', same_route_as_existing:'与已有路线相同', not_evaluated:'未评估', synthetic_verification:'人工测试输入', synthetic_constraints_real_terrain:'人工约束／真实地形' };
Object.assign(labels, { forest_area_m2:'走廊内林地 m²', water_area_m2:'走廊内水域 m²' });
function display(value: unknown): string { return typeof value === 'number' ? Number.isInteger(value) ? String(value) : value.toFixed(2) : typeof value === 'boolean' ? value ? '是' : '否' : typeof value === 'string' ? values[value] ?? value : value === null ? '无数据' : JSON.stringify(value); }
function StructuredResult({ value }: { value: unknown }) {
  if (value === null || typeof value !== 'object') return <p>{display(value)}</p>;
  const record = value as Record<string, unknown>;
  const candidates = record.type === 'FeatureCollection' && Array.isArray(record.features) ? record.features.map(f => f.properties ?? {}) : Array.isArray(value) ? value : [value];
  if (!candidates.length) return <p className="earth-no-result">没有返回可用要素。</p>;
  if (!candidates.every(x => x && typeof x === 'object' && !Array.isArray(x))) return <p>{JSON.stringify(value)}</p>;
  const rows = candidates as Record<string, unknown>[];
  const columns = [...new Set(rows.flatMap(row => Object.keys(row)))].filter(key => !['geometry','type'].includes(key));
  const priority = ['name','id','site','profile','assessment','status','length_m','forest_area_m2','water_area_m2','continuous_area_m2','footprint_max_slope'];
  columns.sort((a,b) => (priority.includes(a) ? priority.indexOf(a) : 100) - (priority.includes(b) ? priority.indexOf(b) : 100));
  return <div className="earth-table-scroll"><table><thead><tr>{columns.map(key => <th key={key}>{labels[key] ?? key}</th>)}</tr></thead><tbody>{rows.slice(0, 200).map((row, i) => <tr key={i}>{columns.map(key => <td key={key}>{display(row[key])}</td>)}</tr>)}</tbody></table>{rows.length > 200 ? <small>显示前 200 行，完整内容请导出。</small> : null}</div>;
}
