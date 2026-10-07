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
