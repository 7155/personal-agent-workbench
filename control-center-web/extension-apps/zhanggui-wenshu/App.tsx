import { BarChart3, CircleAlert, CircleCheck, FolderOpen, LoaderCircle, MoreHorizontal, PackageOpen, Send, ShieldCheck } from 'lucide-react';
import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';

import { useControlTransport } from '@/app/control-transport';
import { isTeamDeployment, useOptionalTeam } from '@/features/team';
import { openPawOsRoute, usePawOsDesktop } from '@/features/paw-os/surface-context';
import { agentCommandReceiptFailure, isAgentCommandPending, isAmbiguousAgentPromptFailure, isUnresolvedAgentCommandPending, publicAgentErrorText } from '@/features/agent/public-error';
import { useAgentLiveStore } from '@/features/agent/state/live-store';
import { sessionItems, type SessionSummary } from '@/features/agent/types';
import { PawSessionWorkspace } from '@/paw-os/apps/PawSessionWorkspace';
import { PawAppIcon } from '@/paw-os/shell/PawAppIcon';
import { projectPawExtensionTeamSelection } from '@/paw-os/extensions/installation';
import type { PawExtensionAppProps } from '@/paw-os/extensions/types';
import './app.css';

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

type AppOperation = {
  scopeKey: string;
  generation: number;
  controller: AbortController;
};

