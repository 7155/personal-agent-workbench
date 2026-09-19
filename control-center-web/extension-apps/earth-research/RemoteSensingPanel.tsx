import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { Check, ChevronDown, MapPin } from 'lucide-react';
import type { ProjectLayer } from './layer-catalog';

export type RemoteSensingWorkflowPlan = {
  kind: 'classification' | 'ndvi' | 'change' | 'animation' | 'research';
  provider?: 'local' | 'gee';
  region: GeoJSON.Polygon | GeoJSON.MultiPolygon;
  sampleLayerId?: string;
  samplePath?: string;
  sampleRevision?: number;
  classField?: string;
  groupField?: string;
  imagePath?: string;
  collection?: string;
  dateFrom?: string;
  dateTo?: string;
  scale?: number;
  bands?: Array<string | number> | { red: string | number; nir: string | number };
  researchQuestion?: string;
};

export type RemoteSensingRequirement = {
  key: string;
  label: string;
  status: 'provided' | 'missing' | 'preparation_only';
  detail?: string;
};

export type RemoteSensingPreparation = {
  status: 'prepared' | 'needs_input';
  planId?: string;
  requirements?: RemoteSensingRequirement[];
  runnable?: boolean;
  execution?: string;
  path?: string;
};

export type RemoteSensingRunReceipt = { status: string; runId?: string; error?: string; outputs?: Array<{ path: string; kind?: string }> };
export type RemoteSensingSampleDraft = { sampleLayerId?: string; layerName: string; classField: string; classValue: string; features: GeoJSON.Feature[] };
export type RemoteSensingPanelProps = {
  projectLayers: ProjectLayer[];
  selectedFeatures: GeoJSON.Feature[];
  initialKind?: RemoteSensingWorkflowPlan['kind'];
  imageFiles?: Array<{ path: string; name: string }>;
  busy?: boolean;
  onPrepareWorkflow?: (plan: RemoteSensingWorkflowPlan) => Promise<RemoteSensingPreparation | void> | RemoteSensingPreparation | void;
  onRunWorkflow?: (planId: string) => Promise<RemoteSensingRunReceipt | void> | RemoteSensingRunReceipt | void;
  onResearch?: (planId: string) => Promise<void> | void;
  onSaveSamples?: (draft: RemoteSensingSampleDraft) => Promise<void> | void;
  onOpenResult?: (path: string) => Promise<void>;
};

const WORKFLOWS: Array<{ kind: RemoteSensingWorkflowPlan['kind']; name: string; description: string }> = [
  { kind: 'classification', name: '寻找类似地物', description: '先标注目标与非目标样本，在小范围内试算，再到地图上检查漏检和误检。' },
  { kind: 'ndvi', name: '查看植被状况', description: '用红光与近红外影像计算植被指数；结果反映观测状况，变化原因需要其他证据。' },
  { kind: 'change', name: '比较两个时段', description: '比较区域内两个时段的植被指数。先核对季节与有效观测，指数差异不直接等同于地物变化。' },
  { kind: 'animation', name: '制作动画', description: '固定区域和日期，用实际时序影像准备动画；缺少观测的时段不能补成历史影像。' },
  { kind: 'research', name: '区域调研', description: '写清问题，保存区域与资料条件，再交给 Agent 继续检索、判断和制定分析方案。' },
];

const STEPS = ['确定范围', '样本 / 影像', '试算', '查看成果'];

function labelKey(value: unknown): string | undefined {
  if (typeof value === 'string' && value.trim()) return JSON.stringify(['string', value]);
  if (typeof value === 'number' && Number.isFinite(value)) return JSON.stringify(['number', value]);
  return undefined;
}

function labelFeature(feature: GeoJSON.Feature, index: number): string {
  const value = feature.properties?.name ?? feature.id ?? feature.properties?.id;
  return value === undefined || value === null ? `地图区域 ${index + 1}` : String(value);
}

