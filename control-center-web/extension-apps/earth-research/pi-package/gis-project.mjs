import fs from 'node:fs';
import path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';

function directory(root) {
  root = fs.realpathSync(root);
  const dir = path.join(root,'.earth','layers');
  for (const part of [path.join(root,'.earth'),dir]) {
    if (fs.existsSync(part) && fs.lstatSync(part).isSymbolicLink()) throw new Error('Project data cannot use a symlink.');
    fs.mkdirSync(part,{recursive:true});
  }
  return dir;
}
function readCatalog(dir) {
  const file = path.join(dir,'catalog.json');
  if (!fs.existsSync(file)) return {schemaVersion:'earth.spatial-layer-catalog.v1',projectId:randomUUID(),layers:[]};
  if (fs.lstatSync(file).isSymbolicLink()) throw new Error('Catalog cannot be a symlink.');
  const data = JSON.parse(fs.readFileSync(file,'utf8'));
  if (!Array.isArray(data.layers)) throw new Error('Invalid project layer catalog.');
  return {...data,projectId:data.projectId || randomUUID()};
}
function atomic(file, data) {
  const temporary = `${file}.${randomUUID()}.tmp`;
  fs.writeFileSync(temporary,JSON.stringify(data,null,2),{flag:'wx',mode:0o600});
  fs.renameSync(temporary,file);
}
export function saveProjectLayer({root,layerId,expectedRevision,name,features,commandId,source}) {
  const dir = directory(root), lock = path.join(dir,'.write-lock');
  try { fs.mkdirSync(lock); } catch { throw new Error('项目正在保存，请稍后重试；没有覆盖现有版本。'); }
  let revisionFile;
  try {
    const catalog = readCatalog(dir);
    const commandHash=createHash('sha256').update(JSON.stringify({layerId,expectedRevision,name,features,source})).digest('hex');
    const previousCommand=catalog.layers.find(item=>item.lastCommandId===commandId && commandId);
    if(previousCommand) {if(previousCommand.lastCommandHash!==commandHash)throw new Error('同一命令 ID 的内容不能改变。');const file=path.join(fs.realpathSync(root),previousCommand.path);return {status:'completed',projectId:catalog.projectId,layer:{...previousCommand,features:JSON.parse(fs.readFileSync(file,'utf8')).features}};}
    const receiptPath = commandId && /^[A-Za-z0-9_-]{1,100}$/.test(commandId) ? path.join(dir,`command-${commandId}.json`) : null;
    if (commandId && !receiptPath) throw new Error('Invalid command ID.');
    if (receiptPath && fs.existsSync(receiptPath)) {const prior=JSON.parse(fs.readFileSync(receiptPath,'utf8'));if(prior.layer.lastCommandHash!==commandHash)throw new Error('同一命令 ID 的内容不能改变。');return prior;}
    const existing = layerId ? catalog.layers.find(item=>item.id===layerId) : undefined;
    if (layerId && !existing) throw new Error('项目中没有这个图层。');
    if (existing && (existing.revision ?? 1) !== expectedRevision) throw new Error('图层已有新版本，请重新载入后再编辑；本次草稿没有覆盖它。');
    const cleanName = String(name || '').trim();
    if (!cleanName || cleanName.length > 160) throw new Error('请填写有效的图层名称。');
    if (!Array.isArray(features)) throw new Error('Expected a feature array.');
    const id = existing?.id ?? `layer:${randomUUID()}`, revision = (existing ? existing.revision ?? 1 : 0)+1;
    const ids = new Set();
    const stableFeatures = features.map(feature => {
      if (feature?.type !== 'Feature' || !feature.geometry || !['Point','MultiPoint','LineString','MultiLineString','Polygon','MultiPolygon'].includes(feature.geometry.type)) throw new Error('Unsupported or missing geometry.');
      const cloned = structuredClone(feature); delete cloned.pawLayerId; delete cloned.pawRevision;
      cloned.id ??= randomUUID();
      if (!['string','number'].includes(typeof cloned.id)) throw new Error('Invalid feature ID.');
      const key = JSON.stringify(cloned.id); if(ids.has(key)) throw new Error('一个图层不能有重复的要素 ID。'); ids.add(key);
      return cloned;
    });
    // Immutable snapshots are authoritative. Catalog switches only after a complete readback.
    const filename = `${id.replace(/[^A-Za-z0-9_-]/g,'_')}-v${revision}-${randomUUID().slice(0,8)}.geojson`;
    revisionFile = path.join(dir,filename);
    const collection = {type:'FeatureCollection',features:stableFeatures};
    fs.writeFileSync(revisionFile,JSON.stringify(collection,null,2),{flag:'wx',mode:0o600});
    const readback = JSON.parse(fs.readFileSync(revisionFile,'utf8'));
    if (JSON.stringify(readback)!==JSON.stringify(collection)) throw new Error('Saved layer readback failed.');
    const relative = path.relative(fs.realpathSync(root),revisionFile);
    const history = [...new Set([...(existing?.history ?? []),...(existing?.path ? [existing.path] : []),relative])];
    const layer = {id,lastCommandId:commandId ?? null,lastCommandHash:commandHash,name:cleanName,path:relative,format:'geojson',featureCount:stableFeatures.length,geometryTypes:[...new Set(stableFeatures.map(x=>x.geometry.type))],crs:'EPSG:4326',revision,history,visible:existing?.visible !== false,updatedAt:new Date().toISOString(),source:source ?? existing?.source ?? null};
    const next = {...catalog,updatedAt:layer.updatedAt,layers:[...catalog.layers.filter(x=>x.id!==id),layer]};
    atomic(path.join(dir,'catalog.json'),next);
    revisionFile = undefined;
    const result = {status:'completed',projectId:next.projectId,layer:{...layer,features:stableFeatures}};
    if(receiptPath) atomic(receiptPath,result);
    return result;
  } finally {
    if(revisionFile) fs.rmSync(revisionFile,{force:true});
    fs.rmdirSync(lock);
  }
}

export function updateProjectLayerMetadata({root,layerId,expectedRevision,visible,remove=false}) {
 const dir=directory(root),lock=path.join(dir,'.write-lock');
 try{fs.mkdirSync(lock);}catch{throw new Error('项目正在保存，请稍后重试。');}
 try{
  const catalog=readCatalog(dir),layer=catalog.layers.find(item=>item.id===layerId);
  if(!layer)throw new Error('图层不存在。');
  if((layer.revision ?? 1)!==expectedRevision)throw new Error('图层已有新版本，请重新载入。');
  const layers=remove?catalog.layers.filter(item=>item.id!==layerId):catalog.layers.map(item=>item.id===layerId?{...item,visible:visible!==false}:item);
  atomic(path.join(dir,'catalog.json'),{...catalog,layers,updatedAt:new Date().toISOString()});
  return {status:'completed',projectId:catalog.projectId,layers};
 }finally{fs.rmdirSync(lock);}
}
