/** Compare only the Prompt strings frozen in the selected execution receipt. */
export function PromptComparison({ baseline, candidate }: { baseline: unknown; candidate: unknown }) {
  const complete = typeof baseline === 'string' && typeof candidate === 'string';
  const before = typeof baseline === 'string' ? baseline : undefined;
  const after = typeof candidate === 'string' ? candidate : undefined;
  return <section className="golden-method-diff" aria-label="冻结 Prompt 对照"><header><h4>本次 Prompt</h4><span>{complete ? before === after ? '正文相同' : '正文有变化' : '正文未完整返回'}</span></header>
    <div className="golden-answer-comparison">{[['基线 Prompt', before], ['候选 Prompt', after]].map(([label, text]) => <div key={label}><h5>{label}</h5><pre className="golden-preserve-text">{text === undefined ? '原回执未提供，不能推断正文。' : text || '未附加独立 Prompt（空正文）'}</pre></div>)}</div>
    {complete && before !== after ? <details><summary>查看完整替换差异</summary><p>以下逐行显示本轮被替换的原文和替换后的正文。</p><pre>{before!.split('\n').map((line, index) => <span key={`before-${index}`} data-change="removed">− {line}{'\n'}</span>)}{after!.split('\n').map((line, index) => <span key={`after-${index}`} data-change="added">+ {line}{'\n'}</span>)}</pre></details> : null}
  </section>;
}
