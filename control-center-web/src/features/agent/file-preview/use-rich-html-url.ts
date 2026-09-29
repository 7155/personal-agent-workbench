import { useEffect, useState } from 'react';
import { RICH_HTML_PREVIEW_PATH, richHtmlPreviewUrl } from './rich-html';

/**
 * The preview is a dedicated loopback document rather than srcdoc/blob/data.
 * Those local documents inherit the Control Center CSP and therefore cannot
 * execute authored inline scripts. The loopback document has its own
 * preview-only CSP and is still isolated by an opaque iframe sandbox.
 */
export function useRichHtmlUrl(document: string, enabled = true): string | undefined {
  const [url, setUrl] = useState('');
  useEffect(() => {
    if (!enabled || !document) {
      setUrl('');
      return undefined;
    }
    // A Blob inherits the Control Center CSP, disabling authored scripts. For
    // large documents load the isolated preview policy before sending HTML in
    // memory, without placing authored content in an HTTP request or giant URL.
    const loopbackUrl = document.length < 750_000 ? richHtmlPreviewUrl(document) : '';
    if (loopbackUrl && loopbackUrl.length < 1_500_000) {
      setUrl(loopbackUrl);
      return undefined;
    }
    const token = crypto.randomUUID();
    const previewUrl = `${RICH_HTML_PREVIEW_PATH}#message:${token}`;
    const onReady = (event: MessageEvent) => {
      if (event.origin !== 'null' || event.data?.type !== 'paw-html-preview-ready' || event.data.token !== token) return;
      // The token alone is insufficient: reply only to our actual sandboxed frame.
      const frame = Array.from(globalThis.document.querySelectorAll<HTMLIFrameElement>('iframe[src]')).find((candidate) =>
        candidate.getAttribute('src') === previewUrl && candidate.contentWindow === event.source,
      );
      if (!frame) return;
      // A sandboxed report can send messages too; never retransmit megabytes on
      // repeated ready notifications after the first authenticated handshake.
      window.removeEventListener('message', onReady);
      frame.contentWindow?.postMessage({ type: 'paw-html-preview-document', token, source: document }, '*');
    };
    window.addEventListener('message', onReady);
    setUrl(previewUrl);
    return () => window.removeEventListener('message', onReady);
  }, [document, enabled]);
  return url || undefined;
}
