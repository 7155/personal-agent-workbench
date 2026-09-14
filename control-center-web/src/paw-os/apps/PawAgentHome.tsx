/**
 * PawAgentHome — Agent 新建工作主页
 *
 * 创建链路、乐观入场（optimistic admission）、模型/思考配置与 Room 创建语义
 * 与 PawAgentApp 原内联实现逐项一致。
 *
 * 设计合同：
 * - 桌面首屏合同：这是一块固定的 App 表面，不是往下翻的落地页。新建 Composer
 *   与「继续工作」在同一屏内完成；继续工作列表只在自身内部滚动，页脚是钉在
 *   底部的状态条。
 * - UR-002/040：单一 Agent 入口，Session / Room 在 Composer 底栏选择；
 *   PF-CM-003：Room 真实伙伴在 Composer 下方展示；高级过程说明在工作区按需展开。
 *   UR-253：首页与 Session 共用 ModelPicker；推理直接可见，模型搜索按需展开。
 * - UR-046：四档 Session 权限（只读沙箱 / 完全访问免逐项批准 / 工作区托管沙箱 /
 *   全自动），全自动明确提示自动批准与 OS 边界。
 * - UR-011/025 与 PF-CM-018/021：页脚只投影真实目录状态（读取中 / 失败可重试 /
 *   模型数量），不虚构“Runtime 已连接”这类前端无法证明的声明。
 *
 * 样式：paw-os/styles/paw-os-agent-next.css（类名 an-* 作用域）。
 */

