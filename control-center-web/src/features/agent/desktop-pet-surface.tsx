import { useEffect, useLayoutEffect, useRef, useState, type PointerEvent } from 'react';
import { ChatPresentationProvider, useChatPresentation } from '@/features/conversation-ui/reading/chat-presentation';
import { RoomPlanetAvatar } from '@/features/rooms/RoomPlanetAvatar';
import { PetStatusSignal } from './desktop-pet-status';
import { createPetGesture } from './desktop-pet-interaction';
import './desktop-pet.css';
import { acceptPetSnapshot, petConversationLabel, petPresentation, unavailablePetSnapshot, type PetConversationTarget, type PetSnapshot } from './desktop-pet-snapshot';

type PetDirection = 'left' | 'right' | 'up' | 'down';
const moveKeys: Record<string, PetDirection | undefined> = { ArrowLeft: 'left', ArrowRight: 'right', ArrowUp: 'up', ArrowDown: 'down' };
type PetHost = {
  ready(): Promise<PetSnapshot>;
  onSnapshot(listener: (snapshot: PetSnapshot) => void): () => void;
  hide(): Promise<void>;
  openAssistant(): Promise<void>;
  openConversation(target: PetConversationTarget): Promise<void>;
  setExpanded(expanded: boolean): Promise<void>;
  drag(phase: 'start' | 'move' | 'end' | 'cancel'): Promise<void>;
  move(direction: PetDirection): Promise<void>;
};
declare global { interface Window { pawDesktopPet?: PetHost } }