export default function ZhangguiWenshuApp({ manifest }: PawExtensionAppProps) {
  const transport = useControlTransport();
  const desktop = usePawOsDesktop();
  const team = useOptionalTeam();
  const teamMode = isTeamDeployment();
  const teamScopeKey = teamMode ? team?.scopeKey ?? '' : 'local';
  const teamSpaceId = team?.activeSpace?.id ?? '';
  const teamSpaceKind = team?.activeSpace?.kind ?? '';
  const teamUserId = team?.user?.id ?? '';
  const appSandbox = teamMode ? undefined : manifest.sandbox;
  const [modeId, setModeId] = useState<ModeId>('ask');
  const [sessions, setSessions] = useState<Partial<Record<ModeId, SessionSummary>>>({});
  const [preparedSessions, setPreparedSessions] = useState<Partial<Record<ModeId, SessionPreparation>>>({});
  const [drafts, setDrafts] = useState<Partial<Record<ModeId, string>>>({});
  const [workspaceRoot, setWorkspaceRoot] = useState('');
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [sandboxEnabled, setSandboxEnabled] = useState(appSandbox?.default === 'required');
  const [sandboxReceipt, setSandboxReceipt] = useState<SandboxExperimentReceipt | null>(null);
  const [error, setError] = useState('');
  const [historyError, setHistoryError] = useState('');
  const [historyRevision, setHistoryRevision] = useState(0);
  const scopeRef = useRef(teamScopeKey);
  const scopeGenerationRef = useRef(0);
  const mountedRef = useRef(true);
  const activeOperationRef = useRef<AppOperation | null>(null);
  scopeRef.current = teamScopeKey;
  const modeTabsId = useId();
  const modeTabRefs = useRef(new Map<ModeId, HTMLButtonElement>());
  const activeMode = MODES.find((mode) => mode.id === modeId)!;
  const activeSession = sessions[modeId];
  const preparedSession = preparedSessions[modeId]?.session;
  const sandboxDecision = preparedSessions[modeId]?.sandbox?.requestedDecision
    ?? (appSandbox?.default === 'required' || sandboxEnabled ? 'run' : 'skip');
  const draft = drafts[modeId] ?? '';
  const visibleError = error || historyError;
  const managedDataSourceLabel = teamMode && team?.activeSpace
    ? `${manifest.label} · ${team.activeSpace.name}受控数据`
    : `${manifest.label}受控数据`;
  const dataWorkspaceRoot = teamMode
    ? ''
    : preparedSession ? preparedSession.workspaceRoots[0] ?? '' : workspaceRoot;
  const selectedWorkspaceName = dataWorkspaceRoot.split(/[\\/]/).filter(Boolean).at(-1) ?? '';
  const dataSourceLabel = selectedWorkspaceName || managedDataSourceLabel;
  const canPickDataWorkspace = !teamMode && typeof transport.pickFiles === 'function';
  const sourceLocked = sending || Boolean(preparedSession);

  useEffect(() => {
    const generation = ++scopeGenerationRef.current;
    mountedRef.current = true;
    return () => {
      if (scopeGenerationRef.current !== generation) return;
      mountedRef.current = false;
      scopeGenerationRef.current += 1;
      activeOperationRef.current?.controller.abort();
      activeOperationRef.current = null;
    };
  }, [teamScopeKey]);

  useEffect(() => {
    setSessions({});
    setPreparedSessions({});
    setDrafts({});
    setWorkspaceRoot('');
    setSandboxReceipt(null);
    setSending(false);
    setError('');
    setHistoryError('');
  }, [teamScopeKey]);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setHistoryError('');
    if (teamMode && (
      team?.phase !== 'authenticated'
      || !teamSpaceId
      || !teamUserId
      || !teamScopeKey
    )) {
      setLoading(false);
      return () => { active = false; };
    }
    const historyRequest = teamMode && teamSpaceKind === 'project'
      ? team!.listProjectSessions(teamSpaceId, {
        surfaceKind: 'extension_app',
        ownerAppId: manifest.id,
        includeArchived: false,
        limit: 100,
      })
      : transport.request({
        pathId: 'agent.sessions.list',
        query: {
          limit: 100,
          includeArchived: false,
          surfaceKind: 'extension_app',
          ownerAppId: manifest.id,
        },
      });
    void historyRequest
      .then(async (value) => {
        if (!active) return;
        const candidates: SessionSummary[] = [];
        const candidateSessions = Array.isArray(value)
          ? value as SessionSummary[]
          : sessionItems(value, { includeAppOwned: true });
        for (const session of candidateSessions) {
          if (session.surfaceKind !== 'extension_app' || session.ownerAppId !== manifest.id) continue;
          if (teamMode && (
            session.ownerUserId !== teamUserId
            || session.canControl !== true
            || (session.spaceId && session.spaceId !== teamSpaceId)
          )) continue;
          const surfaceKey = session.surfaceKey as ModeId;
          if (!MODES.some((mode) => mode.id === surfaceKey) || candidates.some((item) => item.surfaceKey === surfaceKey)) continue;
          candidates.push(session);
          if (candidates.length >= MODES.length) break;
        }
        const restored: Partial<Record<ModeId, SessionSummary>> = {};
        if (teamMode && teamSpaceId && team?.api) {
          const snapshots = await Promise.allSettled(candidates.map((session) => (
            team.api.getSessionResourceSnapshot(teamSpaceId, session.id, team.csrfToken ?? undefined)
          )));
          if (!active) return;
          snapshots.forEach((snapshot, index) => {
            const session = candidates[index];
            if (!session || snapshot.status !== 'fulfilled') return;
            if (!teamSessionSnapshotMatchesApp(snapshot.value, teamSpaceId, session.id, manifest.id)) return;
            restored[session.surfaceKey as ModeId] = session;
          });
          if (candidates.length > 0 && Object.keys(restored).length === 0) {
            setHistoryError('没有找到与当前掌柜问数版本匹配的固定资源记录。');
          } else {
            setHistoryError('');
          }
        } else {
          for (const session of candidates) restored[session.surfaceKey as ModeId] = session;
        }
        setSessions(restored);
        if (!teamMode || candidates.length === 0 || Object.keys(restored).length > 0) setHistoryError('');
      })
      .catch((reason) => {
        if (active) setHistoryError(publicError(reason, '没有读到掌柜问数的对话记录。'));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, [historyRevision, manifest.id, team?.api, team?.listProjectSessions, team?.phase, teamMode, teamScopeKey, teamSpaceId, teamSpaceKind, teamUserId, transport]);

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
    const operation: AppOperation = {
      scopeKey: teamScopeKey,
      generation: scopeGenerationRef.current,
      controller: new AbortController(),
    };
    activeOperationRef.current = operation;
    const isCurrentOperation = () => (
      mountedRef.current
      && scopeRef.current === operation.scopeKey
      && scopeGenerationRef.current === operation.generation
      && activeOperationRef.current === operation
      && !operation.controller.signal.aborted
    );
    setSending(true);
    setError('');
    try {
      let preparation = preparedSessions[modeId];
      if (!preparation) {
        const created = await transport.request<Record<string, unknown>>({
          pathId: 'agent.sessions.create',
          body: {
            title: `${manifest.label} · ${activeMode.label}`,
            mode: teamMode ? 'coordinator' : workspaceRoot ? 'coordinator' : 'assistant',
            toolProfileVersion: 'control-center-v1',
            executionMode: teamMode ? 'workspace_managed' : 'per_action',
            workspaceRoots: teamMode ? [] : workspaceRoot ? [workspaceRoot] : [],
            surfaceKind: 'extension_app',
            ownerAppId: manifest.id,
            surfaceKey: modeId,
            ...(teamMode ? { piSkillsEnabled: false, codexSkillsEnabled: false } : {}),
          },
          signal: operation.controller.signal,
        });
        if (!isCurrentOperation()) return;
        const raw = record(record(created).session);
        const sessionId = text(raw.id);
        if (!sessionId) throw new Error('服务端没有返回可验证的 Session。');
        preparation = {
          session: sessionSummary(
            raw, sessionId, `${manifest.label} · ${activeMode.label}`,
            userMessage, workspaceRoot, manifest.id, modeId, teamMode,
          ),
          modeReady: false,
          ...(appSandbox ? {
            sandbox: { experimentId: `experiment:${modeId}:${Date.now()}`, requestedDecision: sandboxDecision },
          } : {}),
        };
        // Creation has already happened. Keep that identity even if the next
        // preparation step fails; a user retry resumes only unfinished steps.
        setPreparedSessions((current) => ({ ...current, [modeId]: preparation }));
      }
      if (!isCurrentOperation()) return;
      const session = preparation.session;
      const sessionId = session.id;
      if (!preparation.modeReady) {
        await transport.request({
          pathId: 'agent.session.mode.update',
          params: { sessionId },
          body: {
            mode: teamMode ? 'coordinator' : session.workspaceRoots.length ? 'coordinator' : 'assistant',
            executionMode: teamMode ? 'workspace_managed' : 'per_action',
            toolProfileVersion: 'control-center-v1',
            projectContextEnabled: false,
            piSkillsEnabled: teamMode ? false : true,
            codexSkillsEnabled: false,
            ...(teamMode ? { workspaceRoots: [] } : {}),
          },
          signal: operation.controller.signal,
        });
        if (!isCurrentOperation()) return;
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
          signal: operation.controller.signal,
        });
        if (!isCurrentOperation()) return;
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
      if (!isCurrentOperation()) return;
      const message = bootstrapPrompt(
        manifest.skillRef,
        activeMode,
        userMessage,
        session.workspaceRoots[0] ?? '',
        managedDataSourceLabel,
        manifest.verticalSuiteId,
        manifest.verticalSuiteRevision,
        teamMode,
      );
      const clientMessageId = `extension:${manifest.id}:${modeId}:${crypto.randomUUID()}`;
      const store = useAgentLiveStore.getState();
      // Keep the exact App context with the user's question: the existing
      // Session retry must recover this command, not silently omit the Skill
      // or source boundaries on its second attempt.
      if (!isCurrentOperation()) return;
      store.appendOptimistic(sessionId, { clientMessageId, text: message, nowMs: Date.now() });
      try {
        const response = record(await transport.request({
          pathId: 'agent.session.prompt',
          params: { sessionId },
          body: { message, clientMessageId, delivery: 'prompt' },
          signal: operation.controller.signal,
        }));
        if (!isCurrentOperation()) return;
        if (response.accepted === false && response.cancelled === true && response.admissionCancelled === true) {
          store.discardOptimistic(sessionId, clientMessageId);
          setError('这条消息已取消，原问题已保留；可在同一对话中重新发送。');
          return;
        }
        store.acknowledgeOptimistic(sessionId, clientMessageId, Date.now());
        setDraft('');
      } catch (reason) {
        if (!isCurrentOperation()) return;
        if (agentCommandReceiptFailure(reason)?.code === 'AGENT_COMMAND_CONFLICT') {
          store.discardOptimistic(sessionId, clientMessageId);
          setError(publicAgentErrorText(reason));
          return;
        }
        settleFirstPromptFailure(sessionId, clientMessageId, reason);
      }
      // The first composer owns the draft until admission settles. Afterwards
      // the same Session owns its visible pending/failed turn and recovery.
      // Proven non-admission above leaves the prepared Session in place so an
      // explicit new send cannot create another Session or rerun its sandbox.
      if (!isCurrentOperation()) return;
      setSessions((current) => ({ ...current, [modeId]: session }));
      setPreparedSessions((current) => {
        const next = { ...current };
        delete next[modeId];
        return next;
      });
    } catch (reason) {
      if (!isCurrentOperation()) return;
      setError(publicError(reason, '掌柜问数没有开始，请重试。'));
    } finally {
      if (activeOperationRef.current === operation) {
        activeOperationRef.current = null;
        if (mountedRef.current && scopeRef.current === operation.scopeKey && scopeGenerationRef.current === operation.generation) {
          setSending(false);
        }
      }
    }
  }

  async function pickDataWorkspace(): Promise<void> {
    if (teamMode || !transport.pickFiles || sourceLocked) return;
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
          {teamMode ? (
            <span aria-label={`当前团队空间数据源 ${dataSourceLabel}`} className="zhanggui-app__data-button">
              <FolderOpen size={15} />
              <span>{dataSourceLabel}</span>
            </span>
          ) : (
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
          )}
          <span className="zhanggui-app__status" data-status="selected"><i />{teamMode ? '已绑定团队空间数据' : dataWorkspaceRoot ? '已选择数据目录' : '已选择受控数据'}</span>
          <details className="zhanggui-app__more">
            <summary aria-label="掌柜问数更多操作"><MoreHorizontal size={18} /></summary>
            <div>
              <small>{teamMode ? `团队共享版本 · v${manifest.version}` : `沙箱套件 · SGG ${manifest.verticalSuiteRevision}`}</small>
              <button onClick={() => openPawOsRoute(desktop, `/plugins?packageId=${encodeURIComponent(manifest.packageId)}`)} type="button"><PackageOpen size={15} />{teamMode ? '查看共享版本' : '管理与卸载'}</button>
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
      {!teamMode && sandboxReceipt ? (
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
          <PawSessionWorkspace
            active
            appearance="embedded"
            composerPlaceholder={`${activeMode.placeholder.replace('例如：', '')}，或继续追问…`}
            onNewWork={resetMode}
            onSessionCreated={(session) => {
              const next = { ...sessions, [modeId]: session };
              setSessions(next);
            }}
            onSessionUpdated={(session) => {
              setSessions((current) => {
                return { ...current, [modeId]: session };
              });
            }}
            record={activeSession}
            recordId={activeSession.id}
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
            {!teamMode ? (
              <button
                aria-label={canPickDataWorkspace ? undefined : '迁移到数据目录（当前宿主不支持）'}
                disabled={!canPickDataWorkspace || sourceLocked}
                onClick={() => void pickDataWorkspace()}
                title={sourceLocked ? '首条问题会继续使用已准备的数据源' : canPickDataWorkspace ? undefined : '当前宿主不支持目录迁移'}
                type="button"
              >
                <FolderOpen size={15} />{dataWorkspaceRoot ? '更换数据目录' : '迁移到数据目录'}
              </button>
            ) : <span>团队空间托管数据源</span>}
            <span>{dataWorkspaceRoot
              ? dataWorkspaceRoot
              : teamMode
                ? `当前空间${team?.activeSpace?.name ?? ''}的数据由团队服务托管；不会读取宿主目录或个人扩展能力。`
                : `默认绑定${managedDataSourceLabel} · ${manifest.verticalSuiteId.toUpperCase()} ${manifest.verticalSuiteRevision} · 只读沙箱，不作为真实经营数据${canPickDataWorkspace ? '' : '；当前宿主不支持目录迁移'}`}</span>
          </div>
          {appSandbox ? (
            <label className="zhanggui-app__sandbox-choice">
              <input
                aria-label="启动前运行受管沙箱自测"
                checked={sandboxDecision === 'run'}
                disabled={appSandbox.default !== 'optional' || sourceLocked}
                onChange={(event) => setSandboxEnabled(event.target.checked)}
                type="checkbox"
              />
              <ShieldCheck aria-hidden="true" size={16} />
              <span>
                <strong>启动前运行受管沙箱自测</strong>
                <small>{appSandbox.policyId} · 断网、只读、禁止生产写入</small>
              </span>
            </label>
          ) : null}
          <form onSubmit={(event) => { event.preventDefault(); void startConversation(draft); }}>
            <textarea aria-label={`${activeMode.label}问题`} onChange={(event) => setDraft(event.target.value)} placeholder={activeMode.placeholder} readOnly={sending} rows={3} value={draft} />
            <button aria-label={sending ? '正在发送问题' : '发送'} disabled={sending || !draft.trim()} type="submit">
              {sending ? <LoaderCircle className="ui-spin" size={16} /> : <Send size={16} />}
            </button>
          </form>
          <p>{teamMode
            ? `使用当前团队空间托管的普通 Pi Session 与已选 ${manifest.skillRef} Skill；项目空间的对话对项目成员可见。`
            : `使用普通 Pi Session 与专属 ${manifest.skillRef} Skill；SGG 仅用于沙盒自测，不会冒充真实经营数据。`}</p>
        </section>
      )}
      </div>
    </main>
  );
}

