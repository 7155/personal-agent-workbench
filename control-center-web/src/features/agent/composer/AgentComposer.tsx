import {
  Archive,
  ArrowDown,
  Bot,
  BrainCircuit,
  ChartNoAxesCombined,
  CircleHelp,
  Cpu,
  FileText,
  GitBranch,
  History,
  Keyboard,
  LoaderCircle,
  MessageSquarePlus,
  PanelRight,
  PencilLine,
  Plug,
  Send,
  Settings2,
  ShieldCheck,
  Sparkles,
  StopCircle,
  Wrench,
  X,
  type LucideIcon,
} from 'lucide-react';
import {
  startTransition,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type ClipboardEvent,
  type CompositionEvent,
  type KeyboardEvent,
} from 'react';
import { IconButton } from '@/components/primitives';
import type { AgentPersonaV1 } from '@/contracts/generated/agent-persona.v1';
import { ComposerShell } from '@/features/composer/ComposerShell';
import { ComposerAddMenu } from '@/features/composer/ComposerAddMenu';
import { ComposerExpandButton, useComposerEditor } from '@/features/composer/ComposerEditor';
import { usePastedTextAttachments, type ComposerFileImporter } from '@/features/composer/pasted-text';
import type {
  CapabilityCatalog,
  CapabilityPreference,
} from '@/features/plugins/capability-policy';
import {
  buildCommandCatalog,
  commandTitle,
  nextEnabledCommandIndex,
  type ComposerCommand,
} from './command-catalog';
import {
  composerActionLabel,
  composerBlockedReasonLabel,
  composerSubmitMode,
  projectComposerActionModel,
  type ComposerSubmitMode,
} from './composer-action-model';
import { ModelPicker } from './ModelPicker';
import { PermissionPicker } from './PermissionPicker';
import { ToolPicker } from './ToolPicker';
import { permissionLabel } from './permission-policy';
import { ContextUsagePopover, type ContextUsageTelemetry } from '../status/ContextUsagePopover';
import { unseenUpdatesLabel } from '../timeline/transcript-follow';
import type {
  AgentCommand,
  AgentPermissionSelection,
  AgentProductCommandName,
  ComposerAttachment,
  ModelCatalog,
  SessionSummary,
  ThinkingLevel,
  ToolManifest,
} from '../types';

export type AgentMessageDelivery = 'prompt' | 'steer' | 'followUp';

export interface AgentComposerEditState {
  entryId: string;
  messageId: string;
  resolving?: boolean;
}

const productCommandIcons: Record<AgentProductCommandName, LucideIcon> = {
  new: MessageSquarePlus,
  resume: History,
  name: PencilLine,
  branch: GitBranch,
  compact: Archive,
  model: Cpu,
  thinking: BrainCircuit,
  permissions: ShieldCheck,
  tools: Wrench,
  session: ChartNoAxesCombined,
  status: PanelRight,
  subagents: Bot,
  settings: Settings2,
  hotkeys: Keyboard,
  stop: StopCircle,
  help: CircleHelp,
};

const piCommandIcons: Record<Exclude<ComposerCommand['source'], 'product'>, LucideIcon> = {
  extension: Plug,
  prompt: FileText,
  skill: Sparkles,
};

