import { describe, expect, it } from 'vitest';
import { filePreviewRequestFromBlock, fileSizeLabel, safeManagedContentUrl } from './file-descriptor';

const MEDIA_ID = 'media_abcdefghijkl';
const SESSION_ID = 'session-preview-1';

describe('managed file descriptors', () => {
  it('binds a file block to the parent message session and immutable receipt', () => {
    expect(filePreviewRequestFromBlock({
      mediaId: MEDIA_ID,
      sessionId: SESSION_ID,
      receiptUrl: `/api/agent/media/${MEDIA_ID}/content?sessionId=${SESSION_ID}`,
      fileName: 'handoff.md',
      mimeType: 'text/markdown',
      byteSize: 2_049,
      sha256: 'a'.repeat(64),
    }, SESSION_ID)).toEqual({
      mediaId: MEDIA_ID,
      sessionId: SESSION_ID,
      expectedSha256: 'a'.repeat(64),
      fileNameHint: 'handoff.md',
      mimeTypeHint: 'text/markdown',
      byteSizeHint: 2_049,
    });
  });

  it('rejects cross-session, malformed digest, and arbitrary URL blocks', () => {
    expect(filePreviewRequestFromBlock({
      mediaId: MEDIA_ID,
      sessionId: 'session-other',
    }, SESSION_ID)).toBeNull();
    expect(filePreviewRequestFromBlock({
      mediaId: MEDIA_ID,
      sha256: 'not-a-digest',
    }, SESSION_ID)).toBeNull();
    expect(filePreviewRequestFromBlock({
      mediaId: MEDIA_ID,
      receiptUrl: 'https://example.com/file.md',
      sessionId: SESSION_ID,
    })).toBeNull();
  });

  it('accepts only the exact managed content route returned by the backend', () => {
    const descriptor = { mediaId: MEDIA_ID, sessionId: SESSION_ID };
    expect(safeManagedContentUrl(
      `/api/agent/media/${MEDIA_ID}/content?sessionId=${SESSION_ID}`,
      descriptor,
    )).toBe(`/api/agent/media/${MEDIA_ID}/content?sessionId=${SESSION_ID}`);
    expect(safeManagedContentUrl(
      `/api/agent/media/${MEDIA_ID}/content?sessionId=${SESSION_ID}&next=https://evil.example`,
      descriptor,
    )).toBeNull();
    expect(safeManagedContentUrl(
      `/api/agent/media/media_other12345/content?sessionId=${SESSION_ID}`,
      descriptor,
    )).toBeNull();
    expect(safeManagedContentUrl('https://example.com/file.md', descriptor)).toBeNull();
  });

  it('formats compact file sizes without pretending unknown bytes are known', () => {
    expect(fileSizeLabel(0)).toBe('');
    expect(fileSizeLabel(Number.NaN)).toBe('');
    expect(fileSizeLabel(-1)).toBe('');
    expect(fileSizeLabel(72)).toBe('72 B');
    expect(fileSizeLabel(124)).toBe('124 B');
    expect(fileSizeLabel(1_023)).toBe('1023 B');
    expect(fileSizeLabel(1_024)).toBe('1 KB');
    expect(fileSizeLabel(2_049)).toBe('3 KB');
    expect(fileSizeLabel(1_572_864)).toBe('1.5 MB');
  });
});
