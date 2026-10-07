/** PAW artwork protocol. Expressions are presentation, never task receipts. */
export type PlanetActivity = 'static' | 'idle' | 'thinking' | 'working' | 'waiting' | 'done' | 'error' | 'stopped';
export const PLANET_EXPRESSIONS = [
  ['neutral', '平静'], ['attentive', '专注'], ['curious', '好奇'], ['focused', '凝神'],
  ['thinking', '思考'], ['talking', '回应'], ['waiting', '等待'], ['happy', '开心'],
  ['proud', '得意'], ['surprised', '惊讶'], ['sleepy', '困倦'], ['concerned', '担心'],
  ['sad', '失落'], ['calm', '安定'], ['relieved', '释然'], ['wink', '眨眼'],
] as const;
export type PlanetExpression = typeof PLANET_EXPRESSIONS[number][0];
export const PLANET_SIGNAL_STATES = ['idle', 'working', 'waiting', 'done', 'error', 'offline'] as const;
export type PlanetSignalState = typeof PLANET_SIGNAL_STATES[number];
export type PlanetMotionMode = 'full' | 'transition' | 'static';
export const PLANET_ACTIVITY_SIGNAL: Record<PlanetActivity, PlanetSignalState> = {
  static: 'idle', idle: 'idle', thinking: 'working', working: 'working',
  waiting: 'waiting', done: 'done', error: 'error', stopped: 'idle',
};
export const PLANET_MATERIALS = {
  Earth: { surface: '#459fb6', detail: '#78b277', pale: '#83c5d0', shade: '#388ba0' },
  Mercury: { surface: '#b4b3b5', detail: '#85838c', pale: '#d6d3d6', shade: '#9d9ca4' },
  Venus: { surface: '#ecca85', detail: '#b88f58', pale: '#fae3af', shade: '#dfb970' },
  Mars: { surface: '#e79172', detail: '#ba6d58', pale: '#f3b99d', shade: '#d47c61' },
  Saturn: { surface: '#e4bb70', detail: '#ac895c', pale: '#f5d796', shade: '#d2a763' },
  Jupiter: { surface: '#edb896', detail: '#c78463', pale: '#ffd9b4', shade: '#dda485' },
  Uranus: { surface: '#80c5c0', detail: '#559e9f', pale: '#bce4db', shade: '#6bb3b0' },
  Neptune: { surface: '#528dc8', detail: '#2c68a6', pale: '#8ab5db', shade: '#3d7db9' },
} as const;
export type PlanetIdentity = keyof typeof PLANET_MATERIALS;
export function isPlanetIdentity(name: string): name is PlanetIdentity { return name in PLANET_MATERIALS; }
