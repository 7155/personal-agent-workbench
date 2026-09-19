import {expect,it} from 'vitest';
import {createGISCommandQueue} from './gis-command-queue';
it('orders reads behind pending writes and recovers after a failed command',async()=>{
 const run=createGISCommandQueue(),events:string[]=[];
 let release!:()=>void;
 const writing=new Promise<void>(resolve=>{release=resolve;});
 const write=run('a',async()=>{events.push('write');await writing;throw new Error('save failed');});
 const read=run('a',async()=>{events.push('read');return 'receipt-a';});
 const other=run('b',async()=>{events.push('other');return 'receipt-b';});
 await other;expect(events).toEqual(['write','other']);
 release();await expect(write).rejects.toThrow('save failed');
 expect(await read).toBe('receipt-a');expect(events).toEqual(['write','other','read']);
 expect(await run('a',async()=> 'next')).toBe('next');
});
