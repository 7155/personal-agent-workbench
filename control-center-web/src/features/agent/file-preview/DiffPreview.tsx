import { useMemo, useState } from 'react';
import { Columns2, Rows3 } from 'lucide-react';
import { SegmentedControl } from '@/components/primitives';
import { CopyTextButton } from './CopyTextButton';
import { countDiffLines, countFileDiffLines, pairDiffLines, parseUnifiedDiff, type DiffFile, type DiffLine } from './unified-diff';

export function DiffPreview({
  content,
  disclosureRegionId,
  disclosureRegionLabel,
  fileName = '',
  showCopy = true,
}: {
  content: string;
  disclosureRegionId?: string;
  disclosureRegionLabel?: string;
  fileName?: string;
  showCopy?: boolean;
}) {
  const [mode, setMode] = useState<'unified' | 'split'>('unified');
  const files = useMemo(() => {
    const parsed = parseUnifiedDiff(content);
    if (parsed.length || !fileName || !content.trimStart().startsWith('@@')) return parsed;
    /* Agent blocks often already carry the path separately and therefore send
       only hunks. The preview parser correctly rejects that as a standalone
       patch, but rendering it as undifferentiated preformatted text loses the
       actual change. Reattach the trusted display path locally; no contract or
       file content is changed. */
    return parseUnifiedDiff(`--- a/${fileName}\n+++ b/${fileName}\n${content}`);
  }, [content, fileName]);
  const totals = useMemo(() => countDiffLines(files), [files]);
  if (!files.length) {
    return (
      <pre
        aria-label={fileName ? `${fileName} 变更内容` : 'Diff 变更内容'}
        className="agent-file-preview__plain"
        id={disclosureRegionId}
        role="region"
        tabIndex={0}
      >
        <code>{content || '没有可展示的变更。'}</code>
      </pre>
    );
  }
  return (
    <div
      aria-label={disclosureRegionLabel}
      className="agent-diff-preview"
      id={disclosureRegionId}
      role={disclosureRegionId ? 'region' : undefined}
    >
      <header>
        <small>{files.length} 个文件 · +{totals.added} −{totals.removed}</small>
        <span className="agent-diff-preview__actions">
          {showCopy ? <CopyTextButton label="补丁原文" value={content} /> : null}
          <SegmentedControl
            aria-label="Diff 展示方式"
            className="agent-diff-preview__mode"
            items={[
              { value: 'unified', label: <span><Rows3 size={13} />单栏</span> },
              { value: 'split', label: <span><Columns2 size={13} />并排</span> },
            ]}
            onValueChange={setMode}
            value={mode}
          />
        </span>
      </header>
      <div
        aria-label="Diff 文件列表"
        className="agent-diff-preview__files"
        role="region"
        tabIndex={0}
      >
        {files.map((file, index) => <FileDiff key={`${file.path}:${index}`} file={file} mode={mode} />)}
      </div>
    </div>
  );
}

function FileDiff({ file, mode }: { file: DiffFile; mode: 'unified' | 'split' }) {
  const totals = countFileDiffLines(file);
  return (
    <section className="agent-diff-file" data-status={file.status}>
      <header><strong>{file.path}</strong><span>{statusLabel(file.status)} · +{totals.added} −{totals.removed}</span></header>
      <div
        aria-label={`${file.path} 变更内容`}
        className="agent-diff-file__scroll"
        role="region"
        tabIndex={0}
      >
        {file.hunks.map((hunk, index) => (
          <div className="agent-diff-hunk" key={`${hunk.header}:${index}`}>
            <div className="agent-diff-hunk__header">{hunk.header}</div>
            {mode === 'unified' ? <UnifiedLines lines={hunk.lines} /> : <SplitLines lines={hunk.lines} />}
          </div>
        ))}
      </div>
    </section>
  );
}

function UnifiedLines({ lines }: { lines: readonly DiffLine[] }) {
  return <table><tbody>{lines.map((line, index) => (
    <tr data-kind={line.kind} key={index}>
      <td className="agent-diff-gutter">{line.oldLine ?? ''}</td>
      <td className="agent-diff-gutter">{line.newLine ?? ''}</td>
      <td className="agent-diff-code"><span>{prefix(line.kind)}</span>{line.content}</td>
    </tr>
  ))}</tbody></table>;
}

function SplitLines({ lines }: { lines: readonly DiffLine[] }) {
  return <table><tbody>{pairDiffLines(lines).map((pair, index) => (
    <tr key={index}>
      <SplitCell line={pair.left} side="left" />
      <SplitCell line={pair.right} side="right" />
    </tr>
  ))}</tbody></table>;
}

function SplitCell({ line, side }: { line: DiffLine | null; side: 'left' | 'right' }) {
  const lineNumber = side === 'left' ? line?.oldLine : line?.newLine;
  return (
    <td className="agent-diff-split" data-empty={!line || undefined} data-kind={line?.kind}>
      <span className="agent-diff-gutter">{lineNumber ?? ''}</span>
      <code>{line?.content ?? ''}</code>
    </td>
  );
}

function prefix(kind: DiffLine['kind']): string {
  if (kind === 'add') return '+';
  if (kind === 'remove') return '-';
  return ' ';
}

function statusLabel(status: DiffFile['status']): string {
  return ({ modified: '修改', added: '新增', deleted: '删除', renamed: '重命名' })[status];
}
