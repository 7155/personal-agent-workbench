import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
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
