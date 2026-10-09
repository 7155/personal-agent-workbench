import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import './index';
import { CHAT_PRESENTATION_STORAGE_KEY } from '@/features/conversation-ui/reading/chat-presentation';

describe('portable Agent controls', () => {
  let handle: ReturnType<typeof window.pawAgentUI.mount> | undefined;
  afterEach(() => { act(() => handle?.destroy()); document.body.replaceChildren(); localStorage.removeItem(CHAT_PRESENTATION_STORAGE_KEY); delete document.body.dataset.chatPresentation; delete document.body.dataset.pawPortableReading; });
  it('scopes display rollback to the real App owner while retaining draft and model selection', async () => {
    const selection={provider:'owner',model:'actual-model',thinkingLevel:'high'};
    window.pawApp={identity:{appId:'app-one',version:16},models:vi.fn(async()=>({selected:selection,catalog:{providers:[{id:'owner',models:[{id:'actual-model',thinkingLevels:['high']}]}]}}))};
    const make=(id: string)=>{
      const root=document.createElement('section'),controls=document.createElement('div'),recovery=document.createElement('div'),settings=document.createElement('div'),draft=document.createElement('textarea');
      draft.value='版本切换仍保留的草稿';root.append(controls,recovery,settings,draft);document.body.append(root);
      return {root,controls,recovery,settings,draft,presentation:{ownerKey:id,root,settings,defaultVersion:'v2' as const},onRetry:vi.fn()};
    };
    const one=make('portable:app-one'),two=make('portable:app-two');let second: ReturnType<typeof window.pawAgentUI.mount> | undefined;
    try {
      await act(async()=>{handle=window.pawAgentUI.mount(one);second=window.pawAgentUI.mount(two);await Promise.all([handle.ready,second.ready])});
      expect(one.root).toHaveAttribute('data-chat-presentation','v2');expect(two.root).toHaveAttribute('data-chat-presentation','v2');
      fireEvent.click(within(one.settings).getByRole('button',{name:'经典 v1'}));
      await waitFor(()=>expect(one.root).toHaveAttribute('data-chat-presentation','v1'));
      expect(two.root).toHaveAttribute('data-chat-presentation','v2');
      expect(one.draft).toHaveValue('版本切换仍保留的草稿');expect(one.draft.isConnected).toBe(true);expect(handle?.selection()).toEqual(selection);
      fireEvent.click(within(one.settings).getByRole('button',{name:'恢复上一显示版本 v2'}));
      await waitFor(()=>expect(one.root).toHaveAttribute('data-chat-presentation','v2'));
      expect(window.pawApp.models).toHaveBeenCalledTimes(2);
    } finally {act(()=>second?.destroy())}
  });
  it('keeps Luna Max, uses readable errors, and checks uncertain work instead of issuing a retry', async () => {
    const selection = { provider: 'openai-codex', model: 'gpt-5.6-luna', thinkingLevel: 'max' };
    const controls = document.createElement('div'), recovery = document.createElement('div');
    document.body.append(controls, recovery);
    window.pawApp = { models: async () => ({ selected: selection, catalog: { providers: [{ id: selection.provider,
      models: [{ id: selection.model, name: 'Luna', thinkingLevels: ['low', 'high', 'max'] }] }] } }) };
    const retry = vi.fn(), check = vi.fn();
    await act(async () => { handle = window.pawAgentUI.mount({ controls, recovery, onRetry: retry, onCheck: check }); await handle.ready; });
    expect(handle?.selection()).toEqual(selection);
    expect(document.body).not.toHaveAttribute('data-paw-portable-reading');
    expect(screen.getByRole('button', { name: /模型与推理：Luna.*最高/u })).toBeEnabled();
    act(() => handle?.failure({ state: 'failed', message: 'APP_API_BASE_URL missing' }));
    expect(screen.getByRole('alert')).toHaveTextContent('模型连接配置不完整');
    expect(screen.queryByText(/APP_API_BASE_URL/u)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '重试本轮' })); expect(retry).toHaveBeenCalledTimes(1);
    act(() => handle?.failure({ state: 'unconfirmed', message: 'connection lost' }));
    expect(screen.queryByRole('button', { name: '重试本轮' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '核对原调用' })); expect(check).toHaveBeenCalledTimes(1);
    expect(retry).toHaveBeenCalledTimes(1);
    act(() => handle?.setBusy(true));
    await waitFor(() => expect(screen.getByRole('button', { name: '核对原调用' })).toBeDisabled());
  });
});
