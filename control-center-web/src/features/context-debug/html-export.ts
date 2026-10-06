import {
  formatJson,
  messagePreview,
  record,
  type DebugContextRecord,
  type DebugContextResponse,
  type DebugModelCall,
  type DebugToolBatch,
  type DebugToolExecution,
} from './model';

const MESSAGE_ROLE_LABELS: Record<string, string> = {
  user: '用户',
  assistant: '助手',
  system: '系统',
  tool: '工具',
  toolresult: '工具结果',
  message: '消息',
};

const TOOL_STATUS_LABELS: Record<string, string> = {
  completed: '已完成',
  succeeded: '已完成',
  success: '已完成',
  running: '进行中',
  pending: '等待中',
  failed: '失败',
  cancelled: '已取消',
  canceled: '已取消',
};

export interface ContextDebugHtmlOptions {
  generatedAtMs?: number;
  response: DebugContextResponse;
  sessionTitle?: string;
  reportScriptSrc?: string;
}

/**
 * Build one self-contained, local-only report of the exact inputs captured at
 * every Provider call. The export deliberately keeps the Provider boundary
 * visible: system prompt, context messages and tool schemas are separate
 * inputs, while providerExchanges shows the final serialized request.
 */
export function buildContextDebugHtml({
  generatedAtMs = Date.now(),
  response,
  sessionTitle = '',
  reportScriptSrc,
}: ContextDebugHtmlOptions): string {
  const context = response.context;
  if (!context) throw new Error('当前回合没有可导出的上下文快照');

  const title = sessionTitle || context.sessionId || '对话记录';
  const toolsById = new Map(context.toolExecutions.map((tool) => [tool.toolCallId, tool]));
  const calls = context.modelCalls.map((call) => renderCall(call, context, toolsById)).join('\n');
  const metadata = [
    ['对话编号', context.sessionId],
    ['回合编号', context.turnId],
    ['模型', modelLabel(context.model)],
    ['记录时间', formatTimestamp(context.capturedAtMs)],
    ['更新时间', formatTimestamp(context.updatedAtMs)],
    ['模型调用', String(context.modelCalls.length)],
    ['工具调用', String(context.toolExecutions.length)],
  ];

  return `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>${escapeHtml(title)} · 上下文报告</title>
<style>${REPORT_CSS}</style>
</head>
<body>
<header class="report-header">
  <div>
    <p class="eyebrow">本机上下文报告</p>
    <h1>${escapeHtml(title)}</h1>
    <p>逐次还原每次模型调用实际收到的系统指令、消息、工具定义与模型服务请求。</p>
  </div>
  <dl>${metadata.map(([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value || '—')}</dd></div>`).join('')}</dl>
</header>
<nav aria-busy="true" class="report-controls">
  <input aria-label="搜索导出内容" disabled id="search" placeholder="搜索消息、工具、错误或模型调用…" type="search">
  <button disabled id="expand" type="button">展开全部</button>
  <button disabled id="collapse" type="button">折叠全部</button>
  <span>导出于 ${escapeHtml(formatTimestamp(generatedAtMs))}</span>
</nav>
<main>
  <section class="legend">
    <strong>装配边界</strong>
    <span><i data-tone="stable"></i>稳定输入</span>
    <span><i data-tone="dynamic"></i>本次增量</span>
    <span><i data-tone="tool"></i>工具执行</span>
    <span><i data-tone="error"></i>失败证据</span>
  </section>
  ${calls || '<p class="empty">当前回合尚无模型调用。</p>'}
  <details class="raw-turn">
    <summary>原始回合记录</summary>
    <pre>${escapeHtml(formatJson(context.raw))}</pre>
  </details>
