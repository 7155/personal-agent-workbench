import { useEffect, useRef, useState, type PointerEvent } from 'react';
import { PersonaAvatar } from '@/features/agent/timeline/PersonaAvatar';
import { createPetGesture } from './desktop-pet-interaction';
import './desktop-pet.css';

type PetHost = {
  ready(): Promise<void>;
  hide(): Promise<void>;
  openAssistant(): Promise<void>;
  drag(phase: 'start' | 'move' | 'end' | 'cancel'): Promise<void>;
};
declare global { interface Window { pawDesktopPet?: PetHost } }

export function DesktopPetSurface() {
  const gesture = useRef(createPetGesture());
  const movePending = useRef(false);
  const [error, setError] = useState('');
  const host = window.pawDesktopPet;
  useEffect(() => { void host?.ready().catch(() => setError('桌面伙伴未就绪，请重新开启')); }, [host]);
  const invoke = (action: Promise<void> | undefined) => { void action?.catch(() => setError('操作未完成，请重试')); };
  const finish = (event: PointerEvent<HTMLButtonElement>, cancelled = false) => {
    if (!gesture.current.end(event.pointerId, Date.now(), cancelled)) return;
    invoke(host?.drag(cancelled ? 'cancel' : 'end'));
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  };
  return <main className="desktop-pet" aria-label="PAW 桌面伙伴" onKeyDown={(event) => {
    if (event.key === 'Escape') invoke(host?.hide());
  }}>
    <button className="desktop-pet__hide" type="button" aria-label="隐藏桌面伙伴" disabled={!host} onClick={() => invoke(host?.hide())}>×</button>
    <button className="desktop-pet__avatar" type="button" aria-label="打开 PAW 主助手" disabled={!host}
      title="拖动移动 · 双击打开主助手 · Esc 隐藏"
      onPointerDown={(event) => {
        if (event.button !== 0 || !event.isPrimary || !gesture.current.start({ pointerId: event.pointerId, screenX: event.screenX, screenY: event.screenY })) return;
        event.currentTarget.setPointerCapture(event.pointerId);
        invoke(host?.drag('start'));
      }}
      onPointerMove={(event) => {
        if (!gesture.current.move(event) || !host || movePending.current) return;
        // Backpressure: never build an IPC promise queue from pointermove.
        // The host samples its current cursor again at the end of a drag.
        movePending.current = true;
        void host.drag('move').catch(() => setError('操作未完成，请重试')).finally(() => { movePending.current = false; });
      }}
      onPointerUp={(event) => finish(event)} onPointerCancel={(event) => finish(event, true)}
      onLostPointerCapture={(event) => finish(event, true)}
      onDoubleClick={() => { if (gesture.current.canActivate(Date.now())) invoke(host?.openAssistant()); }}
      onClick={(event) => { if (event.detail === 0) invoke(host?.openAssistant()); }}>
      <PersonaAvatar fallbackName="PAW" presence="idle" size="hero" />
    </button>
    <span className="desktop-pet__hint">{host ? '双击打开主助手' : '请从 PAW 桌面端开启'}</span>
    {error ? <span className="desktop-pet__error" role="status">{error}</span> : null}
  </main>;
}
