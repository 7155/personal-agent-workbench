import type { CSSProperties } from 'react';
import { motion } from 'motion/react';
import { motionTokens } from '@/design/motion';
import { PLANET_ACTIVITY_SIGNAL, PLANET_MATERIALS, type PlanetActivity, type PlanetExpression, type PlanetIdentity, type PlanetMotionMode, type PlanetSignalState } from './planet-companion-protocol';

/** Original vector artwork, also used verbatim by the standalone design preview. */
export function PlanetCompanionMark({ name, ordinal, size, idPrefix, className, decorative = false, activity = 'static', expression = 'neutral', signalState, motionMode = 'static', statePulse = false }: {
  name: PlanetIdentity; ordinal: number; size: number; idPrefix: string; className?: string; decorative?: boolean;
  activity?: PlanetActivity; expression?: PlanetExpression; signalState?: PlanetSignalState; motionMode?: PlanetMotionMode; statePulse?: boolean;
}) {
  const face = expression;
  const signal = signalState ?? PLANET_ACTIVITY_SIGNAL[activity];
  const clip = `${idPrefix}-surface`;
  const frame = size <= 64 ? name === 'Saturn' ? '8 14 106 84' : name === 'Uranus' ? '24 14 90 92' : name === 'Jupiter' ? '18 14 96 86' : '22 14 92 86' : '0 0 120 120';
  const material = PLANET_MATERIALS[name];
  return <svg className={['paw-planet-companion', className].filter(Boolean).join(' ')} viewBox={frame} width={size} height={size}
    aria-hidden={decorative || undefined} aria-label={decorative ? undefined : name} role={decorative ? undefined : 'img'} focusable="false"
    style={{ '--paw-planet-signal-stroke': size <= 32 ? '1px' : size <= 64 ? '1.1px' : '1.8px' } as CSSProperties}
    data-family="paw-v2" data-room-planet={ordinal} data-identity={name} data-activity={activity} data-expression={face}
    data-signal-state={signal} data-motion={signal === 'offline' ? 'static' : motionMode} data-state-pulse={statePulse} preserveAspectRatio="xMidYMid meet">
    <defs><clipPath id={clip}><PlanetBody name={name}/></clipPath>
      <linearGradient id={`${idPrefix}-volume`} x1="20%" y1="0%" x2="85%" y2="100%">
        <stop offset="0" stopColor={material.pale}/><stop offset=".4" stopColor={material.surface}/><stop offset="1" stopColor={material.shade}/>
      </linearGradient>
    </defs>
    <g className="paw-planet-pose" stroke="#34444b" strokeOpacity={size <= 64 ? .8 : .4} strokeWidth={size <= 64 ? 1.8 : 1.1} strokeLinejoin="round" strokeLinecap="round">
      {name === 'Saturn' ? <ellipse cx="60" cy="67" rx="49" ry="10" fill="none" stroke={material.detail} strokeWidth="3"/> : null}
      {name === 'Uranus' ? <ellipse cx="60" cy="60" rx="47" ry="17" transform="rotate(-57 60 60)" fill="none" stroke={material.detail} strokeWidth="4"/> : null}
      <PlanetBody name={name} fill={`url(#${idPrefix}-volume)`}/>
      <g clipPath={`url(#${clip})`} stroke="none" data-surface={name}>
        <path d="M 78 23 Q 111 66 72 99 L 108 103 110 19Z" fill={material.detail} opacity=".1"/>
        <path d="M 34 39 Q 37 30 50 28 Q 45 33 42 38 Q 39 42 34 39Z" fill={material.pale} opacity=".45"/>
        {name === 'Earth' ? <g fill={material.detail} opacity=".7"><path d="M 26 32 39 27 44 33 40 41 35 45 37 53 28 59 22 49Z M 86 72 98 69 98 91 80 95 83 86 79 81Z"/></g> : null}
        {name === 'Mercury' ? <g fill={material.detail} opacity=".65"><circle cx="40" cy="34" r="5"/><circle cx="85" cy="74" r="6"/><circle cx="34" cy="76" r="3"/></g> : null}
        {name === 'Venus' ? <path d="M 26 36 Q 45 26 50 34 Q 50 39 44 38 Q 53 46 30 46" fill="none" stroke={material.pale} strokeWidth="5"/> : null}
        {name === 'Mars' ? <path d="M 28 74 34 69 38 72 M 80 33 88 38 93 35" fill="none" stroke={material.detail} strokeWidth="2.5" opacity=".6"/> : null}
        {name === 'Saturn' ? <path d="M 32 35 Q 60 29 84 36" fill="none" stroke={material.pale} strokeWidth="3"/> : null}
        {name === 'Jupiter' ? <><path d="M 22 35 Q 59 42 100 34" fill="none" stroke={material.pale} strokeWidth="4"/><ellipse cx="92" cy="73" rx="6" ry="4" fill={material.detail} opacity=".75"/></> : null}
        {name === 'Uranus' ? <path d="M 39 32 Q 60 29 76 34" fill="none" stroke={material.pale} strokeWidth="3"/> : null}
        {name === 'Neptune' ? <path d="M 85 74 Q 98 72 91 81 Q 83 87 82 80 Q 82 77 88 78" fill="none" stroke={material.detail} strokeWidth="2.5" opacity=".8"/> : null}
      </g>
      {name === 'Saturn' ? <path d="M 11 67 Q 15 77 60 77 Q 104 77 109 67" fill="none" stroke={material.pale} strokeWidth="4"/> : null}
      {name === 'Uranus' ? <path d="M 41 101 Q 62 99 82 65 Q 99 33 88 21" fill="none" stroke={material.detail} strokeWidth="3.5"/> : null}
      <g transform="translate(60 60) scale(1.4) translate(-60 -60)">
        <PlanetFace expression={face} identity={name} size={size} animated={motionMode !== 'static' && signal !== 'offline'}/>
      </g>
    </g>
    <PlanetSignal state={signal} size={size}/>
  </svg>;
}

