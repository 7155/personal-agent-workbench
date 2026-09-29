import { ArrowUpRight, CheckCircle2, FileText } from 'lucide-react';
import { createContext, memo, useContext, useMemo, type ComponentProps, type ReactNode } from 'react';
import ReactMarkdown, { type Components, type ExtraProps } from 'react-markdown';
import remarkMath from 'remark-math';
import { RichTableFrame } from './rich/RichBlockTools';
import { RichMath } from './rich/RichMath';
import { MarkdownImage } from './rich/RichImage';
import './rich/rich-conversation.css';
import remarkGfm from 'remark-gfm';
import {
  evidenceEchoRoute,
  openEvidenceEchoEntity,
  type EvidenceEchoEntity,
} from '@/features/evidence-echo/evidence-echo';
import { openPawOsRoute, usePawOsDesktop } from '@/features/paw-os/surface-context';
import type { AgentBlockRenderProps } from './renderer-contract';
import { CodeContentBlock, StreamingCursor } from './CodeDiffRenderers';
import {
  ProgressiveMarkdown,
  useDeferredStreaming,
  type ProgressiveChunkRenderContext,
} from './progressive-markdown';
import { text } from './renderer-values';
import {
  HtmlOutputPlaceholder,
  InlineHtmlOutput,
  standaloneHtmlSource,
} from './InlineHtmlOutput';

export function TextBlockRenderer({
  block,
  allowTraceDiagnosticReceipt,
  sessionId,
  streamingTail,
}: AgentBlockRenderProps) {
  return (
    <MarkdownBody
      allowTraceDiagnosticReceipt={allowTraceDiagnosticReceipt}
      documentKey={block.id}
      sessionId={sessionId}
      streamingTail={streamingTail}
      text={text(block.data.text ?? block.data.markdown)}
    />
  );
}

export function MarkdownBody({
  allowTraceDiagnosticReceipt = true,
  documentKey = '',
  sessionId = '',
  streamingTail = false,
  text: source,
}: {
  allowTraceDiagnosticReceipt?: boolean;
  documentKey?: string;
  sessionId?: string;
  streamingTail?: boolean;
  text: string;
}) {
  /* Deferred settle latch: the progressive renderer stays mounted for one
     transition render after the stream ends, so the final whole-document
     parse never swaps render modes inside the urgent settle commit. */
  const progressiveMode = useDeferredStreaming(streamingTail);
  const standaloneHtml = useMemo(() => standaloneHtmlSource(source), [source]);
  const traceDiagnostic = useMemo(
    () => allowTraceDiagnosticReceipt ? traceDiagnosticResultReceipt(source) : null,
    [allowTraceDiagnosticReceipt, source],
  );
  if (!source) return null;
  if (traceDiagnostic && !progressiveMode) {
    return <TraceDiagnosticReceipt {...traceDiagnostic} />;
  }
  if (standaloneHtml) {
    return streamingTail
      ? <HtmlOutputPlaceholder />
      : <InlineHtmlOutput content={standaloneHtml} />;
  }
  if (!progressiveMode) {
    return (
      <div className="agent-markdown paw-rich-prose">
        <StableMarkdownFragment sessionId={sessionId} source={source} />
      </div>
    );
  }
  // Live path: the cleanroom scanner freezes the stable prefix chunk-by-chunk
  // (React.memo keeps frozen DOM identical per token batch) while only the
  // active tail re-parses. holdBack is on: the safe-text release scheduler
  // turns batched live-store commits into a paced Markdown-safe reveal — the
  // visible typing motion. It starts fully flushed on mount (restores and
  // Virtuoso remounts never replay) and flushes instantly under reduced
  // motion or a hidden document, so the transcript stays truthful.
  return (
    <ProgressiveMarkdown
      className="agent-markdown paw-rich-prose"
      documentKey={documentKey}
      isStreaming={streamingTail}
      renderChunk={(context) => renderProgressiveChunk(context, sessionId)}
      text={source}
    />
  );
}

