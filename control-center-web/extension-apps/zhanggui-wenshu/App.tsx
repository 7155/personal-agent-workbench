import { BarChart3, CircleAlert, CircleCheck, FolderOpen, LoaderCircle, MoreHorizontal, PackageOpen, Send, ShieldCheck } from 'lucide-react';
import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';

import { useControlTransport } from '@/app/control-transport';
import { openPawOsRoute, usePawOsDesktop } from '@/features/paw-os/surface-context';
import { agentCommandReceiptFailure, isAgentCommandPending, isAmbiguousAgentPromptFailure, isUnresolvedAgentCommandPending, publicAgentErrorText } from '@/features/agent/public-error';
import { agentSessionAddress, useAgentLiveStore, type AgentSessionTarget } from '@/features/agent/state/live-store';
import { sessionItems, type SessionSummary } from '@/features/agent/types';
import { PawChatSurface } from '@/paw-os/extensions/ChatSurface';
import { PawAppIcon } from '@/paw-os/shell/PawAppIcon';
import type { PawExtensionAppProps } from '@/paw-os/extensions/types';
import './app.css';

export const chatSurface = { major: 1, features: ['embedded'] } as const;

const MODES = [
  {
    id: 'ask',
    label: '问数',
    eyebrow: '经营问答',
    title: '直接问经营数据',
    description: '先定位口径和来源，再给数值、时间范围与证据。',
    placeholder: '例如：北斗项目一季度销售额是多少？',
    suggestions: ['本月销售额和上月相比怎样？', '哪些项目贡献了主要收入？'],
  },
  {
    id: 'reconcile',
    label: '对账',
    eyebrow: '差异核对',
    title: '把两个口径放在一起核对',
    description: '列出差异、来源、时间窗和仍需补充的数据，不静默抹平冲突。',
    placeholder: '例如：核对销售台账与回款表的季度差异',
    suggestions: ['检查订单金额与回款金额差异', '找出两份报表口径不一致的项目'],
  },
  {
    id: 'explain',
    label: '解释',
    eyebrow: '指标说明',
    title: '把数字为什么变化讲清楚',
    description: '把计算口径、影响因素和证据拆开，不把估计值说成事实。',
    placeholder: '例如：解释本月毛利率下降的主要原因',
    suggestions: ['解释收入增长但现金回款下降', '说明这个指标的计算口径'],
  },
] as const;

type ModeId = (typeof MODES)[number]['id'];

type SandboxExperimentReceipt = {
  schemaVersion: 'rag-ime.extension-sandbox-experiment-receipt.v1';
  ok: true;
  sessionId: string;
  ownerAppId: string;
  candidateBindingSha256: string;
  requestedDecision: 'run' | 'skip';
  executed: boolean;
  executionStatus: 'completed' | 'skipped';
  sandboxRunId?: string;
  traceId?: string;
  evalRunId?: string;
};

type SessionPreparation = {
  session: SessionSummary;
  modeReady: boolean;
  sandbox?: {
    experimentId: string;
    requestedDecision: 'run' | 'skip';
    receipt?: SandboxExperimentReceipt;
  };
};

