import { useEffect, useId, useState } from 'react';
import { Plus, Trash2 } from 'lucide-react';
import { Button, Disclosure, Field, IconButton, Input, TextArea } from '@/components/primitives';
import type { GoldenCase, GoldenCommand, GoldenSource, GoldenSuite } from './types';
import { isRunnableGoldenModel, splitLabel } from './types';
import { ModelFields } from './Shared';
import { SavedProgress } from './WorkflowGuide';

type Draft = Omit<GoldenCase, 'review' | 'samples'> & { note: string; reviewAuthor: 'human' | 'agent' };
const reviewLabel = { pending: '待审核', approved: '已通过', rejected: '已拒绝' };
const lines = (text: string) => text.split('\n').map((line) => line.trim()).filter(Boolean);
const caseDraft = (item: GoldenCase): Draft => ({
  caseId: item.caseId, question: item.question, taskType: item.taskType, answerable: item.answerable,
  requiredFacts: item.requiredFacts, evidence: item.evidence, rubric: item.rubric,
  split: item.split, note: item.review.note, reviewAuthor: item.review.author ?? 'human',
});

export function CaseReview({ suite, disabled, onReview, onNext, onDraft, onDirtyChange, onAgentReview }: {
  suite: GoldenSuite; disabled: boolean;
  onReview: (input: GoldenCommand['input']) => Promise<boolean>; onNext: () => void; onDraft?: () => void;
  onDirtyChange?: (dirty: boolean) => void; onAgentReview?: (input: GoldenCommand['input']) => Promise<boolean>;
}) {
  const id = useId();
  const [reviewModel, setReviewModel] = useState(suite.judgeConfig);
  const [selectedId, setSelectedId] = useState('');
  const [filter, setFilter] = useState('all');
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const filtered = suite.cases.filter((item) => filter === 'all' || item.review.status === filter);
  const selected = filtered.find((item) => item.caseId === selectedId) ?? filtered.find((item) => item.review.status === 'pending') ?? filtered[0];
  const approved = suite.cases.filter((item) => item.review.status === 'approved');
  const pending = suite.cases.filter((item) => item.review.status === 'pending').length;
  const dirtyIds = new Set(suite.cases.filter((item) => drafts[item.caseId]
    && JSON.stringify(drafts[item.caseId]) !== JSON.stringify(caseDraft(item))).map((item) => item.caseId));
  const hasUnsaved = dirtyIds.size > 0;
  useEffect(() => { onDirtyChange?.(hasUnsaved); }, [hasUnsaved, onDirtyChange]);
  const draft = selected ? drafts[selected.caseId] ?? caseDraft(selected) : null;
  const change = (value: Draft) => setDrafts((current) => ({ ...current, [value.caseId]: value }));
  const submit = async (verdict: 'approved' | 'rejected', advance = false) => {
    if (!draft) return;
    if (await onReview({ ...draft, verdict })) {
      setDrafts((current) => { if (current[draft.caseId] && JSON.stringify(current[draft.caseId]) !== JSON.stringify(draft)) return current; const next = { ...current }; delete next[draft.caseId]; return next; });
      if (advance) {
        const next = suite.cases.find((item) => item.caseId !== draft.caseId && item.review.status === 'pending');
        if (next) { setFilter('all'); setSelectedId(next.caseId); }
      }
    }
  };
  const enoughSplits = approved.some((item) => item.split === 'development') && approved.some((item) => item.split === 'holdout');
  return <section className="golden-section" aria-labelledby={`${id}-review-title`}>
    <header className="golden-section__heading"><div>
      <h3 id={`${id}-review-title`}>逐题确认标准与证据</h3>
      <p>先核对问题、必要事实与引用。可以逐题审核，或让独立 Agent 核对并标注样例；会保留审核来源。</p>
    </div><span className="golden-count">{approved.length} / {suite.cases.length} 题通过 · {pending} 题待审核</span></header>
    {onAgentReview && pending > 0 ? <div className="golden-agent-review"><div><strong>独立 Agent 核对 · {pending} 题</strong><p className="golden-note">逐题检查来源与评分标准，并独立标注样例。调用所选模型，结果记录为 Agent 核对。</p></div><Disclosure summary="核对模型与推理设置"><ModelFields showPrompt={false} label="核对" value={reviewModel} onChange={setReviewModel} disabled={disabled} /></Disclosure><Button disabled={disabled || hasUnsaved || !isRunnableGoldenModel(reviewModel)} onClick={() => void onAgentReview({ model: reviewModel })}>让 Agent 核对待审题目</Button></div> : null}
    {suite.cases.length ? <SavedProgress label="审核已保存" value={suite.cases.length - pending} total={suite.cases.length} /> : null}
    {!suite.cases.length ? <div className="golden-empty"><h4>题目尚未就绪</h4><p>起草任务结束后，真实题目和引用会出现在这里。</p>{onDraft ? <Button size="small" onClick={onDraft}>返回起草题目</Button> : null}</div> : <div className="golden-notebook">
      <aside className="golden-case-list" aria-label="题目列表">
        <label className="golden-list-filter">显示题目<select value={filter} onChange={(event) => setFilter(event.target.value)}>
          <option value="all">全部题目</option><option value="pending">待审核</option><option value="approved">已通过</option><option value="rejected">已拒绝</option>
        </select></label>
        <ol>{filtered.map((item, index) => <li key={item.caseId}><button type="button" aria-current={item.caseId === selected?.caseId ? 'true' : undefined} onClick={() => setSelectedId(item.caseId)}>
          <span className="golden-case-list__number">{index + 1}</span><span><strong>{drafts[item.caseId]?.question || item.question}</strong><small>{splitLabel[item.split]} · {reviewLabel[item.review.status]}{dirtyIds.has(item.caseId) ? ' · 有未保存修改' : ''}</small></span>
        </button></li>)}</ol>
        {!filtered.length ? <p className="golden-note">此筛选下没有题目。</p> : null}
      </aside>
      {selected && draft ? <CaseEditor key={selected.caseId} value={draft} onChange={change} sources={suite.sources} disabled={disabled} onSubmit={submit} hasNext={suite.cases.some((item) => item.caseId !== selected.caseId && item.review.status === 'pending')} /> : null}
    </div>}
    <footer className="golden-section__footer golden-action-bar"><p className="golden-note">{enoughSplits ? pending ? `还有 ${pending} 题待审核；继续后只使用已通过的题目。` : '题目审核已完成。接下来用示例答案对齐评审标准。' : '至少通过一道开发题和一道留出题，才能冻结用于比较的评测集。'}</p>
      <Button variant="primary" onClick={onNext} disabled={!enoughSplits || disabled || hasUnsaved}>继续校准评审</Button>
    </footer>
  </section>;
}

