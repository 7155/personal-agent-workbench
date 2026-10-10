import { Check, Clipboard, Code2 } from 'lucide-react';
import { useEffect, useState } from 'react';
import { IconButton } from '@/components/primitives';
import { writeClipboardText } from '@/platform/clipboard';
import { highlightCode } from './syntax-highlighter';

export function CodePreview({ content, fileName, language, showHeader = true }: { content: string; fileName: string; language: string; showHeader?: boolean }) {
  const [highlighted, setHighlighted] = useState('');
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let current = true;
    setHighlighted('');
    void highlightCode(content, language).then((html) => {
      if (current) setHighlighted(html);
    }).catch(() => {
      if (current) setHighlighted('');
    });
    return () => { current = false; };
  }, [content, language]);

  async function copy(): Promise<void> {
    await writeClipboardText(content);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1_500);
  }

  return (
    <figure aria-label={showHeader ? undefined : fileName} className="agent-file-code" data-header={showHeader ? undefined : 'hidden'}>
      {showHeader ? <figcaption>
        <span><Code2 size={15} />{fileName}</span>
        <IconButton label={copied ? '已复制' : '复制代码'} icon={copied ? <Check size={15} /> : <Clipboard size={15} />} onClick={() => void copy()} size="small" tooltip />
      </figcaption> : null}
      {highlighted ? (
        // Shiki emits escaped token spans from the verified text receipt; the
        // original file HTML is never inserted through this path.
        <div className="agent-file-code__highlight" dangerouslySetInnerHTML={{ __html: highlighted }} />
      ) : (
        <pre aria-label={`${fileName} 代码内容`}><code>{content}</code></pre>
      )}
    </figure>
  );
}
