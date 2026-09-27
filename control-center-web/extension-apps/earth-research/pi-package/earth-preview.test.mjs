import test from 'node:test';
import assert from 'node:assert/strict';
import {fetchEarthPreview} from './earth-execution.mjs';
test('report previews validate host, size, response and PNG signature',async()=>{
 const url='https://earthengine.googleapis.com/preview';
 await assert.rejects(fetchEarthPreview('https://example.com',()=>{throw Error('must not fetch')}),/host/);
 await assert.rejects(fetchEarthPreview(url,async()=>new Response('error',{status:403})),/403/);
 await assert.rejects(fetchEarthPreview(url,async()=>new Response('html')),/not PNG/);
 await assert.rejects(fetchEarthPreview(url,async()=>new Response(new Uint8Array(1024*1024+1))),/exceeds/);
 assert.match(await fetchEarthPreview(url,async()=>new Response(Buffer.from([137,80,78,71,13,10,26,10]))),/^data:image\/png;base64,/);
});