function CaseEditor({ value, onChange, sources, disabled, onSubmit, hasNext }: {
  value: Draft; onChange: (value: Draft) => void; sources: GoldenSource[]; disabled: boolean;
  onSubmit: (verdict: 'approved' | 'rejected', advance?: boolean) => Promise<void>; hasNext: boolean;
}) {
  const id = useId();
  const invalidQuote = value.evidence.some((item) => !item.quote.trim() || !sources.find((source) => source.sourceId === item.sourceId)?.text.includes(item.quote));
  const valid = Boolean(value.question.trim() && value.taskType.trim() && value.rubric.some((line) => line.trim())) && !invalidQuote
    && (!value.answerable || (value.requiredFacts.some((line) => line.trim()) && value.evidence.length > 0));
  return <form className="golden-case-editor" onSubmit={(event) => { event.preventDefault(); if (valid && !disabled) void onSubmit('approved'); }}>
    <fieldset disabled={disabled}>
      <Field htmlFor={`${id}-question`} label="问题" required><TextArea id={`${id}-question`} value={value.question} rows={3} onChange={(event) => onChange({ ...value, question: event.target.value })} /></Field>
      <div className="golden-field-row">
        <Field htmlFor={`${id}-type`} label="任务类型"><Input id={`${id}-type`} value={value.taskType} onChange={(event) => onChange({ ...value, taskType: event.target.value })} /></Field>
        <Field htmlFor={`${id}-split`} label="题目用途"><select id={`${id}-split`} value={value.split} onChange={(event) => onChange({ ...value, split: event.target.value as GoldenCase['split'] })}><option value="development">开发题 · 用于调整</option><option value="holdout">留出题 · 最后验证</option></select></Field>
      </div>
      <label className="golden-check"><input type="checkbox" checked={value.answerable} onChange={(event) => onChange({ ...value, answerable: event.target.checked })} />来源足以回答此题</label>
      <Field htmlFor={`${id}-facts`} label="必须回答的事实" description="每行一条；无法回答的问题可以留空。"><TextArea id={`${id}-facts`} value={value.requiredFacts.join('\n')} rows={4} onChange={(event) => onChange({ ...value, requiredFacts: event.target.value.split('\n') })} onBlur={() => onChange({ ...value, requiredFacts: lines(value.requiredFacts.join('\n')) })} /></Field>
      <Field htmlFor={`${id}-rubric`} label="通过标准" description="每行一条，写清什么算满足、什么算遗漏。"><TextArea id={`${id}-rubric`} value={value.rubric.join('\n')} rows={4} onChange={(event) => onChange({ ...value, rubric: event.target.value.split('\n') })} onBlur={() => onChange({ ...value, rubric: lines(value.rubric.join('\n')) })} /></Field>
      <section className="golden-citations" aria-label="引用证据"><h4>引用证据</h4>
        {value.evidence.map((item, index) => <div className="golden-citation-editor" key={index}>
          <div className="golden-citation-editor__heading"><label htmlFor={`${id}-source-${index}`}>引用 {index + 1} 的来源</label><IconButton label={`移除引用 ${index + 1}`} icon={<Trash2 size={15} />} onClick={() => onChange({ ...value, evidence: value.evidence.filter((_item, itemIndex) => itemIndex !== index) })} /></div>
          <select id={`${id}-source-${index}`} value={item.sourceId} onChange={(event) => onChange({ ...value, evidence: value.evidence.map((item, itemIndex) => itemIndex === index ? { ...item, sourceId: event.target.value } : item) })}>
            <option value="">选择来源</option>{sources.map((source) => <option key={source.sourceId} value={source.sourceId}>{source.title}</option>)}
          </select>
          <label className="golden-visually-hidden" htmlFor={`${id}-quote-${index}`}>引用 {index + 1} 的原文</label>
          <TextArea id={`${id}-quote-${index}`} value={item.quote} rows={3} placeholder="摘录来源中的原文" onChange={(event) => onChange({ ...value, evidence: value.evidence.map((item, itemIndex) => itemIndex === index ? { ...item, quote: event.target.value } : item) })} />
        </div>)}
        <Button size="small" variant="quiet" leadingIcon={<Plus size={14} />} onClick={() => onChange({ ...value, evidence: [...value.evidence, { sourceId: sources[0]?.sourceId ?? '', quote: '' }] })}>添加引用</Button>
        {invalidQuote ? <p className="golden-field-error" role="status">请确认引用逐字出现在所选来源中。</p> : null}
        <Disclosure className="golden-disclosure" summary="查看来源原文"><div className="golden-source-reference">{sources.map((source) => <section key={source.sourceId}><h5>{source.title}</h5><small>{source.uri}</small><p>{source.text}</p></section>)}</div></Disclosure>
      </section>
      <Field htmlFor={`${id}-note`} label="审核说明"><TextArea id={`${id}-note`} rows={2} value={value.note} placeholder="记录通过或拒绝的理由" onChange={(event) => onChange({ ...value, note: event.target.value })} /></Field>
      <Field htmlFor={`${id}-author`} label="审核来源"><select id={`${id}-author`} value={value.reviewAuthor} onChange={(event) => onChange({ ...value, reviewAuthor: event.target.value as Draft['reviewAuthor'] })}><option value="human">用户本人审核</option><option value="agent">Agent 辅助审核</option></select></Field>
    </fieldset>
    <div className="golden-editor-actions"><Button type="submit" variant={hasNext ? 'secondary' : 'primary'} disabled={disabled || !valid}>保存并通过</Button>{hasNext ? <Button variant="primary" disabled={disabled || !valid} onClick={() => void onSubmit('approved', true)}>通过并看下一题</Button> : null}<Button variant="quiet" disabled={disabled || !value.question.trim() || invalidQuote} onClick={() => void onSubmit('rejected')}>保存并拒绝</Button></div>
  </form>;
}
