import { BrainCircuit, LoaderCircle, Send, Sparkles } from 'lucide-react';
import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { useControlTransport } from '@/app/control-transport';
import { sessionItems, type SessionSummary } from '@/features/agent/types';
import { agentCommandReceiptFailure, isAgentCommandPending, isAmbiguousAgentPromptFailure, isUnresolvedAgentCommandPending, publicAgentErrorText } from '@/features/agent/public-error';
import { agentSessionAddress, useAgentLiveStore, type AgentSessionTarget } from '@/features/agent/state/live-store';
import { PawSessionWorkspace } from '@/paw-os/apps/PawSessionWorkspace';
import './memory-steward.css';

const STEWARD_QUESTIONS = [
  '最近有哪些 idea 还没完成？',
  '我最近有哪些安排？',
  '你感觉我最近心情怎么样？',
  '你建议我接下来继续做什么？',
] as const;

type MemoryStewardProps = { date: string; timelineId: string };

export function MemorySteward(props: MemoryStewardProps) {
  // A late receipt may still settle its original Session, but it must never
  // replace another day's composer, draft, or preparation state.
  return <MemoryStewardDay key={props.date} {...props} />;
}

function MemoryStewardDay({ date, timelineId }: MemoryStewardProps) {
  const transport = useControlTransport();
  const surfaceKey = useMemo(() => `journal-${date}`, [date]);
  const [session, setSession] = useState<SessionSummary>();
  const [preparedSession, setPreparedSession] = useState<SessionSummary>();
  const [draft, setDraft] = useState('');
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let active = true;
    setLoading(true);
    setSession(undefined);
    setPreparedSession(undefined);
    setDraft('');
    setError('');
    void transport.request({
      pathId: 'agent.sessions.list',
      query: {
        limit: 20,
        includeArchived: false,
        surfaceKind: 'builtin_app',
        ownerAppId: 'memory',
        surfaceKey,
      },
    }).then((value) => {
      if (!active) return;
      setSession(sessionItems(value, { includeAppOwned: true }).find((item) => (
        String(item.surfaceKind) === 'builtin_app'
        && item.ownerAppId === 'memory'
        && item.surfaceKey === surfaceKey
      )));
    }).catch((reason) => {
      if (active) setError(publicError(reason, '暂时没有读到这一天的管家对话，仍可重新开始。'));
    }).finally(() => {
      if (active) setLoading(false);
    });
    return () => { active = false; };
  }, [surfaceKey, transport]);

  async function send(event: FormEvent): Promise<void> {
    event.preventDefault();
    const question = draft.trim();
    if (!question || sending) return;
    setSending(true);
    setError('');
    try {
      let next = preparedSession;
      if (!next) {
        const ensured = record(await transport.request({
          pathId: 'agent.sessions.surface.ensure',
          body: {
            title: `Memory 管家 · ${date}`,
            mode: 'assistant',
            toolProfileVersion: 'control-center-v1',
            executionMode: 'read_only',
            workspaceRoots: [],
            surfaceKind: 'builtin_app',
            ownerAppId: 'memory',
            surfaceKey,
          },
        }));
        next = sessionItems({ items: [ensured.session] }, { includeAppOwned: true })[0];
        if (!next?.id) throw new Error('服务端没有返回可验证的 Memory 管家 Session。');
        setPreparedSession(next);
      }
      await transport.request({
        pathId: 'agent.session.mode.update',
        params: { sessionId: next.id },
        body: {
          mode: 'assistant',
          executionMode: 'read_only',
          toolProfileVersion: 'control-center-v1',
          projectContextEnabled: false,
          piSkillsEnabled: true,
          codexSkillsEnabled: false,
        },
      });
      const sessionId = next.id;
      const address = agentSessionAddress(transport, sessionId);
      const message = stewardPrompt({ date, message: question, timelineId });
      const clientMessageId = `memory-steward:${surfaceKey}:${crypto.randomUUID()}`;
      const store = useAgentLiveStore.getState();
      // The shared Session must retain the complete command so its normal
      // recovery keeps this date and timeline boundary with the question.
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
      // Once the request returns, the same Session owns the visible turn and
      // its existing confirmation/retry actions. Only confirmed admission
      // clears the starter draft; conflict/cancellation stays editable above.
      setSession(next);
      setPreparedSession(undefined);
    } catch (reason) {
      setError(publicError(reason, '记忆管家没有开始，请稍后重试。'));
    } finally {
      setSending(false);
    }
  }

  return (
    <section className="memory-steward" data-active={session ? true : undefined}>
      <header className="memory-steward__header">
        <span aria-hidden="true" className="memory-steward__mark"><BrainCircuit size={19} /></span>
        <span><small>Memory 管家</small><strong>和这一天的记忆直接聊</strong></span>
        <p>从真实日记、时间线和已治理记忆里找线索；没有证据时会明确说不知道。</p>
      </header>

      {error ? <p className="memory-steward__error" role="alert">{error}</p> : null}
      {loading ? (
        <div className="memory-steward__loading" role="status"><LoaderCircle className="ui-spin" size={16} />正在恢复这一天的对话…</div>
      ) : session ? (
        <div className="memory-steward__session">
          <PawSessionWorkspace
            active
            appearance="embedded"
            composerPlaceholder="继续问最近的 idea、安排、未完成事项，或让我给出下一步建议…"
            onNewWork={() => setSession(undefined)}
            onSessionCreated={setSession}
            onSessionUpdated={setSession}
            record={session}
            recordId={session.id}
          />
        </div>
      ) : (
        <div className="memory-steward__start">
          <div className="memory-steward__suggestions" aria-label="可以问记忆管家">
            {STEWARD_QUESTIONS.map((question) => (
              <button disabled={sending} key={question} onClick={() => setDraft(question)} type="button"><Sparkles size={13} />{question}</button>
            ))}
          </div>
          <form onSubmit={(event) => void send(event)}>
            <textarea
              aria-label="问记忆管家"
              onChange={(event) => setDraft(event.target.value)}
              placeholder="问我最近有哪些 idea、没完成的事、安排，或请我基于证据给建议…"
              readOnly={sending}
              rows={2}
              value={draft}
            />
            <button aria-label="发送给记忆管家" disabled={sending || !draft.trim()} type="submit">
              {sending ? <LoaderCircle className="ui-spin" size={16} /> : <Send size={16} />}
            </button>
          </form>
        </div>
      )}
    </section>
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

function stewardPrompt({ date, message, timelineId }: { date: string; message: string; timelineId: string }): string {
  return [
    '你是 PAW Memory 内的个人记忆整理小管家。',
    '只使用当前 Session 真正可访问的 Memory、时间线、计划和来源工具；不要编造活动、情绪、安排或完成状态。',
    '推测情绪时必须区分事实、迹象和推测，并说明证据不足之处。',
    '回答要简洁、可行动；涉及未完成事项时给出来源和建议的下一步。',
    `当前日记日期：${date}。`,
    timelineId ? `当前真实时间线引用：${timelineId}。` : '当前日期尚无可验证的时间线引用。',
    '',
    `用户问题：${message}`,
  ].join('\n');
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function publicError(reason: unknown, fallback: string): string {
  if (!(reason instanceof Error) || !reason.message) return fallback;
  const normalized = reason.message.trim().toLowerCase();
  if (
    normalized === 'failed to fetch'
    || normalized.includes('networkerror')
    || normalized.includes('network request failed')
  ) return fallback;
  return publicAgentErrorText(reason, fallback);
}
