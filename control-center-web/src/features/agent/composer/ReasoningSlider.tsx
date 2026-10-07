import { useEffect, useRef, useState, type CSSProperties } from 'react';
import { ReasoningSliderEffects } from './ReasoningSliderEffects';
import type { ThinkingLevel } from '../types';

/** Native range implements PAW's catalog-defined discrete transaction.
 * Interaction reference: bakabaicai/DSH-Codex-reasoning-effort-slider,
 * 6e71fa408bc2fb5e9b20ed040b1ad42ed49f4333 (MIT, Nachoneko_miao, 2026).
 * This is an independent implementation; no injected DOM or catalog owner is copied.
 * Adapted visual effects have their own MIT header and third-party notice. Preview is local; only a completed gesture reaches Pi's owner. */
export function ReasoningSlider({ levels, selected, disabled, effectsEnabled = false, label, onPreview, onCommit }: {
  levels: ThinkingLevel[]; selected: ThinkingLevel; disabled: boolean; effectsEnabled?: boolean;
  label: (level: ThinkingLevel) => string;
  onPreview: (level: ThinkingLevel | undefined) => void;
  onCommit: (level: ThinkingLevel) => void;
}) {
  const selectedIndex = Math.max(0, levels.indexOf(selected));
  const [index, setIndex] = useState(selectedIndex);
  const [dragging, setDragging] = useState(false);
  const velocity = useRef({speed:0,at:0});
  const pointerPosition = useRef({x:0,at:0});
  const gesture = useRef<number | 'keyboard' | undefined>(undefined);
  const currentIndex = useRef(selectedIndex);
  const levelKey = levels.join('|');
  const cancel = () => {
    gesture.current = undefined; currentIndex.current = selectedIndex; setDragging(false); velocity.current.speed = 0;
    setIndex(selectedIndex); onPreview(undefined);
  };
  useEffect(() => {
    gesture.current = undefined; currentIndex.current = selectedIndex; setDragging(false); velocity.current.speed = 0;
    setIndex(selectedIndex); onPreview(undefined);
  }, [selectedIndex, levelKey, disabled, onPreview]);
  const commit = (next: number) => {
    gesture.current = undefined; setDragging(false); velocity.current.speed = 0;
    if (!disabled && levels[next] && levels[next] !== selected) onCommit(levels[next]);
    else onPreview(undefined);
  };
  const fraction = levels.length > 1 ? index / (levels.length - 1) : 0;
  return <div className="agent-reasoning-slider" data-dragging={dragging} data-effects-max={levels[index] === 'max'}
    style={{ '--slider-fraction': fraction } as CSSProperties}>
    <ReasoningSliderEffects enabled={effectsEnabled && !disabled} maximum={levels[index] === 'max'} dragging={dragging} velocity={velocity}/>
    <div className="agent-reasoning-slider__ticks" aria-hidden="true">
      {levels.map((level, n) => <span key={level} data-selected={n === index} data-passed={n <= index}
        style={{ '--tick-fraction': levels.length > 1 ? n / (levels.length - 1) : 0 } as CSSProperties}/>) }
    </div>
    <input aria-label="推理强度" aria-valuetext={label(levels[index] ?? selected)}
      disabled={disabled || levels.length < 2} min={0} max={Math.max(0, levels.length - 1)} step={1}
      type="range" value={index}
      onChange={event => {
        if (disabled) return;
        const next = Number(event.currentTarget.value); currentIndex.current = next;
        setIndex(next); onPreview(levels[next]);
        // Assistive technology may change a native range without pointer/key
        // events. Its single change is still a complete selection.
        if (gesture.current === undefined) commit(next);
      }}
      onPointerDown={event => {
        if (disabled || levels.length < 2 || event.button !== 0) return;
        gesture.current = event.pointerId; setDragging(true);
        pointerPosition.current = {x:event.clientX,at:performance.now()};
        event.currentTarget.setPointerCapture?.(event.pointerId);
      }}
      onPointerMove={event => {
        if (gesture.current !== event.pointerId) return;
        const now=performance.now(), delta=Math.max(1,now-pointerPosition.current.at);
        velocity.current={speed:Math.min(1,Math.abs(event.clientX-pointerPosition.current.x)/delta),at:now};
        pointerPosition.current={x:event.clientX,at:now};
      }}
      onPointerUp={event => {
        if (gesture.current !== event.pointerId) return;
        commit(Number(event.currentTarget.value));
      }}
      onPointerCancel={cancel}
      onLostPointerCapture={() => { if (typeof gesture.current === 'number') cancel(); }}
      onKeyDown={event => {
        if (event.key === 'Escape') { cancel(); return; }
        if (!event.altKey && !event.ctrlKey && !event.metaKey && ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) gesture.current = 'keyboard';
      }}
      onKeyUp={event => {
        if (gesture.current === 'keyboard' && ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) commit(currentIndex.current);
      }}
      onBlur={cancel}/>
  </div>;
}