function parseBand(value: string): number | string {
  const trimmed = value.trim();
  if (!trimmed) throw new Error('请填写影像波段。');
  if (/^\d+$/.test(trimmed)) {
    const index = Number(trimmed);
    if (index < 1 || index > 256) throw new Error('波段序号从 1 开始，最大为 256。');
    return index;
  }
  return trimmed;
}

export function RemoteSensingPanel({ projectLayers, selectedFeatures, initialKind = 'classification', imageFiles = [], busy = false, onPrepareWorkflow, onRunWorkflow, onResearch, onSaveSamples, onOpenResult }: RemoteSensingPanelProps) {
  const panelId = useId();
  const [kind, setKind] = useState<RemoteSensingWorkflowPlan['kind']>(initialKind);
  const kindRef = useRef(kind);
  kindRef.current = kind;
  const [ndviProvider, setNdviProvider] = useState<'local' | 'gee'>('local');
  const [regionChoice, setRegionChoice] = useState('');
  const [region, setRegion] = useState<{ geometry: GeoJSON.Polygon | GeoJSON.MultiPolygon; label: string }>();
  const [sampleLayerId, setSampleLayerId] = useState('');
  const [newSampleLayer, setNewSampleLayer] = useState<{ name: string; previousIds: string[] }>();
  const [sampleLayerName, setSampleLayerName] = useState('裂缝训练样本');
  const [classField, setClassField] = useState('class');
  const [classValue, setClassValue] = useState('裂缝');
  const [groupField, setGroupField] = useState('');
  const [imagePath, setImagePath] = useState('');
  const [classificationBands, setClassificationBands] = useState('');
  const [redBand, setRedBand] = useState('');
  const [nirBand, setNirBand] = useState('');
  const [collection, setCollection] = useState('COPERNICUS/S2_SR_HARMONIZED');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [scale, setScale] = useState('10');
  const [researchQuestion, setResearchQuestion] = useState('');
  const [preparation, setPreparation] = useState<RemoteSensingPreparation>();
  const [preparedFor, setPreparedFor] = useState('');
  const [runReceipt, setRunReceipt] = useState<RemoteSensingRunReceipt>();
  const [researchDispatchedPlanId, setResearchDispatchedPlanId] = useState('');
  const [pending, setPending] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const actionLock = useRef(false);
  const disabled = busy || Boolean(pending);
  const workflow = WORKFLOWS.find(item => item.kind === kind)!;
  const provider = kind === 'classification' ? 'local' : kind === 'ndvi' ? ndviProvider : 'gee';
  const local = provider === 'local';
  const polygons = selectedFeatures.filter((feature): feature is GeoJSON.Feature<GeoJSON.Polygon | GeoJSON.MultiPolygon> => feature.geometry?.type === 'Polygon' || feature.geometry?.type === 'MultiPolygon');
  const regionIndex = Math.min(Number(regionChoice) || 0, Math.max(0, polygons.length - 1));
  const regionCandidate = polygons[regionIndex];
  const sampleLayer = projectLayers.find(layer => layer.id === sampleLayerId);
  useEffect(() => { setKind(initialKind); }, [initialKind]);
  useEffect(() => {
    setPreparation(undefined); setPreparedFor(''); setRunReceipt(undefined);
    setResearchDispatchedPlanId(''); setError(''); setNotice('');
  }, [kind]);
  useEffect(() => {
    if (!newSampleLayer) return;
    const matches = projectLayers.filter(layer => layer.name === newSampleLayer.name && !newSampleLayer.previousIds.includes(layer.id));
    if (matches.length === 1) {
      setSampleLayerId(matches[0].id);
      setNewSampleLayer(undefined);
    }
  }, [newSampleLayer, projectLayers]);
  const sampleFields = [...new Set((sampleLayer?.features ?? []).flatMap(feature => Object.keys(feature.properties ?? {})))];
  const eligibleSamples = selectedFeatures.filter(feature => ['Point', 'MultiPoint', 'Polygon', 'MultiPolygon'].includes(feature.geometry?.type) && JSON.stringify(feature.geometry) !== JSON.stringify(region?.geometry));
  const classSummary = useMemo(() => {
    const groups = new Map<string, { label: string; count: number; distinctGroups: Set<string> }>();
    let missing = 0, missingGroups = 0;
    for (const [index, feature] of (sampleLayer?.features ?? []).entries()) {
      const value: unknown = feature.properties?.[classField.trim()];
      const key = labelKey(value);
      if (!key) { missing += 1; continue; }
      const group = groups.get(key) ?? { label: typeof value === 'number' ? `${value}（数值）` : String(value), count: 0, distinctGroups: new Set<string>() };
      const featureId = feature.id !== undefined ? feature.id : feature.properties?.id ?? `feature:${index}`;
      const groupKey = groupField.trim() ? labelKey(feature.properties?.[groupField.trim()]) : labelKey(featureId);
      if (groupKey) group.distinctGroups.add(groupKey);
      else missingGroups += 1;
      group.count += 1; groups.set(key, group);
    }
    return { groups: [...groups.values()], missing, missingGroups };
  }, [sampleLayer, classField, groupField]);
  const fingerprint = JSON.stringify({ kind, provider, region, sampleLayerId: sampleLayer?.id, samplePath: sampleLayer?.path, sampleRevision: sampleLayer?.revision, classField, groupField, imagePath, classificationBands, redBand, nirBand, collection, dateFrom, dateTo, scale, researchQuestion });
  const preparationStale = Boolean(preparation && preparedFor !== fingerprint);
  const currentStep = !region ? 0 : runReceipt?.status === 'completed' && !preparationStale ? 3 : preparation?.status === 'prepared' && !preparationStale ? 2 : 1;

  async function perform(label: string, action: () => Promise<void>) {
    if (actionLock.current || busy) return;
    actionLock.current = true; setPending(label); setError(''); setNotice('');
    try { await action(); }
    catch (reason) { if (kindRef.current === kind) setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { actionLock.current = false; setPending(''); }
  }

  function captureRegion() {
    if (!regionCandidate) return;
    setRegion({ geometry: structuredClone(regionCandidate.geometry), label: labelFeature(regionCandidate, regionIndex) });
    setNotice(kind === 'classification' ? '分析范围已固定，可以继续在地图上选择训练样本。' : '分析范围已固定，继续补充本次任务的材料。');
    setError('');
  }

  async function prepare() {
    if (!region || !onPrepareWorkflow) return;
    await perform('准备分析', async () => {
      if (local && imagePath.trim() && (imagePath.trim().startsWith('/') || imagePath.trim().split(/[\\/]/).includes('..'))) throw new Error('影像路径需位于当前工作区内，例如 data/image.tif。');
      if (!local && (!dateFrom || !dateTo) && kind !== 'research') throw new Error('请填写开始和结束日期。');
      if (!local && dateFrom && dateTo && dateFrom > dateTo) throw new Error('结束日期不能早于开始日期。');
      if (!local && (!Number.isFinite(Number(scale)) || Number(scale) <= 0)) throw new Error('分析分辨率必须是大于 0 的米数。');
      if (kind === 'classification' && !classField.trim()) throw new Error('请填写样本的分类字段。');
      if (kind === 'research' && !researchQuestion.trim()) throw new Error('请先写下研究问题。');
      const plan: RemoteSensingWorkflowPlan = { kind, provider, region: structuredClone(region.geometry) };
      if (local && imagePath.trim()) plan.imagePath = imagePath.trim();
      if (!local) {
        if (collection.trim()) plan.collection = collection.trim();
        if (dateFrom) plan.dateFrom = dateFrom;
        if (dateTo) plan.dateTo = dateTo;
        plan.scale = Number(scale);
      }
      if (kind === 'classification') {
        if (sampleLayer) { plan.sampleLayerId = sampleLayer.id; plan.samplePath = sampleLayer.path; plan.sampleRevision = sampleLayer.revision; }
        plan.classField = classField.trim();
        if (groupField.trim()) plan.groupField = groupField.trim();
        if (classificationBands.trim()) plan.bands = classificationBands.split(',').map(parseBand);
      }
      if (kind === 'ndvi' && (redBand.trim() || nirBand.trim())) plan.bands = { red: parseBand(redBand), nir: parseBand(nirBand) };
      if (kind === 'research') plan.researchQuestion = researchQuestion.trim();
      const result = await onPrepareWorkflow(plan);
      if (kindRef.current !== kind) return;
      setRunReceipt(undefined); setResearchDispatchedPlanId(''); setPreparedFor(fingerprint);
      if (result) setPreparation(result);
      else { setPreparation(undefined); setNotice('准备请求已提交，请查看任务回执。'); }
    });
  }

  async function saveSamples() {
    if (!onSaveSamples || !eligibleSamples.length) return;
    await perform('保存训练样本', async () => {
      if (!classField.trim() || !classValue.trim() || (!sampleLayer && !sampleLayerName.trim())) throw new Error('请填写样本图层名称、分类字段和类别。');
      await onSaveSamples({ sampleLayerId: sampleLayer?.id, layerName: sampleLayer?.name ?? sampleLayerName.trim(), classField: classField.trim(), classValue: classValue.trim(), features: structuredClone(eligibleSamples) });
      if (kindRef.current !== kind) return;
      if (!sampleLayer) setNewSampleLayer({ name: sampleLayerName.trim(), previousIds: projectLayers.map(layer => layer.id) });
      setNotice(`已保存 ${eligibleSamples.length} 个“${classValue.trim()}”样本。请核对样本图层与类别统计。`);
    });
  }

  async function runPrepared() {
    if (!preparation?.planId || preparation.status !== 'prepared' || !preparation.runnable || preparationStale || kind === 'research' || !onRunWorkflow) return;
    await perform('运行分析', async () => {
      const result = await onRunWorkflow(preparation.planId!);
      if (kindRef.current !== kind) return;
      if (result) { setRunReceipt(result); if (result.status === 'failed') setError(result.error ?? '分析失败，请检查运行记录。'); }
      else setNotice('运行请求已提交，请查看运行记录。');
    });
  }

  async function continueResearch() {
    const planId = preparation?.planId;
    if (kind !== 'research' || !planId || preparation.status !== 'prepared' || preparationStale || researchDispatchedPlanId === planId || !onResearch) return;
    await perform('交给 Agent 继续调研', async () => {
      await onResearch(planId);
      if (kindRef.current !== kind) return;
      setResearchDispatchedPlanId(planId);
      setNotice('研究计划已交给当前 Agent，会沿用已保存的区域、问题和证据要求。');
    });
  }

  async function openResult(path: string) {
    if (!onOpenResult) return;
    await perform('打开成果', async () => { await onOpenResult(path); });
  }

  return <section className="earth-remote-sensing" aria-label="遥感分析流程" aria-busy={disabled}>
    <header className="earth-remote-sensing__header">
      <div><h2>{workflow.name}</h2><p>{workflow.description}</p></div>
      <label>分析内容<select aria-label="遥感任务" value={kind} disabled={disabled} onChange={event => setKind(event.target.value as RemoteSensingWorkflowPlan['kind'])}>{WORKFLOWS.map(item => <option value={item.kind} key={item.kind}>{item.name}</option>)}</select></label>
    </header>
    <ol className="earth-remote-sensing__steps" aria-label="分析步骤">
      {STEPS.map((step, index) => <li className="earth-remote-sensing__step" key={step} data-state={index < currentStep ? 'complete' : index === currentStep ? 'current' : 'upcoming'} aria-current={index === currentStep ? 'step' : undefined}><span aria-hidden="true">{index + 1}</span><span>{step}</span></li>)}
    </ol>
    <div className="earth-remote-sensing__body">
      <section className="earth-remote-sensing__stage earth-remote-sensing__region" aria-label="固定分析范围">
        <div className="earth-remote-sensing__section-title"><MapPin size={15} aria-hidden="true" /><h3>确定范围</h3><span>{region ? '已固定' : '尚未选定'}</span></div>
        {region ? <p><strong>{region.label}</strong></p> : <p>先在地图上绘制或选择一个面，再将它固定为本次分析范围。</p>}
        <div className="earth-remote-sensing__region-actions">
          {polygons.length > 1 ? <select aria-label="当前地图分析范围" value={regionIndex} disabled={disabled} onChange={event => setRegionChoice(event.target.value)}>{polygons.map((feature, index) => <option key={index} value={index}>{labelFeature(feature, index)}</option>)}</select> : null}
          <button type="button" disabled={disabled || !regionCandidate} onClick={captureRegion}>{region ? '更新为当前范围' : '使用当前范围'}</button>
          {region ? <small>后续选择样本不会改变这个范围。</small> : null}
        </div>
        {kind === 'classification' ? <p>这个面只限定分析位置，不会把面内所有像元标成目标。</p> : null}
      </section>

      <section className="earth-remote-sensing__stage" aria-label="分析材料">
        <div className="earth-remote-sensing__stage-heading"><h3>样本与影像</h3><p>{kind === 'research' ? '问题必填；日期和影像来源可作为调研线索。' : kind === 'classification' ? '准备带坐标的影像，以及目标和非目标的样本。' : local ? '核对实际影像的波段。普通 RGB 截图不能计算植被指数。' : '选择实际观测的日期范围，数据来源可在进阶设置中调整。'}</p></div>
        <div className="earth-remote-sensing__inputs">
          {kind === 'research' ? <label className="earth-remote-sensing__wide">研究问题<textarea aria-label="遥感研究问题" rows={3} value={researchQuestion} disabled={disabled} onChange={event => setResearchQuestion(event.target.value)} placeholder="例如：2019–2025 年间，这个区域的植被变化与哪些因素相关？" /><small>写清对象、时段、要回答的问题与希望交付的图表。</small></label> : null}
          {kind === 'ndvi' ? <fieldset className="earth-remote-sensing__provider"><legend>影像来源</legend><label><input type="radio" name={`${panelId}-ndvi-provider`} checked={ndviProvider === 'local'} disabled={disabled} onChange={() => setNdviProvider('local')} />本地 GeoTIFF</label><label><input type="radio" name={`${panelId}-ndvi-provider`} checked={ndviProvider === 'gee'} disabled={disabled} onChange={() => setNdviProvider('gee')} />Earth Engine</label></fieldset> : null}
          {local ? <label>工作区内的影像<input aria-label="遥感影像路径" value={imagePath} list={imageFiles.length ? `${panelId}-image-files` : undefined} disabled={disabled} onChange={event => setImagePath(event.target.value)} placeholder="选择影像或输入 data/multiband.tif" />
            {imageFiles.length ? <datalist id={`${panelId}-image-files`}>{imageFiles.map(file => <option key={file.path} value={file.path} label={file.name} />)}</datalist> : null}
            <small>{imageFiles.length ? `可选择工作区内的 ${imageFiles.length} 个影像，也可手填相对路径。` : '可填写工作区内的影像相对路径。'}使用带坐标信息的 GeoTIFF；底图截图不能代替分析影像。</small>
          </label> : <>
            <label>开始日期{kind === 'research' ? '（可选）' : ''}<input aria-label="遥感开始日期" type="date" value={dateFrom} disabled={disabled} onChange={event => setDateFrom(event.target.value)} /></label>
            <label>结束日期{kind === 'research' ? '（可选）' : ''}<input aria-label="遥感结束日期" type="date" value={dateTo} disabled={disabled} onChange={event => setDateTo(event.target.value)} /></label>
          </>}
          {kind === 'classification' ? <label>已标注的样本<select aria-label="训练样本图层" value={sampleLayer?.id ?? ''} disabled={disabled} onChange={event => { setSampleLayerId(event.target.value); setNewSampleLayer(undefined); }}><option value="">新建或选择已标注的图层</option>{projectLayers.filter(layer => layer.geometryTypes.some(type => ['Point', 'MultiPoint', 'Polygon', 'MultiPolygon'].includes(type))).map(layer => <option value={layer.id} key={layer.id}>{layer.name} · v{layer.revision ?? 1}</option>)}</select><small>从“{classField || '未指定'}”字段读取类别；可在进阶设置中调整。</small></label> : null}
          {kind === 'ndvi' ? <>
            <label>红光波段<input aria-label="NDVI 红光波段" value={redBand} disabled={disabled} onChange={event => setRedBand(event.target.value)} placeholder={local ? '例如 3' : '例如 B4'} /></label>
            <label>近红外波段<input aria-label="NDVI 近红外波段" value={nirBand} disabled={disabled} onChange={event => setNirBand(event.target.value)} placeholder={local ? '例如 4' : '例如 B8'} /></label>
          </> : null}
        </div>

        {kind === 'classification' ? <section className="earth-remote-sensing__samples" aria-label="分类样本检查">
          <div className="earth-remote-sensing__section-title"><h4>样本与验证</h4><span>{sampleLayer ? `${sampleLayer.featureCount} 个样本要素` : '尚未选择样本图层'}</span></div>
          <p>至少两类样本，每类需来自至少两个可分开的要素或空间组，分别用于训练与留出验证。同一地块的样本应设置相同分组。</p>
          {classSummary.groups.length ? <div className="earth-remote-sensing__classes">{classSummary.groups.map((group, index) => <span key={index}><strong>{group.label}</strong>{group.count} 个要素 · {group.distinctGroups.size} 组</span>)}</div> : null}
          {sampleLayer ? <>
            <p className="earth-remote-sensing__sample-summary">这里只预检已载入样本的属性；试算还会检查有效像元、样本重叠和训练 / 验证分组。{sampleLayer.features.length < sampleLayer.featureCount ? `当前载入 ${sampleLayer.features.length} / ${sampleLayer.featureCount} 个要素。` : ''}</p>
            {classSummary.groups.length < 2 || classSummary.missing > 0 || classSummary.missingGroups > 0 || classSummary.groups.some(group => group.distinctGroups.size < 2) ? <p className="earth-remote-sensing__warning">
              {classSummary.groups.length < 2 ? '至少需要两类已标注样本。' : ''}
              {classSummary.missing ? `${classSummary.missing} 个要素缺少可用的 ${classField} 类别。` : ''}
              {classSummary.missingGroups ? `${classSummary.missingGroups} 个要素缺少可用的空间分组。` : ''}
              {classSummary.groups.some(group => group.distinctGroups.size < 2) ? '部分类型不足两个有效分组，请补充不同位置的样本。' : ''}
            </p> : null}
          </> : null}
          {onSaveSamples ? <details className="earth-remote-sensing__capture">
            <summary>标注当前地图选择<ChevronDown size={14} aria-hidden="true" /></summary>
            <div className="earth-remote-sensing__sample-form">
              {!sampleLayer ? <label>新样本图层名称<input aria-label="新样本图层名称" value={sampleLayerName} disabled={disabled} onChange={event => setSampleLayerName(event.target.value)} /></label> : null}
              <label>本批样本类别<input aria-label="当前样本类别" value={classValue} disabled={disabled} onChange={event => setClassValue(event.target.value)} placeholder="裂缝 / 非裂缝" /></label>
              <button type="button" disabled={disabled || !eligibleSamples.length || !region} onClick={() => void saveSamples()}>保存 {eligibleSamples.length} 个标注样本</button>
            </div>
            <small>已固定的分析范围会排除在样本之外。选择点或面后，用同一个样本图层分别保存各类别。</small>
          </details> : null}
        </section> : null}

        {kind === 'classification' || !local ? <details className="earth-remote-sensing__advanced">
          <summary>进阶设置<ChevronDown size={14} aria-hidden="true" /></summary>
          <div className="earth-remote-sensing__inputs">
            {kind === 'classification' ? <>
              <label>分类字段<input aria-label="分类字段" value={classField} disabled={disabled} onChange={event => setClassField(event.target.value)} list={`${panelId}-sample-fields`} placeholder="class" /><datalist id={`${panelId}-sample-fields`}>{sampleFields.map(field => <option value={field} key={field} />)}</datalist></label>
              <label>空间分组字段（可选）<input aria-label="空间分组字段" value={groupField} disabled={disabled} onChange={event => setGroupField(event.target.value)} placeholder="site_id" /><small>同一地块或采样区使用同一组，避免训练与验证混用同一处样本；留空时按要素分组。</small></label>
              <label>参与分类的波段（可选）<input aria-label="分类波段" value={classificationBands} disabled={disabled} onChange={event => setClassificationBands(event.target.value)} placeholder="1, 2, 3, 4" /><small>序号从 1 开始，留空使用影像的全部波段。</small></label>
              <p className="earth-remote-sensing__wide">当前本地方法为随机森林像元分类。按要素或空间组留出验证，并在训练与验证像元之间保留一像元间隔；这不能代替独立地区或日期的检验。</p>
            </> : <>
              <label className="earth-remote-sensing__wide">影像集合<input aria-label="遥感影像集合" value={collection} disabled={disabled} onChange={event => setCollection(event.target.value)} /><small>填写 Earth Engine 集合 ID，准备后核对数据来源和脚本。</small></label>
              <label>分辨率（米）<input aria-label="遥感分析分辨率" type="number" min="0.01" step="any" value={scale} disabled={disabled} onChange={event => setScale(event.target.value)} /></label>
            </>}
          </div>
        </details> : null}
      </section>

      <section className="earth-remote-sensing__stage" aria-label="准备与试算">
        <div className="earth-remote-sensing__stage-heading"><h3>试算</h3><p>{kind === 'research' ? '保存问题和资料条件后，交给当前 Agent 继续；此处不生成研究结论。' : kind === 'classification' ? '先在当前小范围检查输入和识别质量。扩大范围前，核对漏检、误检与样本分组。' : '先检查输入，再执行已保存的计划。准备完成不会自动生成结果。'}</p></div>
        <div className="earth-remote-sensing__actions">
          <div><strong>{kind === 'research' ? '研究计划' : local ? '本地计算' : '云端计算'}</strong><small>{kind === 'research' ? '沿用当前区域、问题与证据要求。' : local ? '执行后读取真实结果与验证指标。' : '先保存计划与脚本，再通过已连接的执行环境运行。'}</small></div>
          <button type="button" className="earth-remote-sensing__primary" disabled={disabled || !region || !onPrepareWorkflow} onClick={() => void prepare()}>{pending === '准备分析' ? '正在准备…' : preparationStale ? '重新准备分析' : '准备分析'}</button>
        </div>
        {!onPrepareWorkflow ? <p className="earth-remote-sensing__warning">当前尚未连接分析服务。关联工作区并连接会话后再准备分析。</p> : null}
        {preparation ? <section className="earth-remote-sensing__receipt" aria-label="分析准备回执">
          <div className="earth-remote-sensing__section-title"><h4>{preparation.status === 'needs_input' ? '还需要补充输入' : '分析计划已准备'}</h4>{preparation.status === 'prepared' ? <Check size={16} aria-hidden="true" /> : null}</div>
          {preparationStale ? <p className="earth-remote-sensing__warning">参数或输入版本已改变，请重新准备后再运行。</p> : null}
          <ul>{(preparation.requirements ?? []).map(item => <li key={item.key} data-status={item.status}><strong>{item.label}</strong><span>{item.status === 'provided' ? '已提供' : item.status === 'missing' ? '待补充' : '仅准备计划'}</span>{item.detail ? <small>{item.detail}</small> : null}</li>)}</ul>
          {preparation.path ? <code>{preparation.path}</code> : null}
          {preparation.status === 'prepared' && preparation.runnable && kind !== 'research' && onRunWorkflow ? <button type="button" className="earth-remote-sensing__primary" disabled={disabled || preparationStale || !preparation.planId || runReceipt?.status === 'completed'} onClick={() => void runPrepared()}>{pending === '运行分析' ? '正在运行…' : runReceipt?.status === 'completed' ? '本次分析已完成' : '运行已准备的分析'}</button> : <p>{preparation.status === 'needs_input' ? '补齐上面的输入后，重新准备分析。' : '准备回执已保存；尚未生成分析结果。'}</p>}
          {kind === 'research' && onResearch ? <button type="button" className="earth-remote-sensing__primary" disabled={disabled || preparationStale || preparation.status !== 'prepared' || !preparation.planId || researchDispatchedPlanId === preparation.planId} onClick={() => void continueResearch()}>{pending === '交给 Agent 继续调研' ? '正在交给 Agent…' : researchDispatchedPlanId === preparation.planId ? '已交给 Agent 继续调研' : '交给 Agent 继续调研'}</button> : null}
        </section> : null}
      </section>

      <section className="earth-remote-sensing__stage" aria-label="查看分析成果">
        <div className="earth-remote-sensing__stage-heading"><h3>查看成果</h3></div>
        {runReceipt ? <section className="earth-remote-sensing__receipt" aria-label="遥感运行回执">
          <h4>{runReceipt.status === 'completed' ? '分析已完成' : runReceipt.status === 'failed' ? '分析失败' : '运行回执'}</h4>
          {preparationStale ? <p className="earth-remote-sensing__warning">这里是上一次输入的运行记录，不代表当前参数的结果。</p> : null}
          {runReceipt.runId ? <code>{runReceipt.runId}</code> : null}
          {runReceipt.outputs?.length ? <ul>{runReceipt.outputs.map(output => <li key={output.path}><code>{output.path}</code>{onOpenResult ? <button type="button" aria-label={`打开成果 ${output.path}`} disabled={disabled} onClick={() => void openResult(output.path)}>{pending === '打开成果' ? '正在打开…' : '打开成果'}</button> : null}</li>)}</ul> : null}
          {runReceipt.status === 'completed' ? <p>{kind === 'classification' ? '到地图上复核候选区域，并查看分类映射、留出验证指标和样本分组。像元分类不是裂缝中心线；未识别出目标不代表可以安全通行。' : kind === 'animation' ? '检查实际输出的日期与有效观测。缺测时段不作推断。' : '结合有效观测覆盖、日期和数据来源阅读结果；指数差异的原因仍需其他证据。'}</p> : null}
        </section> : <p className="earth-remote-sensing__result-empty">{researchDispatchedPlanId ? '已交给 Agent 继续调研，请在会话中查看后续证据与成果。这里尚无计算结果。' : '尚无成果。真实执行回执返回后，在这里查看输出；保存计划不等于分析完成。'}</p>}
      </section>
      {notice ? <p className="earth-remote-sensing__notice" role="status">{notice}</p> : null}
      {error ? <p className="earth-remote-sensing__error" role="alert">{error}</p> : null}
    </div>
  </section>;
}
