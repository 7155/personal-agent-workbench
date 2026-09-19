import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import { MockControlTransport } from '@/test/mock-transport';
import { PiProviderCredentials } from './PiProviderCredentials';

afterEach(cleanup);

describe('Pi provider credential UI', () => {
  it('explains that model account support is still being checked', async () => {
    const transport = new MockControlTransport({
      capabilities: { features: { piProviderCredentials: true } },
      routes: { 'agent.providers.get': providerCatalog() },
    });
    const settledCapabilities = await transport.capabilities();
    const pendingCapabilities = deferred<typeof settledCapabilities>();
    vi.spyOn(transport, 'capabilities').mockReturnValue(pendingCapabilities.promise);

    renderProvider(transport);

    expect(screen.getByText('正在检查模型账号')).toBeInTheDocument();
    expect(screen.getByText('正在确认这台 Mac 是否可以连接和管理远程模型账号。')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '重新检查' })).not.toBeInTheDocument();

    await act(async () => pendingCapabilities.resolve(settledCapabilities));
    expect(await screen.findByLabelText('API 密钥')).toBeInTheDocument();
  });

  it('hides capability errors and offers a real retry', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      capabilities: { features: { piProviderCredentials: true } },
      routes: { 'agent.providers.get': providerCatalog() },
    });
    const settledCapabilities = await transport.capabilities();
    const capabilities = vi.spyOn(transport, 'capabilities')
      .mockRejectedValueOnce(new Error('pathId=agent.providers.get providerId=gpt secret=internal'))
      .mockResolvedValue(settledCapabilities);

    renderProvider(transport);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('模型账号检查失败');
    expect(alert).toHaveTextContent('没有读到这台 Mac 的账号管理能力。');
    expect(alert).not.toHaveTextContent(/pathId|providerId|secret=internal/);

    await user.click(screen.getByRole('button', { name: '重新检查' }));

    expect(await screen.findByLabelText('API 密钥')).toBeInTheDocument();
    expect(capabilities).toHaveBeenCalledTimes(2);
  });

  it('saves an API key directly without rendering the secret', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      capabilities: { features: { piProviderCredentials: true } },
      routes: {
        'agent.providers.get': providerCatalog(),
        'agent.provider.auth.preview': {
          ok: true,
          previewToken: 'preview-provider-key',
          provider: 'gpt',
          providerName: 'GPT',
          action: 'set_api_key',
          requiredConfirm: 'replace',
          expiresAtMs: Date.now() + 60_000,
          summary: ['替换 GPT 的 API 密钥。', '现有密钥不会读取或显示。'],
          secretPolicy: '密钥仅在确认写入时送往本机 Pi。',
          sessionBoundary: '当前回复不被中断。',
        },
        'agent.provider.auth.apply': {
          ok: true,
          receiptId: 'pi-auth-receipt-1',
          provider: 'gpt',
          action: 'set_api_key',
          receiptState: 'applied',
          requiresAgentRestart: true,
        },
      },
    });
    renderProvider(transport);

    const secret = 'secret-sentinel-ui';
    await user.type(await screen.findByLabelText('API 密钥'), secret);
    await user.click(screen.getByRole('button', { name: '保存密钥' }));

    expect(await screen.findByText('API 密钥已保存')).toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    const apply = transport.requests.find(({ request }) => request.pathId === 'agent.provider.auth.apply');
    expect(apply?.request.body).toEqual({
      previewToken: 'preview-provider-key',
      confirmText: 'replace',
      apiKey: secret,
    });
    expect(screen.queryByText(secret)).not.toBeInTheDocument();
    await waitFor(() => expect(
      transport.requests.filter(({ request }) => request.pathId === 'agent.providers.get').length,
    ).toBeGreaterThanOrEqual(2));
  });

  it('saves a Jev key and explains immediate approval activation', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      capabilities: { features: { piProviderCredentials: true } },
      routes: {
        'agent.providers.get': { ...providerCatalog(), providers: [{ id: 'typesafe', name: 'TypeSafe / Jev · 工具审批', auth: { configured: false }, scenarios: [{ id: 'approval', name: '工具审批', status: 'needs_key', description: '按授权范围判断' }, { id: 'compression', name: '压缩前内容筛选', status: 'candidate', description: '摘要仍由 Pi 完成' }], models: [{ id: 'jev-latest' }] }] },
        'agent.provider.auth.preview': {
          ok: true,
          previewToken: 'preview-provider-key',
          provider: 'typesafe',
          providerName: 'GPT',
          action: 'set_api_key',
          requiredConfirm: 'replace',
          expiresAtMs: Date.now() + 60_000,
          summary: ['替换 GPT 的 API 密钥。', '现有密钥不会读取或显示。'],
          secretPolicy: '密钥仅在确认写入时送往本机 Pi。',
          sessionBoundary: '当前回复不被中断。',
        },
        'agent.provider.auth.apply': {
          ok: true,
          receiptId: 'pi-auth-receipt-1',
          provider: 'typesafe',
          action: 'set_api_key',
          receiptState: 'applied',
          requiresAgentRestart: false,
        },
      },
    });
    renderProvider(transport);

    const secret = 'secret-sentinel-ui';
    await user.type(await screen.findByLabelText('API 密钥'), secret);
    await user.click(screen.getByRole('button', { name: '保存密钥' }));

    expect(await screen.findByText('Jev 密钥已保存')).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Jev 应用场景' })).toHaveTextContent('候选场景');
    expect(screen.getByText('压缩前内容筛选')).toBeInTheDocument();
    expect(screen.queryByText('还没有可用模型')).not.toBeInTheDocument();
    expect(screen.getByText('密钥已存入 macOS 钥匙串，下一次工具审批生效，无需重启。')).toBeInTheDocument();
    expect(screen.getByLabelText('API 密钥')).toHaveValue('');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    const apply = transport.requests.find(({ request }) => request.pathId === 'agent.provider.auth.apply');
    expect(apply?.request.body).toEqual({
      previewToken: 'preview-provider-key',
      confirmText: 'replace',
      apiKey: secret,
    });
    expect(screen.queryByText(secret)).not.toBeInTheDocument();
    await waitFor(() => expect(
      transport.requests.filter(({ request }) => request.pathId === 'agent.providers.get').length,
    ).toBeGreaterThanOrEqual(2));
  });

  it('keeps confirmation for disconnecting a configured account', async () => {
    const user = userEvent.setup();
    const catalog = providerCatalog();
    catalog.providers[0].auth = {
      ...catalog.providers[0].auth,
      configured: true,
      type: 'api_key',
    };
    const transport = new MockControlTransport({
      capabilities: { features: { piProviderCredentials: true } },
      routes: {
        'agent.providers.get': catalog,
        'agent.provider.auth.preview': {
          ok: true,
          previewToken: 'preview-provider-logout',
          provider: 'gpt',
          providerName: 'GPT',
          action: 'logout',
          requiredConfirm: 'disconnect',
          expiresAtMs: Date.now() + 60_000,
          summary: ['断开 GPT。'],
        },
        'agent.provider.auth.apply': {
          ok: true,
          receiptId: 'pi-auth-logout-1',
          provider: 'gpt',
          action: 'logout',
          receiptState: 'applied',
          requiresAgentRestart: true,
        },
      },
    });
    renderProvider(transport);

    await user.click(await screen.findByRole('button', { name: '断开账号' }));
    expect(await screen.findByRole('heading', { name: '断开这个模型账号？' })).toBeInTheDocument();
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.provider.auth.apply')).toHaveLength(0);

    await user.click(screen.getByRole('button', { name: '确认断开' }));
    expect(await screen.findByText('账号已断开')).toBeInTheDocument();
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.provider.auth.apply')).toHaveLength(1);
  });

  it('uses browser callback OAuth by default and stays pending until completion', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      capabilities: { features: { piProviderCredentials: true } },
      routes: {
        'agent.providers.get': oauthProviderCatalog(),
        'agent.provider.auth.preview': {
          ok: true,
          previewToken: 'preview-oauth',
          requiredConfirm: 'connect',
          provider: 'openai-codex',
          action: 'oauth_browser',
          secretPolicy: '令牌只由 Pi 保存。',
          sessionBoundary: '登录完成后重启运行时。',
        },
        'agent.provider.auth.apply': {
          ok: true,
          receiptId: 'oauth-started',
          provider: 'openai-codex',
          action: 'oauth_browser',
          receiptState: 'login_started',
          requiresAgentRestart: false,
          login: {
            loginId: 'pi-login-1',
            state: 'waiting_for_user',
            loginMethod: 'browser',
            verificationUri: 'https://auth.openai.com/oauth/authorize?client_id=test&state=test',
          },
        },
        'agent.provider.oauth.status': {
          loginId: 'pi-login-1',
          state: 'waiting_for_user',
          loginMethod: 'browser',
          verificationUri: 'https://auth.openai.com/oauth/authorize?client_id=test&state=test',
        },
      },
    });
    renderProvider(transport);

    await user.click(await screen.findByRole('button', { name: '连接 ChatGPT' }));

    expect(await screen.findByText('登录流程已启动')).toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: /继续浏览器登录/ })).toHaveAttribute(
      'href',
      'https://auth.openai.com/oauth/authorize?client_id=test&state=test',
    );
    expect(screen.queryByText(/设备码：/)).not.toBeInTheDocument();
    expect(screen.queryByText('操作已完成。结束当前回复后重启 Agent 运行时，新凭据会统一生效。')).not.toBeInTheDocument();
  });

  it('explains how to enable ChatGPT device-code authorization', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      capabilities: { features: { piProviderCredentials: true } },
      routes: {
        'agent.providers.get': oauthProviderCatalog(),
        'agent.provider.auth.preview': {
          ok: true,
          previewToken: 'preview-oauth-disabled',
          requiredConfirm: 'connect',
          provider: 'openai-codex',
          action: 'oauth_device_code',
        },
        'agent.provider.auth.apply': {
          ok: true,
          receiptId: 'oauth-disabled',
          provider: 'openai-codex',
          action: 'oauth_device_code',
          receiptState: 'login_started',
          requiresAgentRestart: false,
          login: {
            loginId: 'pi-login-disabled',
            state: 'failed',
            error: 'Enable device code authorization for Codex in ChatGPT Security Settings, then run "codex login --device-auth" again.',
          },
        },
      },
    });
    renderProvider(transport);

    await user.click(await screen.findByRole('button', { name: '使用设备码' }));

    expect(await screen.findByText('登录失败')).toBeInTheDocument();
    expect(screen.getByText('请先在 ChatGPT「设置 → 安全」中开启设备码授权，然后重新连接。'))
      .toBeInTheDocument();
    expect(screen.queryByText(/codex login --device-auth/)).not.toBeInTheDocument();
  });
  it('keeps a configured custom x1top provider and its governed model visible without exposing credentials', async () => {
    const catalog = providerCatalog();
    const transport = new MockControlTransport({
      capabilities: { features: { piProviderCredentials: true } },
      routes: {
        'agent.providers.get': {
          ...catalog,
          providers: [{
            ...catalog.providers[0],
            id: 'x1top',
            name: 'x1top',
            auth: { ...catalog.providers[0].auth, configured: true, type: 'api_key' },
            availableModelCount: 1,
            availableModels: [{
              id: 'x1top-luna',
              name: 'Luna Max',
              reasoning: true,
              imageInput: true,
            }],
          }],
        },
      },
    });

    renderProvider(transport);

    expect(await screen.findByRole('combobox', { name: '模型服务' })).toHaveTextContent('x1top');
    expect(screen.getByText('Luna Max')).toBeInTheDocument();
    expect(screen.getByLabelText('API 密钥')).toHaveValue('');
    expect(screen.queryByText(/secret|token/i)).not.toBeInTheDocument();
  });

  it('keeps the model catalog in an explicit, keyboard-expandable local window', async () => {
    const user = userEvent.setup();
    const baseCatalog = providerCatalog();
    const catalog = {
      ...baseCatalog,
      providers: [{
        ...baseCatalog.providers[0],
        availableModelCount: 10,
        availableModels: Array.from({ length: 10 }, (_, index) => ({
          id: `model-${index + 1}`,
          name: `模型 ${index + 1}`,
          reasoning: index % 2 === 0,
        })),
      }],
    };
    const transport = new MockControlTransport({
      capabilities: { features: { piProviderCredentials: true } },
      routes: { 'agent.providers.get': catalog },
    });

    renderProvider(transport);

    expect(await screen.findByText('当前 8 / 共 10 个可用模型')).toBeInTheDocument();
    expect(screen.queryByText('模型 9')).not.toBeInTheDocument();
    const disclosure = screen.getByRole('button', { name: '显示全部 10 个模型' });
    expect(disclosure).toHaveAttribute('aria-expanded', 'false');
    disclosure.focus();
    await user.keyboard('{Enter}');
    expect(disclosure).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('已显示 10 / 共 10 个可用模型')).toBeInTheDocument();
    expect(screen.getByText('模型 9')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '收起到最近 8 个模型' }));
    expect(screen.getByText('当前 8 / 共 10 个可用模型')).toBeInTheDocument();
    expect(screen.queryByText('模型 9')).not.toBeInTheDocument();
  });

  it('offers real recovery actions when account management is unavailable', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      capabilities: {
        features: { piProviderCredentials: false },
        routeIds: [],
      },
    });
    const capabilities = vi.spyOn(transport, 'capabilities');

    renderProvider(transport);

    expect(await screen.findByText('模型账号管理仍不可用')).toBeInTheDocument();
    expect(screen.getByText(/已保存的模型连接不会因此丢失/)).toBeInTheDocument();
    expect(screen.queryByText(/更新应用/)).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '重新检查' }));
    await waitFor(() => expect(capabilities.mock.calls.length).toBeGreaterThanOrEqual(2));

    await user.click(screen.getByRole('button', { name: '打开问题排查' }));
    expect(screen.getByTestId('provider-route')).toHaveTextContent('/diagnostics');
  });

});

