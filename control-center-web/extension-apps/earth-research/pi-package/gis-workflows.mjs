import fs from 'node:fs';
import path from 'node:path';
import {randomUUID,createHash} from 'node:crypto';
import {runGISOperation} from './gis-operations.mjs';
import {snapshotGISInputs} from './gis-delivery.mjs';
const atomic=(file,data)=> {const temp=`${file}.${randomUUID()}.tmp`;fs.writeFileSync(temp,JSON.stringify(data,null,2));fs.renameSync(temp,file);};

/** A resumable record of a bounded deterministic site analysis, shared by UI and Agent. */
export async function runSitingWorkflow({root,python,parcels,avoidance,distance,commandId}) {
  root=fs.realpathSync(root);
  if(!Number.isFinite(distance)||distance<=0||distance>100000)throw new Error('避让距离必须在 0–100000 米之间。');
  if(commandId&&!/^[A-Za-z0-9_-]{1,100}$/.test(commandId))throw new Error('Invalid command ID.');
  const commandDirectory=path.join(root,'.earth/gis/commands');fs.mkdirSync(commandDirectory,{recursive:true});
  const commandPath=commandId ? path.join(commandDirectory,`${commandId}.json`) : null;
  const fingerprint=createHash('sha256').update(JSON.stringify({parcels,avoidance,distance})).digest('hex');
  if(commandPath&&fs.existsSync(commandPath)) {
    const saved=JSON.parse(fs.readFileSync(commandPath,'utf8'));
    if(saved.fingerprint!==fingerprint)throw new Error('同一命令 ID 不能使用不同参数。');
    return JSON.parse(fs.readFileSync(path.join(root,'.earth/gis/runs',saved.runId,'run.json'),'utf8'));
  }
  const runId=randomUUID(),runDir=path.join(root,'.earth/gis/runs',runId);fs.mkdirSync(runDir,{recursive:true});
  const receipt={schemaVersion:'earth.gis-workflow.v1',backend:'local',runId,op:'site-selection',status:'running',params:{distance,units:'m'},startedAt:new Date().toISOString(),updatedAt:new Date().toISOString(),steps:[],outputs:[],inputVersions:[]};
  const persist=()=>{receipt.updatedAt=new Date().toISOString();atomic(path.join(runDir,'run.json'),receipt);};
  persist();
  if(commandPath)fs.writeFileSync(commandPath,JSON.stringify({runId,fingerprint}),{flag:'wx'});
  try {
    const snapshot=snapshotGISInputs(root,runDir,{parcels,avoidance});receipt.inputVersions=snapshot.versions;persist();
    const buffer=await runGISOperation({root,python,request:{op:'buffer',inputs:{layer:snapshot.bindings.avoidance},params:{distance,dissolve:true,keep_projected:true},output:'avoidance_buffer',saveAs:'pred_results/avoidance_buffer.gpkg'}});
    receipt.steps.push({name:'buffer',runId:buffer.runId,status:buffer.status});persist();
    if(buffer.status!=='completed')throw new Error(buffer.error || '缓冲区计算失败');
    const metricBuffer=buffer.outputs.find(output=>output.name==='avoidance_buffer.gpkg');
    const measurementCrs=metricBuffer?.summary?.crs;
    if(!metricBuffer || !measurementCrs)throw new Error('缓冲区没有返回可核对的米制坐标系。');
    receipt.params.measurementCrs=measurementCrs;persist();
    const projected=await runGISOperation({root,python,request:{op:'reproject',inputs:{layer:snapshot.bindings.parcels},params:{target_crs:measurementCrs},output:'analysis_parcels',saveAs:'pred_results/analysis_parcels.gpkg'}});
    receipt.steps.push({name:'project-parcels',runId:projected.runId,status:projected.status});persist();
    if(projected.status!=='completed')throw new Error(projected.error || '地块投影失败');
    const metricParcels=projected.outputs.find(output=>output.name==='analysis_parcels.gpkg');
    if(!metricParcels)throw new Error('地块没有返回米制分析文件。');
    const difference=await runGISOperation({root,python,request:{op:'difference',inputs:{layer:metricParcels.path,overlay:metricBuffer.path},params:{},output:'safe_sites',saveAs:'pred_results/safe_sites.gpkg'}});
    receipt.steps.push({name:'difference',runId:difference.runId,status:difference.status});persist();
    if(difference.status!=='completed')throw new Error(difference.error || '扣除避让区失败');
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
  }catch(error){receipt.status='failed';receipt.error=error instanceof Error?error.message:String(error);}
  persist();atomic(path.join(root,'.earth/gis/workspace.json'),receipt);return receipt;
}
