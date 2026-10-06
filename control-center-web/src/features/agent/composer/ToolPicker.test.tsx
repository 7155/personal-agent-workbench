import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ComponentProps } from 'react';
import type { CapabilityCatalog } from '@/features/plugins/capability-policy';
import { previewSessions } from '../preview-data';
import type { ToolManifest } from '../types';
import { ToolPicker } from './ToolPicker';
import { countAvailableTools } from './tool-policy';
import { buildCapabilityRows } from './capability-display';
import { PawOsDesktopProvider } from '@/features/paw-os/surface-context';

afterEach(cleanup);
const tools: ToolManifest[] = (['memory', 'knowledge'] as const).map(id => ({
  schemaVersion: 'rag-ime.control-tool-manifest.v1', id, domain: id,
  displayName: id === 'memory' ? '记忆与工具书' : '知识检索', description: `原始${id}工具说明`,
  category: id, riskLevel: 'R0', sessionModes: ['assistant', 'coordinator'],
  operations: ['status'], resultPresentation: 'tool_result', availability: 'online', version: '1',
}));
function catalog(): CapabilityCatalog {
  return {
    schemaVersion: 'rag-ime.capability-catalog.v1', ok: true, revision: '1', effectiveAtMs: 1,
    projectScope: { supported: false, identityKind: 'none', reason: 'test' },
    sessionPolicy: { sessionId: previewSessions[0].id, policyRevision: 1, effectiveAtMs: 1,
      disclosurePreferences: { globalDefault: {}, projectDefault: {}, session: { 'tool:knowledge': 'disabled' }, effective: { 'tool:memory': 'enabled', 'tool:knowledge': 'disabled' } } },
    items: tools.map(tool => ({ id: tool.id, canonicalId: `tool:${tool.id}`, kind: 'tool', displayName: tool.displayName,
      description: tool.description, source: { kind: 'built_in', label: 'Control Center' }, status: 'available',
      risk: 'R0', requiredPermissions: [], authorization: { state: 'authorized', reason: 'test' },
      disclosure: { preference: 'inherit', effective: tool.id === 'memory' ? 'enabled' : 'disabled', state: tool.id === 'memory' ? 'disclosed' : 'hidden', reason: 'test' },
      effectiveScope: tool.id === 'memory' ? 'built_in_default' : 'session', reasons: [], revision: '1', effectiveAtMs: 1,
    })),
  };
}
function setup(overrides: Partial<ComponentProps<typeof ToolPicker>> = {}, openRoute?: (route:string) => void) {
  const props: ComponentProps<typeof ToolPicker> = { adjustmentDisabled: false, capabilityCatalog: catalog(), capabilityPolicyPending: false,
    disabled: false, onCapabilityPreferenceChange: vi.fn(), onSelect: vi.fn(), requestOpen: 0,
    session: previewSessions[0], status: 'ready', tools, ...overrides };
  return { ...render(<ToolPicker {...props}/>, {wrapper:openRoute ? ({children}) => <PawOsDesktopProvider openRoute={openRoute} openWindow={()=>{}}>{children}</PawOsDesktopProvider> : undefined}), props, user: userEvent.setup() };
}
async function open(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: /对话功能：/ }));
}
const memory = () => screen.getByRole('button', { name: /^记忆召回 当前可用/ });

