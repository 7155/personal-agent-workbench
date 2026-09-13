import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, fireEvent } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { MockControlTransport } from '@/test/mock-transport';
import { LabKnowledgeRecord } from './LabKnowledgeRecord';

const clients: QueryClient[] = [];
afterEach(() => { cleanup(); clients.splice(0).forEach((client) => client.clear()); });
function mount(projectId: string) {
  const transport = new MockControlTransport({ routes: { 'agent.eval-lab.trials.get': () => ({ schemaVersion: 'rag-ime.agent-lab-trial.v1', job: {
    jobId: 'old-exact-job', sceneId: 'knowledge-resource', state: 'completed', publicSpec: { projectId, operation: 'search' },
    result: { query: 'original question', hits: [{ sourceId: 'source-one', chunkId: 'chunk-one', title: '原始来源', content: '完整原片段，而不是新一次检索结果。' }] },
  } }) } });
  const client = new QueryClient(); clients.push(client);
  render(<QueryClientProvider client={client}><ControlTransportProvider transport={transport}><LabKnowledgeRecord projectId="project-one" jobId="old-exact-job" /></ControlTransportProvider></QueryClientProvider>);
  return transport;
}
it('reads the exact original job and source on demand without executing or selecting a newer job', async () => {
  const transport = mount('project-one');
  expect(await screen.findByText('完整原片段，而不是新一次检索结果。')).toBeVisible();
  fireEvent.click(screen.getByText('查看完整公开回执与配置'));
  expect(screen.getByText(/"jobId": "old-exact-job"/)).toBeVisible();
  expect(transport.requests).toHaveLength(1);
  expect(transport.requests[0]?.request).toMatchObject({ pathId: 'agent.eval-lab.trials.get', query: { jobId: 'old-exact-job' } });
});
it('refuses a mismatched project owner without showing its source text', async () => {
  mount('another-project');
  expect(await screen.findByRole('alert')).toHaveTextContent('原任务身份与当前项目不匹配');
  expect(screen.queryByText('完整原片段，而不是新一次检索结果。')).not.toBeInTheDocument();
});
