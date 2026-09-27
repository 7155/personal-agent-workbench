import { AudioLines, ExternalLink, FileOutput, Globe2, Image as ImageIcon, MonitorPlay } from 'lucide-react';
import { useMemo, useState } from 'react';
import type { PawOsWindowTarget } from './model/desktop';
import { RICH_HTML_SANDBOX, richHtmlDocument } from '@/features/agent/file-preview/rich-html';
import { useRichHtmlUrl } from '@/features/agent/file-preview/use-rich-html-url';

type ResultTarget = Extract<PawOsWindowTarget, { kind: 'result' }>;

const kindLabel: Record<ResultTarget['resultKind'], string> = {
  html: 'HTML 输出', web: '网页结果', game: '互动作品', music: '音乐可视化',
  image: '图像结果', audio: '音频结果', artifact: '文件产物',
};

export function PawResultWindow({ target }: { target: ResultTarget }) {
  const [failedSource, setFailedSource] = useState(false);
  const html = useMemo(() => target.content?.trim() ? richHtmlDocument(target.content) : '', [target.content]);
  // Every HTML result uses the dedicated preview document: srcDoc inherits the
  // Control Center CSP, so large reports would lose their authored scripts.
  const htmlUrl = useRichHtmlUrl(html, Boolean(html));
  const source = safeResultSource(target.source);
  const isHtml = target.resultKind === 'html' || target.resultKind === 'web' || target.resultKind === 'game' || target.resultKind === 'music';

  return (
    <section aria-label={target.title || '结果'} className="paw-result-window" data-result-kind={target.resultKind} role="region">
      <header className="paw-result-window__hero">
        <span className="paw-result-window__icon" aria-hidden="true">
          {isHtml ? <MonitorPlay size={18} /> : target.resultKind === 'image' ? <ImageIcon size={18} /> : target.resultKind === 'audio' ? <AudioLines size={18} /> : <FileOutput size={18} />}
        </span>
        <div><small>{kindLabel[target.resultKind]}</small><h1>{target.title}</h1>{target.subtitle ? <p>{target.subtitle}</p> : null}</div>
        <span className="paw-result-window__state"><Globe2 size={13} />已隔离</span>
      </header>
      {isHtml && htmlUrl ? <iframe className="paw-result-window__frame" referrerPolicy="no-referrer" sandbox={RICH_HTML_SANDBOX} src={htmlUrl} title={target.title} /> : null}
      {target.resultKind === 'image' && source && !failedSource ? <figure className="paw-result-window__media paw-result-window__media--image"><img alt={target.title} onError={() => setFailedSource(true)} src={source} /></figure> : null}
      {target.resultKind === 'audio' && source && !failedSource ? <div className="paw-result-window__media paw-result-window__media--audio"><AudioLines aria-hidden="true" size={28} /><audio controls onError={() => setFailedSource(true)} preload="metadata" src={source} /></div> : null}
      {target.resultKind === 'artifact' && source ? <section className="paw-result-window__artifact"><FileOutput aria-hidden="true" size={28} /><div><strong>{target.title}</strong><p>{target.subtitle || '受控文件回执已准备好。'}</p></div><a href={source} rel="noreferrer" target="_blank"><ExternalLink size={15} />打开文件</a></section> : null}
      {isHtml && !htmlUrl && html ? <section className="paw-result-window__empty" role="status"><strong>正在准备 HTML 输出</strong><p>正在为报告建立隔离预览。</p></section> : null}
      {isHtml && !htmlUrl && !html ? <ResultEmpty detail="这个结果没有可渲染的 HTML 内容。" /> : null}
      {(target.resultKind === 'image' || target.resultKind === 'audio') && (!source || failedSource) ? <ResultEmpty detail={source ? '媒体回执暂时无法读取，请重试或打开原始产物。' : '这个结果没有可验证的媒体回执。'} /> : null}
      {target.resultKind === 'artifact' && !source ? <ResultEmpty detail="这个产物没有可验证的文件回执。" /> : null}
    </section>
  );
}

function ResultEmpty({ detail }: { detail: string }) {
  return <section className="paw-result-window__empty" role="status"><strong>结果暂不可用</strong><p>{detail}</p></section>;
}

function safeResultSource(value?: string): string | null {
  const source = value?.trim() || '';
  if (!source || source.includes('\\') || source.startsWith('//')) return null;
  if (source.startsWith('/api/agent/') || source.startsWith('/media/') || source.startsWith('/companions/') || source.startsWith('blob:')) return source;
  return null;
}
