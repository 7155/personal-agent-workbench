import type { ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import { previewPersonas } from '@/features/agent/preview-data';
import { MockControlTransport, type MockRouteHandler } from '@/test/mock-transport';
import { MemoryPreferences } from './MemoryPreferences';
import { MotionActivityBoundary } from '@/design/motion';
import { RoleBookLayer } from './RoleBookLayer';

afterEach(cleanup);

const persona = previewPersonas[0]!;
const revision = {
  revisionId: 'role-history-1', revisionNumber: 1, displayName: persona.displayName,
  status: 'archived', mission: '保留可核对的伙伴经历',
  sections: { recentWork: [{ itemId: 'work-1', text: '完成一次有来源的整理', evidenceIds: ['evidence-1'] }] },
};

describe('partner memory recovery', () => {
  it('retries the partner catalog without leaving the current memory view', async () => {
    let reads = 0;
    const transport = new MockControlTransport({ routes: {
      'agent.roles.list': () => { if (++reads === 1) throw new Error('catalog unavailable'); return { items: [persona] }; },
      'agent.roleBook.get': { active: { ...revision, status: 'active' }, history: [] },
    } });
    renderWithTransport(transport, <RoleBookLayer enabled onOpenGovernance={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: '重新读取伙伴目录' }));
    expect(await screen.findByRole('button', { name: '完成一次有来源的整理' })).toBeInTheDocument();
    expect(reads).toBe(2);
  });

  it('retries the selected partner revision without changing partners', async () => {
    let reads = 0;
    const transport = new MockControlTransport({ routes: {
      'agent.roles.list': { items: [persona] },
      'agent.roleBook.get': () => { if (++reads === 1) throw new Error('book unavailable'); return { active: { ...revision, status: 'active' }, history: [] }; },
    } });
    renderWithTransport(transport, <RoleBookLayer enabled onOpenGovernance={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: '重新读取伙伴记忆' }));
    expect(await screen.findByRole('button', { name: '完成一次有来源的整理' })).toBeInTheDocument();
    expect(reads).toBe(2);
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.roleBook.get').every(({ request }) => request.query?.roleId === persona.roleId)).toBe(true);
  });

  it('keeps historical revisions readable when there is no active revision', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.roles.list': { items: [persona] },
      'agent.roleBook.get': { active: null, history: [revision] },
    } });
    renderWithTransport(transport, <RoleBookLayer enabled initialReferenceId={revision.revisionId} onOpenGovernance={vi.fn()} />);
    expect(await screen.findByRole('button', { name: '完成一次有来源的整理' })).toBeInTheDocument();
    expect(screen.getByText('历史版本')).toBeInTheDocument();
    expect(screen.queryByText('还没有可用版本')).not.toBeInTheDocument();
  });
});

