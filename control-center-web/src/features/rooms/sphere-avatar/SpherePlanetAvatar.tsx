import { useEffect, useId, useRef, useState, type CSSProperties, type PointerEvent } from 'react';
import { motion } from 'motion/react';
import { motionTokens, useMotionActivity } from '@/design/motion';
import { pose, type Identity } from './sphere-avatar-geometry';
import { PLANET_SIGNAL_PALETTE } from './sphere-avatar-protocol';
import type { PlanetExpression, PlanetSignalState, PlanetMotionMode, PlanetActivity } from './sphere-avatar-protocol';
import './sphere-planet-avatar.css';
import { SpherePlanetSurface, SphereMaterialDefs, SaturnRing } from './SpherePlanetPaint';

function Feature({ d, opacity = 1, quiet, fill, stroke, mouth, transform }: { d: string; opacity?: number; quiet: boolean; fill: string; stroke?: string; mouth?: PlanetExpression; transform?: string }) {
  return <motion.path initial={false} animate={{ d, opacity }} transition={{ duration: quiet ? 0 : motionTokens.duration.enter, ease: motionTokens.easing.standard }} data-mouth={mouth} transform={transform} fill={fill} stroke={stroke} strokeWidth={stroke ? 1.1 : undefined}/>;
}
export type SpherePlanetAvatarProps = {
  identity: Identity; ordinal: number; size?: number; className?: string; decorative?: boolean; label?: string;
  expression?: PlanetExpression; signal?: PlanetSignalState; mode?: PlanetMotionMode;
  activity?: PlanetActivity; interactive?: boolean; showSignal?: boolean;
};
/** SVG body only: the surrounding consumer keeps its native action and focus. */
export function SpherePlanetAvatar({ identity, ordinal, size = 32, className, decorative = false, label,
  expression = 'neutral', signal = 'idle', mode = 'full', activity = 'static', interactive = false, showSignal = true,
}: SpherePlanetAvatarProps) {
  const motionActive = useMotionActivity();
  const id = 'sphere-' + useId().replace(/[^\w-]/g, ''), face = pose(expression);
  const signalPaint = PLANET_SIGNAL_PALETTE[signal];
  const quiet = !motionActive || mode === 'static' || signal === 'offline';
  const canInteract = interactive && mode === 'full' && !quiet;
  const previous = useRef(signal);
  // A loaded completion is history. Only a locally observed active-to-done
  // transition may celebrate; reconnecting from unknown/idle never does.
  const completionEligible = useRef(signal === 'working' || signal === 'waiting');
  const [pulse, setPulse] = useState(false), [gaze, setGaze] = useState({ x: 0, y: 0 });
  const [blinkActive, setBlinkActive] = useState(false), [blinkCount, setBlinkCount] = useState(0);
  const eyesRef = useRef<SVGGElement>(null), animation = useRef<Animation | null>(null);
  useEffect(() => {
    const entered = previous.current !== signal;
    previous.current = signal;
    const completion = signal === 'done' && completionEligible.current;
    completionEligible.current = signal === 'working' || signal === 'waiting';
    const go = !quiet && entered && (signal === 'waiting' || signal === 'error' || completion);
    setPulse(go);
    if (go) { const timer = setTimeout(() => setPulse(false), motionTokens.duration.slow * 1000); return () => clearTimeout(timer); }
  }, [signal, quiet]);
  useEffect(() => {
    if (!canInteract) { animation.current?.cancel(); animation.current = null; setBlinkActive(false); setGaze({ x: 0, y: 0 }); }
  }, [canInteract]);
  useEffect(() => () => animation.current?.cancel(), []);
  const onClick = () => {
    if (!canInteract || !eyesRef.current?.animate) return;
    animation.current?.cancel();
    const next = eyesRef.current.animate([{ transform: 'scaleY(1)' }, { transform: 'scaleY(.10)', offset: .3 }, { transform: 'scaleY(1)' }], {
      duration: motionTokens.duration.enter * 1000, easing: `cubic-bezier(${motionTokens.easing.standard.join(',')})`,
    });
    animation.current = next; setBlinkCount(value => value + 1); setBlinkActive(true);
    next.onfinish = () => { if (animation.current === next) { animation.current = null; setBlinkActive(false); } };
  };
  const onPointerMove = (event: PointerEvent<SVGSVGElement>) => {
    if (!canInteract || event.pointerType !== 'mouse' || !matchMedia('(hover:hover) and (pointer:fine)').matches) return;
    const r = event.currentTarget.getBoundingClientRect();
    setGaze({ x: Math.max(-3.8, Math.min(3.8, (event.clientX - r.left - r.width / 2) / r.width * 7.6)),
      y: Math.max(-2.4, Math.min(2.4, (event.clientY - r.top - r.height / 2) / r.height * 4.8)) });
  };
  return <svg xmlns="http://www.w3.org/2000/svg"
    className={["sphere-planet-avatar",className].filter(Boolean).join(" ")} width={size} height={size} style={{ '--sphere-fast': `${motionTokens.duration.fast}s`, '--sphere-normal': `${motionTokens.duration.normal}s`, '--sphere-slow': `${motionTokens.duration.slow}s` } as CSSProperties}
    viewBox="0 0 320 320" role={decorative ? undefined : "img"} aria-hidden={decorative || undefined} aria-label={decorative ? undefined : label ?? identity}
    data-room-planet={ordinal} data-avatar-variant="sphere" data-activity={activity}
    data-identity={identity} data-expression={expression} data-signal={signal} data-motion={quiet?'static':mode}
    data-pulse={pulse&&!quiet} data-size={size} data-motion-active={motionActive} data-blink-count={blinkCount} data-interacting={blinkActive}
    data-gaze={`${quiet?0:gaze.x},${quiet?0:gaze.y}`} onPointerMove={onPointerMove} onPointerLeave={() => setGaze({x:0,y:0})} onClick={onClick}>
  <defs>
   <SphereMaterialDefs id={id} identity={identity}/>
   <linearGradient id={id+'-satellite'} x1="15%" y1="0%" x2="80%" y2="100%">
    {[["0",signalPaint.light],[".55",signalPaint.base],["1",signalPaint.shade]].map(([offset,color]) => <motion.stop key={offset} offset={offset} initial={false}
      animate={{stopColor:color}} transition={{duration:quiet ? 0 : motionTokens.duration.fast, ease:motionTokens.easing.standard}}/>) }
   </linearGradient>
   <linearGradient id={id+'-eye'} x1="35%" y1="5%" x2="75%" y2="100%"><stop stopColor={identity==='Earth'?'#fff6df':'#302b26'}/><stop offset="1" stopColor={identity==='Earth'?'#eaddb9':'#171c20'}/></linearGradient>
   <radialGradient id={id+'-shadow'}><stop stopColor="#314a41" stopOpacity=".2"/><stop offset="1" stopColor="#314a41" stopOpacity="0"/></radialGradient>
   <clipPath id={id+'-clip'}><circle cx="160" cy="160" r={identity === 'Saturn' ? 116 : 126}/></clipPath>
  </defs>
  <ellipse cx="160" cy="295" rx="104" ry="15" fill={'url(#'+id+'-shadow)'} opacity={size<=48?0:.8}/>

  {identity === 'Saturn' ? <SaturnRing id={id} offline={signal === 'offline'}/> : null}
  <g className="sphere-body" style={signal==='offline'?{filter:'grayscale(.9)',opacity:.63}:undefined}>
   <circle data-layer="shell" cx="160" cy="160" r={identity === 'Saturn' ? 116 : 126} fill={'url(#'+id+'-shell)'}/>
   <g clipPath={'url(#'+id+'-clip)'}>
    <SpherePlanetSurface identity={identity} id={id} size={size} rotating={!quiet && mode === 'full' && signal === 'working'}/>
    {identity === 'Saturn' ? <g transform="rotate(-18 160 176)" data-layer="ring-shadow">
      <path d="M 12 181 A 148 48 0 0 0 308 181" fill="none" stroke="#60492e" strokeOpacity=".18" strokeWidth="30"/>
    </g> : null}
    <ellipse cx="114" cy="92" rx="98" ry="76" fill={'url(#'+id+'-shine)'}/><circle cx="160" cy="160" r={identity === 'Saturn' ? 116 : 126} fill={'url(#'+id+'-edge)'}/>

    <g className="sphere-face" data-face={expression}>
     <g className="sphere-gaze-rig" style={{transform:quiet?'translate(0px,0px)':`translate(${gaze.x}px,${gaze.y}px)`}}><g ref={eyesRef} className="sphere-eye-rig" data-blink={blinkActive} filter={`url(#${id}-eye-depth)`}>
      <Feature d={face.left} quiet={quiet} fill={'url(#'+id+'-eye)'} stroke={identity === 'Earth' ? '#927f64' : '#352a22'}/><Feature d={face.right} quiet={quiet} fill={'url(#'+id+'-eye)'} stroke={identity === 'Earth' ? '#927f64' : '#352a22'}/>
     </g></g>
     <Feature d={'M 105 100 Q 119 '+(90+face.tilt)+' 134 99 L 134 103 Q 119 '+(96+face.tilt)+' 105 104Z'} opacity={face.brows*.65} quiet={quiet} fill={identity==='Earth'?'#eaddb9':'#302b26'}/>
     <Feature d="M 182 95 Q 196 90 211 96 L 210 100 Q 196 95 182 99Z" opacity={face.brows*.65} quiet={quiet} fill={identity==='Earth'?'#eaddb9':'#302b26'}/>
     <Feature d="M 133 177 C 131 181 127 186 127 189 C 127 194 135 194 135 189 C 135 186 134 182 133 177Z" opacity={face.tear*.7} quiet={quiet} fill="#80b6bd"/>
     <Feature d={face.mouth} mouth={expression} transform={identity === 'Saturn' ? 'translate(0 -14)' : undefined} quiet={quiet}
       fill={identity === 'Earth' ? '#eef0cf' : '#493629'} stroke={identity === 'Earth' ? '#537366' : '#302820'}/>
    </g>
   </g>
  </g>
  {identity === 'Saturn' ? <SaturnRing id={id} front offline={signal === 'offline'}/> : null}
  {showSignal && <g className="sphere-signal" data-state={signal} data-satellite-color={signalPaint.base} transform={size<=48?'translate(276 67) scale(2) translate(-276 -67)':undefined}>
   {signal==='working'?<path className="sphere-progress" d="M 253 71 A 22 22 0 1 1 285 87" fill="none" stroke={signalPaint.base} strokeWidth={size<=48?3.5:2.5} strokeLinecap="round"/>:null}
   {signal==='done'?<circle className="sphere-halo" cx="276" cy="67" r="22" fill="none" stroke={signalPaint.base} strokeWidth="2.5"/>:null}
   <g className="sphere-badge">
    <circle cx="276" cy="67" r={signal==='idle'||signal==='working'?13:17} fill={'url(#'+id+'-satellite)'} stroke={signalPaint.shade} strokeWidth={size<=48?2.2:1.8}/>
    {signal==='done'?<path className="sphere-check" d="M 267 67 274 73 285 60" pathLength="1" strokeDasharray="1" strokeDashoffset="0" fill="none" stroke={signalPaint.ink} strokeWidth={size<=48?3.2:3} strokeLinecap="round" strokeLinejoin="round"/>:null}
    {signal==='waiting'?<><path d="M 269 64 C 269 55 283 55 282 63 C 282 67 276 67 276 71" fill="none" stroke={signalPaint.ink} strokeWidth={size<=48?3.2:2.8} strokeLinecap="round"/><circle cx="276" cy="76" r="1.9" fill={signalPaint.ink}/></>:null}
    {signal==='error'?<><path d="M 276 56 276 69" stroke={signalPaint.ink} strokeWidth="3.8" strokeLinecap="round"/><circle cx="276" cy="76" r="2" fill={signalPaint.ink}/></>:null}
    {signal==='offline'?<path d="M 267 67 285 67" stroke={signalPaint.ink} strokeWidth="2.8" strokeLinecap="round"/>:null}
   </g>
  </g>}
 </svg>;
}
