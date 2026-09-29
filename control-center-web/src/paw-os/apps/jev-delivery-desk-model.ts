import type { JevTask } from '@/features/semantic-workspace/jev-execution';

/** The existing fileEvidence owner supplies records. This is not a media resolver. */
export interface JevDeliveryFile {
  key: string; ref: string; path: string; name: string;
  task: JevTask; sessionId: string; ownerName: string;
}
export type DeliveryKind = 'document' | 'image' | 'code' | 'data' | 'web' | 'media' | 'archive' | 'other';
export const DELIVERY_KINDS: readonly { id: DeliveryKind; label: string; mark: string }[] = [
  { id: 'document', label: '文档', mark: 'Aa' }, { id: 'image', label: '图像', mark: 'IMG' },
  { id: 'code', label: '代码', mark: '{ }' }, { id: 'data', label: '数据', mark: '01' },
  { id: 'web', label: '网页', mark: '</>' }, { id: 'media', label: '音视频', mark: 'AV' },
  { id: 'archive', label: '压缩包', mark: 'ZIP' }, { id: 'other', label: '其他', mark: 'FILE' },
];
export interface DeliveryDeskState {
  scope: string; query: string; kind: 'all' | DeliveryKind;
  filter: 'all' | 'pinned' | 'openable'; layout: 'gallery' | 'list';
  selectedKey: string; pins: string[]; limit: number;
}
export function emptyDeliveryDesk(scope: string): DeliveryDeskState {
  return { scope, query: '', kind: 'all', filter: 'all', layout: 'gallery', selectedKey: '', pins: [], limit: 36 };
}
export function deliveryKind(name: string): DeliveryKind {
  const dot = name.lastIndexOf('.');
  const extension = dot > 0 ? name.slice(dot + 1).toLowerCase() : '';
  if (/^(?:md|mdx|markdown|pdf|doc|docx|rtf|txt|odt|ppt|pptx)$/.test(extension)) return 'document';
  if (/^(?:png|jpg|jpeg|webp|gif|avif|svg|heic|tif|tiff|bmp)$/.test(extension)) return 'image';
  if (/^(?:csv|tsv|xlsx|xls|ods|parquet|geojson|nc|h5|jsonl)$/.test(extension)) return 'data';
  if (/^(?:html|htm)$/.test(extension)) return 'web';
  if (/^(?:mp4|webm|mov|m4v|mp3|wav|ogg|m4a|flac)$/.test(extension)) return 'media';
  if (/^(?:zip|gz|tar|7z|rar|bz2)$/.test(extension)) return 'archive';
  if (/^(?:js|jsx|ts|tsx|py|rs|go|c|cpp|h|java|swift|css|scss|json|yaml|yml|toml|sh|sql|vue|svelte|diff|patch)$/.test(extension)) return 'code';
  return 'other';
}
export function deliverySuffix(name: string): string {
  const dot = name.lastIndexOf('.');
  return dot >= 0 && dot < name.length - 1 ? name.slice(dot + 1).slice(0, 10).toUpperCase() : 'FILE';
}
export function deliveryExcerpt(text: string, length = 84): string {
  const value = text.replace(/\s+/gu, ' ').trim();
  return value.length > length ? `${value.slice(0, length)}…` : value;
}
export function filterDeliveries(files: readonly JevDeliveryFile[], state: DeliveryDeskState, hasFileOpener: boolean): JevDeliveryFile[] {
  const query = state.query.trim().toLocaleLowerCase(); const pins = new Set(state.pins);
  return files.filter(file => (state.kind === 'all' || deliveryKind(file.name) === state.kind)
    && (state.filter !== 'pinned' || pins.has(file.key))
    && (state.filter !== 'openable' || hasFileOpener && Boolean(file.sessionId))
    && (!query || [file.name, file.path, file.ownerName, file.task.objective].join(' ').toLocaleLowerCase().includes(query)));
}
/** Exact references only. No invented title, current bytes, or batch-download promise. */
export function deliveryReferenceList(files: readonly JevDeliveryFile[]): string {
  return files.map(file => `${file.name}\n  引用：${file.ref}\n  任务：${file.task.objective}\n  修订：${file.task.revision}${file.ownerName ? `\n  负责人：${file.ownerName}` : ''}`).join('\n\n');
}