describe('memory preference rereading', () => {
  it('updates untouched preferences from a background reread without inventing a draft', async () => {
    let stableDays = 365;
    const transport = preferenceTransport(() => settings(stableDays));
    const { client } = renderWithTransport(transport, <MemoryPreferences />);
    expect(await screen.findByRole('combobox', { name: '稳定偏好' })).toHaveTextContent('正常记住');
    stableDays = 180;
    await act(() => client.invalidateQueries({ queryKey: ['memory', 'preferences', 'settings'] }));
    await waitFor(() => expect(screen.getByRole('combobox', { name: '稳定偏好' })).toHaveTextContent('弱化'));
    expect(screen.getByRole('button', { name: '保存记忆偏好' })).toBeDisabled();
  });

  it('preserves only edited fields while untouched fields follow the refreshed authority', async () => {
    let enabled = true;
    const transport = preferenceTransport(() => ({ settings: { memory: { enabled, timeDecay: { stablePreferenceHalfLifeDays: 365 } } }, runtimeConfig: { runtimeRevision: 4 } }));
    const user = userEvent.setup();
    const { client } = renderWithTransport(transport, <MemoryPreferences />);
    await choosePriority(user);
    enabled = false;
    await act(() => client.invalidateQueries({ queryKey: ['memory', 'preferences', 'settings'] }));
    await waitFor(() => expect(screen.getByRole('switch', { name: '启用记忆增强' })).not.toBeChecked());
    expect(screen.getByRole('combobox', { name: '稳定偏好' })).toHaveTextContent('优先记住');
    expect(screen.getByText('1 项待保存')).toBeInTheDocument();
  });

  it('stops retaining a field when the user restores its saved value', async () => {
    let stableDays = 365;
    const transport = preferenceTransport(() => settings(stableDays));
    const user = userEvent.setup();
    const { client } = renderWithTransport(transport, <MemoryPreferences />);
    await choosePriority(user);
    await user.click(screen.getByRole('combobox', { name: '稳定偏好' }));
    await user.click(await screen.findByRole('option', { name: '正常记住' }));
    expect(screen.getByRole('button', { name: '保存记忆偏好' })).toBeDisabled();
    stableDays = 180;
    await act(() => client.invalidateQueries({ queryKey: ['memory', 'preferences', 'settings'] }));
    await waitFor(() => expect(screen.getByRole('combobox', { name: '稳定偏好' })).toHaveTextContent('弱化'));
    expect(screen.getByRole('button', { name: '保存记忆偏好' })).toBeDisabled();
  });

  it('replaces a discarded draft with the fresh response instead of the old cache', async () => {
    let stableDays = 365;
    const transport = preferenceTransport(() => settings(stableDays));
    const user = userEvent.setup();
    renderWithTransport(transport, <MemoryPreferences />);
    await choosePriority(user);
    stableDays = 180;
    await user.click(screen.getByRole('button', { name: '重新读取' }));
    await waitFor(() => expect(screen.getByRole('combobox', { name: '稳定偏好' })).toHaveTextContent('弱化'));
    expect(screen.getByRole('button', { name: '保存记忆偏好' })).toBeDisabled();
    expect(screen.queryByText('1 项待保存')).not.toBeInTheDocument();
  });

  it('retains a draft after a failed reread and permits a read-only recovery', async () => {
    let fail = false;
    const transport = preferenceTransport(() => { if (fail) throw new Error('settings unavailable'); return settings(365); });
    const user = userEvent.setup();
    renderWithTransport(transport, <MemoryPreferences />);
    await choosePriority(user);
    fail = true;
    await user.click(screen.getByRole('button', { name: '重新读取' }));
    expect(await screen.findByText('暂时无法更新记忆偏好')).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: '稳定偏好' })).toHaveTextContent('优先记住');
    expect(screen.getByRole('button', { name: '保存记忆偏好' })).toBeDisabled();
    fail = false;
    await user.click(screen.getByRole('button', { name: '重新读取' }));
    await waitFor(() => expect(screen.getByRole('combobox', { name: '稳定偏好' })).toHaveTextContent('正常记住'));
    expect(transport.requests.some(({ request }) => request.pathId === 'configuration.settings.apply')).toBe(false);
  });

  it('distinguishes an applied receipt from a failed post-save reread', async () => {
    let applied = false;
    const transport = preferenceTransport(() => { if (applied) throw new Error('reread unavailable'); return settings(365); }, () => { applied = true; return receipt; });
    const user = userEvent.setup();
    renderWithTransport(transport, <MemoryPreferences />);
    await choosePriority(user);
    await user.click(screen.getByRole('button', { name: '保存记忆偏好' }));
    expect(await screen.findByText('已写入本机设置，但尚未读到最新偏好。请重新读取后核对。')).toBeInTheDocument();
    expect(screen.queryByText(/已写入本机设置并重新读取/)).not.toBeInTheDocument();
    expect(screen.queryByText('记忆偏好没有保存')).not.toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: '稳定偏好' })).toHaveTextContent('优先记住');
    expect(screen.getByRole('button', { name: '保存记忆偏好' })).toBeDisabled();
    expect(transport.requests.filter(({ request }) => request.pathId === 'configuration.settings.apply')).toHaveLength(1);
  });
});

