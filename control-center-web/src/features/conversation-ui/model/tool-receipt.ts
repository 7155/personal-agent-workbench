import type { ToolCallBlock } from './types';

const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};

/** Only receipt fields count. Never inspect arguments or arbitrary tool output
 * for status words: a file being written may itself contain an error example. */
export function toolExecutionOutcome(payload: Record<string, unknown>): ToolCallBlock['executionOutcome'] {
  const result = record(payload.result);
  for (const receipt of [record(result.details), result, record(payload.details), payload]) {
    if (['applied', 'unknown', 'not_started'].includes(String(receipt.executionOutcome))) {
      return receipt.executionOutcome as ToolCallBlock['executionOutcome'];
    }
  }
  // Older Pi versions threw this exact error without a structured receipt.
  if (typeof payload.error === 'string' && /^Tool gateway request timed out after \d+ms$/.test(payload.error.trim())) return 'unknown';
  return undefined;
}

export function toolReceiptPresentation(block: ToolCallBlock) {
  if (block.executionOutcome === 'unknown') return { status: 'pending' as const, label: '回执待核实', summary: '请求已发出，尚未确认执行结果；请勿重复执行。' };
  if (block.executionOutcome === 'not_started') return { status: 'pending' as const, label: '尚未执行', summary: '请求未发送到工具服务。' };
  const status = block.executionOutcome === 'applied' ? 'success' : block.status;
  const labels = { pending: '等待', running: '正在执行', success: '已完成', error: '失败', cancelled: '已停止' };
  const dispatchLabels = { pending: '等待分派', running: '正在分派', success: '已分派', error: '分派失败', cancelled: '已停止' };
  return { status, label: (block.receiptKind === 'dispatch' ? dispatchLabels : labels)[status], summary: block.summary };
}
