import fs from 'node:fs';
import path from 'node:path';
import {randomUUID,createHash} from 'node:crypto';
import {throwIfAborted, executionFailure} from './runner-process.mjs';
import {workspacePath} from './workspace-path.mjs';
import {runGISOperation} from './gis-operations.mjs';
import {snapshotGISInputs} from './gis-delivery.mjs';
const atomic=(file,data)=> {const temp=`${file}.${randomUUID()}.tmp`;fs.writeFileSync(temp,JSON.stringify(data,null,2));fs.renameSync(temp,file);};

/** A resumable record of a bounded deterministic site analysis, shared by UI and Agent. */
export async function runSitingWorkflow({root,python,parcels,avoidance,distance,commandId,signal,expectedInputs}) {
  throwIfAborted(signal);
  root=fs.realpathSync(root);
  if(!Number.isFinite(distance)||distance<=0||distance>100000)throw new Error('避让距离必须在 0–100000 米之间。');
  if(commandId&&!/^[A-Za-z0-9_-]{1,100}$/.test(commandId))throw new Error('Invalid command ID.');
  const commandDirectory=workspacePath(root,'.earth/gis/commands',{allowMissing:true});fs.mkdirSync(commandDirectory,{recursive:true});
  const commandPath=commandId ? workspacePath(root,`.earth/gis/commands/${commandId}.json`,{allowMissing:true}) : null;
  const fingerprint=createHash('sha256').update(JSON.stringify({parcels,avoidance,distance,expectedInputs})).digest('hex');
  if(commandPath&&fs.existsSync(commandPath)) {
    const saved=JSON.parse(fs.readFileSync(commandPath,'utf8'));
    if(saved.fingerprint!==fingerprint)throw new Error('同一命令 ID 不能使用不同参数。');
    if(typeof saved.runId!=='string'||! /^[a-f0-9-]{36}$/i.test(saved.runId))throw new Error('Invalid stored workflow run ID.');
    return JSON.parse(fs.readFileSync(workspacePath(root,`.earth/gis/runs/${saved.runId}/run.json`),'utf8'));
  }
  const runId=randomUUID(),runDir=workspacePath(root,`.earth/gis/runs/${runId}`,{allowMissing:true});fs.mkdirSync(runDir,{recursive:true});
  const receipt={schemaVersion:'earth.gis-workflow.v1',backend:'local',runId,op:'site-selection',status:'running',params:{distance,units:'m'},startedAt:new Date().toISOString(),updatedAt:new Date().toISOString(),steps:[],outputs:[],inputVersions:[]};
  const persist=()=>{receipt.updatedAt=new Date().toISOString();atomic(path.join(runDir,'run.json'),receipt);};
  persist();
  if(commandPath)fs.writeFileSync(commandPath,JSON.stringify({runId,fingerprint}),{flag:'wx'});
  try {
    const snapshot=snapshotGISInputs(root,runDir,{parcels,avoidance});receipt.inputVersions=snapshot.versions;persist();
    if (expectedInputs !== undefined) {
      if (!Array.isArray(expectedInputs) || expectedInputs.length !== 2 || new Set(expectedInputs.map(item=>item?.role)).size !== 2) throw new Error('输入检查凭据无效。');
      for (const input of snapshot.versions) {
        const expected = expectedInputs.find(item => item.role === input.role);
        if (!expected || expected.path !== input.source || expected.sha256 !== input.sha256) throw new Error('输入已在检查后改变，请重新检查并创建新方案。');
        const actualFiles = input.files.map(file => ({name:path.basename(file.path),sha256:file.sha256})).sort((a,b)=>a.name.localeCompare(b.name));
        if (!Array.isArray(expected.files) || JSON.stringify([...expected.files].sort((a,b)=>String(a.name).localeCompare(String(b.name)))) !== JSON.stringify(actualFiles)) throw new Error('输入或其附属文件已在检查后改变，请重新检查并创建新方案。');
      }
    }
    const buffer=await runGISOperation({root,python,signal,request:{op:'buffer',inputs:{layer:snapshot.bindings.avoidance},params:{distance,dissolve:true,keep_projected:true},output:'avoidance_buffer',saveAs:'pred_results/avoidance_buffer.gpkg'}});
    receipt.steps.push({name:'buffer',runId:buffer.runId,status:buffer.status});persist();
    if(buffer.status!=='completed')throw Object.assign(new Error(buffer.error || '缓冲区计算失败'),{code:buffer.code || 'workflow_step_failed'});
    const metricBuffer=buffer.outputs.find(output=>output.name==='avoidance_buffer.gpkg');
    const measurementCrs=metricBuffer?.summary?.crs;
    if(!metricBuffer || !measurementCrs)throw new Error('缓冲区没有返回可核对的米制坐标系。');
    receipt.params.measurementCrs=measurementCrs;persist();
    const projected=await runGISOperation({root,python,signal,request:{op:'reproject',inputs:{layer:snapshot.bindings.parcels},params:{target_crs:measurementCrs},output:'analysis_parcels',saveAs:'pred_results/analysis_parcels.gpkg'}});
    receipt.steps.push({name:'project-parcels',runId:projected.runId,status:projected.status});persist();
    if(projected.status!=='completed')throw Object.assign(new Error(projected.error || '地块投影失败'),{code:projected.code || 'workflow_step_failed'});
    const metricParcels=projected.outputs.find(output=>output.name==='analysis_parcels.gpkg');
    if(!metricParcels)throw new Error('地块没有返回米制分析文件。');
    const difference=await runGISOperation({root,python,signal,request:{op:'difference',inputs:{layer:metricParcels.path,overlay:metricBuffer.path},params:{},output:'safe_sites',saveAs:'pred_results/safe_sites.gpkg'}});
    receipt.steps.push({name:'difference',runId:difference.runId,status:difference.status});persist();
    if(difference.status!=='completed')throw Object.assign(new Error(difference.error || '扣除避让区失败'),{code:difference.code || 'workflow_step_failed'});
    fs.mkdirSync(path.join(runDir,'pred_results'));
    // Persist the metric geometry as the analysis result. WGS84 remains the
    // separately generated output.geojson preview used by the map host.
    const canonical=difference.outputs.filter(output=>output.name==='safe_sites.gpkg');
    if(canonical.length!==1)throw new Error('扣除避让区后没有返回可交付的分析文件。');
    receipt.outputs=canonical.map(output=>{
      const destination=path.join(runDir,'pred_results',path.basename(output.path));
      fs.copyFileSync(path.join(root,output.path),destination);
      return {...output,path:path.relative(root,destination),relativePath:path.relative(runDir,destination),sha256:createHash('sha256').update(fs.readFileSync(destination)).digest('hex')};
    });
    receipt.status='completed';
    receipt.quality={conclusion:receipt.outputs.some(x=>x.summary?.featureCount>0)?'符合已检查的避让条件':'没有符合已检查条件的区域',notChecked:['土地权属','工程审批','未提供的其他限制'],geometrySource:'metric buffer and deterministic difference',measurementCrs,inputVersions:receipt.inputVersions};
  }catch(error){Object.assign(receipt,executionFailure(error));}
  persist();atomic(path.join(root,'.earth/gis/workspace.json'),receipt);return receipt;
}
