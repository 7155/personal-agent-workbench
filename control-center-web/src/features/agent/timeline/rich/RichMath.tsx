import { useEffect, useState } from 'react';
import { CopyAction } from './RichBlockTools';

/** Native MathML avoids a second webfont load. Only KaTeX output reaches the DOM. */
export function RichMath({ source, inline = false, streaming = false }: { source: string; inline?: boolean; streaming?: boolean }) {
  const [result, setResult] = useState<{ source: string; inline: boolean; html: string; error: string } | null>(null);
  useEffect(() => {
    if (streaming) return;
    let current = true;
    if (source.length > 20_000) { setResult({ source, inline, html: '', error: '公式过长，已保留 TeX 原文。' }); return; }
    void import('katex').then(module => {
      const katex = module.default;
      const html = katex.renderToString(source, { displayMode: !inline, output: 'mathml', trust: false,
        throwOnError: true, strict: 'warn', maxSize: 15, maxExpand: 1_000, macros: {} });
      if (current) setResult({ source, inline, html, error: '' });
    }).catch(() => { if (current) setResult({ source, inline, html: '', error: '暂未排版此公式，TeX 原文仍可复制。' }); });
    return () => { current = false; };
  }, [source, inline, streaming]);
  const fresh = result?.source === source && result.inline === inline ? result : null;
  const math = fresh?.html
    ? <span className="paw-rich-math__rendered" dangerouslySetInnerHTML={{ __html: fresh.html }} />
    : <code className="paw-rich-math__source">{source}</code>;
  if (inline) return <span className="paw-rich-math paw-rich-math--inline" title={fresh?.error || undefined}>{math}</span>;
  return <figure className="paw-rich-math" aria-label="数学公式" data-streaming={streaming || undefined}>
    <div className="paw-rich-math__scroll" tabIndex={0}>{math}</div>
    <figcaption>{fresh?.error ? <span role="status">{fresh.error}</span> : <span>TeX</span>}<CopyAction value={source} label="复制公式 TeX" compact /></figcaption>
  </figure>;
}