</main>
${reportScriptSrc ? `<script src="${escapeAttribute(reportScriptSrc)}"></script>` : `<script>${REPORT_SCRIPT}</script>`}
</body>
</html>`;
}

function renderCall(
  call: DebugModelCall,
  context: DebugContextRecord,
  toolsById: Map<string, DebugToolExecution>,
): string {
  const batches = context.toolBatches.filter((batch) => batch.modelCallIndex === call.index);
  const providerContext = call.providerContext;
  const exactProviderContext = Object.keys(providerContext).length > 0;
  const systemPrompt = typeof providerContext.systemPrompt === 'string'
    ? providerContext.systemPrompt
    : context.systemPrompt;
  const contextMessages = Object.hasOwn(providerContext, 'messages')
    ? Array.isArray(providerContext.messages) ? providerContext.messages : []
    : call.contextMessages;
  const toolSchemas = Object.hasOwn(providerContext, 'tools')
    ? Array.isArray(providerContext.tools) ? providerContext.tools : []
    : context.toolSchemas;
  const delta = call.contextDelta;
  const status = call.completedAtMs ? 'completed' : 'running';
  const searchable = [
    `模型调用 ${call.index}`,
    ...contextMessages.map(messagePreview),
    ...batches.flatMap((batch) => batch.toolCallIds.map((id) => toolsById.get(id)?.toolName || id)),
  ].join(' ').toLowerCase();

  return `<article class="model-call" data-search="${escapeAttribute(searchable)}" data-status="${status}">
  <header>
    <span class="call-index">#${call.index}</span>
    <div>
      <h2>模型调用 ${call.index}</h2>
      <p>${escapeHtml(callCaption(call, contextMessages.length))}</p>
    </div>
    <time>${escapeHtml(formatTimestamp(call.capturedAtMs))}</time>
    <span class="status">${status === 'completed' ? '已完成' : '进行中'}</span>
  </header>
  <dl class="delta">
    <div><dt>共同前缀</dt><dd>${delta.commonPrefixMessages} 条</dd></div>
    <div data-tone="dynamic"><dt>新增</dt><dd>+${delta.addedMessageCount}</dd></div>
    <div data-tone="removed"><dt>移除</dt><dd>-${delta.removedMessageCount}</dd></div>
    <div><dt>实际上下文</dt><dd>${contextMessages.length} 条</dd></div>
  </dl>
  <div class="assembly">
    ${exactProviderContext ? '' : '<p class="capture-warning">这条历史记录没有逐次模型服务快照；以下系统指令与工具定义来自回合级记录，不能作为当次实际请求的精确证据。</p>'}
    ${renderTextSection('系统指令 · 本次模型调用', systemPrompt, 'stable')}
    ${renderMessages(contextMessages)}
    ${renderJsonSection(
      `工具定义 · 本次模型调用 ${toolSchemas.length} 个`,
      toolSchemas,
      'stable',
    )}
    ${renderJsonSection('本次上下文增量', delta.addedMessages, 'dynamic')}
    ${renderJsonSection('模型服务最终请求与响应信息', call.providerExchanges, 'provider')}
    ${renderJsonSection('助手原始消息', call.assistantMessage ?? null, 'assistant')}
    ${renderToolBatches(batches, toolsById)}
  </div>
