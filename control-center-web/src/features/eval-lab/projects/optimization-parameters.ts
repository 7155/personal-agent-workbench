import { preferenceWeights, type OptimizationWeights } from './optimization-weights';
import type { LabWorkflowNode } from './project-workflow-types';

export type OptimizationDimension = '模型' | 'Prompt' | 'RAG' | 'Embedding' | '切片' | '重排' | 'Skill' | 'Tool' | 'MCP / Workflow';
type Parameter = { name: string; purpose: string; access: string; advanced?: boolean };
type ParameterGroup = { id: OptimizationDimension; title: string; question: string; measure: string; route: 'knowledge' | 'agent'; parameters: Parameter[] };

export const optimizationObjectives = [
  { id: 'quality', title: '效果优先', target: '提高任务通过率、证据完整性与回答正确性', guardrail: '在约定成本与耗时预算内，不牺牲引用和拒答标准', measure: '任务通过 / 引用正确 / 遗漏', tradeoff: '允许合理增加成本或耗时', x: '成本与耗时', y: '任务质量' },
  { id: 'cost', title: '成本优先', target: '质量达标后，降低每个完整任务的成本', guardrail: '保持基线质量与必过检查，便宜但不达标的候选不保留', measure: '每任务成本 / Token / 调用次数', tradeoff: '先满足质量约束，再比较成本', x: '任务质量', y: '单位成本' },
  { id: 'latency', title: '速度优先', target: '缩短端到端完成时间，改善慢任务', guardrail: '保持基线质量，不以超时、遗漏或未完成换取低耗时', measure: '完成耗时 / P50 / P95', tradeoff: '同时观察成本和完成率', x: '任务质量', y: '完成耗时' },
  { id: 'balanced', title: '均衡优化', target: '在质量、成本和耗时之间选择合适的方案', guardrail: '先满足必过标准和预算上限，再展示各方案的取舍', measure: '质量 × 成本 × 耗时', tradeoff: '不预设无依据的综合分数', x: '成本与耗时', y: '任务质量' },
] as const;
export type OptimizationObjective = typeof optimizationObjectives[number]['id'];
export type OptimizationPreference = { objective: OptimizationObjective; target: string; guardrail: string; weights?: OptimizationWeights };

/** Guidance, not a capability manifest or an automatically applied search space. */
export const optimizationGroups: ParameterGroup[] = [
  { id: '模型', title: '生成模型', question: '质量达标后，是否能更快或更省？', measure: '任务通过、引用正确性、用量、成本与耗时', route: 'agent', parameters: [
    { name: 'Provider / 模型 / 推理强度', purpose: '固定任务、Prompt 与工具，先比较一个模型因素。', access: '项目回答评测' },
    { name: '输出长度 / temperature / top_p', purpose: '输出截断或稳定性有问题时再测；仅使用当前模型支持的参数。', access: '需核对执行器支持', advanced: true },
  ] },
  { id: 'Prompt', title: 'Prompt', question: '要求、引用和工具参数是否说清楚？', measure: '任务通过、遗漏、拒答与参数错误', route: 'agent', parameters: [
    { name: '指令 / 示例 / 输出约束', purpose: '针对一种失败修改，保存前后文本与版本。', access: '项目回答评测' },
    { name: '上下文组织 / 证据顺序', purpose: '证据已命中但回答遗漏时再测。', access: '候选 Prompt', advanced: true },
  ] },
  { id: 'RAG', title: '检索 · RAG', question: '该命中的证据有没有召回？', measure: 'Recall@K、MRR、nDCG 与回答引用；相同 K 才直接比较同名指标', route: 'knowledge', parameters: [
    { name: '检索模式 / Top K', purpose: '比较关键词、语义、混合；Top K 为最终返回条数（1–20）。', access: '可在知识库实验配置' },
    { name: '回答证据预算', purpose: '控制送入回答的证据长度（1,000–60,000 字符），同时看遗漏和成本。', access: '可在知识库实验配置' },
    { name: '分数阈值', purpose: '仅在同一评分方式下校准，不能照搬其他模型的阈值。', access: '知识库实验 · 高级项', advanced: true },
    { name: '混合权重 / RRF / 查询改写', purpose: '只有召回诊断支持时再试，先确认对应检索器与预算。', access: '当前 Lab 表单未接入', advanced: true },
  ] },
  { id: 'Embedding', title: 'Embedding 模型', question: '向量模型是否适合资料语言和查询类型？', measure: '固定切片下的 Recall@K / MRR，以及索引时间与存储', route: 'knowledge', parameters: [
    { name: '模型 / 固定版本', purpose: '固定语料与切片，换模型后建立新索引，保留旧索引作对照。', access: '先在知识库设置配置，再建新索引' },
    { name: 'query / document 前缀', purpose: '先遵循模型要求，避免问题和文档使用错误的编码方式。', access: '知识库 Embedding 设置' },
    { name: '维度', purpose: '仅比较模型支持的维度，兼顾召回和存储；变更后重建索引。', access: '知识库 Embedding 设置', advanced: true },
    { name: '归一化 / 相似度 / 量化', purpose: '需与模型和索引实现匹配；出现性能瓶颈后再评估。', access: '当前 Lab 表单未接入', advanced: true },
  ] },
  { id: '切片', title: '资料切片', question: '答案是否被切断，或被无关内容淹没？', measure: '召回、引用完整性、切片数与索引成本', route: 'knowledge', parameters: [
    { name: '切片策略 / 长度 / 重叠', purpose: '按文档结构选择；长度 200–8,000 字符，重叠小于长度且不超过 2,000。', access: '配置后建立新索引' },
    { name: '父子块 / 邻块扩展 / 上下文标题', purpose: '只在跨块信息缺失时引入，保持来源可定位。', access: '当前 Lab 表单未接入', advanced: true },
  ] },
  { id: '重排', title: '重排 · Rerank', question: '证据已召回，但排序是否不够好？', measure: 'MRR / nDCG、最终回答与额外耗时', route: 'knowledge', parameters: [
    { name: '重排开关 / 模型', purpose: '有独立重排器才启用；模型由知识库运行环境提供。', access: '开关可配置；模型由运行环境配置' },
    { name: '重排候选数 / 最终 Top K', purpose: '先排较宽的候选，再取 Top K；候选数不小于 K、最多 100。', access: '仅开启重排时生效' },
    { name: '单篇截断 / 超时 / fallback', purpose: '长文被截断或延迟过高时诊断，记录实际模型与失败。', access: '当前 Lab 表单未接入', advanced: true },
  ] },
  { id: 'Skill', title: 'Skill 方法', question: '工作方法能否稳定解决目标失败？', measure: '任务通过、步骤与证据覆盖、调用次数', route: 'agent', parameters: [
    { name: 'Skill 版本 / 方法步骤', purpose: '固定模型与评测集，比较方法启用前后。', access: '项目 Agent 候选草稿' },
  ] },
  { id: 'Tool', title: '工具调用', question: '是否选对工具，并传入正确参数？', measure: '工具成功率、参数错误、重试与任务完成', route: 'agent', parameters: [
    { name: '工具集合 / Schema / 返回长度', purpose: '每轮改变一个因素，保留真实调用和结果。', access: '需对应场景执行器' },
  ] },
  { id: 'MCP / Workflow', title: '执行流程', question: '步骤、调用预算和停止条件是否合适？', measure: '任务完成、循环或超时、调用数与耗时', route: 'agent', parameters: [
    { name: '步骤 / 调用预算 / 重试上限', purpose: '固定输入和验收标准，比较完整任务终态。', access: '需对应场景执行器' },
    { name: '并发 / 查询拆分 / 图检索', purpose: '只有串行瓶颈或多跳问题需要时再引入。', access: '按执行器能力规划', advanced: true },
  ] },
];

