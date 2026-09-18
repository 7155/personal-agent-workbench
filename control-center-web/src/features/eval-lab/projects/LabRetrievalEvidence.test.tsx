import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { LabRetrievalRun, LabRetrievalHitMetrics } from './LabRetrievalEvidence';

afterEach(cleanup);
it('uses the sandbox execution receipt instead of model configuration or requested reranking', () => {
  render(<LabRetrievalRun result={{ profile: { mode: 'hybrid', topK: 5, threshold: 0.2, rerank: true, candidateDepth: 40, contextChars: 16000 },
    reranker: { configured: true, provider: 'configured-model' },
    retrieval: { effectiveMode: 'lexical', lexicalCandidates: 12, denseCandidates: 0, graphCandidates: 0, rerank: { enabled: false } },
  }} />);
  const evidence = screen.getByRole('region', { name: '本次召回参数与执行' });
  expect(evidence).toHaveTextContent('本次未重排');
  expect(evidence).not.toHaveTextContent('本次已重排');
  expect(evidence).toHaveTextContent('5 / 0.2');
  expect(evidence).toHaveTextContent('实际 关键词');
});
it('keeps final result order distinct from pre-diversification reranker order and original score', () => {
  render(<LabRetrievalHitMetrics hit={{ score: 0.21, rerankOriginalRank: 7, rerankRank: 4, rerankScore: 0.987654 }} index={1} />);
  const scores = within(screen.getByLabelText('结果 2 排名与分数'));
  expect(scores.getByText('#2')).toBeVisible();
  expect(scores.getByText('7 / 0.21')).toBeVisible();
  expect(scores.getByText('4 / 0.987654')).toBeVisible();
});
it('shows unknown values when an older run has no diagnostics', () => {
  render(<><LabRetrievalRun result={{}} /><LabRetrievalHitMetrics hit={{}} index={0} /></>);
  expect(screen.getByRole('region')).toHaveTextContent('本次是否重排未报告');
  expect(screen.getAllByText('未报告 / 未报告').length).toBeGreaterThan(0);
  expect(screen.queryByText('本次已重排')).not.toBeInTheDocument();
});
