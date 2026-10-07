import type { AgentProjectionState } from '@/contracts/agent-reducer';
import type { ComposerAttachment, SessionSummary } from '@/features/agent/types';
import type { ControlTransport } from '@/platform/transport';

export type ScreenCapture = {
  dataUrl: string; mimeType: 'image/png'; pixelWidth: number; pixelHeight: number;
  sourceAppBundleId: string; capturedAtMs: number;
  sourceSessionId?: string;
  creationRequestId?: string;
};
export type ScreenContext = Pick<ScreenCapture, 'sourceAppBundleId' | 'capturedAtMs'> & { mediaId: string };
export type ScreenAssistantHost = {
  getCapture(): Promise<ScreenCapture>;
  getConversation?(): Promise<ScreenConversation>;
  rememberConversation?(conversation: ScreenConversation): Promise<void>;
  capture(options?: { sourceSessionId?: string }): Promise<boolean>;
  openSession(sessionId: string): Promise<void>;
  saveNote(note: { body: string; sessionId: string }): Promise<{ saved: boolean; name?: string }>;
};
declare global { interface Window { pawScreenAssistant?: ScreenAssistantHost } }

export const screenActions = [
  { id: 'translate', label: '翻译', draft: '把选区中的文字翻译成中文，保留段落、数字和专有名词。' },
  { id: 'explain', label: '解释', draft: '解释这片选区的主要内容和关键术语，区分原文内容与补充解释。' },
  { id: 'note', label: '整理笔记', draft: '把选区内容整理成笔记，保留关键结论和来源，区分原文与补充解释。' },
  { id: 'act', label: '操作电脑', draft: '我想在当前应用中完成以下操作：' },
] as const;

export type ScreenConversation = { session?: SessionSummary; attachments?: ComposerAttachment[] };
export type CapturePreparation = ScreenConversation & { capture?: ScreenCapture; restored?: boolean };

// Retain each completed step across a retry: a failed media upload must not
// create another Session, and React StrictMode must not import twice.
export async function prepareScreenConversation(transport: ControlTransport, host: ScreenAssistantHost, state: CapturePreparation) {
  state.capture ??= await host.getCapture();
  const capture = state.capture;
  if (!/^data:image\/png;base64,[A-Za-z0-9+/]+=*$/.test(capture.dataUrl) || capture.dataUrl.length > 28_000_000) throw new Error('选区图片不可用，请重新框选。');
  if (!state.session) {
    const remembered = await host.getConversation?.();
    if (remembered?.session) {
      state.session = remembered.session;
      state.attachments = remembered.attachments;
      state.restored = true;
    }
  }
  if (!state.session) {
    if (!capture.sourceSessionId && transport.capabilities) {
      const capabilities = await transport.capabilities();
      if (capabilities.routeIds?.includes('agent.coordinator.ensure')) {
        const source = await transport.request<{ sourceSessionId: string }>({ pathId: 'agent.coordinator.ensure', body: {} });
        if (!source.sourceSessionId) throw new Error('没有取得星伴对话，请重试连接。');
        capture.sourceSessionId = source.sourceSessionId;
      }
    }
    if (capture.sourceSessionId) {
      if (!capture.creationRequestId) throw new Error('缺少原框选请求身份，请重新框选。');
      const response = await transport.request<{ kind: string; target: SessionSummary }>({
        pathId: 'agent.coordinator.command', body: {
          sourceSessionId: capture.sourceSessionId, action: 'create_session', clientRequestId: capture.creationRequestId,
          input: { purpose: 'screen_capture', title: '屏幕选区', task: '围绕用户框选的屏幕内容对话；根据用户本次选择翻译、解释、整理笔记或操作电脑。' },
        },
      });
      if (response.kind !== 'session' || !response.target?.id) throw new Error('没有取得屏幕会话，请核对原请求。');
      state.session = response.target;
    } else {
      const response = await transport.request<{ session: SessionSummary }>({
        pathId: 'agent.sessions.create', body: { title: '选区对话', mode: 'assistant' },
      });
      if (!response.session?.id) throw new Error('没有取得会话，请重试。');
      state.session = response.session;
    }
  }
  // The popup host keeps only the existing Session/managed-media receipts in
  // memory. A renderer reload resumes those owners instead of creating a fork.
  await host.rememberConversation?.({ session: state.session });
  if (!state.attachments) {
    if (!transport.pasteImages) throw new Error('当前连接不支持图片附件。');
    const binary = atob(capture.dataUrl.slice(capture.dataUrl.indexOf(',') + 1));
    const file = new File([Uint8Array.from(binary, (char) => char.charCodeAt(0))], '屏幕选区.png', { type: 'image/png' });
    const imported = await transport.pasteImages({ sessionId: state.session.id, files: [file], maxFiles: 1 });
    if (imported.length !== 1) throw new Error('选区附件未导入，请重试。');
    state.attachments = imported.map((item) => ({ ...item, source: 'picker', previewFile: file }));
  }
  await host.rememberConversation?.({ session: state.session, attachments: state.attachments.map(({ id, name, mimeType, byteSize, source }) => ({ id, name, mimeType, byteSize, source })) });
  let initialAttachments = state.attachments;
  if (state.restored) {
    const snapshot = await transport.request<{ items?: ScreenMessage[]; messages?: ScreenMessage[] }>({
      pathId: 'agent.session.snapshot', params: { sessionId: state.session.id }, query: { view: 'full' },
    });
    if ((snapshot.items ?? snapshot.messages ?? []).some((message) =>
      message.role === 'user' && message.status === 'completed' && message.attachments?.includes(state.attachments![0].id))) {
      initialAttachments = [];
    }
  }
  return { capture, session: state.session, attachments: state.attachments, initialAttachments };
}

type ScreenMessage = { role: string; status: string; attachments: string[] };

export function screenContextForMessage(context: ScreenContext | undefined, attachmentIds: string[], projection?: AgentProjectionState): ScreenContext | undefined {
  if (!context) return undefined;
  const delivered = projection?.messageOrder.some((id) => {
    const message = projection.messagesById[id];
    return message?.role === 'user' && message.status === 'completed' && message.attachments.includes(context.mediaId);
  });
  return attachmentIds.includes(context.mediaId) || delivered ? context : undefined;
}

export function latestCompletedScreenAnswer(projection?: AgentProjectionState): string {
  if (!projection) return '';
  for (const id of [...projection.messageOrder].reverse()) {
    const message = projection.messagesById[id];
    if (message?.role !== 'assistant' || message.status !== 'completed') continue;
    const body = message.blocks.filter((block) => block.type === 'text').map((block) => String(block.data.text || '')).join('\n\n').trim();
    if (body) return body;
  }
  return '';
}

export function screenNoteBody(answer: string, capture: ScreenCapture, sessionId: string): string {
  return `${answer}\n\n---\n来源：用户框选的屏幕内容\n${capture.sourceAppBundleId ? `触发时应用：${capture.sourceAppBundleId}\n` : ''}采集时间：${new Date(capture.capturedAtMs).toISOString()}\nPAW 会话：${sessionId}\n`;
}
