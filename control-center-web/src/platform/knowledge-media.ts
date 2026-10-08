// Match the existing Knowledge parser/source receipts, not browser MIME wildcards.
export const KNOWLEDGE_AUDIO_SOURCE_MIME_TYPES: readonly string[] = [
  'audio/wav', 'audio/x-wav', 'audio/mpeg', 'audio/flac', 'audio/x-flac',
  'audio/ogg', 'audio/mp4', 'audio/mp4a-latm',
];
export const KNOWLEDGE_VIDEO_SOURCE_MIME_TYPES: readonly string[] = [
  'video/mp4', 'video/quicktime', 'video/webm', 'video/x-matroska',
];
export const KNOWLEDGE_AUDIO_ASSET_MIME_TYPES: readonly string[] = ['audio/wav'];
export const KNOWLEDGE_IMAGE_ASSET_MIME_TYPES: readonly string[] = [
  'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/bmp',
];
