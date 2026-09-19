export interface QGISOptions { executable?: string; root?: string; timeoutMs?: number }
export interface QGISReceipt { status: 'completed'; backend: 'qgis'; executable: string; command: string[]; algorithm?: string; inputs?: Record<string, unknown>; result: unknown }
export function findQGISProcess(options?: { candidates?: string[] | string }): string | null;
export function qgisBackendStatus(options?: { candidates?: string[] | string }): { id: 'qgis'; available: boolean; executable: string | null; nativeTested: false; status: 'installed_unverified' | 'unavailable'; role: string };
export function listQGISAlgorithms(options?: QGISOptions): Promise<QGISReceipt>;
export function helpQGISAlgorithm(options: QGISOptions & { algorithm: string }): Promise<QGISReceipt>;
export function runQGISAlgorithm(options: QGISOptions & { algorithm: string; inputs?: Record<string, unknown> }): Promise<QGISReceipt>;