export function AgentComposer({
  draft,
  attachments,
  session,
  sessionMetadataKnown = Boolean(session),
  persona,
  catalog,
  commands: piCommands,
  tools,
  toolCatalogStatus,
  capabilityCatalog,
  capabilityPolicyPending = false,
  busy,
  stopping = false,
  sending,
  modelChanging = false,
  onDraftChange,
  onAttachmentsChange,
  onPickAttachments,
  onPasteFromClipboard,
  onPasteImages,
  onToolSelect,
  onCapabilityPreferenceChange = () => {},
  onProductCommand,
  onSend,
  onStop,
  editState,
  onEditPrevious,
  onCancelEdit,
  onPermissionChange,
  onWorkspaceRootsChange,
  onModelChange,
  modelPickerRequest = 0,
  thinkingPickerRequest = 0,
  permissionPickerRequest = 0,
  toolPickerRequest = 0,
  toolPickerQuery = '',
  helpRequest = 0,
  imageSupport = 'unknown',
  showJumpLatest = false,
  unseenUpdates = 0,
  onJumpLatest,
  contextUsage,
  onQueue,
  minimal = false,
  placeholder,
}: {
  assistantName?: string;
  draft: string;
  attachments: ComposerAttachment[];
  session?: SessionSummary;
  sessionMetadataKnown?: boolean;
  persona?: AgentPersonaV1;
  catalog?: ModelCatalog;
  commands: AgentCommand[];
  tools: ToolManifest[];
  toolCatalogStatus: 'loading' | 'ready' | 'failed';
  capabilityCatalog?: CapabilityCatalog;
  capabilityPolicyPending?: boolean;
  busy: boolean;
  stopping?: boolean;
  sending: boolean;
  modelChanging?: boolean;
  onDraftChange: (value: string) => void;
  onAttachmentsChange: (value: ComposerAttachment[]) => void;
  onPickAttachments: () => void;
  onPasteFromClipboard?: () => void;
  onPasteImages: ComposerFileImporter;
  onToolSelect: (tool: ToolManifest) => void;
  onCapabilityPreferenceChange?: (canonicalId: string, preference: CapabilityPreference) => void;
  onProductCommand: (command: AgentProductCommandName) => void;
  onSend: (delivery: AgentMessageDelivery, draft: string) => void;
  onStop: () => void | Promise<void>;
  editState?: AgentComposerEditState;
  onEditPrevious?: () => void;
  onCancelEdit?: () => void;
  onPermissionChange: (selection: AgentPermissionSelection) => void;
  onWorkspaceRootsChange: () => void;
  onModelChange: (provider: string, modelId: string, level: ThinkingLevel) => void;
  modelPickerRequest?: number;
  thinkingPickerRequest?: number;
  permissionPickerRequest?: number;
  toolPickerRequest?: number;
  toolPickerQuery?: string;
  helpRequest?: number;
  imageSupport?: 'supported' | 'unsupported' | 'unknown';
  showJumpLatest?: boolean;
  /** Messages and activities appended since the reader left the transcript
   *  end. `0` means they scrolled away and nothing has arrived since. */
  unseenUpdates?: number;
  onJumpLatest?: () => void;
  contextUsage?: ContextUsageTelemetry | null;
  /** How many follow-ups the host is already holding for this Session. */
  queueDepth?: number;
  /** Hold this draft until the running turn settles. `false` means the cap
   *  refused it, so the text has to stay in the composer. */
  onQueue?: (value: string) => boolean;
  /** Embedded vertical Apps keep the ordinary Session but omit generic model,
   * permission, Tool and command controls from their focused composer. */
  minimal?: boolean;
  placeholder?: string;
}) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const commandPanelRef = useRef<HTMLDivElement>(null);
  const composingRef = useRef(false);
  const lastEscapeAtRef = useRef(0);
  const escapeResetRef = useRef(0);
  const [composerDraft, setComposerDraft] = useState(draft);
  const [expanded, setExpanded] = useState(false);
  const [dragging, setDragging] = useState(false);
  const dragDepth = useRef(0);
  const canAttach = Boolean(session && !sending && !busy && attachments.length < 8);
  const pastedText = usePastedTextAttachments({ ownerId: session?.id ?? '', canImport: canAttach, onImport: onPasteImages });
  useComposerEditor(textareaRef, composerDraft, expanded);
  const [activeCommandIndex, setActiveCommandIndex] = useState(0);
  const [dismissedDraft, setDismissedDraft] = useState<string | null>(null);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [commandInputFocused, setCommandInputFocused] = useState(false);
  // The parent reconciles the stop request with Pi and may have to render a
  // large transcript before its state update commits. Keep the button's
  // acknowledgement local so a slow transcript cannot leave an enabled stop
  // action (or accept a second click) between the click and that commit.
  const [stopRequested, setStopRequested] = useState(false);
  const wasStoppingRef = useRef(stopping);
  const commandCatalog = useMemo(
    () => minimal
      ? []
      : buildCommandCatalog({ session, catalog, piCommands, tools, toolCatalogStatus, busy, sending }),
    [busy, catalog, minimal, piCommands, sending, session, toolCatalogStatus, tools],
  );
  const commands = useMemo(() => commandCatalog.filter((command) => {
    const value = composerDraft.toLowerCase();
    if (helpOpen || paletteOpen) return true;
    return composerDraft !== dismissedDraft
      && isCommandLookupDraft(value)
      && (value === '/' || command.invocation.toLowerCase().startsWith(value));
  }), [commandCatalog, composerDraft, dismissedDraft, helpOpen, paletteOpen]);
  const commandPanelVisible = commands.length > 0 && (
    paletteOpen
    || (
      commandInputFocused
      && composerDraft !== dismissedDraft
      && isCommandLookupDraft(composerDraft)
    )
  );
  useEffect(() => {
    setActiveCommandIndex(Math.max(0, commands.findIndex((command) => command.enabled)));
  }, [commands, composerDraft]);
  useEffect(() => {
    if (!commandPanelVisible) return;
    commandPanelRef.current
      ?.querySelector<HTMLElement>(`[data-command-index="${activeCommandIndex}"]`)
      ?.scrollIntoView?.({ block: 'nearest' });
  }, [activeCommandIndex, commandPanelVisible]);
  useEffect(() => {
    if (helpRequest <= 0) return;
    setHelpOpen(true);
    setPaletteOpen(true);
    setDismissedDraft(null);
    textareaRef.current?.focus();
  }, [helpRequest]);
  useEffect(() => {
    // Do not let event-stream/catalog rerenders replace WebKit's marked text.
    if (!composingRef.current) setComposerDraft(draft);
  }, [draft]);
  useEffect(() => {
    if (!editState) return;
    const frame = window.requestAnimationFrame(() => {
      const textarea = textareaRef.current;
      if (!textarea) return;
      textarea.focus();
      textarea.setSelectionRange(textarea.value.length, textarea.value.length);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [editState?.messageId]);
  useEffect(() => {
    if (wasStoppingRef.current && !stopping) setStopRequested(false);
    wasStoppingRef.current = stopping;
  }, [stopping]);
  useEffect(() => () => window.clearTimeout(escapeResetRef.current), []);
  /* One projection answers what the primary action does, whether it is
     available and why not — the button, its label and Enter all read it, so
     they cannot disagree. */
  const actionModel = projectComposerActionModel({
    hasSession: Boolean(session),
    draftHasContent: Boolean(composerDraft.trim() || attachments.length),
    draftHasText: Boolean(composerDraft.trim()),
    draftHasAttachments: attachments.length > 0,
    busy,
    sending: sending || pastedText.blocked,
    stopping: stopping || stopRequested,
    modelChanging,
    // Running turns use the human model: Enter adds a reversible follow-up
    // beside the composer. Hosts without the local queue hand the same intent
    // to Pi as a native follow-up. Immediate steering is an action on that
    // pending user message, not a permanent three-way mode switch.
    preferredBusyDelivery: onQueue ? 'queue' : 'followUp',
    capabilities: { queue: Boolean(onQueue) },
  });
  const sendActionLabel = composerActionLabel(actionModel.primary);
  const sendBlockedReason = composerBlockedReasonLabel(actionModel.blockedReason);
  function publishDraft(value: string): void {
    // The textarea owns keystroke latency; the parent only needs a deferred
    // projection for navigation and recovery. Send receives the local snapshot.
    startTransition(() => onDraftChange(value));
  }
  function selectCommand(
    command: ComposerCommand,
    intent: 'activate' | 'complete' = 'activate',
  ): void {
    if (!command.enabled) return;
    if (intent === 'complete') {
      completeCommand(command);
      return;
    }
    if (command.source === 'product' && command.name === 'help') {
      clearTypedCommandDraft(command.invocation);
      setHelpOpen(true);
      setPaletteOpen(true);
      return;
    }
    if (command.source === 'product' && command.behavior === 'execute') {
      clearTypedCommandDraft(command.invocation);
      setPaletteOpen(false);
      setHelpOpen(false);
      onProductCommand(command.name);
      return;
    }
    completeCommand(command);
  }
  function completeCommand(command: ComposerCommand): void {
    const nextDraft = `${command.invocation} `;
    setPaletteOpen(false);
    setHelpOpen(false);
    setDismissedDraft(nextDraft);
    setComposerDraft(nextDraft);
    publishDraft(nextDraft);
    textareaRef.current?.focus();
  }
  function clearTypedCommandDraft(invocation: string): void {
    const value = composerDraft.trim();
    if (!value.startsWith('/') || value.includes(' ') || !invocation.startsWith(value)) return;
    setComposerDraft('');
    publishDraft('');
  }
  function changeDraft(event: ChangeEvent<HTMLTextAreaElement>): void {
    const nextDraft = event.currentTarget.value;
    setDismissedDraft(null);
    if (paletteOpen) {
      setPaletteOpen(false);
      setHelpOpen(false);
    }
    setComposerDraft(nextDraft);
    if (!composingRef.current) publishDraft(nextDraft);
  }
  function startComposition(): void {
    composingRef.current = true;
  }
  function endComposition(event: CompositionEvent<HTMLTextAreaElement>): void {
    const nextDraft = event.currentTarget.value;
    composingRef.current = false;
    setComposerDraft(nextDraft);
    publishDraft(nextDraft);
  }
  function submit(delivery: ComposerSubmitMode | null): void {
    if (!delivery || pastedText.blocked) return;
    const value = composerDraft;
    /* A refused queue never reaches Runtime, so the draft has to stay exactly
       where the writer left it rather than vanish into a full queue. */
    if (delivery === 'queue' && !onQueue?.(value)) return;
    setComposerDraft('');
    setPaletteOpen(false);
    setHelpOpen(false);
    setDismissedDraft(null);
    publishDraft('');
    if (delivery !== 'queue') onSend(delivery, value);
  }
  function keyDown(event: KeyboardEvent<HTMLTextAreaElement>): void {
    // WebKit can report isComposing=false on the Enter that commits an IME
    // candidate. The ref and legacy 229 keyCode keep that key inside the IME.
    if (composingRef.current || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
    if (commandPanelVisible && commands.length) {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        const delta = event.key === 'ArrowDown' ? 1 : -1;
        setActiveCommandIndex((current) => nextEnabledCommandIndex(commands, current, delta));
        return;
      }
      if (event.key === 'Escape') {
        event.preventDefault();
        setPaletteOpen(false);
        setHelpOpen(false);
        setDismissedDraft(composerDraft);
        return;
      }
      if (event.key === 'Tab' && !event.shiftKey) {
        event.preventDefault();
        const command = commands[activeCommandIndex] ?? commands[0];
        if (command) selectCommand(command, 'complete');
        return;
      }
      if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
        event.preventDefault();
        const command = commands[activeCommandIndex] ?? commands[0];
        if (command) selectCommand(command, 'activate');
        return;
      }
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      if (expanded) { setExpanded(false); return; }
      if (editState) {
        lastEscapeAtRef.current = 0;
        onCancelEdit?.();
        return;
      }
      const now = Date.now();
      if (lastEscapeAtRef.current > 0 && now - lastEscapeAtRef.current <= 650) {
        lastEscapeAtRef.current = 0;
        window.clearTimeout(escapeResetRef.current);
        onEditPrevious?.();
        return;
      }
      lastEscapeAtRef.current = now;
      window.clearTimeout(escapeResetRef.current);
      escapeResetRef.current = window.setTimeout(() => { lastEscapeAtRef.current = 0; }, 650);
      return;
    }
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit(composerSubmitMode(actionModel, { alternate: event.altKey }));
    }
  }
  function paste(event: ClipboardEvent<HTMLTextAreaElement>): void {
    // Any pasted file — image, PDF, code, archive — rides the managed
    // attachment path; plain text keeps the browser's default insertion.
    const { files, hasFileItem } = clipboardFilesFromEvent(event);
    if (!files.length && !hasFileItem) {
      const pastedText = event.clipboardData.getData?.('text/plain') ?? '';
      if (handlePastedText(pastedText)) { event.preventDefault(); return; }
      if (pastedText || !onPasteFromClipboard) return;
      event.preventDefault();
      if (canAttach) onPasteFromClipboard();
      return;
    }
    event.preventDefault();
    if (!canAttach) return;
    // WebKit sometimes reports file items whose bytes it refuses to expose;
    // the owner then reads the trusted system pasteboard instead.
    if (files.length) onPasteImages(files);
    else onPasteFromClipboard?.();
  }
  const handlePastedText = pastedText.pasteText;
  return (
    <div className="agent-composer-wrap" data-minimal={minimal || undefined}
      onDragEnter={(event) => { if (!event.dataTransfer.types.includes('Files')) return; event.preventDefault(); dragDepth.current += 1; if (canAttach) setDragging(true); }}
      onDragOver={(event) => { if (event.dataTransfer.types.includes('Files')) { event.preventDefault(); event.dataTransfer.dropEffect = canAttach ? 'copy' : 'none'; } }}
      onDragLeave={(event) => { event.preventDefault(); dragDepth.current = Math.max(0, dragDepth.current - 1); if (!dragDepth.current) setDragging(false); }}
      onDrop={(event) => { if (!event.dataTransfer.types.includes('Files')) return; event.preventDefault(); dragDepth.current = 0; setDragging(false); if (canAttach) onPasteImages([...event.dataTransfer.files]); }}
    >
      {dragging ? <div className="composer-drop-hint" role="status">松开以添加附件</div> : null}
      {commandPanelVisible ? (
        <div ref={commandPanelRef} id="agent-command-palette" className="agent-command-palette" role="listbox" aria-label="命令面板">
          <header>
            <span className="agent-command-palette__heading">
              <Keyboard aria-hidden="true" size={14} />
              <strong>{helpOpen ? '命令帮助' : '命令'}</strong>
              <small>{session ? `${permissionLabel(session)} · ${commands.length}` : '未选择对话'}</small>
            </span>
            {helpOpen
              ? <p>控制中心命令直接操作界面或 API；Pi 命令只来自当前对话的 RPC 目录。</p>
              : <p id="agent-command-palette-hint"><kbd>↑↓</kbd> 选择 <kbd>Tab</kbd> 补全 <kbd>Esc</kbd> 关闭</p>}
          </header>
          {commands.map((command, index) => (
            <button id={`agent-command-option-${index}`} key={`${command.source}:${command.invocation}`} type="button" role="option" tabIndex={-1} data-command-index={index} data-source={command.source} aria-label={`${command.invocation} ${command.description || commandTitle(command.source)} ${commandTitle(command.source)}`} aria-selected={index === activeCommandIndex} aria-disabled={!command.enabled} disabled={!command.enabled} title={command.disabledReason} onMouseDown={(event) => event.preventDefault()} onMouseEnter={() => command.enabled && setActiveCommandIndex(index)} onClick={() => selectCommand(command)}>
              <span className="agent-command-palette__icon" aria-hidden="true">
                <CommandIcon command={command} />
              </span>
              <span className="agent-command-palette__identity">
                <kbd title={command.invocation}>{command.invocation}</kbd>
              </span>
              <span className="agent-command-palette__copy">
                <strong>{command.description || commandTitle(command.source)}</strong>
                {command.disabledReason ? <small>{command.disabledReason}</small> : null}
              </span>
              <span className="agent-command-palette__shortcut" data-visible={index === activeCommandIndex || undefined} aria-hidden="true"><kbd>Tab</kbd></span>
            </button>
          ))}
        </div>
      ) : null}
      {showJumpLatest ? (
        /* A reader who scrolled away needs to know whether anything arrived,
           not just that a way back exists. The count is content items — new
           messages and activities — never token deltas inside a growing row. */
        <button
          aria-label={unseenUpdates > 0 ? `回到最新，有 ${unseenUpdates} 条新内容` : '回到最新'}
          className="agent-jump-latest"
          data-unseen={unseenUpdates > 0 || undefined}
          onClick={onJumpLatest}
          type="button"
        >
          <ArrowDown aria-hidden="true" size={14} />
          <span aria-hidden="true">回到最新</span>
          {unseenUpdates > 0 ? (
            <b aria-hidden="true" className="agent-jump-latest__count">
              {unseenUpdatesLabel(unseenUpdates)}
            </b>
          ) : null}
        </button>
      ) : null}
      <ComposerShell
        surface="session"
        expanded={expanded}
        editorAction={<ComposerExpandButton expanded={expanded} onToggle={() => { setExpanded(!expanded); textareaRef.current?.focus(); }} />}
        busy={busy}
        jumpLatest={showJumpLatest}
        onSurfacePress={() => textareaRef.current?.focus()}
        banner={<>{pastedText.pendingNotice}{editState ? (
          <div className="agent-composer__edit" role="status">
            <PencilLine size={15} aria-hidden="true" />
            <span><strong>正在修改这条消息</strong><small>{editState.resolving ? '正在定位历史锚点；内容现在就可以编辑' : '发送后将从这里重新生成后续对话'}</small></span>
            <IconButton label="取消修改" icon={<X size={15} />} size="small" onClick={onCancelEdit} tooltip />
          </div>
        ) : null}</>}
        attachments={attachments.map((attachment) => ({ ...attachment, description: pastedText.previews[attachment.name] }))}
        onRemoveAttachment={(id) => onAttachmentsChange(attachments.filter((item) => item.id !== id))}
        textarea={(
          <textarea
            ref={textareaRef}
            rows={1}
            value={composerDraft}
            onChange={changeDraft}
            onCompositionStart={startComposition}
            onCompositionEnd={endComposition}
            onKeyDown={keyDown}
            onPaste={paste}
            onFocus={() => setCommandInputFocused(true)}
            onBlur={() => {
              setCommandInputFocused(false);
              setPaletteOpen(false);
              setHelpOpen(false);
              setDismissedDraft(composerDraft);
            }}
            autoCapitalize="none"
            autoComplete="off"
            autoCorrect="off"
            spellCheck={false}
            placeholder={placeholder ?? composerPlaceholder(imageSupport)}
            aria-label="消息"
            role={commandPanelVisible ? 'combobox' : undefined}
            aria-autocomplete={commandPanelVisible ? 'list' : undefined}
            aria-expanded={commandPanelVisible ? true : undefined}
            aria-controls={commandPanelVisible ? 'agent-command-palette' : undefined}
            aria-describedby={commandPanelVisible && !helpOpen ? 'agent-command-palette-hint' : undefined}
            aria-activedescendant={commandPanelVisible && commands[activeCommandIndex]
              ? `agent-command-option-${activeCommandIndex}`
              : undefined}
          />
        )}
        controls={(
          <>
            <ComposerAddMenu canAttach={canAttach} disabled={!session} onPickAttachments={onPickAttachments} />
            {minimal ? null : (
              <>
                <PermissionPicker session={session} metadataKnown={sessionMetadataKnown} persona={persona} tools={tools} disabled={busy || sending} requestOpen={permissionPickerRequest} onChange={onPermissionChange} onWorkspaceRootsChange={onWorkspaceRootsChange} />
                <ToolPicker
                  adjustmentDisabled={busy || sending}
                  capabilityCatalog={capabilityCatalog}
                  capabilityPolicyPending={capabilityPolicyPending}
                  disabled={!session}
                  requestOpen={toolPickerRequest}
                  requestQuery={toolPickerQuery}
                  session={session}
                  status={toolCatalogStatus}
                  tools={tools}
                  onCapabilityPreferenceChange={onCapabilityPreferenceChange}
                  onSelect={(tool) => {
                    onToolSelect(tool);
                    window.requestAnimationFrame(() => textareaRef.current?.focus());
                  }}
                />
                <ModelPicker
                  catalog={catalog}
                  disabled={busy || sending}
                  pending={modelChanging}
                  requestOpen={modelPickerRequest}
                  thinkingRequestOpen={thinkingPickerRequest}
                  onChange={onModelChange}
                />
                <ContextUsagePopover
                  sessionId={session?.id}
                  telemetry={contextUsage}
                />
              </>
            )}
          </>
        )}
        actions={(
          <>
            {busy ? (
              <IconButton
                className="agent-composer__stop"
                label={stopping || stopRequested ? '正在停止本轮' : '停止本轮'}
                icon={stopping || stopRequested ? <LoaderCircle className="ui-spin" size={16} /> : <StopCircle size={16} />}
                onClick={() => {
                  setStopRequested(true);
                  void Promise.resolve(onStop()).finally(() => setStopRequested(false));
                }}
                disabled={stopping || stopRequested}
                aria-busy={stopping || stopRequested || undefined}
                tooltip
              />
            ) : null}
            <IconButton
              className="agent-composer__send"
              label={sendBlockedReason ? `${sendActionLabel}（${sendBlockedReason}）` : sendActionLabel}
              icon={<Send size={16} />}
              onClick={() => submit(composerSubmitMode(actionModel))}
              disabled={actionModel.primaryDisabled}
              tooltip
            />
          </>
        )}
      />
    </div>
  );
}

