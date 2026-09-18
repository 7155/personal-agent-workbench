export function runEarthScriptBatch(input: { root: string; scripts: string[]; project?: string; python?: string; dependencies?: string; concurrency?: number }): Promise<Record<string, unknown>>;
