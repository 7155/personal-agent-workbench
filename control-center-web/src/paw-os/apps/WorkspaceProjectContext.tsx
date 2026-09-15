import { ArrowUpLeft, FileText, X } from 'lucide-react';
import type { WorkspaceComposerContext } from './workspace-draft';

/** The message payload stays in the send path; the composer shows its human-readable reference. */
export function WorkspaceProjectContext({ context }: { context: WorkspaceComposerContext }) {
  const copy = <><FileText size={16} aria-hidden="true" /><span><strong>{context.label}</strong><small>{context.detail}</small></span></>;
  return <section className="paw-project-context" aria-label="本次消息关联成果">
    {context.onOpen
      ? <button className="paw-project-context__reference" type="button" aria-label={`查看左侧对应结果：${context.label}`} onClick={context.onOpen}>{copy}<em>查看左侧</em><ArrowUpLeft size={16} aria-hidden="true" /></button>
      : <div className="paw-project-context__reference">{copy}</div>}
    <button className="paw-project-context__remove" type="button" aria-label="移除本次项目上下文" onClick={context.onClear}><X size={16} aria-hidden="true" /></button>
  </section>;
}