describe('ToolPicker conversation capability presentation', () => {
  it('retains search and selected capability when returning from defaults to the same conversation', async () => {
    const openRoute=vi.fn();const {user,props,rerender}=setup({},openRoute);await open(user);
    await user.type(screen.getByRole('textbox',{name:'搜索当前对话功能'}),'记忆');await user.click(memory());
    await user.click(screen.getByText('权限与标识'));
    await user.click(screen.getByRole('button',{name:'管理功能与默认设置'}));
    expect(openRoute).toHaveBeenCalledWith(`/plugins?view=capabilities&sessionId=${props.session!.id}&capability=tool%3Amemory`);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    rerender(<ToolPicker {...props} requestOpen={1}/>);
    expect(await screen.findByRole('textbox',{name:'搜索当前对话功能'})).toHaveValue('记忆');
    const detail=screen.getByRole('complementary',{name:'记忆召回的功能详情'});
    expect(detail).toBeVisible();
    await vi.waitFor(()=>expect(within(detail).getByText('权限与标识').closest('details')).toHaveAttribute('open'));
    expect(props.onSelect).not.toHaveBeenCalled();expect(props.onCapabilityPreferenceChange).not.toHaveBeenCalled();
  });

  it('drops the previous management view when the conversation owner changes', async () => {
    const {user,props,rerender}=setup({},vi.fn());await open(user);
    await user.type(screen.getByRole('textbox',{name:'搜索当前对话功能'}),'记忆');await user.click(memory());
    await user.click(screen.getByRole('button',{name:'管理功能与默认设置'}));
    const next=catalog();next.sessionPolicy!.sessionId='other-session';
    rerender(<ToolPicker {...props} sessionId="other-session" capabilityCatalog={next} requestOpen={1}/>);
    expect(await screen.findByRole('textbox',{name:'搜索当前对话功能'})).toHaveValue('');
    expect(screen.queryByRole('complementary')).not.toBeInTheDocument();
  });
  it('counts matching executable tools separately from registered resources', () => {
    setup();
    expect(screen.getByRole('button', { name: /1 个当前可用工具，2 个已登记工具/ })).toBeInTheDocument();
    const wrong = catalog(); wrong.sessionPolicy!.sessionId = 'other';
    expect(countAvailableTools(tools, previewSessions[0], wrong)).toBe(0);
  });
  it('uses backend policy before provisional Session metadata arrives', () => {
    expect(countAvailableTools([{ ...tools[0], sessionModes: ['coordinator'] }], { ...previewSessions[0], mode: 'assistant', toolProfileVersion: undefined }, catalog())).toBe(1);
  });
  it('can show loading without exposing a stale partner catalog', async () => {
    const stale = catalog(); stale.sessionPolicy!.sessionId = 'previous';
    const { user } = setup({ capabilityCatalog: stale }); await open(user);
    expect(screen.getByRole('status')).toHaveTextContent('正在读取能力目录');
    expect(screen.queryByText('记忆召回')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('此对话如何使用')).not.toBeInTheDocument();
  });
  it('closes on partner change and refuses the previous catalog even when reopened', async () => {
    const { user, rerender, props } = setup(); await open(user); await user.click(memory());
    rerender(<ToolPicker {...props} sessionId="next-partner"/>);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    await open(user); expect(screen.queryByText('记忆召回')).not.toBeInTheDocument();
    expect(props.onCapabilityPreferenceChange).not.toHaveBeenCalled();
  });
  it('distinguishes read failure from an empty catalog', async () => {
    const { user } = setup({ status: 'failed' }); await open(user);
    expect(screen.getByRole('status')).toHaveTextContent('能力目录暂不可用');
    expect(screen.queryByText('没有匹配的功能')).not.toBeInTheDocument();
  });
  it('keeps canonical preferences behind explicit details', async () => {
    const { user, props } = setup(); await open(user); await user.click(memory());
    const detail = screen.getByRole('complementary', { name: '记忆召回的功能详情' });
    expect(detail).toHaveTextContent('产品内置默认'); expect(detail).toHaveTextContent('已登记');
    expect(detail).not.toHaveTextContent('可安装');
    await user.selectOptions(within(detail).getByLabelText('此对话如何使用'), 'disabled');
    expect(props.onCapabilityPreferenceChange).toHaveBeenCalledWith('tool:memory', 'disabled');
    expect(props.onSelect).not.toHaveBeenCalled();
  });
  it('filters memory without changing preferences', async () => {
    const { user, props } = setup(); await open(user);
    await user.click(screen.getByRole('button', { name: '查看记忆召回设置' }));
    expect(memory()).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^知识库 \/ Agent RAG/ })).not.toBeInTheDocument();
    expect(props.onCapabilityPreferenceChange).not.toHaveBeenCalled();
  });
  it('offers the memory switch directly and describes inherited opt-out as closed', async () => {
    const value = catalog();
    value.items[0].authorization = { state: 'denied', reason: 'existing_session_policy_does_not_authorize_tool' };
    value.items[0].disclosure = { preference: 'inherit', effective: 'disabled', state: 'hidden', reason: 'inherited_global_default' };
    value.items[0].effectiveScope = 'global_default';
    const { user, props } = setup({ capabilityCatalog: value });
    await open(user);
    await user.click(screen.getByRole('button', { name: '查看记忆召回设置' }));
    expect(screen.getByRole('button', { name: /^记忆召回 当前已关闭/ })).toBeInTheDocument();
    expect(screen.queryByText('受权限限制')).not.toBeInTheDocument();
    const preference = screen.getByRole('combobox', { name: '本对话记忆召回' });
    expect(preference).toBeEnabled();
    expect(preference).toHaveValue('inherit');
    await user.selectOptions(preference, 'enabled');
    expect(props.onCapabilityPreferenceChange).toHaveBeenCalledWith('tool:memory', 'enabled');
    expect(props.onSelect).not.toHaveBeenCalled();
  });
  it('exposes the native codemode setting only when the Session snapshot provides it', async () => {
    const onCodemodeModeChange = vi.fn();
    const { user, props } = setup({ codemodeMode: 'on', onCodemodeModeChange });
    await open(user);
    const mode = screen.getByRole('combobox', { name: '代码执行编排方式' });
    expect(mode).toHaveValue('on');
    await user.selectOptions(mode, 'only');
    expect(onCodemodeModeChange).toHaveBeenCalledWith('only');

    cleanup();
    const hidden = setup();
    await open(hidden.user);
    expect(screen.queryByRole('combobox', { name: '代码执行编排方式' })).not.toBeInTheDocument();
  });
  it('retains a real permission restriction when memory is enabled', () => {
    const value = catalog();
    value.items[0].authorization = { state: 'denied', reason: 'session_policy' };
    expect(buildCapabilityRows(tools, previewSessions[0], value, previewSessions[0].id)[0]).toMatchObject({ state: 'denied' });
  });
  it.each([{ adjustmentDisabled: true }, { capabilityPolicyPending: true }])('locks the direct memory switch while busy: %j', async overrides => {
    const { user, props } = setup(overrides);
    await open(user);
    await user.click(screen.getByRole('button', { name: '查看记忆召回设置' }));
    expect(screen.getByRole('combobox', { name: '本对话记忆召回' })).toBeDisabled();
    expect(props.onCapabilityPreferenceChange).not.toHaveBeenCalled();
  });
  it.each([{ adjustmentDisabled: true }, { capabilityPolicyPending: true }])('locks preference updates while busy: %j', async overrides => {
    const { user, props } = setup(overrides); await open(user); await user.click(memory());
    expect(screen.getByLabelText('此对话如何使用')).toBeDisabled();
    if (overrides.adjustmentDisabled) expect(screen.getByRole('button', { name: '加入消息' })).toBeDisabled();
    expect(props.onCapabilityPreferenceChange).not.toHaveBeenCalled();
  });
  it('searches both user-facing and original tool names and recovers from no results', async () => {
    const { user } = setup(); await open(user);
    const search = screen.getByRole('textbox', { name: '搜索当前对话功能' }); expect(search).toHaveFocus();
    await user.type(search, '记忆与工具书'); expect(memory()).toBeInTheDocument();
    await user.clear(search); await user.type(search, '不存在');
    expect(screen.getByRole('status')).toHaveTextContent('没有匹配的功能');
    await user.click(screen.getByRole('button', { name: '清空筛选' })); expect(memory()).toBeInTheDocument();
  });
  it('requires explicit insertion, never executing or changing policy on selection', async () => {
    const { user, props } = setup(); await open(user); await user.click(memory());
    expect(props.onSelect).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: '加入消息' }));
    expect(props.onSelect).toHaveBeenCalledWith(tools[0]);
    expect(props.onCapabilityPreferenceChange).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
  it('offers extension policy but never invents a tool insertion', async () => {
    const value = catalog(); value.items.push({ ...value.items[0], id: 'review', canonicalId: 'extension:review', kind: 'extension', displayName: '对话复盘', status: 'installed' });
    const { user, props } = setup({ capabilityCatalog: value }); await open(user);
    await user.click(screen.getByRole('button', { name: /^扩展/ }));
    await user.click(screen.getByRole('button', { name: /^对话复盘/ }));
    await user.selectOptions(screen.getByLabelText('此对话如何使用'), 'disabled');
    expect(props.onCapabilityPreferenceChange).toHaveBeenCalledWith('extension:review', 'disabled');
    expect(screen.queryByRole('button', { name: '加入消息' })).not.toBeInTheDocument();
  });
  it('does not treat disclosure as installation or installation as permission', () => {
    const value = catalog();
    for (const [id, status, auth] of [['listed', 'available', 'authorized'], ['denied', 'installed', 'denied'], ['ready', 'installed', 'authorized']] as const)
      value.items.push({ ...value.items[0], id, canonicalId: `skill:${id}`, kind: 'skill', status, authorization: { state: auth, reason: '' } });
    const rows = buildCapabilityRows(tools, previewSessions[0], value, previewSessions[0].id);
    expect(rows.find(row => row.id === 'listed')).toMatchObject({ resourceLabel: '可安装', state: 'unavailable' });
    expect(rows.find(row => row.id === 'denied')).toMatchObject({ resourceLabel: '已安装', state: 'denied' });
    expect(rows.find(row => row.id === 'ready')).toMatchObject({ stateLabel: '已提供给对话' });
  });
});
