export function runGISBatch(input: { root: string; python?: string; requests: Array<Record<string, unknown>>; concurrency?: number }): Promise<Record<string, unknown>>;
