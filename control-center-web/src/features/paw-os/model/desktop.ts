export type PawOsWindowTarget =
  | { kind: 'work-document'; id: string; title: string; subtitle?: string }
  | { kind: 'project'; id: string; title: string; subtitle?: string }
  | { kind: 'task'; id: string; title: string; subtitle?: string; date: string; project?: string }
  | { kind: 'session'; id: string; title: string; subtitle?: string }
  | { kind: 'room'; id: string; title: string; subtitle?: string; panel?: 'focus' | 'progress' | 'governance' }
  | { kind: 'participant'; id: string; title: string; subtitle?: string; roomId: string; sessionId?: string }
  | { kind: 'subagent'; id: string; title: string; subtitle?: string; sessionId: string }
  | {
    kind: 'process-terminal';
    backgroundObserver?: boolean;
    id: string;
    title: string;
    subtitle?: string;
    sessionId: string;
    roomId?: string;
    participantId?: string;
    toolCallId: string;
    runId?: string;
    terminalId?: string;
    command: string;
    cwd?: string;
    runStatus?: 'queued' | 'running' | 'cancelling' | 'completed' | 'failed' | 'cancelled' | 'orphaned';
    roomBound?: boolean;
    roomTurnId?: string;
    exitCode?: number;
  }
  | {
    kind: 'browser-target';
    backgroundObserver?: boolean;
    id: string;
    title: string;
    subtitle?: string;
    sessionId: string;
    roomId?: string;
    participantId?: string;
    toolCallId: string;
    targetId: string;
    provisional?: boolean;
    url?: string;
    tabId?: number;
    commandId?: string;
  }
  | { kind: 'package'; id: string; title: string; subtitle?: string; version: string; resourceCount: number }
  | {
    kind: 'result';
    id: string;
    title: string;
    subtitle?: string;
    resultKind: 'html' | 'web' | 'game' | 'music' | 'image' | 'audio' | 'artifact';
    content?: string;
    source?: string;
    mimeType?: string;
  };
