/** Exercise the actual Pi package registration without the host or an LLM. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL, fileURLToPath } from 'node:url';
import test, { after } from 'node:test';
const require=createRequire(import.meta.url);
let ts;
try{ts=require('typescript');}catch{if(!process.env.PAW_TYPESCRIPT_PATH)throw new Error('Install TypeScript or set PAW_TYPESCRIPT_PATH.');ts=require(process.env.PAW_TYPESCRIPT_PATH);}
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..'),temp=fs.mkdtempSync(path.join(os.tmpdir(),'earth-registration-'));
fs.cpSync(path.join(root,'pi-package'),path.join(temp,'package'),{recursive:true});
for(const name of ['index','view-contract','official-docs']) {
 const source=fs.readFileSync(path.join(root,'pi-package',`${name}.ts`),'utf8');
 const result=ts.transpileModule(source,{fileName:`${name}.ts`,compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022},reportDiagnostics:true});
 assert.equal(result.diagnostics?.filter(item=>item.category===ts.DiagnosticCategory.Error).length,0);
 fs.writeFileSync(path.join(temp,'package',`${name}.mjs`),result.outputText.replace(/from '(\.\/(?:view-contract|official-docs))'/g,"from '$1.mjs'"));
}
const {default:register}=await import(pathToFileURL(path.join(temp,'package/index.mjs')));
after(()=>fs.rmSync(temp,{recursive:true,force:true}));
function setup(){const tools=new Map(),commands=new Map(),receipts=[];register({registerTool:tool=>tools.set(tool.name,tool),registerCommand:(name,command)=>commands.set(name,command),appendEntry:(_type,data)=>receipts.push(data)});const cwd=fs.mkdtempSync(path.join(temp,'workspace-'));return{tools,commands,receipts,cwd};}
test('UI and Agent share preflight and siting services with explicit version contracts',()=>{
 const {tools,commands}=setup();assert.ok(commands.has('earth-gis-preflight'));assert.ok(commands.has('earth-gis-siting'));
 assert.ok(tools.has('earth_gis_preflight'));assert.ok(tools.get('earth_gis_siting').parameters.properties.expectedInputs.items.properties.files);
 assert.equal(tools.get('earth_gis_siting').executionMode,'sequential');
});
test('Pi cancellation reaches every changed local execution tool before work starts',async()=>{
 const {tools,cwd}=setup(),controller=new AbortController();controller.abort();
 for(const [name,input] of [['earth_gis_inspect',{path:'absent'}],['earth_gis_pixel',{path:'absent',longitude:120,latitude:30}],['earth_geoprocess',{op:'buffer',inputs:{}}],['earth_gis_export',{input:'absent',format:'shp',name:'a'}],['earth_remote_run',{planId:'absent'}],['earth_gis_siting',{parcels:'a',avoidance:'b',distance:200}],['earth_gis_preflight',{parcels:'a',avoidance:'b',distance:200}]]) {
  await assert.rejects(tools.get(name).execute('test',input,controller.signal,()=>{},{cwd}),{code:'execution_cancelled'},name);
 }
 assert.equal(fs.existsSync(path.join(cwd,'.earth')),false);
});
test('Pi batch tool propagates cancellation rather than scheduling new jobs',async()=>{
 const {tools,cwd}=setup(),controller=new AbortController();controller.abort();
 const receipt=await tools.get('earth_gis_batch').execute('test',{requests:[{op:'buffer',inputs:{}}]},controller.signal,()=>{},{cwd});
 assert.equal(receipt.details.status,'cancelled');assert.equal(receipt.isError,true);assert.equal(fs.existsSync(path.join(cwd,'.earth')),false);
});
test('the direct preflight command appends a matching receipt without a model call',async()=>{
 const {commands,receipts,cwd}=setup();
 await commands.get('earth-gis-preflight').handler(JSON.stringify({parcels:'missing-parcels',avoidance:'missing-river',distance:200}),{cwd});
 assert.equal(receipts[0].command,'earth-gis-preflight');assert.equal(receipts[0].schemaVersion,'rag-ime.pi-package-command-result.v1');assert.equal(receipts[0].result.ready,false);
});
