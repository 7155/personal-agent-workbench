import {afterEach,it,expect} from 'vitest';
import {render,screen,cleanup} from '@testing-library/react';
import {CloudRunProgress,cloudProgressFromRun,type CloudProgress} from './CloudRunProgress';
afterEach(cleanup);
const base:CloudProgress={planId:'p',submittedAt:Date.parse('2026-09-22T00:00:00Z'),status:'submitted'};
it('shows only artifacts actually returned by the matching execution',()=>{
 const run={sourceHash:'hash',runId:'r',startedAt:'2026-09-22T00:00:01Z',status:'running',artifacts:[{name:'statistics.json'}],layers:[]};
 expect(cloudProgressFromRun({...run,sourceHash:'wrong'},base,'hash')).toBeNull();
 expect(cloudProgressFromRun({...run,startedAt:'2026-09-21'},base,'hash')).toBeNull();
 expect(cloudProgressFromRun(run,{...base,runId:'other'},'hash')).toBeNull();
 const progress=cloudProgressFromRun(run,base,'hash')!;render(<CloudRunProgress progress={progress}/>);
 expect(screen.getByText('正在生成地图与报告')).toBeVisible();expect(screen.getAllByText('已就绪')).toHaveLength(1);expect(screen.getAllByText('等待结果')).toHaveLength(2);
 expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
});
it('completed execution without a report does not pretend that report exists',()=>{
 const p=cloudProgressFromRun({sourceHash:'hash',runId:'r',startedAt:'2026-09-22T00:00:01Z',updatedAt:'2026-09-22T00:01:05Z',status:'completed',artifacts:[],layers:[{status:'ready'}]},base,'hash')!;
 render(<CloudRunProgress progress={p}/>);expect(screen.getByText('云端分析已完成')).toBeVisible();expect(screen.getByText('1 分 5 秒')).toBeVisible();expect(screen.getAllByText('已就绪')).toHaveLength(1);
});