/** Only structured scope/factors establish which dimensions were tested. */
export function testedDimensions(node: LabWorkflowNode): OptimizationDimension[] {
  if (node.kind !== 'experiment') return [];
  const scope = node.optimization?.scope.toLowerCase() ?? '';
  const factors = (node.factors ?? []).filter((factor) => {
    if (!/^(rerank(?:_?enabled)?|重排(?:开关)?)$/i.test(factor.name)) return true;
    // A stored disabled flag is not evidence that reranking was exercised.
    return [factor.before, factor.after].some((value) => /^(true|1|on|enabled|开启|启用)$/i.test(value.trim()));
  }).map((factor) => factor.name.toLowerCase()).join(' ');
  const values = `${scope} ${factors}`;
  const found = new Set<OptimizationDimension>();
  if (/embedding|嵌入|向量模型/.test(values)) found.add('Embedding');
  if (/chunk|切片|分块/.test(values)) found.add('切片');
  if (/rerank|重排/.test(values)) found.add('重排');
  if (/retrieval|rag|检索|top_?k|contextchars|candidatedepth|threshold|索引|语料/.test(values) || node.ref.kind === 'knowledge_job') found.add('RAG');
  const generation = values.replace(/embedding[_\s-]*model|rerank(?:er)?[_\s-]*model|嵌入模型|向量模型|重排模型/g, '');
  if (/(^|[\s_])model([\s_]|$)|^模型$|生成模型|回答模型/.test(generation.trim()) || (node.factors ?? []).some((factor) => factor.name === '模型')) found.add('模型');
  if (/prompt|提示词|回答规则/.test(values)) found.add('Prompt');
  if (/skill/.test(values) || node.applicationMethodComparison?.changed === true) found.add('Skill');
  if (/tool|工具/.test(values)) found.add('Tool');
  if (/workflow|mcp|工作流|执行流程/.test(values)) found.add('MCP / Workflow');
  return optimizationGroups.map((group) => group.id).filter((id) => found.has(id));
}

export function optimizationDraft(dimension: OptimizationDimension, request: string, node?: LabWorkflowNode, preference?: OptimizationPreference): string {
  const group = optimizationGroups.find((item) => item.id === dimension)!;
  const objective = optimizationObjectives.find((item) => item.id === (preference?.objective ?? 'quality'))!;
  const weights = preferenceWeights(preference?.objective ?? 'quality', preference?.weights);
  return `请准备一轮优化候选草稿。\n优化倾向：${objective.title}\n倾向权重：效果 ${weights.quality}% / 降低成本 ${weights.cost}% / 提升速度 ${weights.latency}%（偏好，不是实测评分；先满足质量底线与预算约束）\n主目标：${preference?.target.trim() || objective.target}\n必须保持：${preference?.guardrail.trim() || objective.guardrail}\n实现手段：${group.title}\n具体要求：${request.trim()}\n${node ? `参考实验：${node.title}\n引用：${node.ref.kind}:${node.ref.id}${node.ref.version === undefined ? '' : ` · v${node.ref.version}`}\n` : ''}先读取项目实际配置和运行记录，明确基线版本、参数改动前后、修改原因与预期影响；缺失的配置标为待补充。\n重点观察：${group.measure}。固定资料版本、评测集与划分、检查标准及其余参数，每轮只改变一个主要因素。\n涉及切片或 Embedding 变更时建立新索引并保留旧索引，不混用不同模型的向量。核对执行器支持后再提供可运行方案；不能将建议当成已应用配置。\n本次先准备草稿，不启动模型调用或索引构建。后续执行沿用项目现有执行器和预算，完成后对照主目标和约束记录改动、指标、失败案例、取舍与保留或放弃理由。`;
}
