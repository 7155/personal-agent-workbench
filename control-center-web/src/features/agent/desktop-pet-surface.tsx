import { useEffect, useLayoutEffect, useRef, useState, type PointerEvent } from 'react';
import { RoomPlanetAvatar } from '@/features/rooms/RoomPlanetAvatar';
import { PetStatusSignal } from './desktop-pet-status';
import { createPetGesture } from './desktop-pet-interaction';
import './desktop-pet.css';
import { acceptPetSnapshot, petConversationLabel, petPresentation, unavailablePetSnapshot, type PetConversationTarget, type PetSnapshot } from './desktop-pet-snapshot';

type PetHost = {
  ready(): Promise<PetSnapshot>;
  onSnapshot(listener: (snapshot: PetSnapshot) => void): () => void;
  hide(): Promise<void>;
  openAssistant(): Promise<void>;
  openConversation(target: PetConversationTarget): Promise<void>;
  setExpanded(expanded: boolean): Promise<void>;
  drag(phase: 'start' | 'move' | 'end' | 'cancel'): Promise<void>;
};
declare global { interface Window { pawDesktopPet?: PetHost } }

export function DesktopPetSurface() {
  const gesture = useRef(createPetGesture());
  const movePending = useRef(false);
  const [error, setError] = useState('');
  const [snapshot, setSnapshot] = useState(unavailablePetSnapshot);
  const [expanded, setExpanded] = useState(false);
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
    const leaveWindow = () => { focusedConversation.current = null; };
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
      title="拖动这里移动窗口" onPointerDown={(event) => {
        if (event.button !== 0 || !event.isPrimary || !gesture.current.start({ pointerId: event.pointerId, screenX: event.screenX, screenY: event.screenY })) return;
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
      <RoomPlanetAvatar ordinal={0} activity="static" size={expanded ? 64 : 112} decorative />
      <PetStatusSignal key={presentation.state} state={presentation.state} animate className="desktop-pet__signal" />
    </button>
    <span className="desktop-pet__hint" id="pet-status" role="status" aria-live="polite" aria-atomic="true">{host ? presentation.label : '请从 PAW 桌面端开启'}</span>
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
