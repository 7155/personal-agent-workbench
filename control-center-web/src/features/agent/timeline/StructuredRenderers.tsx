import { publicReasoningSummaryText } from './public-reasoning-summary';
import { useId, useState } from 'react';
import {
  Activity,
  Brain,
  CheckCircle2,
  ChevronRight,
  CircleDashed,
  ListChecks,
  ShieldAlert,
  Table2,
  TriangleAlert,
} from 'lucide-react';
import type { CSSProperties, ReactNode } from 'react';
import { Button, Disclosure } from '@/components/primitives';
import type { UiAgentBlock } from '@/contracts/ui-events';
import {
  approvalDecisionView,
  approvalNeedsHumanDecision,
} from '@/contracts/approval-decision';
import { publicAgentErrorText } from '../public-error';
import { ToolStatusMark, toolReceiptStatus } from '@/features/conversation-ui/components/ToolStatusMark';
import { MarkdownBody } from './MarkdownRenderer';
import { RichTableFrame } from './rich/RichBlockTools';
import { SmoothDisclosureReveal } from './SmoothDisclosureReveal';
import { useDisclosureControl } from './disclosure-anchor';
import { publicToolLabel } from './public-tool-result';
import type { AgentBlockRenderProps } from './renderer-contract';
import {
  displayScalar,
  publicStructuredValue,
  record,
  safeLabelValuePairs,
  text,
} from './renderer-values';

export function CardBlockRenderer({ block, sessionId }: AgentBlockRenderProps) {
  const data = block.data;
  const title = text(data.title) || '信息卡片';
  const tone = ['info', 'success', 'warning', 'danger'].includes(text(data.tone))
    ? text(data.tone)
    : 'info';
  const fields = safeLabelValuePairs(data.fields);
  return (
    <section className="agent-rich-card" data-tone={tone} aria-label={title}>
      <header>
        <span className="agent-insert-icon">{statusIcon(tone)}</span>
        <strong>{title}</strong>
      </header>
      {text(data.bodyMarkdown) ? <MarkdownBody sessionId={sessionId} text={text(data.bodyMarkdown)} /> : null}
      {fields.length ? (
        <dl>
          {fields.map((field, index) => (
            <div key={`${field.label}:${index}`}>
              <dt>{field.label}</dt><dd>{field.value}</dd>
            </div>
          ))}
        </dl>
      ) : null}
    </section>
  );
}

export function ChecklistBlockRenderer({ block }: AgentBlockRenderProps) {
  const data = block.data;
  const items = (Array.isArray(data.items) ? data.items : [])
    .map((item, index) => {
      const value = record(item);
      return {
        id: text(value.id) || String(index),
        label: text(value.text ?? value.label) || `项目 ${index + 1}`,
        checked: value.checked === true
          || ['done', 'completed', 'passed'].includes(text(value.status)),
      };
    });
  const [visibleCount, setVisibleCount] = useState(24);
  const visibleItems = items.slice(0, visibleCount);
  const title = text(data.title) || '检查清单';
  const completed = items.filter((item) => item.checked).length;
  const tone = items.length > 0 && completed === items.length ? 'success' : 'warning';
  return (
    <Disclosure
      className="agent-rich-checklist agent-rich-collapsible"
      data-tone={tone}
      defaultOpen={items.length <= 8}
      summary={<>
        <span className="agent-insert-icon"><ListChecks size={16} /></span>
        <span>{title}</span>
        <small>{completed}/{items.length} · 显示 {visibleItems.length}/{items.length}</small>
        <ChevronRight className="agent-rich-collapsible__chevron" size={14} />
      </>}
    >
      {items.length ? (
        <ul aria-label={`${title}明细`}>
          {visibleItems.map((item) => (
            <li key={item.id} data-checked={item.checked}>
              <span aria-hidden="true">
                {item.checked
                  ? <CheckCircle2 size={16} />
                  : <span className="agent-rich-checklist__empty" />}
              </span>
              <span>{item.label}</span>
            </li>
          ))}
        </ul>
      ) : <p>暂无清单项。</p>}
      {visibleItems.length < items.length ? <Button className="agent-rich-load-more" onClick={() => setVisibleCount((count) => Math.min(items.length, count + 24))} size="small" variant="quiet">加载更多（{visibleItems.length}/{items.length}）</Button> : null}
    </Disclosure>
  );
}

