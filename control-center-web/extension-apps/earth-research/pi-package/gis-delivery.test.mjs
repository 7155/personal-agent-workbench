import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import {spawnSync} from 'node:child_process';
import {runSitingWorkflow} from './gis-workflows.mjs';
import {createGISBundle,verifyGISBundle,compareGISRuns} from './gis-delivery.mjs';
const python=process.env.PAW_EARTH_GIS_PYTHON || 'python3';
const available=spawnSync(python,['-c','import geopandas, rasterio'],{stdio:'ignore'}).status===0;
test('200/300 m analyses deliver two independent readable versions from the same inputs', {skip:!available&&!process.env.PAW_EARTH_GIS_PYTHON},async()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'gis-delivery-'));
 try {
  fs.mkdirSync(path.join(root,'data'));
  const collection=features=>JSON.stringify({type:'FeatureCollection',features});
  fs.writeFileSync(path.join(root,'data/parcels.geojson'),collection([{type:'Feature',id:'p1',properties:{id:'p1'},geometry:{type:'Polygon',coordinates:[[[120,30],[120.03,30],[120.03,30.03],[120,30.03],[120,30]]]}}]));
  fs.writeFileSync(path.join(root,'data/river.geojson'),collection([{type:'Feature',properties:{id:'r1'},geometry:{type:'LineString',coordinates:[[120.015,29.99],[120.015,30.04]]}}]));
  const runs=[];
  for(const distance of [200,300]) {
   const run=await runSitingWorkflow({root,python,parcels:'data/parcels.geojson',avoidance:'data/river.geojson',distance,commandId:`site-${distance}`});
   assert.equal(run.status,'completed',run.error);runs.push(run);
  }
  assert.notEqual(runs[0].runId,runs[1].runId);
  assert.ok(runs[1].outputs[0].summary.areaM2<runs[0].outputs[0].summary.areaM2);
  const subset=spawnSync(python,['-c','import geopandas as g,sys; a=g.read_file(sys.argv[1]).to_crs(32651).geometry.union_all(); b=g.read_file(sys.argv[2]).to_crs(32651).geometry.union_all(); assert b.difference(a).area < 0.01',path.join(root,runs[0].outputs[0].path),path.join(root,runs[1].outputs[0].path)],{encoding:'utf8'});
  assert.equal(subset.status,0,subset.stderr);
  const comparison=compareGISRuns({root,firstRunId:runs[0].runId,secondRunId:runs[1].runId});
  assert.equal(comparison.sameInputs,true);assert.ok(comparison.areaDeltaM2<0);
  const deliveries=runs.map(run=>createGISBundle({root,runId:run.runId}));
  assert.deepEqual(deliveries.map(x=>x.manifest.version),[1,2]);
  for(let i=0;i<2;i++) {
   const delivery=deliveries[i],run=runs[i],dir=path.join(root,delivery.path);
   assert.equal(verifyGISBundle({root,path:delivery.path}).runId,run.runId);
   const stats=JSON.parse(fs.readFileSync(path.join(dir,'statistics.json')));
   assert.equal(stats.runId,run.runId);assert.equal(stats.params.distance,i?300:200);
   assert.equal(stats.outputs[0].areaM2,run.outputs[0].summary.areaM2);
   assert.ok(fs.readFileSync(path.join(dir,'report.html'),'utf8').includes(run.runId));
   const map=JSON.parse(fs.readFileSync(path.join(dir,'map.geojson')));
   assert.deepEqual(map.features,run.outputs[0].geojson.features);
  }
  const clean=fs.mkdtempSync(path.join(os.tmpdir(),'gis-clean-project-'));
  try {fs.cpSync(path.join(root,deliveries[0].path),path.join(clean,'delivery'),{recursive:true});assert.equal(verifyGISBundle({root:clean,path:'delivery'}).runId,runs[0].runId);}finally{fs.rmSync(clean,{recursive:true,force:true});}
  const again=await runSitingWorkflow({root,python,parcels:'data/parcels.geojson',avoidance:'data/river.geojson',distance:200,commandId:'site-200'});assert.equal(again.runId,runs[0].runId);
  const old=fs.readFileSync(path.join(root,deliveries[0].path,'report.html'),'utf8');
  fs.writeFileSync(path.join(root,'data/parcels.geojson'),'changed');
  assert.equal(fs.readFileSync(path.join(root,deliveries[0].path,'report.html'),'utf8'),old);
 }finally{fs.rmSync(root,{recursive:true,force:true});}
});
