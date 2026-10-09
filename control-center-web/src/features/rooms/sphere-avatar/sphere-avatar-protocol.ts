/** Display-only expressions and explicit signals; neither changes Runtime state. */
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

/** Display palette only; color never projects a task/connection state. */
export const PLANET_SIGNAL_PALETTE = {
  idle: { base: '#4d98c3', light: '#8bc8e5', shade: '#2f6c97', ink: '#245c7a' },
  working: { base: '#9674ba', light: '#c8b4e4', shade: '#725492', ink: '#4e386d' },
  waiting: { base: '#ba8a3f', light: '#f0d097', shade: '#906626', ink: '#65481f' },
  done: { base: '#649578', light: '#abd0b5', shade: '#477052', ink: '#285b3b' },
  error: { base: '#ca715f', light: '#f4b39a', shade: '#ad5146', ink: '#773c31' },
  offline: { base: '#93a09b', light: '#bac5bf', shade: '#737f7a', ink: '#455752' },
} as const;
