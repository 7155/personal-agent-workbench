import {test} from 'node:test';
import assert from 'node:assert/strict';
import {earthTaskStatus,earthTaskCancel} from './cloud-tasks.mjs';
test('rejects malformed or missing cancel targets before any authorization access',async()=>{
 await assert.rejects(earthTaskCancel({root:'/not-a-workspace',taskIds:[]}),/请选择/);
 await assert.rejects(earthTaskCancel({root:'/not-a-workspace',taskIds:['']}),/任务 ID/);
 await assert.rejects(earthTaskStatus({root:'/not-a-workspace',taskIds:'all'}),/任务 ID/);
 await assert.rejects(earthTaskStatus({root:'/not-a-workspace',taskIds:Array(101).fill('a')}),/任务 ID/);
});
test('missing Earth Engine project is explicit rather than a synthetic empty task list',async()=>{
 await assert.rejects(earthTaskStatus({root:'/not-a-workspace'}),/authorized Earth Engine project/);
});
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
test('isolates concurrent project authentication and returns SDK callback states',async()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'earth-cloud-tasks-'));
 try{
  const sdk=path.join(root,'sdk');
  fs.mkdirSync(path.join(sdk,'node_modules/@google/earthengine'),{recursive:true});
  fs.mkdirSync(path.join(sdk,'node_modules/https-proxy-agent'),{recursive:true});
  fs.writeFileSync(path.join(sdk,'node_modules/https-proxy-agent/index.js'),"exports.HttpsProxyAgent=require('node:https').Agent;");
  fs.writeFileSync(path.join(sdk,'node_modules/@google/earthengine/index.js'),`let project; module.exports={initialize(a,b,ok,fail,c,p){project=p;setTimeout(ok,5)},data:{setAuthToken(){},getTaskListWithLimit(limit,cb){setTimeout(()=>cb({tasks:[{id:project,state:'RUNNING'}]}),5)},getTaskStatus(ids,cb){cb(ids.map(id=>({id,state:'COMPLETED'})))},cancelTask(id,cb){cb({})}}};`);
  const helper=path.join(root,'auth.mjs');fs.writeFileSync(helper,"process.stdout.write('test-token');");
  function workspace(project){const dir=path.join(root,project);fs.mkdirSync(path.join(dir,'.earth'),{recursive:true});fs.writeFileSync(path.join(dir,'.earth/runtime.json'),JSON.stringify({project,dependencies:sdk,python:process.execPath,authHelper:helper}));return dir;}
  const first=workspace('first'),second=workspace('second');
  const [a,b]=await Promise.all([earthTaskStatus({root:first}),earthTaskStatus({root:second})]);
  assert.equal(a.tasks[0].id,'first');assert.equal(b.tasks[0].id,'second');
  assert.equal((await earthTaskStatus({root:first,taskIds:['one']})).tasks[0].state,'COMPLETED');
  assert.deepEqual((await earthTaskCancel({root:first,taskIds:['one','one']})).cancelledTaskIds,['one']);
 }finally{fs.rmSync(root,{recursive:true,force:true});}
});