import {
  ArrowUp,
  ChevronDown,
  CircleAlert,
  LoaderCircle,
  Minus,
  Plus,
  Users,
} from 'lucide-react';
import { useEffect, useId, useMemo, useRef, useState, type ClipboardEvent, type KeyboardEvent } from 'react';
import { useControlTransport } from '@/app/control-transport';
import {
  Menu,
  MenuContent,
  MenuItem,
  MenuLabel,
  MenuRadioGroup,
  MenuRadioItem,
  MenuSeparator,
  MenuTrigger,
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/primitives';
import {
  useAgentPreferencesRead,
  type AgentExecutionMode,
} from '@/features/agent/composer/agent-preferences-store';
import {
  PERMISSION_PRESETS as SESSION_PERMISSION_PRESETS,
  unrestrictedWorkspaceRoots,
} from '@/features/agent/composer/permission-policy';
import {
  supportedPiThinkingLevels,
  type PiModelOption,
} from '@/features/agent/model-catalog-options';
import {
  PermissionMark,
  WorkspaceMark,
} from '@/features/agent/marks/ConversationMarks';
import type { AgentPersonaV1 } from '@/contracts/generated/agent-persona.v1';
import type { RoomAttachmentReceipt } from '@/contracts/room-reducer';
import type { SessionSummary } from '@/features/agent/types';
import { ComposerAttachmentPreview, type ComposerShellAttachment } from '@/features/composer/ComposerShell';
import {
  MAX_COMPOSER_ATTACHMENT_BYTES,
  MAX_COMPOSER_ATTACHMENTS,
  normalizeComposerAttachmentMimeType,
} from '@/contracts/attachment-policy';
import {
  defaultRoomPermissionPolicy,
  effectiveRoomPermissionPolicy as resolveEffectiveRoomPermissionPolicy,
  roomPermissionPolicyNeedsDangerousConfirmation,
  roomPermissionPolicyNeedsWorkspaceConfirmation,
  type RoomPermissionPolicy,
  type RoomSummary,
} from '@/features/rooms/room-types';
import {
  RoomPermissionPolicyEditor,
  roomPermissionLayerPresentation,
} from '@/features/rooms/room-presentation';
import { roomPlanetName } from '@/features/rooms/room-participant-identity';
import { useAgentLiveStore } from '@/features/agent/state/live-store';
import {
  isAgentCommandPending,
  isAmbiguousAgentPromptFailure,
  isUnresolvedAgentCommandPending,
  publicAgentErrorText,
} from '@/features/agent/public-error';
import { useRoomLiveStore } from '@/features/rooms/state/live-store';
import { clipboardFilesFromEvent } from '@/features/agent/composer/AgentComposer';
import type { PickedFile } from '@/platform/transport';
import { isTeamDeployment } from '@/features/team/deployment';
import { useOptionalTeam } from '@/features/team/team-context';
import { pawBrowserHost } from './paw-browser-host';
import { ModelPicker } from '@/features/agent/composer/ModelPicker';
import { PawAppIcon } from '../shell/PawAppIcon';

type WorkMode = 'session' | 'room';
type Selection =
  | { kind: 'new'; draft?: string }
  | { kind: 'session'; id: string; draft?: string }
  | { kind: 'room'; id: string; draft?: string; error?: string };

type OptionsPanel = 'project' | 'permission' | null;

type HomePendingAttachment = {
  id: string;
  file: File;
};

const PROMPT_STARTERS: ReadonlyArray<{ label: string; prompt: string }> = [
  { label: '梳理现状', prompt: '梳理这个项目的当前状态：正在进行什么、被什么卡住、下一步最值得做什么。' },
  { label: '审查改动', prompt: '审查最近的改动，指出风险、遗漏和需要跟进的问题。' },
  { label: '拆解任务', prompt: '把这件事拆成可执行的步骤，并从第一步开始：' },
];

/* TeamGateway assigns the actual project mount and execution grant on the
 * server. Keep the Room creation contract bounded to that managed workspace
 * instead of exposing the local full-trust default in the shared client. */
const TEAM_ROOM_PERMISSION_POLICY: RoomPermissionPolicy = {
  schemaVersion: 'rag-ime.room-permission-policy.v1',
  room: { executionMode: 'workspace_managed' },
  partner: { executionMode: 'inherit' },
  toolAgent: { executionMode: 'inherit' },
};

export function PawAgentHome({
  catalogError = '',
  catalogLoading = false,
  defaultModel,
  initialDraft,
  models,
  onCreated,
  onOpenRoom,
  onOpenSession,
  onReloadCatalog,
  personas,
  projectRoots,
  rooms,
  sessions,
}: {
  catalogError?: string;
  catalogLoading?: boolean;
  defaultModel: string;
  initialDraft?: string;
  models: PiModelOption[];
  onCreated: (selection: Selection, created?: SessionSummary, createdRoom?: RoomSummary) => void;
  onOpenRoom: (id: string) => void;
  onOpenSession: (id: string) => void;
  onReloadCatalog?: () => void;
  personas: AgentPersonaV1[];
  projectRoots: string[];
  rooms: RoomSummary[];
  sessions: SessionSummary[];
}) {
  const transport = useControlTransport();
  const teamMode = isTeamDeployment();
  const team = useOptionalTeam();
  const teamProjectName = teamMode && team?.activeSpace?.kind === 'project'
    ? team.activeSpace.name
    : '';
  const electronHost = teamMode ? null : pawBrowserHost();
  const preferenceRead = useAgentPreferencesRead();
  const preferences = preferenceRead.preferences;
  const [mode, setMode] = useState<WorkMode>('session');
  const [prompt, setPrompt] = useState(initialDraft ?? '');
  const [workspaceRoot, setWorkspaceRoot] = useState('');
  const [executionMode, setExecutionMode] = useState<AgentExecutionMode>(preferences.executionMode);
  const [modelReference, setModelReference] = useState(preferences.modelReference || defaultModel);
  const [thinking, setThinking] = useState(preferences.thinking);
  const [roomParticipantOverride, setRoomParticipantOverride] = useState<number | null>(null);
  const [roomPermissionPolicy, setRoomPermissionPolicy] = useState<RoomPermissionPolicy>(
    () => defaultRoomPermissionPolicy('collaboration'),
  );
  const [optionsPanel, setOptionsPanel] = useState<OptionsPanel>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [pendingAttachments, setPendingAttachments] = useState<HomePendingAttachment[]>([]);
  const [pendingClipboardPaste, setPendingClipboardPaste] = useState(false);
  const promptRef = useRef<HTMLTextAreaElement>(null);
  const modeRefs = useRef<Record<WorkMode, HTMLButtonElement | null>>({ session: null, room: null });
  const preferenceHydratedRef = useRef(false);
  const preferenceEditedRef = useRef({ executionMode: false, modelReference: false, thinking: false });
  const modeBriefId = useId();

  useEffect(() => {
    if (!modelReference && (preferences.modelReference || defaultModel)) {
      setModelReference(preferences.modelReference || defaultModel);
    }
  }, [defaultModel, modelReference, preferences.modelReference]);
  useEffect(() => {
    if (preferenceRead.isPending || preferenceRead.readError || preferenceHydratedRef.current) return;
    preferenceHydratedRef.current = true;
    if (!teamMode && !preferenceEditedRef.current.executionMode) setExecutionMode(preferences.executionMode);
    if (!preferenceEditedRef.current.modelReference) setModelReference(preferences.modelReference || defaultModel);
    if (!preferenceEditedRef.current.thinking) setThinking(preferences.thinking);
  }, [defaultModel, preferenceRead.isPending, preferenceRead.readError, preferences.executionMode, preferences.modelReference, preferences.thinking, teamMode]);
  useEffect(() => {
    if (teamMode) {
      setWorkspaceRoot('');
      setExecutionMode('workspace_managed');
      setOptionsPanel(null);
      return;
    }
    if (!workspaceRoot && projectRoots[0]) setWorkspaceRoot(projectRoots[0]);
  }, [projectRoots, teamMode, workspaceRoot]);

  const selectedModel = models.find((item) => item.reference === modelReference);
  const thinkingLevels = supportedPiThinkingLevels(selectedModel, { includeOff: true });
  useEffect(() => {
    if (!selectedModel || !thinkingLevels.length || thinkingLevels.includes(thinking)) return;
    setThinking(thinkingLevels.includes('medium') ? 'medium' : thinkingLevels.includes('off') ? 'off' : thinkingLevels[0]!);
  }, [selectedModel, thinking, thinkingLevels.join(',')]);
  const availableRoomPersonas = personas
    .filter((persona) => persona.selectableModes.includes('coordinator'))
    .slice(0, 8);
  const suggestedParticipantCount = suggestedRoomParticipantCount(prompt, availableRoomPersonas.length);
  const roomParticipantCount = Math.min(
    availableRoomPersonas.length,
    Math.max(0, roomParticipantOverride ?? suggestedParticipantCount),
  );
  const roomPersonas = availableRoomPersonas.slice(0, roomParticipantCount);
  const roomReady = roomPersonas.length >= 2;
  const effectiveExecutionMode: AgentExecutionMode = teamMode ? 'workspace_managed' : executionMode;
  const sessionPermission = SESSION_PERMISSION_PRESETS.find(
    (item) => item.executionMode === effectiveExecutionMode,
  ) ?? SESSION_PERMISSION_PRESETS.find((item) => item.id === 'full-access')!;
  const effectiveRoomPermissionPolicy: RoomPermissionPolicy = teamMode
    ? TEAM_ROOM_PERMISSION_POLICY
    : roomPermissionPolicy;
  const roomPermission = roomPermissionLayerPresentation(
    effectiveRoomPermissionPolicy,
    'room',
    'collaboration',
  );
  const effectiveRoomPermissions = resolveEffectiveRoomPermissionPolicy(effectiveRoomPermissionPolicy);
  const uniformRoomPermissions = Object.values(effectiveRoomPermissions).every(
    (value) => value === effectiveRoomPermissions.room,
  );
  const permissionLabel = mode === 'room'
    ? `${uniformRoomPermissions ? roomPermission.effectiveLabel : '自定义'} · 分层`
    : sessionPermission.label;
  const permissionMode = mode === 'room'
    ? effectiveRoomPermissionPolicy.room.executionMode
    : effectiveExecutionMode;

  // 继续工作按真实更新时间取最近四条，而不是按目录返回顺序截断。
  const recents = useMemo(() => [
    ...sessions.map((item) => ({ kind: 'session' as const, item })),
    ...rooms.map((item) => ({ kind: 'room' as const, item })),
  ]
    .sort((left, right) => right.item.updatedAtMs - left.item.updatedAtMs)
    .slice(0, 4), [rooms, sessions]);

  function moveModeFocus(event: KeyboardEvent<HTMLButtonElement>): void {
    const next = event.key === 'Home' ? 'session'
      : event.key === 'End' ? 'room'
        : ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)
          ? mode === 'session' ? 'room' : 'session'
          : undefined;
    if (!next) return;
    event.preventDefault();
    setMode(next);
    setOptionsPanel(null);
    modeRefs.current[next]?.focus();
  }

  async function pickWorkspace(): Promise<void> {
    if (teamMode) {
      setError('团队项目由服务管理，无法在本机选择目录。');
      return;
    }
    if (!transport.pickFiles && !electronHost?.pickWorkspaceDirectory) {
      setError('当前运行环境不能选择本地目录。');
      return;
    }
    try {
      const path = transport.pickFiles
        ? (await transport.pickFiles({ purpose: 'workspace-root', selection: 'directory', multiple: false, maxFiles: 1 }))[0]?.path?.trim()
        : (await electronHost?.pickWorkspaceDirectory?.())?.path?.trim();
      if (path) {
        setWorkspaceRoot(path);
        setOptionsPanel(null);
      }
    } catch (pickError) {
      setError(errorText(pickError));
    }
  }

  function addPastedFiles(files: File[]): void {
    if (!transport.pasteImages) {
      setError('当前运行环境不能导入剪贴板文件。');
      return;
    }
    const remaining = MAX_COMPOSER_ATTACHMENTS - pendingAttachments.length;
    if (files.length > remaining) {
      setError(`当前输入还可以粘贴 ${remaining} 个附件。`);
      return;
    }
    const oversized = files.find((file) => file.size <= 0 || file.size > MAX_COMPOSER_ATTACHMENT_BYTES);
    if (oversized) {
      setError(`${oversized.name || '附件'} 必须小于 20 MiB 且不能为空。`);
      return;
    }
    setPendingClipboardPaste(false);
    setPendingAttachments((current) => [
      ...current,
      ...files.map((file) => ({ id: clientId('home-attachment'), file })),
    ]);
    setError('');
  }

  function pasteIntoHome(event: ClipboardEvent<HTMLTextAreaElement>): void {
    const { files, hasFileItem } = clipboardFilesFromEvent(event);
    if (!files.length && !hasFileItem) {
      const pastedText = event.clipboardData.getData?.('text/plain') ?? '';
      if (pastedText || !transport.pasteImages) return;
      event.preventDefault();
      setPendingClipboardPaste(true);
      setError('');
      return;
    }
    event.preventDefault();
    if (files.length) {
      addPastedFiles(files);
      return;
    }
    if (!transport.pasteImages) {
      setError('当前运行环境不能导入剪贴板文件。');
      return;
    }
    // WebKit can expose the image item but not its bytes. The native transport
    // will inspect its trusted pasteboard after a Session/Room owner exists.
    setPendingClipboardPaste(true);
    setError('');
  }

  async function importPendingAttachments(
    owner: { sessionId: string } | { roomId: string },
  ): Promise<PickedFile[]> {
    const files = pendingAttachments.map((item) => item.file);
    if (!files.length && !pendingClipboardPaste) return [];
    if (!transport.pasteImages) throw new Error('当前运行环境不能导入剪贴板文件。');
    const imported = await transport.pasteImages({
      ...owner,
      ...(files.length ? { files } : {}),
      maxFiles: files.length || 1,
    });
    if (!imported.length) throw new Error('剪贴板里没有可导入的文件。');
    return imported;
  }

  function clearPendingAttachments(): void {
    setPendingAttachments([]);
    setPendingClipboardPaste(false);
  }

  async function startWork(): Promise<void> {
    const message = prompt.trim() || (pendingAttachments.length || pendingClipboardPaste ? '请查看附件。' : '');
    if (!message || submitting) return;
    if (mode === 'room' && !roomReady) {
      setError('当前没有足够的 Room 伙伴。');
      return;
    }
    if (!teamMode && mode === 'session' && effectiveExecutionMode === 'workspace_managed' && !workspaceRoot) {
      setError('工作区托管需要先选择一个项目。');
      return;
    }
    setSubmitting(true);
    setError('');
    const toolProfileVersion = teamMode ? 'control-center-v1' : sessionPermission.toolProfileVersion;
    const systemWorkspaceRoots = teamMode ? [] : unrestrictedWorkspaceRoots(workspaceRoot);
    const sessionWorkspaceRoots = teamMode ? [] : effectiveExecutionMode === 'per_action' || effectiveExecutionMode === 'full_trust'
      ? systemWorkspaceRoots
      : workspaceRoot ? [workspaceRoot] : [];
    const roomWorkspaceRoots = teamMode ? [] : (
      effectiveRoomPermissionPolicy.room.executionMode === 'per_action'
      || effectiveRoomPermissionPolicy.room.executionMode === 'full_trust'
    )
      ? systemWorkspaceRoots
      : workspaceRoot ? [workspaceRoot] : [];
    try {
      if (mode === 'session') {
        const response = await transport.request<Record<string, unknown>>({
          pathId: 'agent.sessions.create',
          body: {
            title: workTitle(message),
            mode: sessionPermission.mode,
            executionMode: effectiveExecutionMode,
            toolProfileVersion,
            workspaceRoots: sessionWorkspaceRoots,
            ...(!teamMode && effectiveExecutionMode === 'workspace_managed'
              ? { workspaceScopeConfirmation: 'APPROVE_WORKSPACE_SCOPE' }
              : {}),
            ...(!teamMode && effectiveExecutionMode === 'full_trust'
              ? { dangerousModeConfirmation: 'ENABLE_FULL_TRUST' }
              : {}),
          },
        });
        const rawSession = record(record(response).session);
        const sessionId = text(rawSession.id);
        if (!sessionId) throw new Error('服务端没有返回可验证的 Session。');
        const clientMessageId = clientId('session');
        const createdSession = createdSessionSummary(
          rawSession,
          message,
          sessionWorkspaceRoots,
          effectiveExecutionMode,
          toolProfileVersion,
        );
        const attachmentImport = importPendingAttachments({ sessionId });
        const pendingAttachmentIds = [
          ...pendingAttachments.map((attachment) => attachment.id),
          ...(pendingClipboardPaste ? ['pending-clipboard-image'] : []),
        ];
        // Session existence is enough to make the user's intent visible. File
        // import continues under that owner; local attachment identities keep
        // the optimistic row truthful until the durable message replaces it
        // with managed media receipts.
        useAgentLiveStore.getState().appendOptimistic(sessionId, {
          clientMessageId,
          text: message,
          attachments: pendingAttachmentIds,
          nowMs: Date.now(),
        });
        onCreated({ kind: 'session', id: sessionId }, createdSession);
        clearPendingAttachments();
        void (async () => {
          const configuration = [] as Promise<unknown>[];
          const explicitModelSelection = preferenceEditedRef.current.modelReference
            || Boolean(preferences.modelReference && preferences.modelReference !== 'inherit');
          if (selectedModel && explicitModelSelection) {
            configuration.push(transport.request({
              pathId: 'agent.session.model.select',
              params: { sessionId },
              body: { provider: selectedModel.provider, modelId: selectedModel.id },
            }));
            if (thinkingLevels.includes(thinking)) {
              configuration.push(transport.request({
                pathId: 'agent.session.thinking.select',
                params: { sessionId },
                body: { level: thinking },
              }));
            }
          }
          void Promise.allSettled(configuration);
          let importedAttachments: PickedFile[];
          try {
            importedAttachments = await attachmentImport;
          } catch (attachmentError) {
            failHomeAttachmentImportBeforeAdmission(
              sessionId,
              clientMessageId,
              attachmentError,
            );
            return;
          }
          const attachmentIds = importedAttachments.map((attachment) => attachment.id);
          replaceHomeOptimisticAttachmentIds(sessionId, clientMessageId, attachmentIds);
          try {
            const response = await transport.request<Record<string, unknown>>({
              pathId: 'agent.session.prompt',
              params: { sessionId },
              body: { message, attachments: attachmentIds, clientMessageId },
            });
            if (isCancelledPromptAdmission(response)) {
              useAgentLiveStore.getState().discardOptimistic(sessionId, clientMessageId);
              return;
            }
            useAgentLiveStore.getState().acknowledgeOptimistic(sessionId, clientMessageId, Date.now());
          } catch (requestError) {
            settleHomePromptAdmissionFailure(sessionId, clientMessageId, requestError);
          }
        })();
      } else {
        const selectedPersonas = roomPersonas;
        const response = await transport.request<Record<string, unknown>>({
          pathId: 'agent.rooms.create',
          body: {
            title: workTitle(message),
            roomKind: 'collaboration',
            avatar: 'briefcase',
            description: message,
            scenarioPrompt: '',
            participants: selectedPersonas.map((persona, index) => ({
              roleId: persona.roleId,
              roleVersion: persona.version,
              displayName: persona.displayName,
              collaborationRole: index === 0 ? 'coordinator' : index === 1 ? 'reviewer' : 'specialist',
            })),
            routingPolicy: 'parallel',
            routingConfig: { maxResponders: selectedPersonas.length, naturalJitter: 0, fallbackParticipantId: '' },
            workspaceRoots: roomWorkspaceRoots,
            permissionPolicy: effectiveRoomPermissionPolicy,
            ...(!teamMode && roomPermissionPolicyNeedsWorkspaceConfirmation(effectiveRoomPermissionPolicy)
              ? { workspaceScopeConfirmation: 'APPROVE_WORKSPACE_SCOPE' }
              : {}),
            ...(!teamMode && roomPermissionPolicyNeedsDangerousConfirmation(effectiveRoomPermissionPolicy)
              ? { dangerousModeConfirmation: 'ENABLE_FULL_TRUST' }
              : {}),
          },
        });
        const rawRoom = record(record(response).room);
        const roomId = text(rawRoom.id);
        if (!roomId) throw new Error('服务端没有返回可验证的 Room。');
        const clientMessageId = clientId('room');
        const createdRoom = createdRoomSummary(
          rawRoom,
          message,
          roomWorkspaceRoots,
          selectedPersonas,
        );
        const importedAttachments = await importPendingAttachments({ roomId });
        const attachmentIds = importedAttachments.map((attachment) => attachment.id);
        clearPendingAttachments();
        useRoomLiveStore.getState().appendOptimistic(roomId, {
          clientMessageId,
          text: message,
          attachments: roomAttachmentReceipts(importedAttachments, roomId),
          nowMs: Date.now(),
        });
        onCreated({ kind: 'room', id: roomId }, undefined, createdRoom);
        void transport.request<Record<string, unknown>>({
          pathId: 'agent.room.message',
          params: { roomId },
          body: { message, clientMessageId, attachmentIds },
        }).then((messageResponse) => {
          useRoomLiveStore.getState().acceptMessage(roomId, messageResponse);
        }).catch((requestError) => {
          useRoomLiveStore.getState().discardOptimistic(roomId, clientMessageId);
          onCreated({ kind: 'room', id: roomId, draft: message, error: errorText(requestError) });
        });
      }
    } catch (requestError) {
      setError(errorText(requestError));
    } finally {
      setSubmitting(false);
    }
  }

  const permissionTrigger = (
    <button
      aria-label={`权限 · ${permissionLabel}`}
      className="an-chip"
      disabled={submitting || teamMode}
      title={`权限 · ${permissionLabel}`}
      type="button"
    >
      <PermissionMark mode={permissionMode} size={14} />
      <span className="an-chip-text">{permissionLabel}</span>
      <ChevronDown className="caret" size={13} />
    </button>
  );

  return (
    <div className="paw-agent-next an-home-root">
      <div className="an-home">
        <div className="an-home-wrap">
          <header className="an-home-intro">
            <span aria-hidden="true" className="an-home-orbit"><span /></span>
            <h1 className="an-home-title">今天想完成什么？</h1>
          </header>

          <div className="an-composer">
            {pendingAttachments.length || pendingClipboardPaste ? (
              <div aria-label="待发送附件" className="agent-composer__attachments" role="list">
                {pendingAttachments.map(({ id, file }) => {
                  const attachment: ComposerShellAttachment = {
                    id,
                    name: file.name || 'clipboard-file',
                    mimeType: normalizeComposerAttachmentMimeType(file.type),
                    byteSize: file.size,
                    previewFile: file,
                  };
                  return (
                    <span
                      className="agent-composer__attachment-chip"
                      data-attachment-kind={attachment.mimeType.startsWith('image/') ? 'image' : 'file'}
                      key={id}
                      role="listitem"
                    >
                      <ComposerAttachmentPreview attachment={attachment} />
                      <b title={attachment.name}>{attachment.name}</b>
                      <button
                        aria-label={`移除 ${attachment.name}`}
                        onClick={() => setPendingAttachments((current) => current.filter((item) => item.id !== id))}
                        type="button"
                      >×</button>
                    </span>
                  );
                })}
                {pendingClipboardPaste ? (
                  <span className="agent-composer__attachment-chip" data-attachment-kind="image" role="listitem">
                    <ComposerAttachmentPreview attachment={{ id: 'pending-clipboard-image', name: '剪贴板图片', mimeType: 'image/png' }} />
                    <b>剪贴板图片</b>
                    <button aria-label="移除 剪贴板图片" onClick={() => setPendingClipboardPaste(false)} type="button">×</button>
                  </span>
                ) : null}
              </div>
            ) : null}
            <textarea
              aria-describedby={modeBriefId}
              aria-label="描述你想完成的工作"
              onChange={(event) => setPrompt(event.target.value)}
              onPaste={pasteIntoHome}
              ref={promptRef}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                  event.preventDefault();
                  void startWork();
                }
              }}
              placeholder="交给 Agent 一件事…"
              value={prompt}
            />
            {teamProjectName ? (
              <p className="an-project-visibility-note" role="note">
                在「{teamProjectName}」创建的对话和工具记录对项目成员可见；私人讨论请使用个人空间。
              </p>
            ) : null}
            <div className="an-composer-foot">
              <span className="an-mode-seg" role="radiogroup" aria-label="工作类型">
                <button
                  aria-checked={mode === 'session'}
                  aria-label="Session"
                  disabled={submitting}
                  onClick={() => { setMode('session'); setOptionsPanel(null); }}
                  onKeyDown={moveModeFocus}
                  ref={(node) => { modeRefs.current.session = node; }}
                  role="radio"
                  tabIndex={mode === 'session' ? 0 : -1}
                  title="Session"
                  type="button"
                >
                  <PawAppIcon appId="agent" size={14} />
                  <span className="an-chip-text">Session</span>
                </button>
                <button
                  aria-checked={mode === 'room'}
                  aria-label="Room"
                  disabled={submitting}
                  onClick={() => { setMode('room'); setOptionsPanel(null); }}
                  onKeyDown={moveModeFocus}
                  ref={(node) => { modeRefs.current.room = node; }}
                  role="radio"
                  tabIndex={mode === 'room' ? 0 : -1}
                  title="Room"
                  type="button"
                >
                  <PawAppIcon appId="room" size={14} />
                  <span className="an-chip-text">Room</span>
                </button>
              </span>

              <span className="an-anchor">
                {mode === 'room' ? (
                  teamMode ? permissionTrigger : (
                  <Popover open={optionsPanel === 'permission'} onOpenChange={(open) => setOptionsPanel(open ? 'permission' : null)}>
                    <PopoverTrigger asChild>{permissionTrigger}</PopoverTrigger>
                    <PopoverContent align="start" aria-label="Room 三层权限" className="paw-agent-next an-home-menu an-home-menu--policy" side="bottom">
                      <h2 className="an-home-menu__title">Room 三层权限</h2>
                      <RoomPermissionPolicyEditor
                        compact
                        onChange={setRoomPermissionPolicy}
                        policy={roomPermissionPolicy}
                        roomKind="collaboration"
                      />
                    </PopoverContent>
                  </Popover>
                  )
                ) : (
                  teamMode ? permissionTrigger : (
                  <Menu modal={false} open={optionsPanel === 'permission'} onOpenChange={(open) => setOptionsPanel(open ? 'permission' : null)}>
                    <MenuTrigger asChild>{permissionTrigger}</MenuTrigger>
                    <MenuContent align="start" aria-label="权限模式" className="paw-agent-next an-home-menu" side="bottom">
                      <MenuLabel className="an-home-menu__title">权限模式</MenuLabel>
                      <MenuRadioGroup value={executionMode}>
                        {SESSION_PERMISSION_PRESETS.map((item) => (
                          <MenuRadioItem
                            className="an-home-menu__item"
                            key={item.executionMode}
                            onSelect={() => {
                              preferenceEditedRef.current.executionMode = true;
                              setExecutionMode(item.executionMode);
                            }}
                            value={item.executionMode}
                          >
                            <span>
                              <strong>{item.label}</strong>
                              <small>{item.description}</small>
                            </span>
                          </MenuRadioItem>
                        ))}
                      </MenuRadioGroup>
                    </MenuContent>
                  </Menu>
                  )
                )}
              </span>

              <span className="an-anchor an-model-anchor">
                <ModelPicker
                  className="an-chip an-model-chip"
                  options={{ models, modelReference, thinking }}
                  disabled={submitting || !models.length}
                  pending={catalogLoading}
                  requestOpen={0}
                  onOpen={() => setOptionsPanel(null)}
                  onChange={(provider, modelId, level) => {
                    preferenceEditedRef.current.modelReference = true;
                    preferenceEditedRef.current.thinking = true;
                    setModelReference(`${provider}/${modelId}`);
                    setThinking(level);
                  }}
                />
              </span>

              {!teamMode ? <span className="an-anchor">
                <Menu modal={false} open={optionsPanel === 'project'} onOpenChange={(open) => setOptionsPanel(open ? 'project' : null)}>
                <MenuTrigger asChild><button
                  aria-label={workspaceRoot ? `起始项目 · ${projectName([workspaceRoot])}` : '起始项目（可选）'}
                  className="an-chip"
                  disabled={submitting}
                  title={workspaceRoot || '起始项目（可选）'}
                  type="button"
                >
                  <WorkspaceMark bound={Boolean(workspaceRoot)} size={14} />
                  <span className="an-chip-text">{workspaceRoot ? projectName([workspaceRoot]) : '起始项目（可选）'}</span>
                  <ChevronDown className="caret" size={13} />
                </button></MenuTrigger>
                  <MenuContent align="end" aria-label="起始项目（可选）" className="paw-agent-next an-home-menu" side="bottom">
                    <MenuLabel className="an-home-menu__title">起始项目（可选）</MenuLabel>
                    <MenuRadioGroup value={workspaceRoot} onValueChange={setWorkspaceRoot}>
                    {projectRoots.map((root) => (
                      <MenuRadioItem
                        className="an-home-menu__item"
                        key={root}
                        value={root}
                      >
                        <span>
                          <strong>{projectName([root])}</strong>
                          <small className="an-home-menu__path" title={root}>{root}</small>
                        </span>
                      </MenuRadioItem>
                    ))}
                    </MenuRadioGroup>
                    {!projectRoots.length ? <p className="an-home-menu__empty">选择一个项目，让 Agent 从它的目录开始工作。</p> : null}
                    {transport.pickFiles || electronHost?.pickWorkspaceDirectory ? (
                      <>
                        <MenuSeparator />
                        <MenuItem className="an-home-menu__item" onSelect={() => void pickWorkspace()}>浏览其他目录…</MenuItem>
                      </>
                    ) : null}
                  </MenuContent>
                </Menu>
              </span> : null}

              <button
                aria-label={submitting ? '正在创建' : `开始 ${mode === 'session' ? 'Session' : 'Room'}`}
                className="an-send"
                disabled={(!prompt.trim() && !pendingAttachments.length && !pendingClipboardPaste) || submitting || (mode === 'room' && !roomReady)}
                onClick={() => void startWork()}
                type="button"
              >
                {submitting ? <LoaderCircle className="ui-spin" size={14} /> : <ArrowUp size={14} />}
              </button>
            </div>
          </div>
          {!prompt.trim() ? (
            <div aria-label="快速开始" className="an-starters" role="group">
              {PROMPT_STARTERS.map((starter) => (
                <button
                  className="an-starter"
                  key={starter.label}
                  onClick={() => {
                    setPrompt(starter.prompt);
                    promptRef.current?.focus();
                  }}
                  title={starter.prompt}
                  type="button"
                >
                  {starter.label}
                </button>
              ))}
            </div>
          ) : null}
          {mode === 'session' ? (
            <p className="an-mode-brief" id={modeBriefId}>
              随时补充想法，也可以暂停。
            </p>
          ) : availableRoomPersonas.length > 0 ? (
            <div className="an-mode-brief an-room-plan" id={modeBriefId}>
              <span className="an-room-plan__label">任务建议 {suggestedParticipantCount} 位</span>
              <span aria-label="Room 伙伴数量" className="an-room-plan__stepper" role="group">
                <button
                  aria-label="减少 Room 伙伴"
                  disabled={roomParticipantCount <= 1}
                  onClick={() => setRoomParticipantOverride(Math.max(1, roomParticipantCount - 1))}
                  type="button"
                ><Minus size={12} /></button>
                <output aria-live="polite">{roomParticipantCount}</output>
                <button
                  aria-label="增加 Room 伙伴"
                  disabled={roomParticipantCount >= availableRoomPersonas.length || roomParticipantCount >= 8}
                  onClick={() => setRoomParticipantOverride(Math.min(8, roomParticipantCount + 1))}
                  type="button"
                ><Plus size={12} /></button>
              </span>
              {roomPersonas.map((persona, index) => (
                <span className="an-room-plan__chip" data-testid="room-planned-participant" key={persona.roleId}>
                  {roomPlanetName(index)}
                  <i>{collaborationRoleLabel(index)}</i>
                </span>
              ))}
              {!roomReady ? (
                <span className="an-room-plan__constraint">
                  Room Runtime 当前要求至少 2 位伙伴；你可以预览 1 位，但需增加后才能开始。
                </span>
              ) : null}
            </div>
          ) : (
            <div className="an-mode-brief an-room-plan is-blocked" id={modeBriefId}>
              <span>当前没有可用的 Room 伙伴，暂时无法开始。</span>
            </div>
          )}
          {mode === 'room' ? (
            <p aria-label="Room 执行权限" className="an-mode-brief" role="status">
              {uniformRoomPermissions && effectiveRoomPermissions.room === 'full_trust'
                ? 'Room、伙伴和 Tool Agent 均为全权限，开始后无需逐项批准。可在权限中分别调整。'
                : `Room ${roomPermission.effectiveLabel} · 伙伴 ${roomPermissionLayerPresentation(roomPermissionPolicy, 'partner', 'collaboration').effectiveLabel} · Tool Agent ${roomPermissionLayerPresentation(roomPermissionPolicy, 'toolAgent', 'collaboration').effectiveLabel}。按所选分层权限执行。`}
            </p>
          ) : null}
          {preferenceRead.readError ? (
            <p className="an-home-error" role="alert">
              <CircleAlert size={14} /><span>{preferenceRead.readError}</span>
              <button onClick={preferenceRead.reload} type="button">重新读取</button>
            </p>
          ) : null}
          {error ? (
            <p className="an-home-error" role="alert"><CircleAlert size={14} /><span>{error}</span></p>
          ) : null}

          {recents.length ? (
            /* 桌面首屏合同：继续工作与 Composer 同屏。列表在自身内部滚动，
               绝不把页面推成一篇往下翻的长文。 */
            <div className="an-home-section an-home-recents">
              <h2>继续工作</h2>
              <div className="an-recent-list">
                {recents.map((entry) => entry.kind === 'session' ? (
                  <button className="an-recent-card" key={`session:${entry.item.id}`} onClick={() => onOpenSession(entry.item.id)} type="button">
                    <span className="rc-top"><PawAppIcon appId="agent" size={16} /><span className="rc-title">{entry.item.title}</span></span>
                    {entry.item.lastMessagePreview ? <span className="rc-preview">{entry.item.lastMessagePreview}</span> : null}
                    <span className="rc-meta">{projectName(entry.item.workspaceRoots)} · {relativeTime(entry.item.updatedAtMs)}</span>
                  </button>
                ) : (
                  <button className="an-recent-card is-room" key={`room:${entry.item.id}`} onClick={() => onOpenRoom(entry.item.id)} type="button">
                    <span className="rc-top"><PawAppIcon appId="room" size={16} /><span className="rc-title">{entry.item.title}</span></span>
                    {entry.item.description && entry.item.description !== entry.item.title ? <span className="rc-preview">{entry.item.description}</span> : null}
                    <span className="rc-meta"><Users size={11} style={{ verticalAlign: -1 }} /> {entry.item.participants?.length ?? 0} 位伙伴 · {relativeTime(entry.item.updatedAtMs)}</span>
                  </button>
                ))}
              </div>
            </div>
          ) : null}

          {/* 只陈述有真实目录数据支撑的事实；连接状态由 Session 工作区的运行时行负责。 */}
          {(catalogLoading || catalogError || models.length || defaultModel) ? (
            <div className="an-home-foot">
              {catalogLoading ? (
                <span><LoaderCircle className="ui-spin" size={12} />正在读取模型与工作记录…</span>
              ) : null}
              {!catalogLoading && catalogError ? (
                <span className="is-warn" role="status">
                  <CircleAlert size={12} />{catalogError}
                  {onReloadCatalog ? <button onClick={onReloadCatalog} type="button">重新读取目录</button> : null}
                </span>
              ) : null}
              <span className="an-home-shortcuts"><kbd>↵</kbd> 发送<span>·</span><kbd>⇧ ↵</kbd> 换行</span>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}

/* ---------- 本地工具（与 PawAgentApp 原实现语义一致） ---------- */

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}
function record(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {};
}
function text(value: unknown): string {
  return typeof value === 'string' ? value : '';
}
function workTitle(message: string): string {
  const singleLine = message.replace(/\s+/g, ' ').trim();
  return singleLine.length > 28 ? `${singleLine.slice(0, 28)}…` : singleLine || '未命名工作';
}
function clientId(prefix: string): string {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}
function createdSessionSummary(
  raw: Record<string, unknown>,
  firstMessage: string,
  fallbackWorkspaceRoots: string[],
  executionMode: AgentExecutionMode,
  toolProfileVersion: string,
): SessionSummary {
  return {
    id: text(raw.id),
    title: text(raw.title) || workTitle(firstMessage),
    mode: 'coordinator',
    status: text(raw.status) || 'running',
    roleId: text(raw.roleId),
    roleVersion: text(raw.roleVersion),
    roleBookRevisionId: text(raw.roleBookRevisionId),
    updatedAtMs: typeof raw.updatedAtMs === 'number' ? raw.updatedAtMs : Date.now(),
    workspaceRoots: Array.isArray(raw.workspaceRoots)
      ? raw.workspaceRoots.filter((value): value is string => typeof value === 'string')
      : fallbackWorkspaceRoots,
    lastMessagePreview: firstMessage,
    executionMode,
    toolProfileVersion: text(raw.toolProfileVersion) || toolProfileVersion,
  } as SessionSummary;
}
function createdRoomSummary(
  raw: Record<string, unknown>,
  firstMessage: string,
  fallbackWorkspaceRoots: string[],
  selectedPersonas: AgentPersonaV1[],
): RoomSummary {
  return {
    ...raw,
    id: text(raw.id),
    title: text(raw.title) || workTitle(firstMessage),
    status: text(raw.status) || 'active',
    description: text(raw.description) || firstMessage,
    routingPolicy: raw.routingPolicy ?? 'parallel',
    moderatorParticipantId: text(raw.moderatorParticipantId),
    updatedAtMs: typeof raw.updatedAtMs === 'number' ? raw.updatedAtMs : Date.now(),
    participants: Array.isArray(raw.participants)
      ? raw.participants
      : selectedPersonas.map((persona, ordinal) => ({ id: persona.roleId, ordinal })),
    workspaceRoots: Array.isArray(raw.workspaceRoots)
      ? raw.workspaceRoots.filter((value): value is string => typeof value === 'string')
      : fallbackWorkspaceRoots,
  } as RoomSummary;
}

