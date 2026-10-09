import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import type { ControlRequest } from '@/platform/transport';
import { MockControlTransport } from '@/test/mock-transport';
import { TraceAppShell, TraceCapabilityLibrary, TraceKnowledgeLibrary } from './trace-app-shell';

afterEach(() => { cleanup(); vi.restoreAllMocks(); window.localStorage.clear(); });

it('keeps Trace navigation collapsible while preserving the current report and draft', async () => {
  const user = userEvent.setup();
  const onNavigate = vi.fn();
  render(<TraceAppShell view="report" onNavigate={onNavigate}><input aria-label="任务目标" defaultValue="保持已有草稿" /></TraceAppShell>);
  const navigation = screen.getByRole('navigation', { name: 'Trace Agent 应用导航' });
  await user.click(screen.getByRole('button', { name: '收起Trace Agent 导航' }));
  expect(navigation).not.toBeVisible();
  expect(screen.getByRole('textbox', { name: '任务目标' })).toHaveValue('保持已有草稿');
  expect(screen.getByRole('button', { name: '← 返回工作台' })).toBeVisible();
  await user.click(screen.getByRole('button', { name: '展开Trace Agent 导航' }));
  await user.click(within(navigation).getByRole('button', { name: '经验库' }));
  expect(onNavigate).toHaveBeenCalledWith('knowledge');
});