function PlanetBody({ name, fill }: { name: PlanetIdentity; fill?: string }) {
  if (name === 'Mars') return <path data-body="rock-flat" d="M 25 47 Q 29 31 48 29 L 65 28 Q 86 29 96 45 L 98 61 Q 96 81 78 89 L 59 93 Q 38 90 26 75 L 23 61Z" fill={fill}/>;
  if (name === 'Mercury') return <path data-body="rock-compact" d="M 29 44 40 32 Q 57 24 72 31 L 85 36 94 53 92 72 81 85 Q 62 96 44 88 L 31 78 26 61Z" fill={fill}/>;
  if (name === 'Jupiter') return <ellipse data-body="wide" cx="60" cy="60" rx="39" ry="33" fill={fill}/>;
  if (name === 'Neptune') return <ellipse data-body="tall" cx="60" cy="60" rx="32" ry="37" fill={fill}/>;
  return <circle data-body={name === 'Earth' ? 'round' : 'compact-round'} cx="60" cy="60" r={name === 'Earth' ? 35 : name === 'Uranus' ? 34 : 33} fill={fill}/>;
}

const FACE_PROPORTIONS = {
  Earth: { left: 48, right: 71, rx: 3.5, ry: 5, mouth: 8 },
  Mars: { left: 48, right: 71, rx: 3.5, ry: 5.3, mouth: 8 },
  Venus: { left: 49, right: 70, rx: 3.1, ry: 6, mouth: 7 },
  Jupiter: { left: 46, right: 73, rx: 4, ry: 4.8, mouth: 9 },
  Saturn: { left: 49, right: 70, rx: 2.9, ry: 6, mouth: 10 },
  Mercury: { left: 50, right: 69, rx: 3, ry: 4.7, mouth: 6 },
  Neptune: { left: 49, right: 71, rx: 3.2, ry: 5.8, mouth: 7 },
  Uranus: { left: 46, right: 74, rx: 3.5, ry: 4.5, mouth: 8 },
} as const;

const NEUTRAL_PERSONA = {
  Earth: { brow: [39,39], y: 67, curve: 75, tilt: 0 },
  Mars: { brow: [46,36], y: 69, curve: 74, tilt: -3 },
  Venus: { brow: [37,37], y: 68, curve: 74, tilt: 0 },
  Jupiter: { brow: [43,43], y: 69, curve: 73, tilt: 0 },
  Saturn: { brow: [37,38], y: 67, curve: 78, tilt: 0 },
  Mercury: { brow: [40,40], y: 70, curve: 65, tilt: 0 },
  Neptune: { brow: [38,43], y: 68, curve: 75, tilt: -3 },
  Uranus: { brow: [40,44], y: 68, curve: 73, tilt: 1 },
} as const;