function CommandIcon({ command }: { command: ComposerCommand }) {
  const Icon = command.source === 'product'
    ? productCommandIcons[command.name]
    : piCommandIcons[command.source];
  return <Icon size={16} />;
}

function isCommandLookupDraft(value: string): boolean {
  return value.startsWith('/') && !/\s/u.test(value);
}

function composerPlaceholder(support: 'supported' | 'unsupported' | 'unknown'): string {
  const target = '当前 Session';
  if (support === 'supported') return `给${target}发消息，输入 / 查看命令，或粘贴图片、文件…`;
  if (support === 'unsupported') return `给${target}发消息，输入 / 查看命令，或粘贴文件；当前模型不识别图片…`;
  return `给${target}发消息，输入 / 查看命令，或粘贴文件；当前模型图片能力未知…`;
}

/**
 * Read clipboard files using the same browser/WebKit path as the Session
 * composer. WebKit can expose a file item without exposing its bytes; callers
 * use `hasFileItem` to hand that case to the trusted native pasteboard path.
 */
export function clipboardFilesFromEvent(
  event: ClipboardEvent<HTMLTextAreaElement>,
): { files: File[]; hasFileItem: boolean } {
  const files = [...event.clipboardData.files];
  const items = event.clipboardData.items;
  let hasFileItem = files.length > 0;
  if (!files.length && items) {
    for (const item of items) {
      if (item.kind !== 'file') continue;
      hasFileItem = true;
      const file = item.getAsFile();
      if (file) files.push(file);
    }
  }
  return { files, hasFileItem };
}
