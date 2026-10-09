import { useEffect, useRef, useState } from 'react';
import { useChatPresentation } from '@/features/conversation-ui/reading/chat-presentation';
import { PLANET_SIGNAL_PALETTE } from '@/features/rooms/sphere-avatar/sphere-avatar-protocol';
import { useMotionActivity } from '@/design/motion';
import type { PetConversationState } from './desktop-pet-snapshot';

/** Always accompanied by visible status text. Row signals stay still; only the
 * aggregate running arc loops. No task state changes the character's mood. */
export function PetStatusSignal({ state, animate = false, className = '', palette }: {
  state: PetConversationState; animate?: boolean; className?: string; palette?: 'classic' | 'planet';
}) {
  const motionActive = useMotionActivity();
  const presentation = useChatPresentation();
  const previous = useRef(state);
  const [arrivalActive, setArrivalActive] = useState(() => animate && motionActive && (state === 'attention' || state === 'error'));
  useEffect(() => {
    const entered = previous.current !== state;
    previous.current = state;
    // Activity recovery never replays a notice or a state received while quiet.
    if (!animate || !motionActive) setArrivalActive(false);
    else if (entered) setArrivalActive(state === 'attention' || state === 'error');
  }, [state, animate, motionActive]);
  useEffect(() => {
    if (!arrivalActive) return;
    const timer = setTimeout(() => setArrivalActive(false), state === 'error' ? 420 : 260);
    return () => clearTimeout(timer);
  }, [arrivalActive, state]);
  const colored = palette === 'planet' || (palette === undefined && presentation?.ownerKey === 'builtin:desktop-pet' && presentation.version === 'v2');
  // Seven directory states stay intact. Ended is blue/minus, not a success
  // check; unknown remains slate/dashed, never a projected offline fact.
  const color = {
    running: PLANET_SIGNAL_PALETTE.working.base,
    attention: PLANET_SIGNAL_PALETTE.waiting.base,
    error: PLANET_SIGNAL_PALETTE.error.base,
    paused: PLANET_SIGNAL_PALETTE.waiting.base,
    idle: PLANET_SIGNAL_PALETTE.idle.base,
    terminal: PLANET_SIGNAL_PALETTE.idle.base,
    unknown: '#7f919d',
  }[state];
  return <svg aria-hidden="true" focusable="false" viewBox="0 0 24 24"
    className={`desktop-pet-status ${className}`.trim()} data-state={state}
    data-motion-active={animate && motionActive} data-arrival-active={arrivalActive && animate && motionActive} data-palette={colored ? 'planet' : undefined} style={colored ? { color } : undefined} fill="none" stroke="currentColor"
    strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    {state === 'running' ? <>
      <circle className="desktop-pet-status__shape" cx="12" cy="12" r="9" stroke="none" />
      <circle className="desktop-pet-status__ink" cx="12" cy="12" r="2.5" />
      <path className="desktop-pet-status__orbit" d="M 12 3 A 9 9 0 1 1 3 12" />
    </> : state === 'attention' ? <>
      <path className="desktop-pet-status__shape" d="M 5 3 H 19 A 2 2 0 0 1 21 5 V 15 A 2 2 0 0 1 19 17 H 11 L 6 21 V 17 H 5 A 2 2 0 0 1 3 15 V 5 A 2 2 0 0 1 5 3 Z" />
      <path d="M 9.5 8 A 2.5 2.5 0 1 1 13 10.3 Q 12 10.8 12 12" />
      <circle className="desktop-pet-status__ink" cx="12" cy="14.4" r=".9" />
    </> : state === 'error' ? <>
      <path className="desktop-pet-status__shape" d="M 12 3 L 22 20 H 2 Z" />
      <path d="M 12 9 V 13" />
      <circle className="desktop-pet-status__ink" cx="12" cy="16.6" r="1" />
    </> : state === 'paused' ? <>
      <rect className="desktop-pet-status__shape" x="3" y="3" width="18" height="18" rx="4" />
      <path strokeWidth="2.8" d="M 9 8 V 16 M 15 8 V 16" />
    </> : state === 'terminal' ? <>
      <rect className="desktop-pet-status__shape" x="4" y="4" width="16" height="16" rx="2" />
      <path d="M 8 12 H 16" />
    </> : state === 'unknown' ? <>
      <circle className="desktop-pet-status__shape" cx="12" cy="12" r="9" strokeDasharray="2 3" />
      <path d="M 8 16 L 16 8" />
    </> : <>
      <circle className="desktop-pet-status__shape" cx="12" cy="12" r="6" stroke="none" />
      <circle className="desktop-pet-status__ink" cx="12" cy="12" r="3.5" />
    </>}
  </svg>;
}