export function TableBlockRenderer({ block }: AgentBlockRenderProps) {
  const data = block.data;
  const columns = (Array.isArray(data.columns) ? data.columns : [])
    .map((column, index) => {
      const value = record(column);
      return {
        key: text(value.key) || text(column) || String(index),
        label: text(value.label ?? value.title) || text(column) || `列 ${index + 1}`,
      };
    });
  const rows = Array.isArray(data.rows) ? data.rows : [];
  const [visibleColumnCount, setVisibleColumnCount] = useState(8);
  const [visibleRowCount, setVisibleRowCount] = useState(24);
  const visibleColumns = columns.slice(0, visibleColumnCount);
  const visibleRows = rows.slice(0, visibleRowCount);
  const title = text(data.title ?? data.caption) || '数据表';
  return (
    <Disclosure
      className="agent-rich-table agent-rich-collapsible"
      data-tone="info"
      defaultOpen={rows.length <= 8}
      summary={<>
        <span className="agent-insert-icon"><Table2 size={16} /></span>
        <span>{title}</span>
        <small>显示 {visibleRows.length}/{rows.length} 行 · {visibleColumns.length}/{columns.length} 列</small>
        <ChevronRight className="agent-rich-collapsible__chevron" size={14} />
      </>}
    >
      {columns.length ? (
        <RichTableFrame label={title} hint={`可见 ${visibleRows.length}/${rows.length} 行 · ${visibleColumns.length}/${columns.length} 列`}>
          <table>
            <thead><tr>{visibleColumns.map((column) => <th scope="col" key={column.key}>{column.label}</th>)}</tr></thead>
            <tbody>
              {visibleRows.map((row, rowIndex) => {
                const rowRecord = record(row);
                const rowArray = Array.isArray(row) ? row : [];
                return (
                  <tr key={rowIndex}>
                    {visibleColumns.map((column, columnIndex) => (
                      <TableCell
                        key={column.key}
                        value={rowArray[columnIndex] ?? rowRecord[column.key]}
                      />
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </RichTableFrame>
      ) : <p>表格缺少可展示的列。</p>}
      {visibleRows.length < rows.length || visibleColumns.length < columns.length ? <div className="agent-rich-load-more" role="group" aria-label={`${title}加载更多`}>
        {visibleRows.length < rows.length ? <Button onClick={() => setVisibleRowCount((count) => Math.min(rows.length, count + 24))} size="small" variant="quiet">加载更多行（{visibleRows.length}/{rows.length}）</Button> : null}
        {visibleColumns.length < columns.length ? <Button onClick={() => setVisibleColumnCount((count) => Math.min(columns.length, count + 8))} size="small" variant="quiet">加载更多列（{visibleColumns.length}/{columns.length}）</Button> : null}
      </div> : null}
    </Disclosure>
  );
}

export function StatusBlockRenderer({ block }: AgentBlockRenderProps) {
  const data = block.data;
  const state = text(data.state ?? data.status) || 'recorded';
  const tone = ['failed', 'blocked', 'danger'].includes(state)
    ? 'danger'
    : ['waiting', 'pending', 'paused'].includes(state)
      ? 'warning'
      : ['done', 'completed', 'success'].includes(state) ? 'success' : 'info';
  const title = text(data.title) || '状态更新';
  const fields = safeLabelValuePairs(data.fields);
  return (
    <section className="agent-rich-status" data-state={state} data-tone={tone} aria-label={title}>
      <span className="agent-insert-icon">{statusIcon(tone)}</span>
      <span>
        <strong>{title}</strong>
        <small>{text(data.detail ?? data.summary ?? data.label) || publicStructuredValue(state)}</small>
      </span>
      {fields.length ? (
        <dl>
          {fields.map((field, index) => (
            <div key={`${field.label}:${index}`}><dt>{field.label}</dt><dd>{field.value}</dd></div>
          ))}
        </dl>
      ) : null}
    </section>
  );
}

export function TaskPlanBlockRenderer({ block }: AgentBlockRenderProps) {
  const data = block.data;
  const items = Array.isArray(data.items)
    ? data.items
    : Array.isArray(data.tasks) ? data.tasks : [];
  const completed = items.filter((item) => {
    const state = text(record(item).status);
    return ['done', 'completed', 'passed', 'success'].includes(state);
  }).length;
  return (
    <section className="agent-task-plan" data-tone="project">
      <header>
        <span className="agent-insert-icon"><ListChecks size={16} /></span>
        <strong>{text(data.title) || '任务计划'}</strong>
        {items.length ? <small>{completed}/{items.length}</small> : null}
      </header>
      <ol>
        {items.map((item, index) => {
          const value = record(item);
          const label = text(value.title ?? value.label ?? item);
          return (
            <li key={`${label}-${index}`} data-status={text(value.status)}>
              {label || `步骤 ${index + 1}`}
            </li>
          );
        })}
      </ol>
    </section>
  );
}

export function ApprovalBlockRenderer({
  block,
  onApprovalDecision,
}: AgentBlockRenderProps) {
  const data = block.data;
  const approvalId = text(data.approvalId ?? data.id);
  const hash = text(data.payloadSha256);
  const state = text(data.state);
  const pending = !['approved', 'rejected', 'applied'].includes(state);
  const approvalDecision = approvalDecisionView(data);
  const DecisionIcon = approvalDecision.mode === 'model' ? Brain : ShieldAlert;
  /* One decision per approval: the projection may take a moment to echo the
     new state back, and a second click in that window would submit twice.
     The latch is keyed to the approval itself — React reuses this component
     for whatever block occupies the same position, so instance-only state
     would render a *new* approval as already decided and trap the user. */
  const [latched, setLatched] = useState<{ id: string; decision: 'approved' | 'rejected' } | null>(null);
  const submitted = latched && latched.id === approvalId ? latched.decision : null;
  const decide = (decision: 'approved' | 'rejected') => {
    if (submitted || !onApprovalDecision || !approvalId || !hash) return;
    setLatched({ id: approvalId, decision });
    onApprovalDecision(approvalId, decision, hash);
  };
  const detailText = text(data.detail ?? data.action) || (approvalDecision.mode === 'model' ? '审批模型不能扩大工具、目录或系统权限。' : '');
  return (
    <section className="fx-approval" data-state={submitted ?? (pending ? 'pending' : state || 'pending')}>
      <div className="bd">
        <div className="fx-approval__title">
          <b>{text(data.title ?? data.summary) || (approvalDecision.mode === 'model' ? 'Luna Max 正在评估' : '需要批准')}</b>
          {submitted === 'approved' ? <span className="fx-pill ok"><i aria-hidden="true" />已批准</span>
            : submitted === 'rejected' ? <span className="fx-pill danger"><i aria-hidden="true" />已拒绝</span>
              : pending ? <span className="fx-pill wait"><i aria-hidden="true" />等待你</span> : null}
        </div>
        {detailText ? <p>{detailText}</p> : null}
        {pending && approvalNeedsHumanDecision(data) && onApprovalDecision && approvalId && hash ? (
          <div className="ops">
            <button className="fx-btn primary" disabled={submitted !== null} onClick={() => decide('approved')} type="button">{submitted === 'approved' ? '已批准' : '批准并继续'}</button>
            <button className="fx-btn ghost" disabled={submitted !== null} onClick={() => decide('rejected')} type="button">{submitted === 'rejected' ? '已拒绝' : '拒绝'}</button>
          </div>
        ) : null}
      </div>
    </section>
  );
}

export function ErrorBlockRenderer({ block }: AgentBlockRenderProps) {
  const data = block.data;
  return (
    <div className="agent-inline-notice" data-tone="danger" role="alert">
      <TriangleAlert size={16} />
      <span>{publicAgentErrorText(data.message ?? data.summary)}</span>
    </div>
  );
}

export function ReasoningSummaryBlockRenderer({ block }: AgentBlockRenderProps) {
  if (text(block.data.source) !== 'provider_reasoning_summary') return null;
  const values = Array.isArray(block.data.items) ? block.data.items : [];
  const items = values
    .filter((value): value is string => typeof value === 'string')
    .map(publicReasoningSummaryText)
    .filter(Boolean);
  const [visibleCount, setVisibleCount] = useState(8);
  const visibleItems = items.slice(0, visibleCount);
  const fallback = publicReasoningSummaryText(text(block.data.summary ?? block.data.text ?? block.data.detail));
  if (!items.length && !fallback) return null;
  const state = text(block.data.state ?? block.status) || 'completed';
  return (
    <Disclosure
      className="agent-rich-collapsible agent-reasoning-summary"
      data-tone="info"
      summary={<>
        <span className="agent-insert-icon"><CircleDashed size={16} /></span>
        <span>思考摘要</span>
        <small>{state === 'running' ? `思考中 · 显示 ${visibleItems.length}/${items.length || 1}` : `显示 ${visibleItems.length || 1}/${items.length || 1} 项`}</small>
        <ChevronRight className="agent-rich-collapsible__chevron" size={14} />
      </>}
    >
      {items.length
        ? <ol aria-label="思考摘要明细">{visibleItems.map((item, index) => <li key={`${index}:${item}`}>{item}</li>)}</ol>
        : <p>{fallback}</p>}
      {visibleItems.length < items.length ? <Button className="agent-rich-load-more" onClick={() => setVisibleCount((count) => Math.min(items.length, count + 8))} size="small" variant="quiet">加载更多（{visibleItems.length}/{items.length}）</Button> : null}
    </Disclosure>
  );
}

export function ProgressBlockRenderer({ block }: AgentBlockRenderProps) {
  const data = block.data;
  const state = text(data.state ?? data.status) || 'running';
  const title = text(data.title ?? data.summary) || '处理进度';
  const detail = text(data.detail ?? data.label) || publicStructuredValue(state);
  const percent = typeof data.percent === 'number'
    ? Math.max(0, Math.min(100, Math.round(data.percent)))
    : null;
  const tone = state === 'running' ? 'run' : ['failed', 'blocked'].includes(state) ? 'danger' : state === 'completed' ? 'ok' : 'wait';
  const stateLabel = state === 'running' ? '运行中' : ['failed', 'blocked'].includes(state) ? '失败' : state === 'completed' ? '完成' : state;
  return (
    <div aria-label={title} className="paw-tool-card fx-progress-card" data-state={state}>
      <div className="paw-tool-panel" style={{ paddingTop: 12 }}>
        <div className="fx-progress-head">
          <strong>{title}</strong>
          <span className={`fx-pill ${tone}`}><i aria-hidden="true" />{stateLabel}</span>
        </div>
        {detail ? <div className="fx-progress__detail">{detail}</div> : null}
        {percent === null ? null : (
          <>
            <div
              aria-label={`${title}：${percent}%`}
              aria-valuemax={100}
              aria-valuemin={0}
              aria-valuenow={percent}
              className="fx-track"
              role="progressbar"
            >
              <div
                className="fill"
                style={{ '--fx-progress-scale': percent / 100 } as CSSProperties}
              />
            </div>
            <div className="fx-runrow"><span className="fx-meta">{percent}%</span><span className="fx-meta">真实比例 · 已确认进展</span></div>
          </>
        )}
      </div>
    </div>
  );
}

export function ToolCallBlockRenderer({ block }: AgentBlockRenderProps) {
  return <ToolActivityBlock type="tool_call" block={block} />;
}

export function ToolResultBlockRenderer({ block }: AgentBlockRenderProps) {
  return <ToolActivityBlock type="tool_result" block={block} />;
}

function ToolActivityBlock({
  block,
  type,
}: {
  block: UiAgentBlock;
  type: 'tool_call' | 'tool_result';
}) {
  const data = block.data;
  const toolId = text(data.toolName ?? data.toolId ?? data.name);
  const label = publicToolLabel(toolId);
  const status = text(data.status) || block.status;
  const receipt = toolReceiptStatus(status === 'blocked' ? 'failed' : status, text(data.settledByTurnStatus));
  const statusLabel = receipt === 'cancelled' ? '已停止' : receipt === 'error' ? '失败'
    : receipt === 'success' ? '已完成' : receipt === 'running' ? '进行中'
    : status === 'waiting' ? '等待确认' : status === 'paused' ? '已暂停'
    : ['pending', 'queued'].includes(status) ? '等待开始' : '状态待确认';
  const tone = receipt === 'error' ? 'danger' : receipt === 'success' ? 'success'
    : receipt === 'running' ? 'info' : 'warning';
  // Match ordinary tool receipts: show status in the row, but let the reader
  // decide when to inspect details. Progress must not reopen a closed row.
  const disclosure = useDisclosureControl();
  const [presence, setPresence] = useState(disclosure.open);
  const summary = text(data.summary ?? data.title)
    || `${label}${type === 'tool_result' ? '结果' : ''}`;
  return (
    <details
      className="agent-tool-activity agent-structured-block"
      data-state={status}
      data-tone={tone}
      open={disclosure.open || presence}
    >
      <summary {...disclosure.summaryProps}>
        <span className="agent-insert-icon">
          <ToolStatusMark status={receipt} />
        </span>
        <span>{summary}</span>
        <small>{statusLabel}</small>
        <ChevronRight className="agent-rich-collapsible__chevron" size={14} />
      </summary>
      <SmoothDisclosureReveal id={disclosure.contentId} onPresenceChange={setPresence} open={disclosure.open}>
        <SafeFieldList ariaLabel={`${summary}明细`} data={{ ...data, status: statusLabel }} />
      </SmoothDisclosureReveal>
    </details>
  );
}

function StructuredSummaryBlock({
  type,
  data,
}: {
  type: UiAgentBlock['type'];
  data: Record<string, unknown>;
}) {
  const label = text(data.summary ?? data.title ?? data.label) || structuredLabel(type);
  return (
    <Disclosure className="agent-structured-block" data-tone="info" summary={<>
        <span>{label}</span>
        <ChevronRight className="agent-rich-collapsible__chevron" size={14} />
      </>}>
      <SafeFieldList ariaLabel={`${label}明细`} data={data} />
    </Disclosure>
  );
}

export function SafeFieldList({
  ariaLabel,
  contentId,
  data,
}: {
  ariaLabel?: string;
  contentId?: string;
  data: Record<string, unknown>;
}) {
  const allowed = [
    'query',
    'status',
    'resultCount',
    'books',
    'recentItems',
    'completed',
    'artifacts',
    'risk',
  ];
  const entries = allowed
    .filter((key) => Object.hasOwn(data, key))
    .map((key) => [key, safeFieldValue(data[key])] as const)
    .filter((entry) => entry[1]);
  if (entries.length === 0) return <p aria-label={ariaLabel} id={contentId}>暂无可展示的结构化明细。</p>;
  return (
    <dl aria-label={ariaLabel} className="agent-safe-fields" id={contentId}>
      {entries.map(([key, value]) => (
        <div key={key}><dt>{fieldLabel(key)}</dt><dd>{value}</dd></div>
      ))}
    </dl>
  );
}

function structuredLabel(type: UiAgentBlock['type']): string {
  return type === 'reasoning_summary' ? '处理说明' : '进度';
}

function fieldLabel(key: string): string {
  return ({
    query: '查询',
    status: '状态',
    resultCount: '结果数',
    books: '工具书',
    recentItems: '近期记录',
    completed: '已完成',
    artifacts: '产物',
    risk: '确认级别',
  } as Record<string, string>)[key] ?? key;
}

function safeFieldValue(value: unknown): string {
  if (typeof value === 'string') return publicStructuredValue(value);
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (Array.isArray(value)) {
    return value
      .filter((item) => ['string', 'number', 'boolean'].includes(typeof item))
      .map((item) => typeof item === 'string' ? publicStructuredValue(item) : String(item))
      .join('、');
  }
  return '';
}

export function BlockedMedia({
  detail,
  icon,
  label,
}: {
  detail?: string;
  icon: ReactNode;
  label: string;
}) {
  return (
    <div className="agent-inline-notice agent-media-error" data-tone="warning" role="status">
      {icon}
      <span>
        <strong>{label}</strong>
        {detail ? <small>{detail}</small> : null}
      </span>
    </div>
  );
}

function statusIcon(tone: string): ReactNode {
  if (tone === 'success') return <CheckCircle2 size={16} />;
  if (tone === 'danger' || tone === 'warning') return <TriangleAlert size={16} />;
  return <Activity size={16} />;
}

function TableCell({ value }: { value: unknown }) {
  const label = displayScalar(value);
  const tone = tableCellTone(label);
  return (
    <td data-tone={tone || undefined}>
      {tone ? <span>{label}</span> : label}
    </td>
  );
}

function tableCellTone(value: string): 'success' | 'warning' | 'danger' | 'info' | null {
  const normalized = value.trim().toLowerCase();
  if ([
    '已完成', '完成', '通过', '已通过', '成功', '已覆盖', '健康',
    'completed', 'done', 'passed', 'success', 'healthy',
  ].includes(normalized)) return 'success';
  if ([
    '需确认', '待确认', '等待', '等待中', '待处理', '待审批',
    'pending', 'waiting', 'needs review',
  ].includes(normalized)) return 'warning';
  if ([
    '失败', '错误', '阻塞', '已阻塞', '不健康',
    'failed', 'error', 'blocked', 'unhealthy',
  ].includes(normalized)) return 'danger';
  if ([
    '进行中', '处理中', '运行中',
    'running', 'in progress', 'processing',
  ].includes(normalized)) return 'info';
  return null;
}