</article>`;
}

function renderMessages(messages: unknown[]): string {
  const items = messages.map((message, index) => {
    const value = record(message);
    const role = String(value.role ?? value.type ?? 'message');
    const kind = messageKind(value);
    return `<details class="message" data-kind="${escapeAttribute(kind)}">
      <summary><span>${escapeHtml(messageRoleLabel(role))}</span><strong>${escapeHtml(messagePreview(message) || '结构化消息')}</strong><small>第 ${index + 1} 条${kind === 'compaction' ? ' · 压缩摘要' : ''}</small></summary>
      <pre>${escapeHtml(formatJson(message))}</pre>
    </details>`;
  }).join('');
  return `<section class="assembly-section" data-tone="messages">
    <header><strong>实际上下文消息</strong><small>${messages.length} 条，顺序与模型服务收到的一致</small></header>
    <div class="message-list">${items || '<p class="empty">没有消息</p>'}</div>
  </section>`;
}

function renderToolBatches(
  batches: DebugToolBatch[],
  toolsById: Map<string, DebugToolExecution>,
): string {
  if (!batches.length) {
    return '<section class="assembly-section" data-tone="tool"><header><strong>工具执行</strong><small>本次调用没有使用工具</small></header></section>';
  }
  const rendered = batches.map((batch) => {
    const tools = batch.toolCallIds.map((id) => toolsById.get(id)).filter(Boolean) as DebugToolExecution[];
    return `<section class="tool-batch" data-mode="${batch.executionMode}" data-status="${batch.status}">
      <header><strong>阶段 ${batch.stage} · ${batch.executionMode === 'parallel' ? `记录为并行批次 ${tools.length} 项` : '记录为串行批次'}</strong><small>${escapeHtml(durationLabel(batch.startedAtMs, batch.endedAtMs))}</small></header>
      ${tools.map(renderTool).join('')}
    </section>`;
  }).join('');
  return `<section class="assembly-section" data-tone="tool"><header><strong>工具执行批次</strong><small>记录模式不等同于已证明的实际时间重叠</small></header>${rendered}</section>`;
}

function renderTool(tool: DebugToolExecution): string {
  const tone = tool.status === 'failed' || tool.isError ? 'error' : 'tool';
  return `<details class="tool" data-tone="${tone}">
    <summary><span>${escapeHtml(tool.toolName)}</span><small>${escapeHtml(durationLabel(tool.startedAtMs, tool.endedAtMs))} · ${escapeHtml(toolStatusLabel(tool.status))}</small></summary>
    <div class="tool-grid">
      <section><h4>参数</h4><pre>${escapeHtml(formatJson(tool.args))}</pre></section>
      <section><h4>结果</h4><pre>${escapeHtml(formatJson(tool.result ?? null))}</pre></section>
    </div>
    ${tool.updates.length ? `<section><h4>流式更新</h4><pre>${escapeHtml(formatJson(tool.updates))}</pre></section>` : ''}
  </details>`;
}

function renderTextSection(title: string, value: string, tone: string): string {
  return `<details class="assembly-section" data-tone="${tone}">
    <summary><strong>${escapeHtml(title)}</strong><small>${value.length.toLocaleString('zh-CN')} 字符</small></summary>
    <pre>${escapeHtml(value || '（未捕获）')}</pre>
  </details>`;
}

function renderJsonSection(title: string, value: unknown, tone: string): string {
  const json = formatJson(value);
  return `<details class="assembly-section" data-tone="${tone}">
    <summary><strong>${escapeHtml(title)}</strong><small>${json.length.toLocaleString('zh-CN')} 字符</small></summary>
    <pre>${escapeHtml(json)}</pre>
  </details>`;
}

function messageKind(value: Record<string, unknown>): string {
  const role = String(value.role ?? '').toLowerCase();
  const type = String(value.type ?? value.customType ?? '').toLowerCase();
  return role.includes('compaction') || type.includes('compaction') ? 'compaction' : 'message';
}

function messageRoleLabel(value: string): string {
  return MESSAGE_ROLE_LABELS[value.toLowerCase()] ?? '消息';
}

function toolStatusLabel(value: string): string {
  return TOOL_STATUS_LABELS[value.toLowerCase()] ?? '状态未知';
}

function modelLabel(model: Record<string, unknown>): string {
  return [model.provider, model.name ?? model.id].map(String).map((value) => value.trim()).filter(Boolean).join(' / ') || '待捕获';
}

function callCaption(call: DebugModelCall, messageCount: number): string {
  const runtime = call.runtimeTurnIndex === undefined ? '' : `执行轮次 ${call.runtimeTurnIndex + 1}`;
  return [runtime, `${call.providerExchanges.length} 次模型服务尝试`, `${messageCount} 条消息`].filter(Boolean).join(' · ');
}

function durationLabel(startedAtMs: number, endedAtMs?: number): string {
  if (!startedAtMs) return endedAtMs ? formatTimestamp(endedAtMs) : '时间未知';
  if (!endedAtMs) return '运行中';
  return `${Math.max(0, endedAtMs - startedAtMs)} 毫秒`;
}

function formatTimestamp(value: number): string {
  if (!value) return '时间未知';
  return new Intl.DateTimeFormat('zh-CN', {
    dateStyle: 'medium',
    timeStyle: 'medium',
  }).format(new Date(value));
}

function escapeHtml(value: string): string {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function escapeAttribute(value: string): string {
  return escapeHtml(value.replace(/\s+/g, ' ').slice(0, 20_000));
}

const REPORT_SCRIPT = String.raw`
document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape' || event.defaultPrevented || event.isComposing || window.parent === window) return;
  event.preventDefault();
  window.parent.postMessage({ type: 'paw:context-report:close' }, '*');
});
const details = () => [...document.querySelectorAll('details')];
document.getElementById('expand').addEventListener('click', () => details().forEach((item) => { item.open = true; }));
document.getElementById('collapse').addEventListener('click', () => details().forEach((item) => { item.open = false; }));
document.getElementById('search').addEventListener('input', (event) => {
  const query = event.target.value.trim().toLowerCase();
  document.querySelectorAll('.model-call').forEach((item) => {
    item.hidden = Boolean(query) && !item.dataset.search.includes(query);
  });
});
document.querySelectorAll('.report-controls button, .report-controls input').forEach((control) => { control.disabled = false; });
document.querySelector('.report-controls').removeAttribute('aria-busy');
`;

const REPORT_CSS = `
:root{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;color:#d8dde7;background:#11141a}
*{box-sizing:border-box}body{margin:0;background:#11141a;color:#d8dde7}button,input{font:inherit}
.report-header{padding:32px clamp(20px,5vw,72px);border-bottom:1px solid #2a303a;background:#161a21;display:grid;grid-template-columns:minmax(0,1fr) minmax(280px,520px);gap:32px}
.eyebrow{color:#78c6b5;font-size:12px;letter-spacing:.12em}.report-header h1{font:700 clamp(24px,4vw,40px) system-ui;margin:8px 0}.report-header p{color:#9da7b4}
.report-header dl{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1px;background:#303743;border:1px solid #303743}.report-header dl div{padding:10px 12px;background:#171b22}.report-header dl div:last-child:nth-child(odd){grid-column:1/-1}.report-header dt{font-size:11px;color:#8995a4}.report-header dd{margin:4px 0 0;overflow-wrap:anywhere}
.report-controls{position:sticky;top:0;z-index:4;padding:12px clamp(20px,5vw,72px);display:flex;gap:8px;align-items:center;background:#11141aee;border-bottom:1px solid #2a303a;backdrop-filter:blur(12px)}.report-controls input{min-width:240px;flex:1;padding:10px 12px;background:#191e26;color:inherit;border:1px solid #37404d}.report-controls button{padding:9px 12px;background:#202731;color:inherit;border:1px solid #3a4554;cursor:pointer}.report-controls span{color:#7f8a98;font-size:12px}.report-controls :disabled{opacity:.55;cursor:wait}
main{width:min(1180px,calc(100% - 32px));margin:24px auto 80px}.legend{display:flex;gap:18px;align-items:center;padding:12px 16px;border:1px solid #2d3540;background:#151920}.legend span{font-size:12px;color:#a7b0bc}.legend i{display:inline-block;width:8px;height:8px;margin-right:6px;background:#78889c}.legend i[data-tone=stable]{background:#78c6b5}.legend i[data-tone=dynamic]{background:#78a9eb}.legend i[data-tone=tool]{background:#b69aea}.legend i[data-tone=error]{background:#e87979}
.model-call{margin-top:18px;border:1px solid #303844;background:#151920;box-shadow:0 14px 36px #0004}.model-call>header{display:grid;grid-template-columns:auto minmax(0,1fr) auto auto;gap:14px;align-items:center;padding:16px 18px;border-bottom:1px solid #303844}.call-index{display:grid;place-items:center;width:42px;height:42px;background:#202833;color:#82d3c0;border:1px solid #3d4c59}.model-call h2{font:700 18px system-ui;margin:0}.model-call header p,.model-call header time{margin:4px 0 0;color:#8f9aa8;font-size:12px}.status{padding:5px 8px;background:#1d3b34;color:#8ad9c6;font-size:12px}
.delta{display:grid;grid-template-columns:repeat(4,1fr);margin:0;border-bottom:1px solid #303844}.delta div{padding:12px 16px;border-right:1px solid #303844}.delta dt{font-size:11px;color:#8793a1}.delta dd{margin:4px 0 0;font-weight:700}.delta [data-tone=dynamic] dd{color:#82b2f2}.delta [data-tone=removed] dd{color:#e98989}
.assembly{padding:14px}.capture-warning{margin:10px 0;padding:12px 14px;border-left:3px solid #e4aa64;background:#2a2118;color:#efc58d}.assembly-section{margin:10px 0;border-left:3px solid #607083;background:#191e26}.assembly-section[data-tone=stable]{border-color:#61b29f}.assembly-section[data-tone=dynamic]{border-color:#6e9fe3}.assembly-section[data-tone=tool]{border-color:#9276c7}.assembly-section>summary,.assembly-section>header{padding:12px 14px;display:flex;justify-content:space-between;gap:16px;cursor:pointer}.assembly-section small{color:#8793a1}.assembly-section>pre,.raw-turn pre{margin:0;padding:16px;border-top:1px solid #303844}
pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:70vh;overflow:auto;color:#cbd3df;background:#12161c;font-size:12px;line-height:1.55}.message-list{padding:0 12px 12px}.message{border-top:1px solid #2e3742}.message summary,.tool summary{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:12px;padding:10px 8px;cursor:pointer}.message summary span{color:#80cbb9}.message summary strong{font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.message[data-kind=compaction]{border-left:3px solid #e4aa64;background:#2a2118}
.tool-batch{margin:0 12px 12px;border:1px solid #3b334b;background:#17151d}.tool-batch>header{display:flex;justify-content:space-between;padding:10px 12px}.tool-batch[data-mode=parallel]>header strong{color:#c8aef4}.tool{border-top:1px solid #302a3b}.tool[data-tone=error]{border-left:3px solid #e16f6f;background:#28191d}.tool-grid{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:#303844}.tool-grid section{background:#151920}.tool h4{padding:0 12px}.tool pre{margin:0;padding:12px}.raw-turn{margin-top:20px;border:1px solid #303844}.raw-turn summary{padding:14px;cursor:pointer}.empty{padding:18px;color:#8c97a5}
@media(max-width:760px){.report-header{grid-template-columns:1fr}.report-controls{flex-wrap:wrap}.report-controls input,.report-controls button,.assembly-section>summary,.message summary,.tool summary,.raw-turn summary{min-height:44px}.report-controls span{width:100%}.delta{grid-template-columns:repeat(2,1fr)}.model-call>header{grid-template-columns:auto 1fr}.model-call>header time,.model-call>header .status{grid-column:2}.tool-grid{grid-template-columns:1fr}}
`;
