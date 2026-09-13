import { FileText, LoaderCircle } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';

export type ComposerFileImporter = (files: File[]) => void | boolean | Promise<void | boolean>;
export const PASTED_TEXT_ATTACHMENT_THRESHOLD = 4_000;
type TextImport = { id: string; file: File; text: string; error?: string };

/** Keep the exact clipboard bytes locally until the managed owner accepts them. */
export function usePastedTextAttachments({ ownerId, canImport, onImport }: {
  ownerId: string;
  canImport: boolean;
  onImport: ComposerFileImporter;
}) {
  const [pending, setPending] = useState<TextImport[]>([]);
  const [previews, setPreviews] = useState<Record<string, string>>({});
  const currentOwner = useRef(ownerId);
  const importing = useRef(false);
  currentOwner.current = ownerId;
  useEffect(() => { setPending([]); setPreviews({}); }, [ownerId]);

  async function importText(item: TextImport) {
    const owner = ownerId;
    setPending((items) => [...items.filter((candidate) => candidate.id !== item.id), { ...item, error: undefined }]);
    let ownsImport = false;
    try {
      if (!canImport || importing.current) throw new Error('暂时不能添加附件，内容已保留。');
      importing.current = true;
      ownsImport = true;
      if (await onImport([item.file]) === false) throw new Error('文本附件未导入，内容已保留。');
      if (currentOwner.current !== owner) return;
      setPreviews((values) => ({ ...values, [item.file.name]: `${item.text.length.toLocaleString()} 字符 · ${item.text.replace(/\s+/gu, ' ').slice(0, 64)}` }));
      setPending((items) => items.filter((candidate) => candidate.id !== item.id));
    } catch {
      if (currentOwner.current !== owner) return;
      setPending((items) => items.map((candidate) => candidate.id === item.id
        ? { ...candidate, error: canImport ? '文本附件未导入，内容已保留。' : '暂时不能添加附件，内容已保留。' }
        : candidate));
    } finally {
      if (ownsImport) importing.current = false;
    }
  }

  function pasteText(text: string, remainingCharacters = Infinity): boolean {
    if (!text || (text.length < PASTED_TEXT_ATTACHMENT_THRESHOLD && text.length <= remainingCharacters)) return false;
    const id = crypto.randomUUID();
    const title = text.split(/\r?\n/u).find((line) => line.trim())?.trim().replace(/[\\/:*?"<>|\u0000-\u001f]/gu, '').slice(0, 24) || '粘贴文本';
    const file = new File([text], `${title}-${id.slice(0, 6)}.txt`, { type: 'text/plain' });
    void importText({ id, file, text });
    return true;
  }

  return {
    pasteText,
    blocked: pending.length > 0,
    previews,
    pendingNotice: pending.length ? <div className="composer-text-imports" aria-label="长文本附件">
      {pending.map((item) => <div className="composer-text-import" key={item.id}>
        {item.error ? <FileText size={17} /> : <LoaderCircle className="ui-spin" size={17} />}
        <span role="status"><strong>{item.file.name}</strong><small>{item.error || '正在将长文本保存为 TXT 附件…'}</small></span>
        {item.error ? <><button type="button" disabled={!canImport} onClick={() => void importText(item)}>重试</button><button type="button" aria-label={`移除未导入的 ${item.file.name}`} onClick={() => setPending((items) => items.filter((candidate) => candidate.id !== item.id))}>移除</button><details><summary>查看保留的原文</summary><textarea aria-label="未导入的长文本" value={item.text} readOnly /></details></> : null}
      </div>)}
    </div> : null,
  };
}
