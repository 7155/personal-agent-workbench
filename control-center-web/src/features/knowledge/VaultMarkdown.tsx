import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useQuery } from '@tanstack/react-query';
import { useControlTransport } from '@/app/control-transport';

type Node = { type: string; value?: string; url?: string; children?: Node[] };
function wikiLinks() {
  return (tree: Node) => {
    const visit = (node: Node) => {
      if (!node.children || ['link', 'code', 'inlineCode'].includes(node.type)) return;
      node.children = node.children.flatMap(child => {
        if (child.type !== 'text' || !child.value) { visit(child); return [child]; }
        const result: Node[] = []; let from = 0;
        for (const match of child.value.matchAll(/\[\[([^\]\n]+)\]\]/g)) {
          const at = match.index ?? 0;
          result.push({ type: 'text', value: child.value.slice(from, at) });
          const [target, label] = match[1].split('|');
          result.push({ type: 'link', url: target, children: [{ type: 'text', value: label || target }] });
          from = at + match[0].length;
        }
        result.push({ type: 'text', value: child.value.slice(from) }); return result;
      });
    }; visit(tree);
  };
}
function Attachment({ vaultId, noteId, src, alt }: { vaultId: string; noteId: string; src?: string; alt?: string }) {
  const transport = useControlTransport();
  const allowed = !!src && !src.includes(':') && !src.startsWith('/');
  const query = useQuery({ queryKey: ['vault-attachment', vaultId, noteId, src], enabled: allowed, queryFn: async () => await transport.request({ pathId: 'knowledgeVault.manage', body: { action: 'attachment', vaultId, noteId, link: src ?? '' } }) as { dataUrl: string } });
  return allowed && query.data ? <img src={query.data.dataUrl} alt={alt || ''} style={{ maxWidth: '100%', height: 'auto' }} /> : <span>附件：{alt || src || '图片'}（{allowed ? '不可读或尚在加载' : '未加载外部内容'}）</span>;
}
export function VaultMarkdown({ markdown, vaultId, noteId, onLink }: { markdown: string; vaultId: string; noteId: string; onLink: (target: string) => void }) {
  return <ReactMarkdown remarkPlugins={[remarkGfm, wikiLinks]} urlTransform={url => /^(?:javascript|data|file|vbscript):/i.test(url) ? '' : url} components={{
    img: ({ src, alt }) => <Attachment vaultId={vaultId} noteId={noteId} src={src} alt={alt} />,
    a: ({ href, children }) => href?.startsWith('https://') ? <a href={href} target="_blank" rel="noreferrer">{children}</a> : href ? <button className="vault-inline-link" onClick={() => onLink(href)}>{children}</button> : <span>{children}</span>,
  }}>{markdown}</ReactMarkdown>;
}
