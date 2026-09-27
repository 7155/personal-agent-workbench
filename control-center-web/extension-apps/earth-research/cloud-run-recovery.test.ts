import {it,expect,vi} from 'vitest';
import {recoverCloudRun} from './cloud-run-recovery';
it('recovers matching completed execution after transport timeout, ignoring stale runs',async()=>{
 const read=vi.fn().mockResolvedValueOnce({sourceHash:'other',startedAt:'2026-09-22',status:'completed'}).mockResolvedValueOnce({sourceHash:'hash',startedAt:'2026-09-21',status:'completed'}).mockResolvedValueOnce({sourceHash:'hash',startedAt:'2026-09-22',status:'running'}).mockResolvedValue({sourceHash:'hash',startedAt:'2026-09-22',status:'completed'});
 expect((await recoverCloudRun({read,sourceHash:'hash',startedAfter:Date.parse('2026-09-22'),wait:async()=>{}})).status).toBe('completed');expect(read).toHaveBeenCalledTimes(4);
});
it('fails on terminal execution failure and stops when switching projects',async()=>{
 await expect(recoverCloudRun({read:async()=>({sourceHash:'hash',startedAt:'2026-09-22',status:'failed',error:'Cloud error'}),sourceHash:'hash',startedAfter:0})).rejects.toThrow('Cloud error');
 const read=vi.fn();await expect(recoverCloudRun({read,sourceHash:'hash',startedAfter:0,isCurrent:()=>false})).rejects.toThrow('项目已切换');expect(read).not.toHaveBeenCalled();
});
