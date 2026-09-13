export type WorkspaceDraftRequest = { id: number; text: string; contextKey?: string };
export type WorkspaceComposerContext = { label: string; detail: string; text: string; onClear: () => void; items?:Array<{id:string;label:string;onRemove:()=>void}> };
export function messageWithWorkspaceContext(message:string,context?:WorkspaceComposerContext):string {
  if(!context || message.trim().startsWith('/')) return message;
  return `${message.trim()}\n\n地图上下文：${context.label} · ${context.detail}\n\`\`\`geojson\n${context.text}\n\`\`\``;
}
export function applyWorkspaceDraft(current: string, request: WorkspaceDraftRequest): string {
  if (!request.contextKey) return request.text;
  const start = `【${request.contextKey}上下文】`;
  const end = `【${request.contextKey}上下文结束】`;
  const block = request.text ? `${start}\n${request.text}\n${end}` : '';
  const left = current.indexOf(start);
  const right = left < 0 ? -1 : current.indexOf(end, left + start.length);
  if (left >= 0 && right >= 0) return current.slice(0, left) + block + current.slice(right + end.length);
  return block ? `${current}${current.trim() ? '\n\n' : ''}${block}` : current;
}
