export function searchGISKnowledge(query: string, options?: { limit?: number; category?: string }): { query: string; hits: Array<Record<string, unknown>>; total: number; index: string };
export function readGISKnowledge(): Array<Record<string, unknown>>;
