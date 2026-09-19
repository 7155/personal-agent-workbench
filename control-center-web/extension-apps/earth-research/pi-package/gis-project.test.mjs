import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import {spawn} from 'node:child_process';
import {saveProjectLayer} from './gis-project.mjs';
test('saves vertex and remark as one immutable version; reopen preserves untouched types and IDs',()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'gis-edit-'));
 try {
  const original={type:'Feature',id:'parcel-01',properties:{parcel_id:'12345',flag:'true',count:12,nullable:null,note:'before'},geometry:{type:'Polygon',coordinates:[[[120,30],[120.01,30],[120.01,30.01],[120,30.01],[120,30]]]}};
  const first=saveProjectLayer({root,name:'地块',features:[original],commandId:'first'});
  const edited=structuredClone(original);edited.properties.note='after';edited.geometry.coordinates[0][1][0]=120.012;
  const second=saveProjectLayer({root,name:'改名地块',layerId:first.layer.id,expectedRevision:1,features:[edited],commandId:'second'});
  assert.equal(second.layer.id,first.layer.id);assert.equal(second.layer.revision,2);
  const catalog=JSON.parse(fs.readFileSync(path.join(root,'.earth/layers/catalog.json')));
  const reopened=JSON.parse(fs.readFileSync(path.join(root,catalog.layers[0].path)));
  assert.deepEqual(reopened.features,[edited]);
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(root,first.layer.path))).features,[original]);
  assert.throws(()=>saveProjectLayer({root,name:'地块',layerId:first.layer.id,expectedRevision:1,features:[original]}),/新版本/);
  assert.equal(saveProjectLayer({root,name:'改名地块',layerId:first.layer.id,expectedRevision:1,features:[edited],commandId:'second'}).layer.revision,2);
 }finally{fs.rmSync(root,{recursive:true,force:true});}
});

test('two Agent processes sharing one project cannot overwrite the same layer revision', async () => {
 const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gis-shared-project-'));
 const children = [];
 try {
  const feature = {type:'Feature',id:'parcel-01',properties:{note:'original',parcel_id:'001'},geometry:{type:'Point',coordinates:[120,30]}};
  const original = saveProjectLayer({root,name:'共享地块',features:[feature],commandId:'original'});
  const moduleUrl = new URL('./gis-project.mjs', import.meta.url).href;
  const workers = ['agent-a','agent-b'].map(agent => {
   const input = {root,name:'共享地块',layerId:original.layer.id,expectedRevision:1,commandId:agent,features:[{...feature,properties:{...feature.properties,note:agent}}]};
   const code = `import {saveProjectLayer} from ${JSON.stringify(moduleUrl)};
process.stdout.write('ready\\n');
process.stdin.once('data', () => {
 try { const receipt = saveProjectLayer(${JSON.stringify(input)}); process.stdout.write(JSON.stringify({ok:true,projectId:receipt.projectId,revision:receipt.layer.revision})+'\\n'); }
 catch (error) { process.stdout.write(JSON.stringify({ok:false,error:error.message})+'\\n'); }
 process.stdin.pause();
});`;
   const child = spawn(process.execPath,['--input-type=module','-e',code],{stdio:['pipe','pipe','pipe']});
   children.push(child);
   let stdout = '', stderr = '';
   const ready = new Promise((resolve,reject) => {
    child.on('error', reject);
    child.stdout.on('data', data => { stdout += data; if (stdout.includes('ready\n')) resolve(); });
    child.once('exit', () => { if (!stdout.includes('ready\n')) reject(new Error(stderr || 'worker exited before ready')); });
   });
   child.stderr.on('data', data => { stderr += data; });
   const result = new Promise((resolve,reject) => {
    child.on('error', reject);
    child.once('exit', code => {
     if (code !== 0) { reject(new Error(stderr || `worker exited ${code}`)); return; }
     try { resolve(JSON.parse(stdout.trim().split('\n').at(-1))); } catch (error) { reject(error); }
    });
   });
   return {child,ready,result};
  });
  await Promise.all(workers.map(worker => worker.ready));
  for (const worker of workers) worker.child.stdin.end('save');
  const results = await Promise.all(workers.map(worker => worker.result));
  assert.equal(results.filter(result => result.ok).length,1);
  assert.match(results.find(result => !result.ok).error,/新版本|正在保存/);
  const catalog = JSON.parse(fs.readFileSync(path.join(root,'.earth/layers/catalog.json')));
  assert.equal(catalog.projectId,original.projectId);
  assert.equal(catalog.layers.length,1);
  assert.equal(catalog.layers[0].revision,2);
  const current = JSON.parse(fs.readFileSync(path.join(root,catalog.layers[0].path)));
  assert.ok(['agent-a','agent-b'].includes(current.features[0].properties.note));
  assert.equal(current.features[0].properties.parcel_id,'001');
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(root,original.layer.path))).features,[feature]);
 } finally {
  for (const child of children) if (child.exitCode === null) child.kill();
  fs.rmSync(root,{recursive:true,force:true});
 }
});