function roomAttachmentReceipts(files: readonly PickedFile[], roomId: string): RoomAttachmentReceipt[] {
  return files.map((file) => ({
    mediaId: file.id,
    roomId,
    fileName: file.name || '附件',
    mimeType: file.mimeType,
    byteSize: file.byteSize,
    sha256: file.sha256 ?? '',
  }));
}

function projectName(roots: readonly string[] | undefined): string {
  const first = roots?.[0] ?? '';
  if (!first) return '未绑定项目';
  const parts = first.split('/').filter(Boolean);
  return parts.slice(-2).join('/') || first;
}
function relativeTime(atMs: number): string {
  const diff = Date.now() - atMs;
  const minutes = Math.max(0, Math.round(diff / 60_000));
  if (minutes < 1) return '刚刚';
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.round(hours / 24);
  return `${days} 天前`;
}
/** 与 startWork 的 Room 创建 payload 保持同一映射：0=协调，1=审阅，其余=专家。 */
function collaborationRoleLabel(index: number): string {
  return index === 0 ? '协调' : index === 1 ? '审阅' : '专家';
}
function suggestedRoomParticipantCount(prompt: string, available: number): number {
  if (available <= 0) return 0;
  if (available === 1) return 1;
  const normalized = prompt.trim();
  let suggested = 2;
  if (/(?:并行|分别|前端|后端|测试|发布|审查|实现|调研|设计|、|以及)/u.test(normalized) || normalized.length >= 80) suggested = 4;
  if (normalized.length >= 240) suggested = 6;
  return Math.min(8, available, suggested);
}

