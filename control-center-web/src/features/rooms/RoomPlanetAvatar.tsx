import { useId } from 'react';
import atlas from './assets/planet-bodies-v1.png';
import { roomPlanetName } from './room-copy';
import './room-planet-avatar.css';

export type RoomPlanetActivity = 'static' | 'idle' | 'thinking' | 'working' | 'waiting' | 'done' | 'error' | 'stopped';

// Transparent source atlas. Viewports keep Saturn's entire ring and exclude
// adjacent identities; SVG meet preserves each character's proportions.
const FRAMES = [
  [40, 35, 406, 413], [480, 35, 409, 413],
  [911, 35, 405, 413], [1340, 22, 425, 426],
  [0, 458, 528, 391], [527, 461, 369, 390],
  [923, 461, 402, 392], [1362, 460, 402, 395],
] as const;

// Face anchors in the source atlas; separate SVG features let expressions
// blink and change without distorting the textured body or Saturn's rings.
const FACES = [
  [245, 226, -9], [708, 228, -10], [1111, 258, -4], [1558, 217, -2],
  [268, 625, -21], [720, 652, -10], [1120, 637, 4], [1568, 655, -8],
] as const;

/** One stable ordinal identity across chat, execution, roster and deliveries. */
export function RoomPlanetAvatar({ ordinal, size = 32, className, decorative = false, activity = 'static' }: {
  ordinal: number; size?: number; className?: string; decorative?: boolean; activity?: RoomPlanetActivity;
}) {
  const clipId = useId();
  const index = Number.isInteger(ordinal) && ordinal >= 0 ? ordinal : 0;
  const frame = FRAMES[index];
  const name = roomPlanetName(index);
  if (!frame) return <span aria-label={decorative ? undefined : name} aria-hidden={decorative || undefined}
    className={className} style={{ display: 'inline-grid', placeItems: 'center', width: size, height: size, flexShrink: 0 }}>✦</span>;
  const [x, y, width, height] = frame;
  const [faceX, faceY, tilt] = FACES[index];
  const expression = activity === 'done' ? 'happy' : activity === 'thinking' ? 'curious'
    : activity === 'error' ? 'concerned' : activity === 'stopped' ? 'calm'
      : activity === 'working' ? 'talking' : index === 2 ? 'happy'
        : [0, 3, 5, 7].includes(index) ? 'open' : 'smile';
  return <svg aria-hidden={decorative || undefined} aria-label={decorative ? undefined : name}
    className={['room-planet-avatar', className].filter(Boolean).join(' ')} data-activity={activity}
    data-expression={expression}
    data-room-planet={index} focusable="false" height={size}
    preserveAspectRatio="xMidYMid meet" role={decorative ? undefined : 'img'}
    style={{ flexShrink: 0, overflow: 'hidden', verticalAlign: 'middle' }}
    viewBox={`0 0 ${width} ${height}`} width={size}>
    <defs>
      <clipPath id={clipId}><rect height={height} width={width} /></clipPath>
      <radialGradient id={`${clipId}-eye`} cx="35%" cy="25%" r="75%">
        <stop offset="0" stopColor="#33434b" /><stop offset="0.55" stopColor="#11181d" />
        <stop offset="1" stopColor="#030709" />
      </radialGradient>
    </defs>
    <image clipPath={`url(#${clipId})`} height={887} href={atlas} width={1774} x={-x} y={-y} />
    <g aria-hidden="true" transform={`translate(${faceX - x} ${faceY - y}) rotate(${tilt})`}>
      <g className="room-planet-gaze">
        <g className="room-planet-eyes">
          {[-58, 58].map(eyeX => <g className="room-planet-eye" key={eyeX}>
            <ellipse cx={eyeX} cy={0} rx={17} ry={20} fill={`url(#${clipId}-eye)`} stroke="#323131" strokeWidth={2} />
            <ellipse cx={eyeX - 5} cy={-8} rx={5} ry={6} fill="white" opacity={0.95} />
            <circle cx={eyeX + 5} cy={8} r={2.5} fill="#778d97" opacity={0.45} />
          </g>)}
        </g>
        <g className="room-planet-happy-eyes" fill="none" stroke="#172127" strokeWidth={8} strokeLinecap="round">
          <path d="M -74 4 Q -58 -24 -42 4" /><path d="M 42 4 Q 58 -24 74 4" />
        </g>
        <g className="room-planet-brows" fill="none" stroke="#624b3c" strokeOpacity={0.7} strokeWidth={6} strokeLinecap="round">
          <path d="M -74 -37 Q -63 -47 -50 -41" /><path d="M 48 -41 Q 61 -47 74 -37" />
        </g>
      </g>
      <g className="room-planet-mouth" stroke="#261510" strokeWidth={5} strokeLinecap="round" strokeLinejoin="round">
        <path className="room-planet-smile" d="M -19 34 Q 0 57 20 34" fill="none" />
        <g className="room-planet-open-mouth">
          <path d="M -22 29 Q 0 39 23 29 C 28 60 -23 68 -22 29" fill="#3d100e" />
          <path d="M -12 50 Q 0 42 14 49 Q 3 61 -12 50" fill="#ff777d" stroke="none" />
        </g>
        <ellipse className="room-planet-curious-mouth" cx={0} cy={42} rx={9} ry={12} fill="#3d100e" />
        <path className="room-planet-concerned-mouth" d="M -15 45 Q 0 32 15 45" fill="none" />
        <path className="room-planet-calm-mouth" d="M -14 39 Q 0 44 14 39" fill="none" />
      </g>
    </g>
  </svg>;
}
