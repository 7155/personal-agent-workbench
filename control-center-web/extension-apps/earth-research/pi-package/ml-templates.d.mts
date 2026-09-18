export function listMLTemplates(): Array<Record<string, unknown>>;
export function mlTemplate(id: string): Record<string, unknown>;
export function prepareMLScript(root: string, input: { workflow: string; saveAs?: string; replacements?: Record<string, string> }): Record<string, unknown>;