function settleHomePromptAdmissionFailure(
  sessionId: string,
  clientMessageId: string,
  reason: unknown,
): void {
  const store = useAgentLiveStore.getState();
  if (isAgentCommandPending(reason)) {
    store.failOptimistic(
      sessionId,
      clientMessageId,
      publicAgentErrorText(reason),
      Date.now(),
      isUnresolvedAgentCommandPending(reason) ? 'unresolved' : 'pending',
    );
    return;
  }
  if (isAmbiguousAgentPromptFailure(reason)) {
    store.failOptimistic(
      sessionId,
      clientMessageId,
      '暂时无法确认是否已接收。系统不会自动重试；手动重试会核对同一条消息。',
      Date.now(),
      'ambiguous',
    );
    return;
  }
  // Validation and provider rejections are definitive.
  // Keep the original optimistic row as one failed turn so the Session's
  // existing retry control can replay its exact text and client lineage.
  failHomePromptBeforeAdmission(sessionId, clientMessageId, reason);
}

function failHomePromptBeforeAdmission(
  sessionId: string,
  clientMessageId: string,
  reason: unknown,
): void {
  useAgentLiveStore.getState().failOptimistic(
    sessionId,
    clientMessageId,
    publicAgentErrorText(reason, errorText(reason)),
    Date.now(),
  );
}

