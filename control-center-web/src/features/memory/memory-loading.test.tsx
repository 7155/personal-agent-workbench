import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { MockControlTransport } from '@/test/mock-transport';
import { MemoryCurationWorkbench } from './MemoryCurationWorkbench';
import { memoryQueryKeys } from './api';

afterEach(cleanup);

describe('Memory curation loading', () => {
  it('renders governed summary progress while the detailed status request is still loading', async () => {
    const detailedStatus = new Promise<never>(() => {});
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': {
          ok: true,
          pendingGovernedEvidenceCount: 777,
          governedNeedsReviewEvidenceCount: 12,
        },
        'agent.memoryMaintenance.run': detailedStatus,
      },
    });
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });

    render(
      <ControlTransportProvider transport={transport}>
        <QueryClientProvider client={client}>
          <MemoryCurationWorkbench enabled onOpenTimeline={() => {}} />
        </QueryClientProvider>
      </ControlTransportProvider>,
    );

    await waitFor(() => expect(
      transport.requests.some((call) => call.request.pathId === 'agent.memoryMaintenance.run'),
    ).toBe(true));
    expect(client.getQueryData(memoryQueryKeys.summary())).toMatchObject({ pendingGovernedEvidenceCount: 777 });
    expect(await screen.findByText('777 条待整理')).toBeInTheDocument();
    expect(await screen.findByText('777 条来源待整理，正在读取日期和应用分布。')).toBeInTheDocument();
    expect(await screen.findByRole('progressbar', { name: '记忆来源整理进度待确认' })).not.toHaveAttribute('aria-valuenow');
  });
});
