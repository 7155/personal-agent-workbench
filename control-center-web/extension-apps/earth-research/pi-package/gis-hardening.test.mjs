import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';
import test from 'node:test';
import { workspacePath } from './workspace-path.mjs';
import { runGISOperation, inspectGISPath } from './gis-operations.mjs';
import { runGISBatch } from './batch.mjs';
import { preflightSiting, inputFileVersions } from './gis-preflight.mjs';
import { runSitingWorkflow } from './gis-workflows.mjs';

const python=process.env.PAW_EARTH_GIS_PYTHON || 'python3';
const integration={skip:spawnSync(python,['-c','import geopandas,rasterio'],{stdio:'ignore'}).status!==0?'Requires GeoPandas and Rasterio':false};
const makeRoot=()=>fs.mkdtempSync(path.join(os.tmpdir(),'earth-hardening-'));
function fixtures(root) {
  const write=(name,features)=>fs.writeFileSync(path.join(root,name),JSON.stringify({type:'FeatureCollection',features}));
  write('parcels.geojson',[{type:'Feature',properties:{name:'candidate'},geometry:{type:'Polygon',coordinates:[[[120,30],[120.02,30],[120.02,30.02],[120,30.02],[120,30]]]}}]);
  write('river.geojson',[{type:'Feature',properties:{name:'river'},geometry:{type:'LineString',coordinates:[[120.01,29.99],[120.01,30.03]]}}]);
  return {root,python,parcels:'parcels.geojson',avoidance:'river.geojson',distance:200};
}
function fakePython(root, code) {
  const file=path.join(root,'fake-python');fs.writeFileSync(file,`#!${process.execPath}\nprocess.stdin.resume();\n${code}\n`,{mode:0o700});return file;
}

