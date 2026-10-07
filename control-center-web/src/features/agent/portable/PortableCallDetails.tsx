import { useState } from 'react';

type RecordValue = Record<string, unknown>;
export type PortableCallSnapshot = { progress?: unknown; result?: unknown };
export type PortableToolObservation = { id: string; name: string; operation: string; status: string; arguments?: unknown; result?: unknown; executed?: boolean };
const object = (value: unknown): RecordValue => value && typeof value === 'object' && !Array.isArray(value) ? value as RecordValue : {};
const text = (value: unknown) => typeof value === 'string' ? value : '';
const number = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0;

/** Read only the existing public App journal/Runtime observation. No inferred Tool calls. */
export function observePortableTools(snapshot: PortableCallSnapshot, previous: readonly PortableToolObservation[] = []): PortableToolObservation[] {
  const progress = object(snapshot.progress), result = object(snapshot.result);
  const entries = Array.isArray(object(progress.research).journal) ? object(progress.research).journal as unknown[] : [];
  const tools = new Map(previous.map(tool => [tool.id, tool]));
  const runtime = object(progress.runtime);
  if (text(runtime.toolCallId) && text(runtime.toolName)) {
    const id = text(runtime.toolCallId), previousTool = tools.get(id);
    if (previousTool?.result === undefined) tools.set(id, { id, name: text(runtime.toolName), operation: text(runtime.operation), status: text(runtime.status) });
  }
  for (const raw of entries) {
    const entry = object(raw), args = object(entry.arguments), outcome = object(entry.result), usage = object(entry.usage);
    if (!text(entry.toolCallId)) continue;
    tools.set(text(entry.toolCallId), { id: text(entry.toolCallId), name: 'lab_research', operation: text(outcome.operation) || text(args.op) || text(args.operation),
      status: text(outcome.status), arguments: entry.arguments, result: entry.result,
      ...(typeof usage.executed === 'boolean' ? { executed: usage.executed } : {}) });
  }
  // A final result may retain the journal; older App versions retain it only in progress.
  if (Array.isArray(object(result.research).journal)) return observePortableTools({ progress: { research: result.research } }, [...tools.values()]);
  return [...tools.values()];
}
function toolStatus(tool: PortableToolObservation) {
  if (tool.status === 'running') return '最近进度：进行中';
  if (tool.status === 'budget_exhausted') return '达到本轮上限 · 未执行';
  if (['tool_error', 'invalid_argument', 'failed'].includes(tool.status)) return '调用失败';
  if (tool.executed === false) return '未执行';
  return ['ok', 'completed'].includes(tool.status) ? '已返回' : tool.status || '状态未记录';
}
function NumericFacts({ values }: { values: [string, unknown][] }) {
  return <dl>{values.filter(([, value]) => number(value)).map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{(value as number).toLocaleString()}</dd></div>)}</dl>;
}
/** Usage, budgets and results retain their own units and receipt meanings. */
export function PortableCallDetails({ snapshot, tools }: { snapshot: PortableCallSnapshot; tools: readonly PortableToolObservation[] }) {
  const [limit, setLimit] = useState(6);
  const progress = object(snapshot.progress), result = object(snapshot.result);
  const usage = object(result.usage), knowledge = { ...object(progress.knowledge), ...object(result.knowledge) };
  const budget = object(object(result.research).budget || object(progress.research).budget);
  const model = result.model ?? progress.model;
  const modelRecord = object(model);
  const modelLabel = typeof model === 'string' ? model : [text(modelRecord.provider), text(modelRecord.model)].filter(Boolean).join(' / ');
  const usageValues: [string, unknown][] = [['输入 Token', usage.inputTokens ?? usage.input_tokens ?? usage.prompt_tokens], ['输出 Token', usage.outputTokens ?? usage.output_tokens ?? usage.completion_tokens],
    ['缓存读取 Token', usage.cacheReadTokens ?? usage.cacheRead], ['缓存写入 Token', usage.cacheWriteTokens ?? usage.cacheWrite], ['记录的总 Token', usage.totalTokens ?? usage.total_tokens]];
  return <section className="paw-portable-call-details" aria-label="上下文与实际工具调用">
    <details className="paw-portable-call-details__context"><summary>上下文与用量</summary>
      <p>模型：{modelLabel || '未记录'}{text(modelRecord.thinkingLevel) ? ` · ${text(modelRecord.thinkingLevel)}` : ''}</p>
      <NumericFacts values={usageValues}/>
      {!usageValues.some(([, value]) => number(value)) ? <p>模型用量尚未记录。</p> : null}
      <NumericFacts values={[["资料上下文字符", budget.contextChars ?? knowledge.contextChars], ["资料字符上限", budget.maxContextChars], ["采用的来源窗口", knowledge.usedChunks], ["召回的资料片段", knowledge.retrievedChunks], ["实际执行的工具调用", budget.executedToolCalls], ["工具调用上限", budget.maxToolCalls]]}/>
      <p className="paw-portable-call-details__note">资料字符与模型 Token 分别记录；来源片段保留在证据栏。</p>
    </details>
    {tools.length ? <details className="paw-portable-call-details__tools"><summary>实际工具调用 · {tools.length}</summary>
      {tools.slice(0, limit).map(tool => <details className="paw-portable-call-details__tool" key={tool.id} data-tool-call-id={tool.id}>
        <summary><span>{tool.name}{tool.operation ? ` · ${tool.operation}` : ''}</span><span>{toolStatus(tool)}</span></summary>
        <p className="paw-portable-call-details__identity">调用标识：{tool.id}</p>
        {tool.arguments !== undefined ? <details><summary>调用参数</summary><pre>{JSON.stringify(tool.arguments, null, 2)}</pre></details> : <p>此进度记录未包含调用参数。</p>}
        {tool.result !== undefined ? <details><summary>返回结果</summary><pre>{JSON.stringify(tool.result, null, 2)}</pre></details> : <p>尚未收到返回结果。</p>}
      </details>)}
      {tools.length > limit ? <button type="button" onClick={() => setLimit(value => value + 8)}>显示其余 {tools.length - limit} 次调用</button> : null}
    </details> : null}
  </section>;
}
