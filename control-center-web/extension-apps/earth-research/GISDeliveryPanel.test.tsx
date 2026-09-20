import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import {GISDeliveryPanel} from './GISDeliveryPanel';
afterEach(cleanup);
it('delivers the explicitly selected run and opens that immutable report',async()=>{
 const generate=vi.fn().mockResolvedValue({path:'.earth/deliverables/earth-v2',version:2,runId:'second',verifiedFiles:12}),open=vi.fn().mockResolvedValue(undefined);
 render(<GISDeliveryPanel runs={[{runId:'first',op:'site-selection',status:'completed',params:{distance:200},updatedAt:'2026-09-19T00:00:00Z'},{runId:'second',op:'site-selection',status:'completed',params:{distance:300},updatedAt:'2026-09-19T01:00:00Z'}]} onGenerate={generate} onOpenReport={open}/>);
 expect(screen.getByRole('button',{name:'生成成果包'})).toBeDisabled();
 fireEvent.change(screen.getByRole('combobox',{name:'交付分析结果'}),{target:{value:'second'}});
 fireEvent.click(screen.getByRole('button',{name:'生成成果包'}));
 await screen.findByText('第 2 版已保存');expect(generate).toHaveBeenCalledWith('second',expect.objectContaining({paperSize:'A4',orientation:'landscape',legend:true}));
 fireEvent.click(screen.getByRole('button',{name:'打开报告'}));await waitFor(()=>expect(open).toHaveBeenCalledWith('.earth/deliverables/earth-v2/report.html'));
});
