import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { MockControlTransport, type MockRouteHandler } from '@/test/mock-transport';
import { ControlTransportHttpError } from '@/platform/http-transport';
import { MemoryProfile, type PersonalProfile } from './MemoryProfile';
import { TooltipProvider } from '@/components/primitives';

afterEach(cleanup);
const profile: PersonalProfile = { schemaVersion: 'paw.personal-profile.v1', revision: 'profile-1', text: '原来的背景', truncated: false,
  paragraphs: [{ id: 'card-1', memoryIds: ['card-1'], text: '原来的背景', revision: 'card-revision-1', sourceCount: 1, sourceRefs: [{ kind: 'evidence', id: 'source-1' }] }] };
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; }
function setup(save: MockRouteHandler, read: MockRouteHandler = profile) {
  const transport = new MockControlTransport({ routes: { 'memory.profile': read, 'memory.profile.save': save } });
  const onOpenReference = vi.fn();
  const view = render(<ControlTransportProvider transport={transport}><TooltipProvider><MemoryProfile onOpenReference={onOpenReference} /></TooltipProvider></ControlTransportProvider>);
  return { transport, onOpenReference, ...view };
}
describe('editable personal profile', () => {
  it('includes paragraph separators in the rendered budget before saving', async () => {
    const many: PersonalProfile = { ...profile, text: Array(7).fill('a').join('\n\n'), paragraphs: Array.from({ length: 7 }, (_, index) => ({ ...profile.paragraphs[0], id: `card-${index}`, memoryIds: [`card-${index}`], text: 'a' })) };
    const pending = deferred<unknown>();
    const { transport } = setup(() => pending.promise, many);
    await screen.findByRole('textbox', { name: '个人背景 7' });
    for (let index = 1; index <= 7; index++) fireEvent.change(screen.getByRole('textbox', { name: `个人背景 ${index}` }), { target: { value: 'x'.repeat(index === 7 ? 400 : 600) } });
    expect(screen.getByText('4012 / 4000 字')).toBeVisible();
    expect(screen.getByRole('button', { name: '保存修改' })).toBeDisabled();
    expect(transport.requests.filter(({ request }) => request.pathId === 'memory.profile.save')).toHaveLength(0);
    fireEvent.change(screen.getByRole('textbox', { name: '个人背景 7' }), { target: { value: 'x'.repeat(388) } });
    expect(screen.getByText('4000 / 4000 字')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
    expect(transport.requests.filter(({ request }) => request.pathId === 'memory.profile.save')).toHaveLength(1);
  });
  it('counts normalized Unicode text while retaining the complete editable draft', async () => {
    setup({});
    const input = await screen.findByRole('textbox', { name: '个人背景 1' });
    const draft = `\u0085\t${'🙂'.repeat(600)}\n `;
    fireEvent.change(input, { target: { value: draft } });
    expect(input).toHaveValue(draft);
    expect(screen.getByText('600 / 4000 字')).toBeVisible();
    expect(screen.getByRole('button', { name: '保存修改' })).toBeEnabled();
  });
  it('marks edits unsaved and serializes latest-version reads without discarding the draft', async () => {
    const pending = deferred<unknown>();
    let reads = 0;
    const newer = { ...profile, revision: 'profile-new', text: '最新背景' };
    const { transport } = setup(() => { throw new ControlTransportHttpError('memory.profile.save', 409, 'memory_profile_revision_conflict', { code: 'memory_profile_revision_conflict', current: profile }); }, () => ++reads === 1 ? profile : pending.promise);
    const input = await screen.findByRole('textbox', { name: '个人背景 1' });
    fireEvent.change(input, { target: { value: '我正在编辑' } });
    expect(screen.getByText(/未保存的修改 · 版本/)).toBeVisible();
    expect(screen.getByRole('status')).toHaveTextContent('点击“保存修改”');
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
    const read = await screen.findByRole('button', { name: '查看最新版本' });
    await waitFor(() => expect(read).toBeEnabled());
    fireEvent.click(read); fireEvent.click(read);
    expect(screen.getByRole('button', { name: '正在读取最新版本…' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '放弃下方草稿，编辑最新版本' })).toBeDisabled();
    expect(transport.requests.filter(({ request }) => request.pathId === 'memory.profile')).toHaveLength(2);
    await act(async () => pending.resolve(newer));
    expect(screen.getByRole('region', { name: '服务器最新版本' })).toHaveTextContent('最新背景');
    expect(input).toHaveValue('我正在编辑');
  });
  it('saves with whole-profile and paragraph revisions, serializes repeated clicks, and opens the original source', async () => {
    const result = deferred<unknown>(); const { transport, onOpenReference } = setup(() => result.promise);
    const input = await screen.findByRole('textbox', { name: '个人背景 1' });
    fireEvent.click(screen.getByRole('button', { name: '来源 1' }));
    expect(onOpenReference).toHaveBeenCalledWith({ kind: 'evidence', referenceId: 'source-1' });
    fireEvent.change(input, { target: { value: '新背景' } });
    const save = screen.getByRole('button', { name: '保存修改' }); fireEvent.click(save); fireEvent.click(save);
    const writes = transport.requests.filter(({ request }) => request.pathId === 'memory.profile.save');
    expect(writes).toHaveLength(1);
    expect(writes[0].request.body).toMatchObject({ expectedRevision: 'profile-1', clientRequestId: expect.any(String), paragraphs: [{ id: 'card-1', memoryIds: ['card-1'], revision: 'card-revision-1', text: '新背景' }] });
    await act(async () => result.resolve({ ok: true, profile: { ...profile, revision: 'profile-2', text: '新背景', paragraphs: [{ ...profile.paragraphs[0], text: '新背景' }] } }));
    expect(screen.getByRole('button', { name: '已保存' })).toBeDisabled();
  });
  it('preserves the complete draft on a stale save and does not silently overwrite the new version', async () => {
    const latest = { ...profile, revision: 'profile-2', text: '另一个窗口保存的背景', paragraphs: [{ ...profile.paragraphs[0], text: '另一个窗口保存的背景' }] };
    const { transport } = setup(() => { throw new ControlTransportHttpError('memory.profile.save', 409, 'memory_profile_revision_conflict', { code: 'memory_profile_revision_conflict', current: latest }); });
    const input = await screen.findByRole('textbox', { name: '个人背景 1' });
    fireEvent.change(input, { target: { value: '我的未保存草稿' } }); fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('你的草稿完整保留');
    expect(input).toHaveValue('我的未保存草稿');
    expect(screen.getByRole('region', { name: '服务器最新版本' })).toHaveTextContent('另一个窗口保存的背景');
    expect(screen.getByRole('button', { name: '保存修改' })).toBeDisabled();
    expect(transport.requests.filter(({ request }) => request.pathId === 'memory.profile.save')).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: '放弃下方草稿，编辑最新版本' }));
    expect(input).toHaveValue('另一个窗口保存的背景');
  });
  it('reuses the same idempotency key after an uncertain save and keeps the text', async () => {
    let count = 0; const { transport } = setup(() => { if (!count++) throw new Error('network disconnected'); return { ok: true, profile }; });
    const input = await screen.findByRole('textbox', { name: '个人背景 1' });
    fireEvent.change(input, { target: { value: '保留这个草稿' } }); fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('草稿已保留');
    expect(input).toHaveValue('保留这个草稿'); fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
    await waitFor(() => expect(transport.requests.filter(({ request }) => request.pathId === 'memory.profile.save')).toHaveLength(2));
    const writes = transport.requests.filter(({ request }) => request.pathId === 'memory.profile.save'); expect(writes[0].request.body).toEqual(writes[1].request.body);
  });
  it('never exposes an editable empty profile after a failed read', async () => {
    setup({}, () => { throw new Error('offline'); });
    await screen.findByRole('alert'); expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '保存修改' })).not.toBeInTheDocument();
  });
  it('keeps over-limit text intact and prevents a clipped save', async () => {
    const { transport } = setup({}); const input = await screen.findByRole('textbox', { name: '个人背景 1' });
    fireEvent.change(input, { target: { value: '字'.repeat(601) } });
    expect(input).toHaveValue('字'.repeat(601)); expect(screen.getByRole('button', { name: '保存修改' })).toBeDisabled();
    expect(transport.requests.some(({ request }) => request.pathId === 'memory.profile.save')).toBe(false);
  });
});