const TRACE_DIAGNOSTIC_RESULT_START = '--- TRACE_DIAGNOSTIC_RESULT_V1 ---';
const TRACE_DIAGNOSTIC_RESULT_END = '--- END_TRACE_DIAGNOSTIC_RESULT_V1 ---';
const TRACE_DIAGNOSTIC_REPORT_ID = /\btrace-report:[a-f0-9]{32}\b/iu;

type TraceDiagnosticReceiptData = {
  reportId: string;
  summary: string;
  impact: string;
  repair: string;
  technicalDetail: string;
};

function traceDiagnosticResultReceipt(source: string): TraceDiagnosticReceiptData | null {
  const start = source.lastIndexOf(TRACE_DIAGNOSTIC_RESULT_START);
  if (start < 0) return null;
  const payloadStart = start + TRACE_DIAGNOSTIC_RESULT_START.length;
  const end = source.indexOf(TRACE_DIAGNOSTIC_RESULT_END, payloadStart);
  if (end < 0) return null;
  try {
    const value = JSON.parse(source.slice(payloadStart, end).trim()) as unknown;
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
    const result = value as Record<string, unknown>;
    if (result.schemaVersion !== 'rag-ime.trace-diagnostic-result.v1') return null;
    const summary = typeof result.summary === 'string' ? result.summary.trim() : '';
    if (!summary) return null;
    const hardGates = Array.isArray(result.hardGates) ? result.hardGates : [];
    const failedGate = hardGates
      .map(recordValue)
      .find((gate) => ['failed', 'blocked'].includes(stringValue(gate.status)));
    const findings = Array.isArray(result.findings) ? result.findings.map(recordValue) : [];
    const primaryFinding = findings[0] ?? {};
    return {
      reportId: source.match(TRACE_DIAGNOSTIC_REPORT_ID)?.[0] ?? '',
      summary,
      impact: stringValue(failedGate?.reason)
        || '影响范围还需要在报告中核对，Trace 不会把未知当成事实。',
      repair: stringValue(primaryFinding.candidateRepair)
        || '已经保留证据和修复边界；确认后可以交给独立修复 Agent 处理。',
      technicalDetail: [
        stringValue(primaryFinding.observation),
        stringValue(primaryFinding.conclusion),
        stringValue(primaryFinding.hypothesis),
      ].filter(Boolean).join(' '),
    };
  } catch {
    return null;
  }
}

function TraceDiagnosticReceipt({
  impact,
  reportId,
  repair,
  summary,
  technicalDetail,
}: TraceDiagnosticReceiptData) {
  const desktop = usePawOsDesktop();
  const route = reportId
    ? `/trace-agent?reportId=${encodeURIComponent(reportId)}`
    : '/trace-agent';
  return (
    <div
      aria-label="Trace 诊断结构化结果"
      className="agent-trace-diagnostic-receipt"
      role="status"
    >
      <span aria-hidden="true" className="agent-trace-diagnostic-receipt__mark">
        <CheckCircle2 size={18} />
      </span>
      <div>
        <strong>诊断完成</strong>
        <dl className="agent-trace-diagnostic-receipt__summary">
          <div><dt>发生了什么</dt><dd>{summary}</dd></div>
          <div><dt>对你的影响</dt><dd>{impact}</dd></div>
          <div><dt>Trace 可以怎么修复</dt><dd>{repair}</dd></div>
          <div><dt>下一步</dt><dd>先打开报告核对证据；确认后再交给独立修复 Agent，修复后用新 Trace 复检。</dd></div>
        </dl>
        <details className="agent-trace-diagnostic-receipt__details">
          <summary>技术细节与报告编号</summary>
          {technicalDetail ? <p>{technicalDetail}</p> : null}
          <small>{reportId || '完整结果保存在 Trace Agent 报告列表中'}</small>
        </details>
      </div>
      <button
        className="agent-trace-diagnostic-receipt__action"
        onClick={() => openPawOsRoute(desktop, route)}
        type="button"
      >
        打开网页报告
        <ArrowUpRight aria-hidden="true" size={14} />
      </button>
    </div>
  );
}