test('workspace paths reject escaping, ancestor symlinks and dangling symlinks',()=>{
 const root=makeRoot(),external=makeRoot();try{
  fs.mkdirSync(path.join(root,'real'));fs.symlinkSync(external,path.join(root,'linked'));fs.symlinkSync(path.join(external,'missing'),path.join(root,'dangling'));
  for(const candidate of ['../outside','/etc/passwd','linked/file','dangling']) assert.throws(()=>workspacePath(root,candidate,{allowMissing:true}));
  assert.equal(workspacePath(root,'real/new/file',{allowMissing:true}),path.join(root,'real/new/file'));
 }finally{fs.rmSync(root,{recursive:true,force:true});fs.rmSync(external,{recursive:true,force:true});}
});
test('a pre-aborted batch creates no run directories and has cancelled terminal entries',async()=>{
 const root=makeRoot(),controller=new AbortController();controller.abort();try{
  const result=await runGISBatch({root,signal:controller.signal,requests:[{op:'buffer',inputs:{}},{op:'clip',inputs:{}}]});
  assert.equal(result.cancelled,2);assert.equal(result.status,'cancelled');assert.ok(result.results.every(item=>item.status==='cancelled'));
  assert.equal(fs.existsSync(path.join(root,'.earth')),false);
 }finally{fs.rmSync(root,{recursive:true,force:true});}
});
test('a cancelled local operation persists cancelled rather than running or completed',async()=>{
 const root=makeRoot(),controller=new AbortController();try{
  const python=fakePython(root,`require('node:fs').writeFileSync('ready','1');setInterval(()=>{},100);`);
  const promise=runGISOperation({root,python,signal:controller.signal,request:{op:'buffer',inputs:{}}});
  for(let i=0;i<150&&!fs.existsSync(path.join(root,'ready'));i++)await delay(10);
  assert.ok(fs.existsSync(path.join(root,'ready')));controller.abort();const result=await promise;
  assert.equal(result.status,'cancelled');assert.deepEqual(result.outputs,[]);
  assert.equal(JSON.parse(fs.readFileSync(path.join(root,`.earth/gis/runs/${result.runId}/run.json`))).status,'cancelled');
 }finally{fs.rmSync(root,{recursive:true,force:true});}
});
test('missing output files are terminal failures, never stranded running receipts',async()=>{
 const root=makeRoot();try{
  const python=fakePython(root,`process.stdin.on('end',()=>console.log(JSON.stringify({status:'completed',outputs:[{path:'missing.geojson'}]})));`);
  const result=await runGISOperation({root,python,request:{op:'buffer',inputs:{}}});
  assert.equal(result.status,'failed');assert.deepEqual(result.outputs,[]);
  assert.equal(JSON.parse(fs.readFileSync(path.join(root,'.earth/gis/workspace.json'))).status,'failed');
 }finally{fs.rmSync(root,{recursive:true,force:true});}
});
test('real input preflight freezes versions; a changed file blocks all workflow steps',integration,async()=>{
 const root=makeRoot();try{
  const input=fixtures(root),check=await preflightSiting(input);
  assert.equal(check.ready,true,JSON.stringify(check));assert.equal(check.inputs.length,2);assert.ok(check.inputs.every(input=>input.crs&&input.files.length===1));
  fs.appendFileSync(path.join(root,'river.geojson'),'\n');
  const result=await runSitingWorkflow({...input,commandId:'checked-1',expectedInputs:check.inputs});
  assert.equal(result.status,'failed');assert.equal(result.steps.length,0);assert.match(result.error,/检查后改变/);
  assert.equal((await runSitingWorkflow({...input,commandId:'checked-1',expectedInputs:check.inputs})).runId,result.runId);
 }finally{fs.rmSync(root,{recursive:true,force:true});}
});
test('Shapefile preflight protects .dbf and .prj sidecars too',integration,async()=>{
 const root=makeRoot();try{
  const input=fixtures(root);
  const converted=spawnSync(python,['-c',`import geopandas as g;g.read_file('parcels.geojson').to_file('parcels.shp')`],{cwd:root,encoding:'utf8'});assert.equal(converted.status,0,converted.stderr);
  input.parcels='parcels.shp';const check=await preflightSiting(input);assert.equal(check.ready,true,JSON.stringify(check));
  assert.ok(check.inputs[0].files.some(item=>item.name==='parcels.dbf'));
  fs.appendFileSync(path.join(root,'parcels.prj'),' ');
  const result=await runSitingWorkflow({...input,expectedInputs:check.inputs});assert.equal(result.status,'failed');assert.equal(result.steps.length,0);assert.match(result.error,/附属文件/);
 }finally{fs.rmSync(root,{recursive:true,force:true});}
});
test('real checked inputs produce a deterministic site result and a stable retry receipt',integration,async()=>{
 const root=makeRoot();try{
  const input=fixtures(root),checked=await preflightSiting(input);
  const result=await runSitingWorkflow({...input,expectedInputs:checked.inputs,commandId:'stable'});
  assert.equal(result.status,'completed',JSON.stringify(result));assert.equal(result.steps.length,3);assert.equal(result.outputs.length,1);
  assert.equal((await runSitingWorkflow({...input,expectedInputs:checked.inputs,commandId:'stable'})).runId,result.runId);
  await assert.rejects(runSitingWorkflow({...input,distance:250,expectedInputs:checked.inputs,commandId:'stable'}),/不同参数/);
 }finally{fs.rmSync(root,{recursive:true,force:true});}
});
test('preflight blocks point candidates and unreadable inputs without claiming execution',integration,async()=>{
 const root=makeRoot();try{
  const input=fixtures(root);fs.copyFileSync(path.join(root,'river.geojson'),path.join(root,'parcels.geojson'));
  const checked=await preflightSiting({...input,avoidance:'missing.geojson'});
  assert.equal(checked.ready,false);assert.ok(checked.issues.some(item=>item.code==='non_polygon_parcels'));assert.ok(checked.issues.some(item=>item.code==='input_unreadable'));
 }finally{fs.rmSync(root,{recursive:true,force:true});}
});
test('exact raster inspection handles multiple windows, NoData and nonfinite values',integration,async()=>{
 const root=makeRoot();try{
  const create=spawnSync(python,['-c',`import numpy as np,rasterio
from rasterio.transform import from_origin
x=np.arange(2*520*530,dtype='float32').reshape(2,520,530)
x[0,0,0]=-9999;x[0,0,1]=np.nan;x[1,0,0]=np.inf
with rasterio.open('test.tif','w',driver='GTiff',width=530,height=520,count=2,dtype='float32',crs='EPSG:4326',transform=from_origin(120,30,.0001,.0001),nodata=-9999) as f:f.write(x)
`],{cwd:root,encoding:'utf8'});assert.equal(create.status,0,create.stderr);
  const summary=await inspectGISPath({root,python,path:'test.tif'});
  assert.equal(summary.statisticsMode,'exact-windowed');assert.equal(summary.validValues,2*520*530-3);assert.equal(summary.min,2);assert.equal(summary.max,2*520*530-1);
 }finally{fs.rmSync(root,{recursive:true,force:true});}
});