function settleFirstPromptFailure(sessionId: string, clientMessageId: string, reason: unknown): void {
  const admissionState = isAgentCommandPending(reason)
    ? isUnresolvedAgentCommandPending(reason) ? 'unresolved' : 'pending'
    : isAmbiguousAgentPromptFailure(reason) ? 'ambiguous' : undefined;
  useAgentLiveStore.getState().failOptimistic(
    sessionId,
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
  teamMode = false,
): string {
  return [
    `请加载并严格遵循 \`${skillRef}\` Skill。`,
    `当前掌柜问数模式：${mode.label}（${mode.eyebrow}）。`,
    mode.description,
    '只使用当前 Session 中真实可访问的来源；缺少来源时明确指出，不要用 SGG fixture 代替真实经营数据。',
    teamMode
      ? `当前使用团队空间的受控数据源：${managedDataSourceLabel}；不会读取宿主目录或个人 Skills。`
      : workspaceRoot
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
  teamMode = false,
): SessionSummary {
  const rawOwnerUserId = text(raw.ownerUserId);
  const rawSpaceId = text(raw.spaceId);
  return {
    id,
    title: text(raw.title) || title,
    mode: text(raw.mode) || (teamMode || workspaceRoot ? 'coordinator' : 'assistant'),
    status: text(raw.status) || 'running',
    roleId: text(raw.roleId),
    roleVersion: text(raw.roleVersion),
    roleBookRevisionId: text(raw.roleBookRevisionId),
    updatedAtMs: typeof raw.updatedAtMs === 'number' ? raw.updatedAtMs : Date.now(),
    workspaceRoots: teamMode ? [] : workspaceRoot ? [workspaceRoot] : [],
    lastMessagePreview: preview,
    executionMode: teamMode ? 'workspace_managed' : 'per_action',
    piSkillsEnabled: teamMode ? false : true,
    surfaceKind: 'extension_app',
    ownerAppId,
    surfaceKey,
    ...(rawOwnerUserId ? { ownerUserId: rawOwnerUserId } : {}),
    ...(rawSpaceId ? { spaceId: rawSpaceId } : {}),
    ...(typeof raw.canControl === 'boolean' ? { canControl: raw.canControl } : {}),
    ...(typeof raw.audience === 'string' ? { audience: raw.audience } : {}),
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

function teamSessionSnapshotMatchesApp(
  value: unknown,
  spaceId: string,
  sessionId: string,
  appId: string,
): boolean {
  const snapshot = record(value);
  if (text(snapshot.sessionId) !== sessionId || text(snapshot.spaceId) !== spaceId) return false;
  return projectPawExtensionTeamSelection(snapshot).availableExtensionIds.has(appId as `extension:${string}`);
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
