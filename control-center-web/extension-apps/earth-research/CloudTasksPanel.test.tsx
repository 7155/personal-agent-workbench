import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import {CloudTasksPanel} from './CloudTasksPanel';
afterEach(cleanup);
it('keeps the last cloud state on refresh failure and cancels only the requested id',async()=>{
 const refresh=vi.fn().mockResolvedValue({checkedAt:'2026-09-20T01:00:00Z',tasks:[{id:'one',description:'高程导出',state:'RUNNING'}]}),cancel=vi.fn().mockResolvedValue({status:'submitted'});
 render(<CloudTasksPanel onRefresh={refresh} onCancel={cancel}/>);
 await screen.findByText('高程导出');
 refresh.mockRejectedValue(new Error('网络暂不可用'));
 fireEvent.click(screen.getByRole('button',{name:'刷新'}));
 await screen.findByText(/上次状态已保留/);expect(screen.getByText('运行中')).toBeInTheDocument();
 fireEvent.click(screen.getByRole('button',{name:'取消这个任务'}));
 await waitFor(()=>expect(cancel).toHaveBeenCalledWith('one'));
 await screen.findByRole('button',{name:'已请求取消'});
});
it('distinguishes an empty live task list from missing results',async()=>{
 render(<CloudTasksPanel onRefresh={async()=>({tasks:[]})} onCancel={async()=>{}}/>);
 await screen.findByText(/当前没有云端批处理任务/);
 expect(screen.queryByRole('button',{name:'取消这个任务'})).not.toBeInTheDocument();
});