function failHomeAttachmentImportBeforeAdmission(
  sessionId: string,
  clientMessageId: string,
  reason: unknown,
): void {
  const detail = publicAgentErrorText(reason, errorText(reason));
  useAgentLiveStore.getState().failOptimistic(
    sessionId,
    clientMessageId,
    `附件未能导入，这条消息没有发送。请重新上传附件后发送。${detail ? ` 详情：${detail}` : ''}`,
    Date.now(),
  );
}

function replaceHomeOptimisticAttachmentIds(
  sessionId: string,
  clientMessageId: string,
  attachmentIds: string[],
): void {
  useAgentLiveStore.setState((state) => {
    const current = state.projections[sessionId];
    const messageId = current?.optimisticByClientMessageId[clientMessageId];
    const message = messageId ? current?.messagesById[messageId] : undefined;
    if (!current || !messageId || !message) return state;
    return {
      projections: {
        ...state.projections,
        [sessionId]: {
          ...current,
          messagesById: {
            ...current.messagesById,
            [messageId]: { ...message, attachments: [...attachmentIds] },
          },
        },
      },
    };
  });
}

/** Stop won the admission race. This is a successful transport response but
 *  explicitly proves that no prompt was admitted, so the local row must not
 *  remain queued or become accepted. */
function isCancelledPromptAdmission(value: unknown): boolean {
  const response = record(value);
  return response.accepted === false
    && response.cancelled === true
    && response.admissionCancelled === true;
}

function errorText(reason: unknown): string {
  const raw = reason instanceof Error && reason.message
    ? reason.message
    : typeof reason === 'string' && reason
      ? reason
      : '操作没有完成，请重试。';
  return publicAgentErrorText(reason, raw);
}