export function DesktopPetSurface() {
  return <ChatPresentationProvider ownerKey="builtin:desktop-pet" defaultVersion="v2"><DesktopPetBody/></ChatPresentationProvider>;
}
function DesktopPetBody() {
  const avatarPresentation = useChatPresentation();
  const gesture = useRef(createPetGesture());
  const movePending = useRef(false);
  const [error, setError] = useState('');
  const [snapshot, setSnapshot] = useState(unavailablePetSnapshot);
  const [expanded, setExpanded] = useState(false);
  const [keyboardMoving, setKeyboardMoving] = useState(false);
  const expansionRequest = useRef(0);
  const planet = useRef<HTMLButtonElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const focusedConversation = useRef<HTMLElement | null>(null);
  const host = window.pawDesktopPet;
  useEffect(() => {
    if (!host) return;
    let active = true;
    const receive = (next: PetSnapshot) => { if (active) setSnapshot(previous => acceptPetSnapshot(previous, next)); };
    // Subscribe first: a newer pushed revision wins over a delayed ready replay.
    const unsubscribe = host.onSnapshot(receive);
    void host.ready().then(receive).catch(() => { if (active) setError('桌面伙伴未就绪，请重新开启'); });
    const leaveWindow = () => { focusedConversation.current = null; setKeyboardMoving(false); };
    window.addEventListener('blur', leaveWindow);
    return () => { active = false; expansionRequest.current += 1; unsubscribe(); window.removeEventListener('blur', leaveWindow); };
  }, [host]);
  useEffect(() => { if (expanded && document.hasFocus()) list.current?.querySelector('button')?.focus(); }, [expanded]);
  useLayoutEffect(() => {
    const previous = focusedConversation.current;
    if (!expanded || !previous || previous.isConnected) return;
    focusedConversation.current = null;
    // Recover only a removed control in this focused window. A directory
    // refresh must never move focus away from another control or application.
    if (document.hasFocus() && (document.activeElement === document.body || document.activeElement === document.documentElement)) planet.current?.focus();
  }, [expanded, snapshot]);
  const presentation = petPresentation(snapshot);
  const total = Object.values(snapshot.counts).reduce((sum, count) => sum + count, 0);
  const invoke = (action: Promise<void> | undefined) => { void action?.catch(() => setError('操作未完成，请重试')); };
  const expand = (next: boolean) => {
    if (!host) return;
    const request = ++expansionRequest.current;
    void host.setExpanded(next).then(() => {
      if (request !== expansionRequest.current) return;
      setExpanded(next); setError('');
      if (!next && document.hasFocus()) planet.current?.focus();
    }).catch(() => { if (request === expansionRequest.current) setError('操作未完成，请重试'); });
  };
  const finish = (event: PointerEvent<HTMLButtonElement>, cancelled = false) => {
    if (!gesture.current.end(event.pointerId, Date.now(), cancelled)) return;
    invoke(host?.drag(cancelled ? 'cancel' : 'end'));
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  };
  return <main className="desktop-pet" data-expanded={expanded} aria-label="PAW 桌面伙伴"
    onFocusCapture={(event) => { focusedConversation.current = event.target instanceof HTMLElement && event.target.hasAttribute('data-pet-conversation') ? event.target : null; }}
    onBlurCapture={(event) => { if (event.relatedTarget instanceof Node && !event.currentTarget.contains(event.relatedTarget)) focusedConversation.current = null; }}
    onKeyDown={(event) => {
    if (event.key === 'Escape') { event.stopPropagation(); if (expanded) expand(false); else invoke(host?.hide()); }
  }}>
    <button className="desktop-pet__drag" type="button" aria-label="移动桌面伙伴" disabled={!host}
      aria-pressed={keyboardMoving} aria-description="按 Enter 或空格开始，再用方向键移动；Enter 或 Escape 结束"
      title="拖动，或按 Enter 后用方向键移动" onClick={(event) => {
        if (event.detail === 0 && gesture.current.canActivate(Date.now())) setKeyboardMoving(value => !value);
      }} onBlur={() => setKeyboardMoving(false)} onKeyDown={(event) => {
        if (!keyboardMoving) return;
        if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); setKeyboardMoving(false); return; }
        const direction = moveKeys[event.key];
        if (!direction) return;
        event.preventDefault(); event.stopPropagation();
        if (!host || movePending.current) return;
        movePending.current = true;
        void host.move(direction).catch(() => setError('操作未完成，请重试')).finally(() => { movePending.current = false; });
      }} onPointerDown={(event) => {
        if (event.button !== 0 || !event.isPrimary || !gesture.current.start({ pointerId: event.pointerId, screenX: event.screenX, screenY: event.screenY })) return;
        setKeyboardMoving(false);
        event.currentTarget.setPointerCapture(event.pointerId); invoke(host?.drag('start'));
      }} onPointerMove={(event) => {
        if (!gesture.current.move(event) || !host || movePending.current) return;
        movePending.current = true;
        void host.drag('move').catch(() => setError('操作未完成，请重试')).finally(() => { movePending.current = false; });
      }} onPointerUp={(event) => finish(event)} onPointerCancel={(event) => finish(event, true)} onLostPointerCapture={(event) => finish(event, true)}>
      <span aria-hidden="true">⠿</span><span>PAW</span>
    </button>
    <button className="desktop-pet__hide" type="button" aria-label="隐藏桌面伙伴" disabled={!host} onClick={() => invoke(host?.hide())}>×</button>
    <button className="desktop-pet__planet" type="button" aria-label="查看后台对话" aria-expanded={expanded}
      aria-controls="pet-conversations" aria-describedby="pet-status" disabled={!host} ref={planet} title="点击查看对话 · 拖动上方把手移动"
      onClick={() => { if (gesture.current.canActivate(Date.now())) expand(!expanded); }}>
      <RoomPlanetAvatar variant={avatarPresentation?.version === 'v2' ? 'sphere' : 'classic'} showSignal={false} signal={presentation.state === 'running' ? 'working' : 'idle'} interactive={Boolean(host)} motion="full" ordinal={0} activity="static" size={expanded ? 64 : 112} decorative />
      <PetStatusSignal key={presentation.state} state={presentation.state} animate className="desktop-pet__signal" />
    </button>
    <span className="desktop-pet__hint" id="pet-status" role="status" aria-live="polite" aria-atomic="true">{keyboardMoving ? '方向键移动，Esc 结束' : host ? presentation.label : '请从 PAW 桌面端开启'}</span>
    {expanded ? <section className="desktop-pet__panel" id="pet-conversations" aria-label="后台对话">
      <header><strong>对话近况</strong><button type="button" onClick={() => expand(false)} aria-label="收起对话列表">收起</button></header>
      <div className="desktop-pet__list" ref={list}>
        {snapshot.conversations.length ? snapshot.conversations.map(conversation => <button className="desktop-pet__conversation" key={`${snapshot.producerEpoch}:${conversation.id}`}
          data-pet-conversation={conversation.id} type="button" onClick={() => invoke(host?.openConversation({ id: conversation.id,
            producerEpoch: snapshot.producerEpoch, sourceId: snapshot.sourceId, scopeId: snapshot.scopeId }))} title={conversation.label}>
          <PetStatusSignal state={conversation.state} /><span>{conversation.label}</span><small>{petConversationLabel[conversation.state]}</small>
        </button>) : <p>{snapshot.freshness === 'synced' ? '当前目录还没有对话。' : '打开工作台后，同步对话状态。'}</p>}
      </div>
      <footer><span>{total > snapshot.conversations.length ? `当前目录另有 ${total - snapshot.conversations.length} 个` : '来自当前工作台目录'}</span>
        <button type="button" onClick={() => invoke(host?.openAssistant())}>打开主助手 ↗</button></footer>
    </section> : null}
    {error ? <span className="desktop-pet__error" role="alert">{error}</span> : null}
  </main>;
}