describe('Trace App libraries', () => {
  it('reads a selected project and an exact pattern revision without creating an execution', async () => {
    const reveal = vi.spyOn(HTMLElement.prototype, 'scrollIntoView');
    const transport = new MockControlTransport({ routes: {
      'observability.traceOptimization.library': (request: ControlRequest) => ({ ok: true, projects: [{ projectId: 'project:1', title: '排障项目' }], projectId: 'project:1', patterns: [{ patternId: 'pattern:1', revision: 3, title: '区分输入错误与暂时性故障', summary: '对输入错误停止重试。', status: 'supported', componentRef: 'skill:retry', evidenceCount: 2 }], truncated: false, ...(request.query?.patternId ? { pattern: { patternId: 'pattern:1', revision: 3, content: { observations: [{ statement: '两段对话重复出现同类无效重试', evidenceIds: ['evidence:1', 'evidence:2'] }], hypotheses: [{ statement: '错误分类可以减少无效调用', uncertainty: '仍需独立实例验证' }] } } } : {}) }),
    } });
    renderLibrary(<TraceKnowledgeLibrary />, transport);
    const trigger = await screen.findByRole('button', { name: /区分输入错误与暂时性故障/ });
    await userEvent.setup().click(trigger);
    const detail = screen.getByRole('article', { name: '经验版本详情' });
    expect(detail).toHaveFocus();
    expect(reveal).toHaveBeenCalledTimes(2);
    expect(reveal).toHaveBeenLastCalledWith({ block: 'start', behavior: 'instant' });
    expect(await screen.findByText('两段对话重复出现同类无效重试')).toBeInTheDocument();
    expect(screen.getByText(/仍需独立实例验证/)).toBeInTheDocument();
    expect(transport.requests.at(-1)?.request.query).toEqual({ projectId: 'project:1', patternId: 'pattern:1', revision: 3 });
    expect(transport.requests.every(({ request }) => request.pathId === 'observability.traceOptimization.library')).toBe(true);
    await userEvent.setup().keyboard('{Escape}');
    expect(screen.queryByRole('article', { name: '经验版本详情' })).toBeNull();
    expect(trigger).toHaveFocus();
  });

  it('retains the original version and unsent search through detail failure, explicit retry and close without moving focus on receipt', async () => {
    const user = userEvent.setup(); let detailReads = 0; let finishDetail!: () => void;
    const transport = new MockControlTransport({ routes: {
      'observability.traceOptimization.library': (request: ControlRequest) => {
        const base = { ok: true, projects: [{ projectId: 'original-project', title: '原项目' }], projectId: 'original-project', patterns: [{ patternId: 'original-pattern', revision: 7, title: '原版本', summary: '未修改正文', status: 'supported', evidenceCount: 1 }], truncated: false };
        if (!request.query?.patternId) return base;
        detailReads += 1; if (detailReads === 1) throw new Error('原版本正文暂不可读');
        return new Promise((resolve) => { finishDetail = () => resolve({ ...base, pattern: { patternId: 'original-pattern', revision: 7, content: { summary: '原版本已读取' } } }); });
      },
    } });
    renderLibrary(<TraceKnowledgeLibrary />, transport);
    const draft = screen.getByRole('searchbox', { name: '查找经验' });
    await user.type(draft, '未提交查找稿');
    const trigger = await screen.findByRole('button', { name: /原版本/ }); await user.click(trigger);
    const article = screen.getByRole('article', { name: '经验版本详情' }); expect(article).toHaveFocus();
    const alert = await screen.findByRole('alert');
    const retry = within(alert).getByRole('button', { name: '重试' }); await user.click(retry);
    const close = screen.getByRole('button', { name: '返回经验列表' }); close.focus();
    await act(async () => finishDetail());
    expect(await screen.findByText('原版本已读取')).toBeVisible(); expect(close).toHaveFocus();
    expect(transport.requests.filter(({ request }) => request.query?.patternId).map(({ request }) => request.query)).toEqual([
      { projectId: 'original-project', patternId: 'original-pattern', revision: 7 },
      { projectId: 'original-project', patternId: 'original-pattern', revision: 7 },
    ]);
    await user.click(screen.getByRole('button', { name: '返回经验列表' })); expect(trigger).toHaveFocus();
    expect(draft).toHaveValue('未提交查找稿'); expect(screen.queryByRole('article', { name: '经验版本详情' })).toBeNull();
  });

  it('does not reveal the first original body after focus has moved to the unsent search', async () => {
    const user = userEvent.setup(); const reveal = vi.spyOn(HTMLElement.prototype, 'scrollIntoView');
    let finish!: () => void;
    const transport = new MockControlTransport({ routes: {
      'observability.traceOptimization.library': (request: ControlRequest) => {
        const base = { ok: true, projects: [], projectId: 'project:original', patterns: [{ patternId: 'pattern:original', revision: 4, title: '等待原版本', summary: '保留来源', status: 'observed', evidenceCount: 1 }], truncated: false };
        return request.query?.patternId ? new Promise((resolve) => { finish = () => resolve({ ...base, pattern: { patternId: 'pattern:original', revision: 4, content: { summary: '首正文已返回' } } }); }) : base;
      },
    } });
    renderLibrary(<TraceKnowledgeLibrary />, transport);
    await user.click(await screen.findByRole('button', { name: /等待原版本/ }));
    expect(screen.getByRole('article', { name: '经验版本详情' })).toHaveFocus(); expect(reveal).toHaveBeenCalledTimes(1);
    const search = screen.getByRole('searchbox', { name: '查找经验' }); await user.type(search, '保留新查找稿');
    await act(async () => finish());
    expect(await screen.findByText('首正文已返回')).toBeVisible(); expect(search).toHaveFocus(); expect(search).toHaveValue('保留新查找稿');
    expect(reveal).toHaveBeenCalledTimes(1);
  });

  it('keeps installed capabilities and unverified candidates distinct while reporting unavailable catalogs', async () => {
    const transport = new MockControlTransport({ routes: {
      'observability.traceOptimization.capabilities': { ok: true, items: [{ id: 'skill:1', name: '排障 Skill', kind: 'skill', status: 'installed', version: 'v3', summary: '按错误类型决定重试。' }, { id: 'tool:1', name: '查询工具候选', kind: 'tool', status: 'candidate', version: 'draft:1', summary: '待验证的实现。' }], unavailable: ['提示词目录暂不可用'] },
    } });
    renderLibrary(<TraceCapabilityLibrary />, transport);
    expect(await screen.findByText('Skill · 已安装')).toBeInTheDocument();
    expect(screen.getByText('工具 · 候选草稿')).toBeInTheDocument();
    expect(screen.getByText('提示词目录暂不可用')).toBeInTheDocument();
    await userEvent.setup().selectOptions(screen.getByRole('combobox', { name: '版本状态' }), 'candidate');
    expect(screen.queryByRole('heading', { name: '排障 Skill' })).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '查询工具候选' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /安装/ })).not.toBeInTheDocument();
  });

  it('shows a recoverable library error rather than treating unavailable data as an empty catalog', async () => {
    const transport = new MockControlTransport({ routes: {
      'observability.traceOptimization.library': () => { throw new Error('项目目录读取失败'); },
    } });
    renderLibrary(<TraceKnowledgeLibrary />, transport);
    const error = await screen.findByRole('alert');
    expect(error).toHaveTextContent('项目目录读取失败');
    expect(within(error).getByRole('button', { name: '重试' })).toBeInTheDocument();
    expect(screen.queryByText('此项目还没有经验记录')).not.toBeInTheDocument();
  });
});

function renderLibrary(content: React.ReactNode, transport: MockControlTransport) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<ControlTransportProvider transport={transport}><QueryClientProvider client={client}>{content}</QueryClientProvider></ControlTransportProvider>);
}
