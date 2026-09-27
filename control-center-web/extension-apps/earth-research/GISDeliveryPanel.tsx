import { useRef, useState } from 'react';
import type { LocalGISRunSummary } from './EarthDataDock';
export type MapOptions = { title?: string; paperSize?: 'A4' | 'A3' | 'Letter'; orientation?: 'landscape' | 'portrait'; legend?: boolean; scaleBar?: boolean; northArrow?: boolean };
export type GISDeliveryReceipt = { path: string; version: number; runId: string; verifiedFiles: number };
export function GISDeliveryPanel({ runs, reports = [], incomplete = false, onGenerate, onOpenReport }: {
  runs: LocalGISRunSummary[]; reports?: Array<{ path: string; name: string }>; incomplete?: boolean;
  onGenerate: (runId: string, mapOptions?: MapOptions) => Promise<GISDeliveryReceipt>;
  onOpenReport: (path: string) => Promise<void>;
}) {
  const completed = runs.filter(run => run.status === 'completed');
  const [runId, setRunId] = useState(''), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [receipt, setReceipt] = useState<GISDeliveryReceipt>();
  const lock = useRef(false);
  const [mapOptions, setMapOptions] = useState<MapOptions>({ paperSize: 'A4', orientation: 'landscape', legend: true, scaleBar: true, northArrow: true });
  const selected = completed.find(run => run.runId === runId);
  async function generate() {
    if (!selected || lock.current) return;
    lock.current = true; setBusy(true); setError('');
    try {
      const result = await onGenerate(selected.runId, mapOptions);
      if (result.runId !== selected.runId) throw new Error('报告回执与所选分析不一致，请刷新结果后核对。');
      setReceipt(result);
      await onOpenReport(`${result.path}/report.html`);
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { lock.current = false; setBusy(false); }
  }
  async function open(path: string) {
    setError('');
    try { await onOpenReport(path); } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
  }
  return <section className="earth-delivery-panel" aria-label="制图交付">
    <div><h2>报告与成果</h2><p>在 PAW 内查看分析地图、统计与方法。报告以独立窗口打开，可最大化阅读。</p></div>
    <section className="earth-report-library" aria-label="项目报告"><h3>已保存的报告</h3>
      {reports.length ? reports.map(report => <button type="button" key={report.path} onClick={() => void open(report.path)}><strong>{report.name}</strong><span>{report.path.includes('/runs/') ? '云端分析成果' : report.path.includes('/deliverables/') ? '本地 GIS 成果' : '项目报告'}</span></button>) : <p>当前目录中尚未发现 HTML 报告。Agent 生成的网页报告也会显示在这里。</p>}
      {incomplete ? <p>目录尚未完整读取；这里仅列出已发现的文件。</p> : null}
    </section>
    {error ? <p role="alert">{error}</p> : null}
    <details className="earth-report-create" open={!reports.length}><summary>从本地分析生成新报告</summary>
    <label>使用哪次结果<select aria-label="交付分析结果" value={runId} disabled={busy} onChange={event => { setRunId(event.target.value); setReceipt(undefined); setError(''); }}>
      <option value="">选择已完成的分析</option>{completed.map(run => <option key={run.runId} value={run.runId}>{run.op === 'site-selection' ? `地块避让 ${run.params?.distance ?? ''} 米` : run.op} · {new Date(run.updatedAt).toLocaleString()} · {run.runId.slice(0, 8)}</option>)}
    </select></label>
    {!completed.length ? <p>尚无已完成的本地分析。可以先分析，或打开上方已保存的报告。</p> : null}
    <label>报告标题<input aria-label="地图标题" maxLength={160} disabled={busy} placeholder="留空则使用分析名称" value={mapOptions.title || ''} onChange={event => setMapOptions({ ...mapOptions, title: event.target.value })} /></label>
    {selected ? <details><summary>核对分析参数</summary><pre>{JSON.stringify({ runId: selected.runId, parameters: selected.params }, null, 2)}</pre></details> : null}
    <details><summary>地图版式与配套文件</summary><fieldset disabled={busy}><legend>地图版式</legend>
      <label>纸张<select aria-label="纸张" value={mapOptions.paperSize} onChange={event => setMapOptions({ ...mapOptions, paperSize: event.target.value as MapOptions['paperSize'] })}><option>A4</option><option>A3</option><option>Letter</option></select></label>
      <label>方向<select aria-label="方向" value={mapOptions.orientation} onChange={event => setMapOptions({ ...mapOptions, orientation: event.target.value as MapOptions['orientation'] })}><option value="landscape">横向</option><option value="portrait">纵向</option></select></label>
      {([['legend', '图例'], ['scaleBar', '比例尺'], ['northArrow', '指北针']] as const).map(([key, label]) => <label className="earth-report-option" key={key}><input type="checkbox" checked={mapOptions[key]} onChange={event => setMapOptions({ ...mapOptions, [key]: event.target.checked })} />{label}</label>)}
    </fieldset><p>HTML 报告可单独下载，内含地图。成果目录同时保存地图 PDF/SVG/PNG、统计 CSV、原始数据与校验清单；地图 PDF 不是完整文字报告。</p></details>
    <button type="button" className="earth-report-generate" disabled={!selected || busy} onClick={() => void generate()}>{busy ? '正在生成报告并核验文件…' : '生成报告与成果包'}</button>
    {receipt ? <div className="earth-report-success" role="status"><strong>第 {receipt.version} 版已保存</strong><p>已检查 {receipt.verifiedFiles} 个文件，绑定分析 {receipt.runId.slice(0, 8)}。</p><button type="button" onClick={() => void open(`${receipt.path}/report.html`)}>打开报告</button><details><summary>成果位置</summary><code>{receipt.path}</code></details></div> : null}

    </details>
    <p>耕地面积变化需要明确时段和可核验的土地覆盖数据；地块面积或 NDVI 变化不能直接作为耕地变化结论。</p>
  </section>;
}
