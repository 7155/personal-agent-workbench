export function earthTaskStatus(input: { root: string; taskIds?: string[]; project?: string; python?: string; dependencies?: string }): Promise<Record<string, unknown>>;
export function earthTaskCancel(input: { root: string; taskIds?: string[]; project?: string; python?: string; dependencies?: string }): Promise<Record<string, unknown>>;
export function saveCloudReceipt(root: string, name: string, value: unknown): string;
