import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { ControlTransportProvider } from '@/app/control-transport';
import { StubControlTransport } from '@/test/stub-control-transport';
import { TooltipProvider } from '@/components/primitives';
import { PluginStudio } from './PluginStudio';

afterEach(cleanup);
function Location() { const location = useLocation(); return <output aria-label="目的地">{location.pathname}{location.search}</output>; }
function setup(fail = false) {
  const transport = new StubControlTransport('mock', {
    'agent.extensions.create': { ok: true, draft: { sourcePath: '/managed/drafts/review-notes' } },
    'agent.extensions.validate': () => { if (fail) throw new Error('Unavailable'); return { ok: true, validationToken: 'validation-exact' }; },
    'agent.extensions.preview': { ok: true, previewToken: 'preview-exact', payloadSha256: 'a'.repeat(64), summary: { action: 'install' } },
  });
  const onPreview = vi.fn();
  const onPrepare = vi.fn();
  const onPendingChange = vi.fn();
  render(<MemoryRouter><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <ControlTransportProvider transport={transport}><TooltipProvider><PluginStudio onPendingChange={onPendingChange} onPrepare={onPrepare} onPreview={onPreview} /><Location /></TooltipProvider></ControlTransportProvider>
  </QueryClientProvider></MemoryRouter>);
  return { transport, onPreview, onPrepare, onPendingChange, user: userEvent.setup() };
}
it('creates a native Skill package and prepares the exact retained draft for installation', async () => {
  const { transport, onPreview, user } = setup();
  await user.click(screen.getByRole('radio', { name: '自己编写' }));
  await user.type(screen.getByRole('textbox', { name: '插件标识' }), 'review-notes');
  await user.type(screen.getByRole('textbox', { name: '用途' }), '整理复盘笔记');
  await user.type(screen.getByRole('textbox', { name: '内容' }), '先读取材料，再写复盘。');
  await user.click(screen.getByRole('button', { name: '保存草稿并检查安装' }));
  await waitFor(() => expect(onPreview).toHaveBeenCalledWith(expect.objectContaining({ previewToken: 'preview-exact' })));
  const create = transport.requests.find((r) => r.pathId === 'agent.extensions.create');
  expect(create?.body).toMatchObject({ packageJson: { name: '@paw-local/review-notes', version: '0.1.0', pi: { skills: ['./skills'] } },
    files: { 'skills/review-notes/SKILL.md': expect.stringContaining('先读取材料，再写复盘。') } });
  expect(transport.requests.find((r) => r.pathId === 'agent.extensions.validate')?.body).toEqual({ packageSource: '/managed/drafts/review-notes' });
  expect(transport.requests.find((r) => r.pathId === 'agent.extensions.preview')?.body).toEqual({ action: 'install', validationToken: 'validation-exact', enable: true });
  expect(transport.requests.some((r) => r.pathId === 'agent.extensions.apply')).toBe(false);
});
it('preserves authored text and does not propose or claim installation after validation fails', async () => {
  const { transport, onPreview, onPrepare, onPendingChange, user } = setup(true);
  await user.click(screen.getByRole('radio', { name: '自己编写' }));
  await user.type(screen.getByRole('textbox', { name: '插件标识' }), 'review-notes');
  await user.type(screen.getByRole('textbox', { name: '用途' }), '整理复盘笔记');
  await user.type(screen.getByRole('textbox', { name: '内容' }), '保留这段内容');
  await user.click(screen.getByRole('button', { name: '保存草稿并检查安装' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('草稿');
  expect(screen.getByRole('textbox', { name: '内容' })).toHaveValue('保留这段内容');
  expect(onPreview).not.toHaveBeenCalled();
  expect(onPrepare).toHaveBeenCalledTimes(1);
  expect(onPendingChange.mock.calls).toEqual([[true], [false]]);
  expect(transport.requests.some((r) => r.pathId === 'agent.extensions.preview')).toBe(false);
});
it('opens an App creation conversation with the user brief and explicit installation choice', async () => {
  const { user, transport } = setup();
  await user.type(screen.getByRole('textbox', { name: '名称' }), '资料阅读');
  await user.type(screen.getByRole('textbox', { name: '用途' }), '阅读项目文档并回答问题');
  await user.click(screen.getByRole('button', { name: '在 Agent 中制作' }));
  const destination = screen.getByLabelText('目的地').textContent ?? '';
  const query = new URLSearchParams(destination.split('?')[1]);
  expect(destination).toMatch(/^\/agent\?/);
  expect(query.get('draft')).toContain('/skill:pawos-app-builder');
  expect(query.get('draft')).toContain('资料阅读');
  expect(query.get('draft')).toContain('阅读项目文档并回答问题');
  expect(query.get('draft')).toContain('安装');
  expect(transport.requests.some((r) => r.pathId === 'agent.session.prompt')).toBe(false);
});