export default function ZhangguiWenshuApp({ manifest }: PawExtensionAppProps) {
  const transport = useControlTransport();
  const desktop = usePawOsDesktop();
  const [modeId, setModeId] = useState<ModeId>('ask');
  const [sessions, setSessions] = useState<Partial<Record<ModeId, SessionSummary>>>({});
  const [preparedSessions, setPreparedSessions] = useState<Partial<Record<ModeId, SessionPreparation>>>({});
  const [drafts, setDrafts] = useState<Partial<Record<ModeId, string>>>({});
  const [workspaceRoot, setWorkspaceRoot] = useState('');
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [sandboxEnabled, setSandboxEnabled] = useState(manifest.sandbox?.default === 'required');
  const [sandboxReceipt, setSandboxReceipt] = useState<SandboxExperimentReceipt | null>(null);
  const [error, setError] = useState('');
  const [historyError, setHistoryError] = useState('');
  const [historyRevision, setHistoryRevision] = useState(0);
  const modeTabsId = useId();
  const modeTabRefs = useRef(new Map<ModeId, HTMLButtonElement>());
  const activeMode = MODES.find((mode) => mode.id === modeId)!;
  const activeSession = sessions[modeId];
  const preparedSession = preparedSessions[modeId]?.session;
  const sandboxDecision = preparedSessions[modeId]?.sandbox?.requestedDecision
    ?? (manifest.sandbox?.default === 'required' || sandboxEnabled ? 'run' : 'skip');
  const draft = drafts[modeId] ?? '';
  const visibleError = error || historyError;
  const managedDataSourceLabel = `${manifest.label}受控数据`;
  const dataWorkspaceRoot = preparedSession ? preparedSession.workspaceRoots[0] ?? '' : workspaceRoot;
  const selectedWorkspaceName = dataWorkspaceRoot.split(/[\\/]/).filter(Boolean).at(-1) ?? '';
  const dataSourceLabel = selectedWorkspaceName || managedDataSourceLabel;
  const canPickDataWorkspace = typeof transport.pickFiles === 'function';
  const sourceLocked = sending || Boolean(preparedSession);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setHistoryError('');
    void transport.request({
      pathId: 'agent.sessions.list',
      query: {
        limit: 100,
        includeArchived: false,
        surfaceKind: 'extension_app',
        ownerAppId: manifest.id,
      },
    })
      .then((value) => {
        if (!active) return;
        const restored: Partial<Record<ModeId, SessionSummary>> = {};
        for (const session of sessionItems(value, { includeAppOwned: true })) {
          if (session.surfaceKind !== 'extension_app' || session.ownerAppId !== manifest.id) continue;
          const surfaceKey = session.surfaceKey as ModeId;
          if (!MODES.some((mode) => mode.id === surfaceKey) || restored[surfaceKey]) continue;
          restored[surfaceKey] = session;
        }
        setSessions(restored);
        setHistoryError('');
      })
      .catch((reason) => {
        if (active) setHistoryError(publicError(reason, '没有读到掌柜问数的对话记录。'));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, [historyRevision, manifest.id, transport]);

  function setDraft(value: string): void {
    setDrafts((current) => ({ ...current, [modeId]: value }));
  }

  function selectMode(next: ModeId): void {
    setModeId(next);
    setError('');
    setSandboxReceipt(null);
  }

  function moveModeFocus(event: KeyboardEvent<HTMLButtonElement>, index: number): void {
    const nextIndex = event.key === 'Home' ? 0
      : event.key === 'End' ? MODES.length - 1
        : event.key === 'ArrowRight' ? (index + 1) % MODES.length
          : event.key === 'ArrowLeft' ? (index - 1 + MODES.length) % MODES.length
            : -1;
    if (nextIndex < 0) return;
    event.preventDefault();
    const next = MODES[nextIndex].id;
    selectMode(next);
    modeTabRefs.current.get(next)?.focus();
  }

  async function startConversation(message: string): Promise<void> {
    const userMessage = message.trim();
    if (!userMessage || sending) return;
    setSending(true);
    setError('');
    try {
      let preparation = preparedSessions[modeId];
      if (!preparation) {
        const created = await transport.request<Record<string, unknown>>({
          pathId: 'agent.sessions.create',
          body: {
            title: `${manifest.label} · ${activeMode.label}`,
            mode: workspaceRoot ? 'coordinator' : 'assistant',
            toolProfileVersion: 'control-center-v1',
            executionMode: 'per_action',
            workspaceRoots: workspaceRoot ? [workspaceRoot] : [],
            surfaceKind: 'extension_app',
            ownerAppId: manifest.id,
            surfaceKey: modeId,
          },
        });
        const raw = record(record(created).session);
        const sessionId = text(raw.id);
        if (!sessionId) throw new Error('服务端没有返回可验证的 Session。');
        preparation = {
          session: sessionSummary(
            raw, sessionId, `${manifest.label} · ${activeMode.label}`,
            userMessage, workspaceRoot, manifest.id, modeId,
          ),
          modeReady: false,
          ...(manifest.sandbox ? {
            sandbox: { experimentId: `experiment:${modeId}:${Date.now()}`, requestedDecision: sandboxDecision },
          } : {}),
        };
        // Creation has already happened. Keep that identity even if the next
        // preparation step fails; a user retry resumes only unfinished steps.
        setPreparedSessions((current) => ({ ...current, [modeId]: preparation }));
      }
      const session = preparation.session;
      const sessionId = session.id;
      const address = agentSessionAddress(transport, sessionId);
      if (!preparation.modeReady) {
        await transport.request({
          pathId: 'agent.session.mode.update',
          params: { sessionId },
          body: {
            mode: session.workspaceRoots.length ? 'coordinator' : 'assistant',
            executionMode: 'per_action',
            toolProfileVersion: 'control-center-v1',
            projectContextEnabled: false,
            piSkillsEnabled: true,
            codexSkillsEnabled: false,
          },
        });
        preparation = { ...preparation, modeReady: true };
        setPreparedSessions((current) => ({ ...current, [modeId]: preparation }));
      }
      if (preparation.sandbox && !preparation.sandbox.receipt) {
        const sandbox = preparation.sandbox;
        const rawReceipt = await transport.request<unknown>({
          pathId: 'extension.sandbox.experiment.run',
          body: {
            sessionId,
            ownerAppId: manifest.id,
            experimentId: sandbox.experimentId,
            candidateBindingSha256: manifest.bindingSha256,
            requestedDecision: sandbox.requestedDecision,
          },
        });
        const receipt = requireSandboxExperimentReceipt(rawReceipt, {
          sessionId,
          ownerAppId: manifest.id,
          candidateBindingSha256: manifest.bindingSha256,
          requestedDecision: sandbox.requestedDecision,
        });
        preparation = { ...preparation, sandbox: { ...sandbox, receipt } };
        setPreparedSessions((current) => ({ ...current, [modeId]: preparation }));
      }
      if (preparation.sandbox?.receipt) setSandboxReceipt(preparation.sandbox.receipt);
      const message = bootstrapPrompt(
        manifest.skillRef,
        activeMode,
        userMessage,
        session.workspaceRoots[0] ?? '',
        managedDataSourceLabel,
        manifest.verticalSuiteId,
        manifest.verticalSuiteRevision,
      );
      const clientMessageId = `extension:${manifest.id}:${modeId}:${crypto.randomUUID()}`;
      const store = useAgentLiveStore.getState();
      // Keep the exact App context with the user's question: the existing
      // Session retry must recover this command, not silently omit the Skill
      // or source boundaries on its second attempt.
      store.appendOptimistic(address, { clientMessageId, text: message, nowMs: Date.now() });
      try {
        const response = record(await transport.request({
          pathId: 'agent.session.prompt',
          params: { sessionId },
          body: { message, clientMessageId, delivery: 'prompt' },
        }));
        if (response.accepted === false && response.cancelled === true && response.admissionCancelled === true) {
          store.discardOptimistic(address, clientMessageId);
          setError('这条消息已取消，原问题已保留；可在同一对话中重新发送。');
          return;
        }
        store.acknowledgeOptimistic(address, clientMessageId, Date.now());
        setDraft('');
      } catch (reason) {
        if (agentCommandReceiptFailure(reason)?.code === 'AGENT_COMMAND_CONFLICT') {
          store.discardOptimistic(address, clientMessageId);
          setError(publicAgentErrorText(reason));
          return;
        }
        settleFirstPromptFailure(address, clientMessageId, reason);
      }
      // The first composer owns the draft until admission settles. Afterwards
      // the same Session owns its visible pending/failed turn and recovery.
      // Proven non-admission above leaves the prepared Session in place so an
      // explicit new send cannot create another Session or rerun its sandbox.
      setSessions((current) => ({ ...current, [modeId]: session }));
      setPreparedSessions((current) => {
        const next = { ...current };
        delete next[modeId];
        return next;
      });
    } catch (reason) {
      setError(publicError(reason, '掌柜问数没有开始，请重试。'));
    } finally {
      setSending(false);
    }
  }

  async function pickDataWorkspace(): Promise<void> {
    if (!transport.pickFiles || sourceLocked) return;
    try {
      const selection = await transport.pickFiles({
        purpose: 'workspace-root',
        selection: 'directory',
        multiple: false,
        maxFiles: 1,
      });
      const path = selection[0]?.path?.trim() ?? '';
      if (path) {
        setWorkspaceRoot(path);
        setError('');
      }
    } catch (reason) {
      setError(publicError(reason, '数据目录没有选中。'));
    }
  }

  function resetMode(): void {
    const next = { ...sessions };
    delete next[modeId];
    setSessions(next);
    setDraft('');
    setSandboxReceipt(null);
  }

  return (
    <main className="zhanggui-app" data-mode={modeId}>
      <header className="zhanggui-app__header">
        <span className="zhanggui-app__identity">
          <PawAppIcon appId={manifest.id} size={34} />
          <span><h1>{manifest.label}</h1></span>
        </span>
        <span className="zhanggui-app__header-actions">
          <button
            aria-label={canPickDataWorkspace
              ? `更换数据源，当前 ${dataSourceLabel}`
              : `当前数据源 ${dataSourceLabel}；当前宿主不支持更换`}
            className="zhanggui-app__data-button"
            disabled={!canPickDataWorkspace || sourceLocked}
            onClick={() => void pickDataWorkspace()}
            title={sourceLocked ? '首条问题会继续使用已准备的数据源' : canPickDataWorkspace ? '更换或迁移数据目录' : '当前宿主不支持目录迁移，继续使用 App 受控数据'}
            type="button"
          >
            <FolderOpen size={15} />
            <span>{dataSourceLabel}</span>
          </button>
          <span className="zhanggui-app__status" data-status="selected"><i />{dataWorkspaceRoot ? '已选择数据目录' : '已选择受控数据'}</span>
          <details className="zhanggui-app__more">
            <summary aria-label="掌柜问数更多操作"><MoreHorizontal size={18} /></summary>
            <div>
              <small>沙箱套件 · SGG {manifest.verticalSuiteRevision}</small>
              <button onClick={() => openPawOsRoute(desktop, `/plugins?packageId=${encodeURIComponent(manifest.packageId)}`)} type="button"><PackageOpen size={15} />管理与卸载</button>
            </div>
          </details>
        </span>
      </header>

      <nav aria-label="掌柜问数模式" className="zhanggui-app__modes" role="tablist">
        {MODES.map((mode, index) => (
          <button
            aria-controls={`${modeTabsId}-panel`}
            aria-label={mode.label}
            aria-selected={mode.id === modeId}
            disabled={sending}
            id={`${modeTabsId}-${mode.id}`}
            key={mode.id}
            onClick={() => selectMode(mode.id)}
            onKeyDown={(event) => moveModeFocus(event, index)}
            ref={(node) => {
              if (node) modeTabRefs.current.set(mode.id, node);
              else modeTabRefs.current.delete(mode.id);
            }}
            role="tab"
            tabIndex={mode.id === modeId ? 0 : -1}
            type="button"
          >
            <span>{mode.label}</span>
            <small>{mode.eyebrow}</small>
          </button>
        ))}
      </nav>

      {visibleError ? <p className="zhanggui-app__error" role="alert">
        <CircleAlert aria-hidden="true" size={15} />
        <span>{visibleError}</span>
        {!error && historyError ? <button disabled={loading || sending} onClick={() => setHistoryRevision((current) => current + 1)} type="button">重新读取</button> : null}
      </p> : null}
      {sandboxReceipt ? (
        <p className="zhanggui-app__sandbox-receipt" role="status">
          <CircleCheck aria-hidden="true" size={15} />
          {sandboxReceipt.executed
            ? `沙箱自测已完成 · ${sandboxReceipt.sandboxRunId ?? 'SandboxRun 已保存'}`
            : '本次已明确跳过沙箱自测'}
        </p>
      ) : null}
      <div aria-labelledby={`${modeTabsId}-${modeId}`} className="zhanggui-app__content" id={`${modeTabsId}-panel`} role="tabpanel">
      {loading ? <div className="zhanggui-app__loading" role="status"><LoaderCircle className="ui-spin" size={18} />正在恢复问数记录…</div> : activeSession ? (
        <section className="zhanggui-app__session" aria-label={`${activeMode.label}对话`}>
          <div className="zhanggui-app__session-note">
            <span><strong>{activeMode.label}</strong><span>{activeMode.description}</span></span>
            <button onClick={resetMode} type="button">新对话</button>
          </div>
          <PawChatSurface
            api={chatSurface}
            active
            view="embedded"
            composer={{ placeholder: `${activeMode.placeholder.replace('例如：', '')}，或继续追问…` }}
            onNewConversation={resetMode}
            onSessionCreated={(session) => {
              const next = { ...sessions, [modeId]: session };
              setSessions(next);
            }}
            onSessionUpdated={(session) => {
              setSessions((current) => {
                return { ...current, [modeId]: session };
              });
            }}
            session={activeSession}
          />
        </section>
      ) : (
        <section className="zhanggui-app__start">
          <span className="zhanggui-app__mark"><BarChart3 size={24} /></span>
          <div className="zhanggui-app__start-copy"><h2>{activeMode.title}</h2><p>{activeMode.description}</p></div>
          <div className="zhanggui-app__suggestions">
            {activeMode.suggestions.map((suggestion) => <button disabled={sending} key={suggestion} onClick={() => setDraft(suggestion)} type="button">{suggestion}</button>)}
          </div>
          <div className="zhanggui-app__source">
            <button
              aria-label={canPickDataWorkspace ? undefined : '迁移到数据目录（当前宿主不支持）'}
              disabled={!canPickDataWorkspace || sourceLocked}
              onClick={() => void pickDataWorkspace()}
              title={sourceLocked ? '首条问题会继续使用已准备的数据源' : canPickDataWorkspace ? undefined : '当前宿主不支持目录迁移'}
              type="button"
            >
              <FolderOpen size={15} />{dataWorkspaceRoot ? '更换数据目录' : '迁移到数据目录'}
            </button>
            <span>{dataWorkspaceRoot
              ? dataWorkspaceRoot
              : `默认绑定${managedDataSourceLabel} · ${manifest.verticalSuiteId.toUpperCase()} ${manifest.verticalSuiteRevision} · 只读沙箱，不作为真实经营数据${canPickDataWorkspace ? '' : '；当前宿主不支持目录迁移'}`}</span>
          </div>
          {manifest.sandbox ? (
            <label className="zhanggui-app__sandbox-choice">
              <input
                aria-label="启动前运行受管沙箱自测"
                checked={sandboxDecision === 'run'}
                disabled={manifest.sandbox.default !== 'optional' || sourceLocked}
                onChange={(event) => setSandboxEnabled(event.target.checked)}
                type="checkbox"
              />
              <ShieldCheck aria-hidden="true" size={16} />
              <span>
                <strong>启动前运行受管沙箱自测</strong>
                <small>{manifest.sandbox.policyId} · 断网、只读、禁止生产写入</small>
              </span>
            </label>
          ) : null}
          <form onSubmit={(event) => { event.preventDefault(); void startConversation(draft); }}>
            <textarea aria-label={`${activeMode.label}问题`} onChange={(event) => setDraft(event.target.value)} placeholder={activeMode.placeholder} readOnly={sending} rows={3} value={draft} />
            <button aria-label={sending ? '正在发送问题' : '发送'} disabled={sending || !draft.trim()} type="submit">
              {sending ? <LoaderCircle className="ui-spin" size={16} /> : <Send size={16} />}
            </button>
          </form>
          <p>使用普通 Pi Session 与专属 {manifest.skillRef} Skill；SGG 仅用于沙盒自测，不会冒充真实经营数据。</p>
        </section>
      )}
      </div>
    </main>
  );
}

function settleFirstPromptFailure(address: AgentSessionTarget, clientMessageId: string, reason: unknown): void {
  const admissionState = isAgentCommandPending(reason)
    ? isUnresolvedAgentCommandPending(reason) ? 'unresolved' : 'pending'
    : isAmbiguousAgentPromptFailure(reason) ? 'ambiguous' : undefined;
  useAgentLiveStore.getState().failOptimistic(
    address,
    clientMessageId,
    admissionState === 'ambiguous'
      ? '暂时无法确认是否已接收。系统不会自动重试；手动重试会核对同一条消息。'
      : publicAgentErrorText(reason),
    Date.now(),
    admissionState,
  );
}

function bootstrapPrompt(
  skillRef: string,
  mode: (typeof MODES)[number],
  userMessage: string,
  workspaceRoot: string,
  managedDataSourceLabel: string,
  verticalSuiteId: string,
  verticalSuiteRevision: string,
): string {
  return [
    `请加载并严格遵循 \`${skillRef}\` Skill。`,
    `当前掌柜问数模式：${mode.label}（${mode.eyebrow}）。`,
    mode.description,
    '只使用当前 Session 中真实可访问的来源；缺少来源时明确指出，不要用 SGG fixture 代替真实经营数据。',
    workspaceRoot
      ? `用户已显式迁移到数据工作目录：${workspaceRoot}`
      : `默认绑定受控数据源：${managedDataSourceLabel} · ${verticalSuiteId.toUpperCase()} ${verticalSuiteRevision}；该绑定只用于受管只读沙箱自测，不得冒充生产经营数据。`,
    '',
    `用户请求：${userMessage}`,
  ].join('\n');
}

function sessionSummary(
  raw: Record<string, unknown>,
  id: string,
  title: string,
  preview: string,
  workspaceRoot: string,
  ownerAppId: string,
  surfaceKey: ModeId,
): SessionSummary {
  return {
    id,
    title: text(raw.title) || title,
    mode: text(raw.mode) || (workspaceRoot ? 'coordinator' : 'assistant'),
    status: text(raw.status) || 'running',
    roleId: text(raw.roleId),
    roleVersion: text(raw.roleVersion),
    roleBookRevisionId: text(raw.roleBookRevisionId),
    updatedAtMs: typeof raw.updatedAtMs === 'number' ? raw.updatedAtMs : Date.now(),
    workspaceRoots: workspaceRoot ? [workspaceRoot] : [],
    lastMessagePreview: preview,
    executionMode: 'per_action',
    piSkillsEnabled: true,
    surfaceKind: 'extension_app',
    ownerAppId,
    surfaceKey,
  } as SessionSummary;
}

function record(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {};
}

function requireSandboxExperimentReceipt(
  value: unknown,
  expected: {
    sessionId: string;
    ownerAppId: string;
    candidateBindingSha256: string;
    requestedDecision: 'run' | 'skip';
  },
): SandboxExperimentReceipt {
  const receipt = record(value);
  const commonValid = receipt.schemaVersion === 'rag-ime.extension-sandbox-experiment-receipt.v1'
    && receipt.ok === true
    && receipt.sessionId === expected.sessionId
    && receipt.ownerAppId === expected.ownerAppId
    && receipt.candidateBindingSha256 === expected.candidateBindingSha256
    && receipt.requestedDecision === expected.requestedDecision;
  const decisionValid = expected.requestedDecision === 'run'
    ? receipt.executionStatus === 'completed'
      && receipt.executed === true
      && Boolean(text(receipt.sandboxRunId))
      && Boolean(text(receipt.traceId))
      && Boolean(text(receipt.evalRunId))
    : receipt.executionStatus === 'skipped' && receipt.executed === false;
  if (!commonValid || !decisionValid) {
    throw new Error('沙箱运行回执无效，业务对话未启动。');
  }
  return receipt as SandboxExperimentReceipt;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function publicError(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}