function renderProvider(transport: MockControlTransport): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <TooltipProvider delayDuration={0}>
      <ControlTransportProvider transport={transport}>
        <QueryClientProvider client={client}>
          <MemoryRouter initialEntries={['/configuration']}>
            <PiProviderCredentials />
            <RouteProbe />
          </MemoryRouter>
        </QueryClientProvider>
      </ControlTransportProvider>
    </TooltipProvider>,
  );
}

function RouteProbe() {
  const location = useLocation();
  return <output data-testid="provider-route">{location.pathname}</output>;
}

function providerCatalog() {
  return {
    schemaVersion: 'rag-ime.pi-provider-catalog.v1',
    ok: true,
    available: true,
    providers: [{
      id: 'gpt',
      name: 'GPT',
      auth: {
        configured: false,
        type: '',
        source: '',
        sourceLabel: '',
        oauthSupported: false,
        oauthBrowserSupported: false,
        oauthDeviceCodeSupported: false,
      },
      configuredInCatalog: true,
      modelCount: 3,
      availableModelCount: 0,
      availableModels: [],
      modelsTruncated: false,
    }],
  };
}

function oauthProviderCatalog() {
  const catalog = providerCatalog();
  return {
    ...catalog,
    providers: [{
      ...catalog.providers[0],
      id: 'openai-codex',
      name: 'ChatGPT Plus/Pro',
      auth: {
        ...catalog.providers[0].auth,
        oauthSupported: true,
        oauthBrowserSupported: true,
        oauthDeviceCodeSupported: true,
      },
    }],
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}
