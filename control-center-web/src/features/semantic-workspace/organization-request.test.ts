import { afterEach, expect, it, vi } from 'vitest';
import { OrganizationController } from './organization-controller';
import { OrganizationJournal } from './organization-journal';
import { requestOrganization } from './organization-request';
import type { RequestOrganization } from './organization-model';

afterEach(() => vi.useRealTimers());

it('warns that anonymous or unavailable storage cannot survive tab closure', () => {
  expect(new OrganizationJournal('volatile-only').getPersistenceError()).toBe(true);
});

it('bounds an unresponsive host, keeps the original command and ignores its late reply', async () => {
  vi.useFakeTimers();
  let lateReply!: (value: unknown) => void;
  let writeSignal: AbortSignal | undefined;
  let revision = 0;
  const commands: string[] = [];
  const request: RequestOrganization = async input => {
    if (input.pathId === 'agent.organization.read') return { ok: true, receipts: [], unavailable: [], items: [{
      key: 'session:one', title: '等待数据', revision, category: 'unknown', group: '', pinned: false, placement: 'desk',
    }] };
    if (input.pathId === 'agent.organization.command') {
      commands.push(input.body.commandId);
      if (commands.length === 1) {
        revision++;
        writeSignal = input.signal;
        return new Promise(resolve => { lateReply = resolve; });
      }
      return { ok: true, receiptId: input.body.commandId, replayed: true };
    }
    throw new Error('Unexpected route');
  };
  const journal = new OrganizationJournal('timeout-test');
  const controller = new OrganizationController(request, journal, () => 1000, () => 'original-id');
  await controller.setKeys(['session:one']);
  const save = controller.change('session:one', { operation: 'category', value: 'waiting' });
  await vi.advanceTimersByTimeAsync(25_000);
  await save;
  expect(writeSignal?.aborted).toBe(true);
  expect(journal.getSnapshot()).toMatchObject({ id: 'original-id', uncertain: true });
  lateReply({ ok: true, receiptId: 'original-id' });
  await Promise.resolve();
  expect(journal.getSnapshot()).not.toBeNull();
  await controller.retryPending();
  expect(commands).toEqual(['original-id', 'original-id']);
  expect(revision).toBe(1);
  expect(journal.getSnapshot()).toBeNull();
  controller.dispose();
});

it('cancels observation without requiring the host to support AbortSignal', async () => {
  const signal = new AbortController();
  const request = vi.fn<RequestOrganization>(() => new Promise(() => {}));
  const promise = requestOrganization(request, { pathId: 'agent.organization.read', body: { keys: [] }, signal: signal.signal });
  const outcome = expect(promise).rejects.toMatchObject({ name: 'AbortError' });
  signal.abort();
  await outcome;
  expect(request).not.toHaveBeenCalled();
});

it('keeps preview scope read-only after catalog aggregation', async () => {
  const request = vi.fn<RequestOrganization>(async () => ({ ok: true, receipts: [], unavailable: [],
    readOnlyReason: '预览只读', items: [{ key: 'session:one', title: '预览', category: 'unknown',
      placement: 'desk', pinned: false, group: '', revision: 0 }] }));
  const controller = new OrganizationController(request, new OrganizationJournal('preview-test'));
  await controller.setKeys(['session:one']);
  await controller.change('session:one', { operation: 'category', value: 'waiting' });
  await controller.suggest('session:one');
  expect(request).toHaveBeenCalledTimes(1);
  expect(controller.getSnapshot().error).toBe('预览只读');
  controller.dispose();
});
