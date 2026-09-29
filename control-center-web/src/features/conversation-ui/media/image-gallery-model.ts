/** Local presentation shape. Sources are resolved by the existing media/attachment owner. */
export interface GalleryImage {
  id: string; source: string; name: string; alt?: string; caption?: string;
  width?: number; height?: number; mimeType?: string; byteSize?: number;
  origin?: string; originTool?: string; receipt?: string;
}
export interface ImageLoadState { state: 'loaded' | 'failed'; width?: number; height?: number }
export type ImageLoadMap = Readonly<Record<string, ImageLoadState>>;
export type ImageViewerMode = 'single' | 'compare';
export function imageKey(image: GalleryImage): string { return JSON.stringify([image.id, image.source]); }
export function imageDimensions(image: GalleryImage, loads: ImageLoadMap): { width?: number; height?: number } {
  const state = loads[imageKey(image)];
  return { width: state?.width || positiveDimension(image.width), height: state?.height || positiveDimension(image.height) };
}
export function positiveDimension(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isInteger(value) && value > 0 && value <= 100_000 ? value : undefined;
}
export function imageSizeLabel(image: GalleryImage, loads: ImageLoadMap): string {
  const { width, height } = imageDimensions(image, loads);
  return width && height ? `${width} × ${height}` : '尺寸未提供';
}
export function imageBytesLabel(value?: number): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return '';
  if (value >= 1048576) return `${(value / 1048576).toFixed(1)} MB`;
  return value >= 1024 ? `${Math.ceil(value / 1024)} KB` : `${value} B`;
}
export function imageOriginLabel(image: GalleryImage): string {
  if (image.origin === 'tool_result') return image.originTool ? `工具返回 · ${image.originTool}` : '工具返回';
  if (image.origin === 'user_upload' || image.origin === 'upload') return '上传附件';
  if (image.origin === 'local_draft') return '本地预览 · 尚未发送';
  return image.originTool ? `来源工具 · ${image.originTool}` : '对话图片';
}
export function adjacentImageId(items: readonly GalleryImage[], id: string, delta: number): string {
  const position = Math.max(0, items.findIndex(image => image.id === id));
  return items[Math.min(items.length - 1, Math.max(0, position + delta))]?.id ?? '';
}
export function limitZoom(value: number): number { return Number.isFinite(value) ? Math.min(4, Math.max(.25, Math.round(value * 100) / 100)) : 1; }