/** Every expression shares one path topology, so retargeting never doubles a face. */
export function PlanetFace({ expression, identity = 'Earth', size = 160, animated = false }: { expression: PlanetExpression; identity?: PlanetIdentity; size?: number; animated?: boolean }) {
  const proportions = FACE_PROPORTIONS[identity];
  const happy = ['happy', 'proud'].includes(expression);
  const closed = ['sleepy', 'calm', 'relieved'].includes(expression);
  const sad = ['sad', 'concerned'].includes(expression);
  const round = expression === 'surprised';
  const flat = ['focused', 'waiting', 'calm', 'attentive'].includes(expression);
  const open = ['happy', 'talking'].includes(expression);
  const gaze = expression === 'thinking' ? -2 : expression === 'waiting' ? 1.5 : 0;
  const eyeStroke = size <= 32 ? .35 : size <= 48 ? .45 : .7;
  const mouthStroke = size <= 32 ? 1 : size <= 48 ? 1.2 : 2.7;
  const eyes = [proportions.left + gaze, proportions.right + gaze].map((x, i) => {
    const wink = expression === 'wink' && i === 1;
    const y = happy ? 54 : expression === 'sad' ? 55 : expression === 'waiting' ? 53 : 52;
    const w = happy || closed || wink ? 5 : round ? 4 : proportions.rx;
    const h = round ? 7.2 : expression === 'waiting' ? proportions.ry : expression === 'attentive' ? (proportions.ry + 1.5) * 1.33 : proportions.ry * 1.33;
    const top = happy ? 45 : closed || wink ? 55 : y-h;
    const bottom = happy ? 48 : closed || wink ? 57 : y+h;
    const eye = (width: number, deltaX: number, deltaY: number) => `M ${x+deltaX-width} ${y+deltaY} C ${x+deltaX-width} ${top+deltaY} ${x+deltaX+width} ${top+deltaY} ${x+deltaX+width} ${y+deltaY} C ${x+deltaX+width} ${bottom+deltaY} ${x+deltaX-width} ${bottom+deltaY} ${x+deltaX-width} ${y+deltaY}Z`;
    const highlightX = x-w*.25, highlightY = y-(y-top)*.3;
    return { white: eye(w+1,0,0), pupil: eye(happy || closed || wink ? w : w*.75,.5,.2), highlight: `M ${highlightX-1} ${highlightY} C ${highlightX-1} ${highlightY-1.4} ${highlightX+1} ${highlightY-1.4} ${highlightX+1} ${highlightY} C ${highlightX+1} ${highlightY+1.4} ${highlightX-1} ${highlightY+1.4} ${highlightX-1} ${highlightY}Z`, open: !(happy || closed || wink) };

  });
  const browY = identity === 'Jupiter' ? 44 : identity === 'Venus' ? 40 : identity === 'Neptune' ? 41 : 42;
  const persona = NEUTRAL_PERSONA[identity];
  const browApex = sad ? [38,38] : ['curious','thinking','surprised'].includes(expression) ? [35,35] : expression === 'focused' ? [45,45] : persona.brow;
  const browWidth = identity === 'Mercury' ? 4 : identity === 'Jupiter' ? 6 : 5;
  const brows = `M ${proportions.left-browWidth} ${browY} Q ${proportions.left} ${browApex[0]} ${proportions.left+browWidth} ${browY} M ${proportions.right-browWidth} ${browY} Q ${proportions.right} ${browApex[1]} ${proportions.right+browWidth} ${browY}`;
  const neutral = expression === 'neutral';
  const neutralRound = neutral && identity === 'Mercury';
  const w = neutralRound ? 3 : round ? 5 : expression === 'curious' ? 3 : flat ? 6 : expression === 'sleepy' ? 3 : proportions.mouth;
  const y = neutral ? persona.y : round || expression === 'curious' ? 70 : sad ? 72 : flat ? 70 : expression === 'sleepy' ? 71 : 67;
  const top = neutral ? persona.curve : round ? 62 : expression === 'curious' ? 65 : open ? 71 : sad ? 64 : flat || expression === 'sleepy' ? y : 76;
  const bottom = neutralRound ? 75 : round ? 78 : expression === 'curious' ? 75 : open ? 84 : top;
  const mouth = `M ${60-w} ${y} C ${60-w} ${top} ${60+w} ${top} ${60+w} ${neutral ? y+persona.tilt : y} C ${60+w} ${bottom} ${60-w} ${bottom} ${60-w} ${y}Z`;
  return <g className="paw-planet-face" data-face-expression={expression} data-face-motion={animated ? 'transition' : 'static'} fill="#183744" stroke="#183744" strokeOpacity="1">
    <g className="paw-planet-gaze">
      {eyes.map((eye,i) => <g key={i} data-eye={i}>
        <FacePath d={eye.white} animated={animated} opacity={eye.open ? 1 : 0} fill="#f5fbfc" strokeWidth={eyeStroke}/>
        <FacePath d={eye.pupil} animated={animated} fill="#183744" stroke="none"/>
        <FacePath d={eye.highlight} animated={animated} opacity={eye.open ? .95 : 0} fill="#ffffff" stroke="none"/>
      </g>)}
      <FacePath d={brows} animated={animated} fill="none" opacity={.8} strokeWidth={size <= 48 ? .75 : identity === 'Jupiter' ? 2.6 : 1.9}/>
    </g>
    <g className="paw-planet-mouth">
      <FacePath d={mouth} animated={animated} fill={neutralRound || round || expression === 'curious' || open ? '#183744' : 'none'} strokeWidth={mouthStroke}/>
      <FacePath d="M 56 75 Q 60 73 64 75" animated={animated} opacity={open ? 1 : 0} fill="none" stroke="#d1aaa0" strokeWidth={eyeStroke}/>
    </g>
    <FacePath d="M 69 78 Q 71 75 73 78 Q 71 81 69 78Z" animated={animated} opacity={expression === 'thinking' ? 1 : 0} fill="#e2e6df" strokeWidth={eyeStroke}/>
    <FacePath d="M 42 64 45 65 M 75 65 78 64" animated={animated} opacity={expression === 'proud' ? 1 : 0} fill="none" strokeWidth={eyeStroke}/>
  </g>;
}

