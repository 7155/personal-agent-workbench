import { useEffect, useState } from 'react';
import { Check, Circle, Cloud, LoaderCircle, TriangleAlert } from 'lucide-react';
export type CloudProgress = {
  planId: string; submittedAt: number; status: 'submitted' | 'running' | 'completed' | 'failed' | 'cancelled' | 'unconfirmed';
  runId?: string; finishedAt?: number; statisticsSaved?: boolean; mapReady?: boolean; reportSaved?: boolean; error?: string;
};
export function cloudProgressFromRun(run: Record<string, any>, previous: CloudProgress, sourceHash: string): CloudProgress | null {
  if (run.sourceHash !== sourceHash || !(Date.parse(run.startedAt) >= previous.submittedAt) || typeof run.runId !== 'string') return null;
  if (previous.runId && previous.runId !== run.runId) return null;
  const terminal = ['completed','failed','cancelled'].includes(run.status);
  const artifacts = Array.isArray(run.artifacts) ? run.artifacts : [];
  return {...previous,runId:run.runId,status:terminal ? run.status : 'running',finishedAt:terminal ? Date.parse(run.updatedAt) || Date.now() : undefined,
    statisticsSaved:artifacts.some(item=>/\.(?:json|csv)$/i.test(item.name || item.path || '')),reportSaved:artifacts.some(item=>/\.html?$/i.test(item.name || item.path || '')),mapReady:Array.isArray(run.layers)&&run.layers.some((item:any)=>item.status==='ready'),error:run.error || undefined};
}
export function CloudRunProgress({progress}:{progress:CloudProgress}) {
  const [now,setNow]=useState(Date.now());
  const running=['submitted','running'].includes(progress.status);
  useEffect(()=>{if(!running)return;const timer=setInterval(()=>setNow(Date.now()),1000);return()=>clearInterval(timer);},[running]);
  const seconds=Math.max(0,Math.floor(((progress.finishedAt ?? now)-progress.submittedAt)/1000));
  const title=progress.status==='unconfirmed'?'暂未收到运行回执':progress.status==='completed'?'云端分析已完成':progress.status==='failed'?'云端分析未完成':progress.status==='cancelled'?'云端分析已取消':progress.reportSaved?'正在核验成果':progress.statisticsSaved?'正在生成地图与报告':progress.status==='running'?'GEE 正在计算':'正在提交云端任务';
  return <section className="earth-cloud-progress" aria-label="云端计算进度" data-status={progress.status}>
    <div className="earth-cloud-progress__heading"><Cloud size={18} aria-hidden/><strong aria-live="polite">{title}</strong><time>{seconds>=60?`${Math.floor(seconds/60)} 分 `:''}{seconds%60} 秒</time></div>
    <ul>{([['statisticsSaved','统计结果'],['mapReady','地图图层'],['reportSaved','HTML 报告']] as const).map(([key,label])=><li key={key} data-done={Boolean(progress[key])}>{progress[key]?<Check size={14} aria-hidden/>:<Circle size={12} aria-hidden/>}{label}<span>{progress[key]?'已就绪':'等待结果'}</span></li>)}</ul>
    {running?<p><LoaderCircle className="earth-cloud-progress__spinner" size={14} aria-hidden/>根据实际回执更新。GEE 未提供百分比；可继续查看地图。</p>:null}
    {progress.error?<p role="alert"><TriangleAlert size={14} aria-hidden/>{progress.error}</p>:null}
  </section>;
}
