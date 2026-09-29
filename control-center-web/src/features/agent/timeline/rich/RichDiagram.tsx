import { useEffect, useId, useRef, useState } from 'react';

// Mermaid has process-wide configuration. Serialize jobs, including initialization,
// so one conversation's theme cannot race another conversation's diagram.
let renderQueue: Promise<unknown> = Promise.resolve();
function diagramJob(source: string, id: string, dark: boolean): Promise<string> {
  const next = renderQueue.catch(() => undefined).then(async () => {
    const { default: mermaid } = await import('mermaid');
    mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', suppressErrorRendering: true,
      theme: dark ? 'dark' : 'neutral', maxTextSize: 40_000,
      flowchart: { htmlLabels: false, useMaxWidth: true },
      // Do not let model-authored directives weaken the host's isolation.
      secure: ['secure', 'securityLevel', 'startOnLoad', 'maxTextSize', 'suppressErrorRendering'] });
    try { const { svg } = await mermaid.render(id, source); return svg; }
    finally { document.getElementById(`d${id}`)?.remove(); }
  });
  renderQueue = next.catch(() => undefined);
  return next;
}

/** Same restrictive document for Mermaid output and model-authored static SVG. */
export function staticVisualDocument(svg: string): string {
  return `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src 'none'; connect-src 'none'; base-uri 'none'; form-action 'none'"><style>html,body{margin:0;min-height:100%;display:grid;place-items:center;background:transparent}svg{max-width:100%;height:auto}a{pointer-events:none}*{animation:none!important;transition:none!important}</style></head><body>${svg}</body></html>`;
}

export function RichDiagram({ source, svg = false }: { source: string; svg?: boolean }) {
  const id = `pawDiagram${useId().replace(/[^a-zA-Z0-9]/gu, '')}`;
  const root = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);
  const [dark, setDark] = useState(false);
  const [result, setResult] = useState<{ key: string; value: string; error: string } | null>(null);
  const key = `${svg}:${dark}:${source}`;
  useEffect(() => {
    const node = root.current;
    if (!node || typeof IntersectionObserver === 'undefined') { setVisible(true); return; }
    const observer = new IntersectionObserver(entries => { if (entries.some(e => e.isIntersecting)) { setVisible(true); observer.disconnect(); } }, { rootMargin: '180px' });
    observer.observe(node); return () => observer.disconnect();
  }, []);
  useEffect(() => {
    const sync = () => setDark(document.documentElement.dataset.theme === 'dark');
    sync(); const observer = new MutationObserver(sync);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (!visible) return;
    let current = true;
    if (source.length > 40_000) { setResult({ key, value: '', error: '图示超过内联预览范围，请查看源码。' }); return; }
    if (svg) {
      const parsed = new DOMParser().parseFromString(source, 'image/svg+xml');
      if (parsed.querySelector('parsererror') || parsed.documentElement.localName !== 'svg') {
        setResult({ key, value: '', error: 'SVG 尚未形成有效图像，原始文本已保留。' }); return;
      }
      // Defense in depth in addition to the opaque, scriptless iframe.
      parsed.querySelectorAll('script, foreignObject, a, iframe, object, embed, image, animate, animateTransform, set').forEach(node => node.remove());
      parsed.querySelectorAll('*').forEach(node => [...node.attributes].forEach(attr => {
        if (/^on/iu.test(attr.name) || /href$/iu.test(attr.name)) node.removeAttribute(attr.name);
      }));
      setResult({ key, value: new XMLSerializer().serializeToString(parsed.documentElement), error: '' }); return;
    }
    void diagramJob(source, id, dark).then(value => { if (current) setResult({ key, value, error: '' }); })
      .catch(() => { if (current) setResult({ key, value: '', error: '图示暂未渲染；语法或依赖可能不可用，请切换源码查看。' }); });
    return () => { current = false; };
  }, [source, svg, dark, id, key, visible]);
  const fresh = result?.key === key ? result : null;
  return <div className="paw-rich-diagram" ref={root}>
    {fresh?.value ? <iframe title={svg ? '静态 SVG 图示' : 'Mermaid 图示'} sandbox="" referrerPolicy="no-referrer" srcDoc={staticVisualDocument(fresh.value)} />
      : <p className="paw-rich-placeholder" role="status">{fresh?.error || '图示将在进入阅读区域后排版…'}</p>}
  </div>;
}