function FacePath({ d, animated, opacity = 1, ...style }: { d: string; animated: boolean; opacity?: number; fill?: string; stroke?: string; strokeWidth?: number }) {
  // Quiet branches replace an in-flight Motion path immediately at its final pose.
  return animated
    ? <motion.path key="animated" initial={false} animate={{ d, opacity }} transition={{ duration: motionTokens.duration.enter, ease: [...motionTokens.easing.standard] }} {...style} vectorEffect="non-scaling-stroke"/>
    : <path key="static" d={d} opacity={opacity} {...style} vectorEffect="non-scaling-stroke"/>;
}

export function PlanetSignal({ state, size = 160 }: { state: PlanetSignalState; size?: number }) {
  const compact = size <= 64;
  return <g className="paw-planet-signal" data-feature="satellite" strokeLinecap="round" strokeLinejoin="round">
    <circle className="paw-planet-halo" cx="100" cy="28" r="11" fill="none" stroke="currentColor" strokeWidth="2"/>
    {state === 'working' ? <g className="paw-planet-progress"><path d="M 94 39 A 13 13 0 0 0 106 39" fill="none" stroke="currentColor" strokeWidth="2.4"/></g> : null}
    <g className="paw-planet-badge">
      {state === 'waiting' ? <path data-feature="question-bubble" d={compact
        ? 'M 100 17.5 C 105.8 17.5 110.5 22.2 110.5 28 C 110.5 30.8 109.4 33.3 107.5 35.2 L 111 41 L 103.5 38 C 102.4 38.4 101.2 38.5 100 38.5 C 94.2 38.5 89.5 33.8 89.5 28 C 89.5 22.2 94.2 17.5 100 17.5Z'
        : 'M 100 19 C 105 19 109 23 109 28 C 109 30.6 108 32.5 106.5 34 L 110 39 L 103 36.5 C 102 36.8 101 37 100 37 C 95 37 91 32.9 91 28 C 91 23 95 19 100 19Z'} fill="var(--color-workspace-surface)" stroke="currentColor" strokeWidth="1.8"/>
        : <circle cx="100" cy="28" r={state === 'idle' || state === 'working' ? 6 : compact ? 10.5 : 9} fill={state === 'idle' || state === 'working' ? 'currentColor' : 'var(--color-workspace-surface)'} stroke="currentColor" strokeWidth="1.8"/>}
      {state === 'waiting' ? <g fill="none" stroke="currentColor" strokeWidth="1.8"><path d={compact ? 'M 96 25 Q 96 20 101 21 Q 107 22 102 27 L 100 29' : 'M 97 25 Q 97 21 101 22 Q 105 23 101 27 L 100 29'}/><circle cx="100" cy="33" r=".8" fill="currentColor" stroke="none"/></g> : null}
      {state === 'done' ? <path className="paw-planet-check" pathLength="1" strokeDasharray="1" strokeDashoffset="0" d={compact ? 'M 94 28 98 33 106 23' : 'M 95 28 99 32 105 24'} fill="none" stroke="currentColor" strokeWidth="2"/> : null}
      {state === 'error' ? <g stroke="currentColor" strokeWidth="2"><path d={compact ? 'M 100 22 100 30' : 'M 100 23 100 29'}/><circle cx="100" cy="33" r="1" fill="currentColor" stroke="none"/></g> : null}
      {state === 'offline' ? <path d="M 96 28 104 28" stroke="currentColor" strokeWidth="1.8"/> : null}
    </g>
  </g>;
}
