/** Offline tests for the real TypeScript state utilities. No React/DOM mocks. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL, fileURLToPath } from 'node:url';
import test, { after } from 'node:test';
const require=createRequire(import.meta.url);
let ts;
try { ts=require('typescript'); } catch { if(!process.env.PAW_TYPESCRIPT_PATH)throw new Error('Install the repository TypeScript dependency or set PAW_TYPESCRIPT_PATH to its module directory.'); ts=require(process.env.PAW_TYPESCRIPT_PATH); }
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..'), temp=fs.mkdtempSync(path.join(os.tmpdir(),'earth-core-tests-'));
after(()=>fs.rmSync(temp,{recursive:true,force:true}));
for(const name of ['workspace-io','gis-command-queue','project-catalog','layer-catalog']) {
 const source=fs.readFileSync(path.join(root,`${name}.ts`),'utf8');
 const {outputText,diagnostics}=ts.transpileModule(source,{fileName:`${name}.ts`,compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022},reportDiagnostics:true});
 assert.equal(diagnostics?.filter(item=>item.category===ts.DiagnosticCategory.Error).length,0);
 fs.writeFileSync(path.join(temp,`${name}.mjs`),outputText.replace(/from '([.][/]\S+?)'/g,"from '$1.mjs'"));
}
const {createGISCommandQueue}=await import(pathToFileURL(path.join(temp,'gis-command-queue.mjs')));
const {createCoalescingWriter,mapBounded,workspaceFilePath,isMissingWorkspaceFile}=await import(pathToFileURL(path.join(temp,'workspace-io.mjs')));
const {loadProjectCatalog}=await import(pathToFileURL(path.join(temp,'project-catalog.mjs')));
const deferred=()=>{let resolve;const promise=new Promise(done=>resolve=done);return {promise,resolve};};

test('a queued mutation from a superseded workspace never starts',async()=>{
 const gate=deferred(),events=[],records=[];let current=true;
 const queue=createGISCommandQueue({onChange:value=>records.push(value)});
 const first=queue('A',async()=>{events.push('first');await gate.promise;return 1;});
 const second=queue('A',async()=>{events.push('must-not-start');},{isCurrent:()=>current});
 const checked=assert.rejects(second,{code:'command_superseded'});
 await Promise.resolve();current=false;gate.resolve();assert.equal(await first,1);await checked;
 assert.deepEqual(events,['first']);assert.equal(records.at(-1).at(-1).status,'superseded');
});
test('reads stay behind writes; another session is independent; rejection does not poison the queue',async()=>{
 const gate=deferred(),events=[],queue=createGISCommandQueue();
 const first=queue('A',async()=>{events.push('write');await gate.promise;throw new Error('failed');});
 const failure=assert.rejects(first,/failed/);
 const read=queue('A',async()=>events.push('read'));
 await queue('B',async()=>events.push('other-session'));assert.deepEqual(events,['write','other-session']);
 gate.resolve();await failure;await read;assert.deepEqual(events,['write','other-session','read']);assert.equal(await queue('A',async()=>7),7);
});
test('queue backpressure is bounded and observer errors do not block operations',async()=>{
 const gate=deferred(),queue=createGISCommandQueue({maxPending:1,onChange:()=>{throw new Error('observer');}});
 const first=queue('A',()=>gate.promise);await assert.rejects(queue('A',async()=>2),/队列已满/);gate.resolve();await first;
 assert.equal(await queue('A',async()=>3),3);assert.throws(()=>createGISCommandQueue({maxPending:NaN}),TypeError);
});
test('coalesces viewport updates without overlapping read/CAS/write',async()=>{
 const gate=deferred(),events=[];let active=0,max=0;
 const writer=createCoalescingWriter(async value=>{active++;max=Math.max(max,active);events.push(value);if(value===1)await gate.promise;active--;});
 const first=writer.push(1);writer.push(2);writer.push(3);gate.resolve();await first;
 assert.deepEqual(events,[1,3]);assert.equal(max,1);await writer.push(4);assert.deepEqual(events,[1,3,4]);
});
test('viewport write errors are surfaced and disposed writers drop pending state',async()=>{
 const errors=[],writer=createCoalescingWriter(async()=>{throw new Error('CAS conflict');},error=>errors.push(error.message));
 await writer.push(1);await writer.push(2);assert.deepEqual(errors,['CAS conflict','CAS conflict']);writer.dispose();await writer.push(3);assert.equal(errors.length,2);
 const gate=deferred(),events=[],second=createCoalescingWriter(async value=>{events.push(value);await gate.promise;});
 const pending=second.push(1);second.push(2);second.dispose();gate.resolve();await pending;assert.deepEqual(events,[1]);
});
test('parallel catalog reads keep order and a maximum of four in flight',async()=>{
 let active=0,max=0;
 const results=await mapBounded([0,1,2,3,4,5,6],4,async number=>{active++;max=Math.max(max,active);await new Promise(done=>setTimeout(done,7-number));active--;return number;});
 assert.deepEqual(results,[0,1,2,3,4,5,6]);assert.equal(max,4);await assert.rejects(mapBounded([],NaN,async x=>x),TypeError);
});
test('catalog read failures retain old geometry AND its old revision',async()=>{
 const feature={type:'Feature',id:'A',geometry:{type:'Point',coordinates:[120,30]},properties:{name:'retained'}};
 const previous={id:'sites',name:'地块',revision:2,path:'sites.geojson',featureCount:1,format:'geojson',geometryTypes:['Point'],crs:'EPSG:4326',visible:true,updatedAt:'',features:[feature]};
 const result=await loadProjectCatalog({root:'/work',records:[{...previous,revision:3}],previous:[previous],read:async()=>{throw new Error('network unavailable');}});
 assert.equal(result.layers[0].revision,2);assert.deepEqual(result.layers[0].features,[feature]);assert.equal(result.warnings.length,1);assert.match(result.layers[0].loadError,/unavailable/);
});
test('an unreadable new layer is marked; true empty layers remain valid',async()=>{
 const record={id:'empty',name:'empty',revision:1,path:'empty.geojson',featureCount:0,format:'geojson',geometryTypes:[],crs:'EPSG:4326',visible:true,updatedAt:''};
 const empty=await loadProjectCatalog({root:'/work',records:[record],previous:[],read:async()=>JSON.stringify({type:'FeatureCollection',features:[]})});assert.equal(empty.warnings.length,0);assert.equal(empty.layers[0].loadError,undefined);
 const bad=await loadProjectCatalog({root:'/work',records:[{...record,featureCount:2}],previous:[],read:async()=>JSON.stringify({type:'FeatureCollection',features:[]})});assert.equal(bad.warnings.length,1);assert.ok(bad.layers[0].loadError);
});
test('file paths cannot escape the workspace; arbitrary read failures are not ENOENT',()=>{
 assert.equal(workspaceFilePath('/work/','data/file.geojson'),'/work/data/file.geojson');
 for(const relative of ['../secret','/outside','x/../a','x//a','x\\y','a\0b'])assert.throws(()=>workspaceFilePath('/work',relative));
 assert.equal(isMissingWorkspaceFile({code:'ENOENT'}),true);assert.equal(isMissingWorkspaceFile(new Error('path does not exist in the authorized workspace')),true);assert.equal(isMissingWorkspaceFile(new Error('permission denied')),false);
});
