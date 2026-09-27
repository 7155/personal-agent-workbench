import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import {GISDeliveryPanel} from './GISDeliveryPanel';
afterEach(cleanup);
it('delivers the explicitly selected run and opens that immutable report',async()=>{
 const generate=vi.fn().mockResolvedValue({path:'.earth/deliverables/earth-v2',version:2,runId:'second',verifiedFiles:12}),open=vi.fn().mockResolvedValue(undefined);
 render(<GISDeliveryPanel runs={[{runId:'first',op:'site-selection',status:'completed',params:{distance:200},updatedAt:'2026-09-19T00:00:00Z'},{runId:'second',op:'site-selection',status:'completed',params:{distance:300},updatedAt:'2026-09-19T01:00:00Z'}]} onGenerate={generate} onOpenReport={open}/>);
 expect(screen.getByRole('button',{name:'生成报告与成果包'})).toBeDisabled();
 fireEvent.change(screen.getByRole('combobox',{name:'交付分析结果'}),{target:{value:'second'}});
 fireEvent.click(screen.getByRole('button',{name:'生成报告与成果包'}));
 await screen.findByText('第 2 版已保存');expect(generate).toHaveBeenCalledWith('second',expect.objectContaining({paperSize:'A4',orientation:'landscape',legend:true}));
 await waitFor(()=>expect(open).toHaveBeenCalledTimes(1));
 fireEvent.click(screen.getByRole('button',{name:'打开报告'}));await waitFor(()=>expect(open).toHaveBeenCalledWith('.earth/deliverables/earth-v2/report.html'));
});
it('opens existing report documents without launching another analysis', async()=>{
 const generate=vi.fn(),open=vi.fn().mockResolvedValue(undefined);
 render(<GISDeliveryPanel runs={[]} reports={[{name:'research.html',path:'.earth/research/report.html'}]} incomplete onGenerate={generate} onOpenReport={open}/>);
 fireEvent.click(screen.getByRole('button',{name:/research.html/}));
 await waitFor(()=>expect(open).toHaveBeenCalledWith('.earth/research/report.html'));
 expect(generate).not.toHaveBeenCalled();expect(screen.getByText(/目录尚未完整读取/)).toBeVisible();
});
it('keeps the chosen run immutable while generating and rejects mismatched receipts', async()=>{
 let finish!:(value:any)=>void;const generate=vi.fn(()=>new Promise<any>(resolve=>{finish=resolve;}));
 render(<GISDeliveryPanel runs={[{runId:'chosen',op:'buffer',status:'completed',updatedAt:'2026-09-22'}]} onGenerate={generate} onOpenReport={vi.fn()}/>);
 fireEvent.change(screen.getByRole('combobox',{name:'交付分析结果'}),{target:{value:'chosen'}});
 const button=screen.getByRole('button',{name:'生成报告与成果包'});fireEvent.click(button);fireEvent.click(button);
 expect(generate).toHaveBeenCalledTimes(1);expect(screen.getByRole('combobox',{name:'交付分析结果'})).toBeDisabled();
 finish({path:'.earth/wrong',runId:'another',version:1,verifiedFiles:3});
 expect(await screen.findByRole('alert')).toHaveTextContent('报告回执与所选分析不一致');expect(screen.queryByRole('button',{name:'打开报告'})).not.toBeInTheDocument();
});

it('retains completed report when the browser fails and permits opening it again',async()=>{
 const open=vi.fn().mockRejectedValueOnce(new Error('浏览器暂不可用')).mockResolvedValue(undefined);
 const generate=vi.fn().mockResolvedValue({path:'.earth/deliverables/a',version:1,runId:'run',verifiedFiles:13});
 render(<GISDeliveryPanel runs={[{runId:'run',op:'buffer',status:'completed',updatedAt:'2026-09-22'}]} onGenerate={generate} onOpenReport={open}/>);
 fireEvent.change(screen.getByRole('combobox',{name:'交付分析结果'}),{target:{value:'run'}});
 fireEvent.click(screen.getByRole('button',{name:'生成报告与成果包'}));
 expect(await screen.findByRole('alert')).toHaveTextContent('浏览器暂不可用');expect(screen.getByText('第 1 版已保存')).toBeVisible();
 fireEvent.click(screen.getByRole('button',{name:'打开报告'}));await waitFor(()=>expect(open).toHaveBeenCalledTimes(2));expect(generate).toHaveBeenCalledTimes(1);
});