function recordValue(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function renderProgressiveChunk(context: ProgressiveChunkRenderContext, sessionId: string) {
  if (!context.active || context.settled) {
    return (
      <StableMarkdownFragment
        deferRichHtml={!context.settled}
        sessionId={sessionId}
        source={context.text}
      />
    );
  }
  return (
    <div className="agent-markdown__active-tail" data-active-tail="">
      {context.openFence ? (
        <>
          {context.openFence.prefix.trim() ? (
            <StableMarkdownFragment
              deferRichHtml
              sessionId={sessionId}
              source={context.openFence.prefix}
            />
          ) : null}
          <StreamingFenceIsland
            language={context.openFence.language}
            value={context.openFence.value}
          />
        </>
      ) : (
        <MarkdownFragment sessionId={sessionId} source={context.text} streamingTail />
      )}
    </div>
  );
}

/**
 * Open-fence fast path: while a fenced block is still unclosed at the
 * streaming tail, its growing body bypasses the Markdown parser entirely and
 * streams into the same code island the parsed path would produce. HTML
 * fences keep the inert placeholder policy they already have while streaming.
 */
function StreamingFenceIsland({
  language,
  value,
}: {
  language: string;
  value: string;
}) {
  const kind = language.toLowerCase();
  if (kind === 'html' || kind === 'htm') return <HtmlOutputPlaceholder />;
  return (
    <CodeContentBlock
      code={value}
      language={language || 'text'}
      streamingTail
    />
  );
}

const StableMarkdownFragment = memo(function StableMarkdownFragment({
  deferRichHtml = false,
  sessionId = '',
  source,
}: {
  deferRichHtml?: boolean;
  sessionId?: string;
  source: string;
}) {
  return <MarkdownFragment deferRichHtml={deferRichHtml} sessionId={sessionId} source={source} />;
});

/* Component identities are stable across token batches. Dynamic context travels
 * through a provider, never through a freshly-created components object. */
type MarkdownRenderContextValue = {
  desktop: ReturnType<typeof usePawOsDesktop>;
  sessionId: string;
  deferRichHtml: boolean;
  streamingTail: boolean;
};
const MarkdownRenderContext = createContext<MarkdownRenderContextValue | null>(null);

function MarkdownLink({ href, children }: { href?: string; children?: ReactNode }) {
  const context = useContext(MarkdownRenderContext);
  if (!context) return <span>{children}</span>;
  const filePath = workspaceFileReference(href);
  if (filePath) {
    const target: EvidenceEchoEntity = { appId: 'files', entityId: filePath, label: fileName(filePath),
      ...(context?.sessionId ? { sessionId: context.sessionId } : {}) };
    return <a aria-label={`打开文件 ${target.label}`} className="agent-markdown__file-link" href={evidenceEchoRoute(target)}
      onClick={event => { event.preventDefault(); openEvidenceEchoEntity(context.desktop, target); }} title={filePath}>
      {children}<FileText aria-hidden="true" size={12} />
    </a>;
  }
  const safe = safeLink(href);
  return safe ? <a href={safe} target={safe.startsWith('http') ? '_blank' : undefined} rel="noopener noreferrer">{children}</a> : <span>{children}</span>;
}

const MARKDOWN_COMPONENTS: Components = {
  a: MarkdownLink,
  p: ({ children, node: _node, ...props }) => <p {...props}>{children}<StreamingCursor active={hasStreamingTail(props)} /></p>,
  li: ({ children, node: _node, ...props }) => <li {...props}>{children}<StreamingCursor active={hasStreamingTail(props)} /></li>,
  td: ({ children, node: _node, ...props }) => <td {...props}>{children}<StreamingCursor active={hasStreamingTail(props)} /></td>,
  h1: ({ children, node: _node, ...props }) => <h1 {...props}>{children}<StreamingCursor active={hasStreamingTail(props)} /></h1>,
  h2: ({ children, node: _node, ...props }) => <h2 {...props}>{children}<StreamingCursor active={hasStreamingTail(props)} /></h2>,
  h3: ({ children, node: _node, ...props }) => <h3 {...props}>{children}<StreamingCursor active={hasStreamingTail(props)} /></h3>,
  code: MarkdownCode,
  pre: ({ children }) => <>{children}</>,
  table: ({ children, node: _node, ...props }) => <RichTableFrame><table {...props}>{children}</table></RichTableFrame>,
  img: ({ src, alt }) => <MarkdownImage src={typeof src === 'string' ? src : undefined} alt={alt} />,
};

function MarkdownCode({ className, children, node: _node, ...props }: ComponentProps<'code'> & ExtraProps) {
  const context = useContext(MarkdownRenderContext);
  const match = /language-([\w-]+)/u.exec(className ?? '');
  const raw = String(children);
  const code = raw.replace(/\n$/u, '');
  const fenced = Boolean(match) || raw.endsWith('\n');
  const tail = hasStreamingTail(props);
  const waiting = Boolean(context?.deferRichHtml || context?.streamingTail || tail);
  if (className?.includes('math-inline')) return <RichMath source={code} inline streaming={Boolean(context?.streamingTail)} />;
  if (className?.includes('math-display')) return <RichMath source={code} streaming={waiting} />;
  if (fenced && match?.[1]?.toLowerCase() === 'html') return waiting ? <HtmlOutputPlaceholder /> : <InlineHtmlOutput content={code} />;
  return fenced ? <CodeContentBlock code={code} language={match?.[1] ?? 'text'} streamingTail={tail || waiting} />
    : <code {...props}>{children}<StreamingCursor active={tail} /></code>;
}

function MarkdownFragment({ deferRichHtml = false, sessionId = '', source, streamingTail = false }: {
  deferRichHtml?: boolean; sessionId?: string; source: string; streamingTail?: boolean;
}) {
  const desktop = usePawOsDesktop();
  const context = useMemo(() => ({ desktop, sessionId, deferRichHtml, streamingTail }), [desktop, sessionId, deferRichHtml, streamingTail]);
  return <MarkdownRenderContext.Provider value={context}>
    <ReactMarkdown skipHtml
      remarkPlugins={streamingTail
        ? [remarkGfm, remarkMath, remarkWorkspaceFileReferences, remarkLiteralHtml, remarkStreamingTail]
        : [remarkGfm, remarkMath, remarkWorkspaceFileReferences, remarkLiteralHtml]}
      components={MARKDOWN_COMPONENTS}>{source}</ReactMarkdown>
  </MarkdownRenderContext.Provider>;
}

type MarkdownAstNode = {
  type?: string;
  value?: string;
  url?: string;
  children?: MarkdownAstNode[];
  data?: { hProperties?: Record<string, unknown> };
};

const WORKSPACE_FILE_REFERENCE = /(?<![\w./:-])(?:\/(?:[\w@+.-]+\/)*|(?:\.\.?\/)?(?:[\w@+.-]+\/)+)?[\w@+.-]+\.(?:md|mdx|markdown|txt|json|jsonl|yaml|yml|toml|ts|tsx|js|jsx|css|scss|html|htm|py|swift|sql|sh|zsh|go|rs|java|rb|c|cpp|h|vue|svelte|xml|csv|tsv|diff|patch)(?![\w.-])/giu;

/** Turn only prose file references into Markdown links. Inline and fenced code
 * are separate AST nodes and therefore stay literal; command-looking lines
 * are also left alone so a shell path argument is not presented as a file. */
function remarkWorkspaceFileReferences() {
  return (tree: MarkdownAstNode) => {
    const visit = (node: MarkdownAstNode) => {
      if (node.type === 'link' || node.type === 'linkReference') return;
      if (!node.children?.length) return;
      node.children = node.children.flatMap((child) => {
        if (child.type !== 'text' || !child.value || commandLikeText(child.value)) {
          visit(child);
          return [child];
        }
        return splitWorkspaceFileReferences(child.value);
      });
    };
    visit(tree);
  };
}

function splitWorkspaceFileReferences(value: string): MarkdownAstNode[] {
  const parts: MarkdownAstNode[] = [];
  let cursor = 0;
  for (const match of value.matchAll(WORKSPACE_FILE_REFERENCE)) {
    const candidate = match[0];
    if (!workspaceFileReference(candidate)) continue;
    const start = match.index ?? 0;
    if (start > cursor) parts.push({ type: 'text', value: value.slice(cursor, start) });
    parts.push({
      type: 'link',
      url: candidate,
      children: [{ type: 'text', value: candidate }],
    });
    cursor = start + candidate.length;
  }
  if (!parts.length) return [{ type: 'text', value }];
  if (cursor < value.length) parts.push({ type: 'text', value: value.slice(cursor) });
  return parts;
}

function commandLikeText(value: string): boolean {
  const commands = '(?:npm|pnpm|yarn|bun|git|python3?|node|npx|bash|sh|zsh|pytest|vitest|vite|cargo|make|curl|docker|swift|xcodebuild)';
  return value.split(/\r?\n/u).some((line) => new RegExp(
    `^(?:[$>#]\\s*)?(?:(?:命令|运行|执行)[:：]?\\s*)?${commands}\\b`,
    'iu',
  ).test(line.trim()));
}

function workspaceFileReference(value: string | undefined): string | undefined {
  if (!value || value.includes('\\') || value.includes('://') || value.startsWith('#')) return undefined;
  const normalized = value.trim().replace(/^\.\//u, '');
  if (!normalized || normalized === '.' || normalized === '..' || !/\.[a-z0-9]{1,12}$/iu.test(normalized)) return undefined;
  if (!WORKSPACE_FILE_REFERENCE.test(normalized) && !/^\/[\w@+./-]+\.[a-z0-9]{1,12}$/iu.test(normalized)) return undefined;
  WORKSPACE_FILE_REFERENCE.lastIndex = 0;
  return value.trim();
}

function fileName(path: string): string {
  return path.split('/').filter(Boolean).at(-1) || path;
}

/**
 * Raw HTML in assistant prose is turned into literal text.
 *
 * react-markdown does not render HTML without rehype-raw, so these nodes were
 * being dropped outright — an agent quoting `<script>alert(1)</script>` in its
 * explanation produced a blank line, and the reader never learned what was
 * quoted. Rewriting the node to text keeps the content visible while React's
 * own escaping keeps it inert; the alternative, rehype-raw, would make the
 * transcript a live HTML renderer, which is exactly what it must never be.
 */
function remarkLiteralHtml() {
  return (tree: MarkdownAstNode) => {
    const visit = (node: MarkdownAstNode) => {
      if (!node.children?.length) return;
      node.children = node.children.map((child) => {
        visit(child);
        return child.type === 'html' ? { type: 'text', value: child.value ?? '' } : child;
      });
    };
    visit(tree);
  };
}

function remarkStreamingTail() {
  return (tree: MarkdownAstNode) => {
    let terminalPath: MarkdownAstNode[] = [];
    const visit = (node: MarkdownAstNode, parents: MarkdownAstNode[]) => {
      const path = [...parents, node];
      if (node.children?.length) {
        node.children.forEach((child) => visit(child, path));
      } else if (['text', 'inlineCode', 'code', 'image', 'break'].includes(node.type ?? '')) {
        terminalPath = path;
      }
    };
    visit(tree, []);
    const target = tailContainer(terminalPath);
    if (!target) return;
    target.data = target.data ?? {};
    target.data.hProperties = {
      ...target.data.hProperties,
      'data-stream-tail': 'true',
    };
  };
}

function tailContainer(path: MarkdownAstNode[]) {
  for (const type of ['code', 'tableCell', 'listItem', 'paragraph', 'heading']) {
    for (let index = path.length - 1; index >= 0; index -= 1) {
      if (path[index]?.type === type) return path[index];
    }
  }
  return undefined;
}

function hasStreamingTail(props: Record<string, unknown>) {
  return props['data-stream-tail'] === true || props['data-stream-tail'] === 'true';
}

function safeLink(value: string | undefined): string | undefined {
  if (!value) return undefined;
  if (value.includes('\\') || value.startsWith('//')) return undefined;
  if (value.startsWith('#/') || value.startsWith('/')) return value;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : undefined;
  } catch {
    return undefined;
  }
}