const digest = 'a'.repeat(64);
const receipt = { schemaVersion: 'rag-ime.management-work-receipt.v1', ok: true, receiptId: 'preferences-receipt', pathId: 'configuration.settings.apply', payloadSha256: digest, appliedAtMs: 1, rollbackAvailable: false };
function settings(stableDays: number) { return { settings: { memory: { timeDecay: { stablePreferenceHalfLifeDays: stableDays } } }, runtimeConfig: { runtimeRevision: 4 } }; }
function preferenceTransport(read: MockRouteHandler, apply: MockRouteHandler = receipt) {
  return new MockControlTransport({ capabilities: { features: { managementWorkContract: true, configurationSettingsWorkContract: true } }, routes: {
    'configuration.settings': read,
    'configuration.settings.preview': { schemaVersion: 'rag-ime.management-work-preview.v1', ok: true, previewToken: 'preferences-preview', pathId: 'configuration.settings.apply', payloadSha256: digest, expectedRevision: { runtimeRevision: 4, subjectRevision: 'settings:4' }, expiresAtMs: Date.now() + 60_000, requiredConfirm: 'apply', summary: { title: '保存记忆偏好', items: ['更新稳定偏好'], risk: 'R1' } },
    'configuration.settings.apply': apply,
  } });
}
async function choosePriority(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('combobox', { name: '稳定偏好' }));
  await user.click(await screen.findByRole('option', { name: '优先记住' }));
}
function renderWithTransport(transport: MockControlTransport, child: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return { ...render(<MemoryRouter><TooltipProvider><ControlTransportProvider transport={transport}><QueryClientProvider client={client}>{child}</QueryClientProvider></ControlTransportProvider></TooltipProvider></MemoryRouter>), client };
}

describe('preference feedback follows request ownership', () => {
  it('keeps an unsaved draft quiet and tracks a real reread through inactivity and failure', async () => {
    let rejectRead: ((reason: Error) => void) | undefined;
    let reading = false;
    const transport = preferenceTransport(() => reading ? new Promise((_, reject) => { rejectRead = reject; }) : settings(365));
    const user = userEvent.setup();
    const surface = (active: boolean) => <MotionActivityBoundary active={active}><MemoryPreferences /></MotionActivityBoundary>;
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const tree = (active: boolean) => <MemoryRouter><TooltipProvider><ControlTransportProvider transport={transport}><QueryClientProvider client={client}>{surface(active)}</QueryClientProvider></ControlTransportProvider></TooltipProvider></MemoryRouter>;
    const view = render(tree(true));
    await choosePriority(user);
    const status = () => view.container.querySelector('.memory-preferences__persistence')!;
    expect(status()).toHaveTextContent('等待保存');expect(status()).toHaveAttribute('data-busy', 'false');
    reading = true;await user.click(screen.getByRole('button', { name: '重新读取' }));
    await waitFor(() => expect(status()).toHaveAttribute('data-busy', 'true'));
    view.rerender(tree(false));expect(status()).toHaveAttribute('data-motion-active', 'false');
    expect(screen.getByRole('combobox', { name: '稳定偏好' })).toHaveTextContent('优先记住');
    await act(async () => rejectRead?.(new Error('read unavailable')));
    await screen.findByText('暂时无法更新记忆偏好');
    expect(status()).toHaveAttribute('data-busy', 'false');expect(status()).toHaveTextContent('等待读取最新设置');
    view.rerender(tree(true));expect(status()).toHaveAttribute('data-busy', 'false');
    expect(screen.getByRole('combobox', { name: '稳定偏好' })).toHaveTextContent('优先记住');
    expect(transport.requests.some(({ request }) => request.pathId === 'configuration.settings.apply')).toBe(false);
  });
});
