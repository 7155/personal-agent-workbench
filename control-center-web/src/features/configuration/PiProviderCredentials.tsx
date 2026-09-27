import { ExternalLink, KeyRound, LogOut, RefreshCw, Unplug, UserRoundCheck } from 'lucide-react';
import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Field,
  Input,
  Select,
} from '@/components/primitives';
import { InlineNotice, ManagementSection, StatusBadge, arrayRecords, asRecord, stringValue } from '@/features/overview/management-ui';
import { usePiProviderCatalog } from './api';

type ProviderAction = 'set_api_key' | 'logout' | 'oauth_browser' | 'oauth_device_code';

export function PiProviderCredentials({ onlyProvider }: { onlyProvider?: string } = {}) {
  return onlyProvider ? <ProviderCredentials onlyProvider={onlyProvider} /> : <RoutedProviderCredentials />;
}

function RoutedProviderCredentials() {
  const navigate = useNavigate();
  return <ProviderCredentials navigate={navigate} />;
}

function ProviderCredentials({ onlyProvider, navigate }: { onlyProvider?: string; navigate?: (path: string) => void }) {
  const {
    authChangesSupported,
    capabilities,
    catalog,
    oauthCancelSupported,
    oauthStatusSupported,
    supported,
    transport,
  } = usePiProviderCatalog();
  const envelope = asRecord(catalog.data);
  const providers = useMemo(() => arrayRecords(envelope.providers).filter(item => !onlyProvider || stringValue(item.id) === onlyProvider).sort(providerOrder), [envelope.providers, onlyProvider]);
  const [providerId, setProviderId] = useState(onlyProvider ?? '');
  const [apiKey, setApiKey] = useState('');
  const [preview, setPreview] = useState<Record<string, unknown> | null>(null);
  const [previewAction, setPreviewAction] = useState<ProviderAction>('set_api_key');
  const [working, setWorking] = useState(false);
  const [error, setError] = useState('');
  const [receipt, setReceipt] = useState<Record<string, unknown> | null>(null);
  const [login, setLogin] = useState<Record<string, unknown> | null>(null);
  const [loginStatusError, setLoginStatusError] = useState('');
  const [statusChecking, setStatusChecking] = useState(false);
  const [modelsExpanded, setModelsExpanded] = useState(false);
  const loginEpochRef = useRef(0);
  const modelListId = useId();
  const loginId = stringValue(login?.loginId);
  const loginState = stringValue(login?.state);
  const loginWaiting = isPendingLoginState(loginState);
  const refetchCatalog = catalog.refetch;

  useEffect(() => {
    if (!providers.length || providers.some((item) => stringValue(item.id) === providerId)) return;
    loginEpochRef.current += 1;
    setProviderId(stringValue(providers.find((item) => asRecord(item.auth).configured === true)?.id, stringValue(providers[0]?.id)));
    setApiKey('');
    setPreview(null);
    setReceipt(null);
    setLogin(null);
    setError('');
    setLoginStatusError('');
  }, [providerId, providers]);

  useEffect(() => {
    if (!oauthStatusSupported || !loginId || !loginWaiting || loginStatusError) return;
    let active = true;
    let inFlight = false;
    const epoch = loginEpochRef.current;
    const timer = window.setInterval(() => {
      if (inFlight) return;
      inFlight = true;
      void transport.request({ pathId: 'agent.provider.oauth.status', query: { loginId } })
        .then((value) => {
          if (!active || epoch !== loginEpochRef.current) return;
          const next = parseOAuthStatus(value, loginId);
          setLogin(next);
          setLoginStatusError('');
          if (stringValue(next.state) === 'completed') void refetchCatalog();
        })
        .catch((statusError) => {
          if (active && epoch === loginEpochRef.current) setLoginStatusError(errorText(statusError));
        })
        .finally(() => { inFlight = false; });
    }, 1_500);
    return () => { active = false; window.clearInterval(timer); };
  }, [loginId, loginStatusError, loginWaiting, oauthStatusSupported, refetchCatalog, transport]);

  const selected = providers.find((item) => stringValue(item.id) === providerId) ?? providers[0] ?? {};
  const auth = asRecord(selected.auth);
  const models = arrayRecords(selected.availableModels);
  const modelPreviewLimit = 8;
  const declaredModelCount = finiteNonNegativeNumber(selected.availableModelCount);
  const modelTotal = Math.max(models.length, declaredModelCount ?? 0);
  const loadedModels = modelsExpanded ? models : models.slice(0, modelPreviewLimit);
  const hasAdditionalLoadedModels = models.length > modelPreviewLimit;
  const modelCatalogTruncated = selected.modelsTruncated === true || modelTotal > models.length;
  const canUseBrowserOAuth = auth.oauthBrowserSupported === true
    && authChangesSupported
    && oauthStatusSupported;
  const canUseDeviceOAuth = auth.oauthDeviceCodeSupported === true
    && authChangesSupported
    && oauthStatusSupported;
  const refresh = () => void refetchCatalog();
  const receiptNotice = providerReceiptNotice(receipt, loginState);

  function selectProvider(nextProviderId: string): void {
    if (nextProviderId === providerId) return;
    loginEpochRef.current += 1;
    setProviderId(nextProviderId);
    setApiKey('');
    setPreview(null);
    setReceipt(null);
    setLogin(null);
    setError('');
    setLoginStatusError('');
    setModelsExpanded(false);
  }

  async function openPreview(action: ProviderAction): Promise<void> {
    if (!providerId || !authChangesSupported || loginWaiting) return;
    setWorking(true);
    setError('');
    try {
      const value = await transport.request({
        pathId: 'agent.provider.auth.preview',
        body: { provider: providerId, action },
      });
      const nextPreview = parseProviderPreview(value, providerId, action);
      setPreviewAction(action);
      if (action !== 'logout') {
        await applyPreview(nextPreview, action);
      } else {
        setPreview(nextPreview);
      }
    } catch (nextError) {
      setError(errorText(nextError, apiKey));
    } finally {
      setWorking(false);
    }
  }

  async function applyPreview(
    pendingPreview: Record<string, unknown> | null = preview,
    action: ProviderAction = previewAction,
  ): Promise<void> {
    if (!pendingPreview) return;
    setWorking(true);
    setError('');
    try {
      const value = parseProviderApplyResult(await transport.request({
        pathId: 'agent.provider.auth.apply',
        body: {
          previewToken: stringValue(pendingPreview.previewToken),
          confirmText: stringValue(pendingPreview.requiredConfirm),
          ...(action === 'set_api_key' ? { apiKey: apiKey.trim() } : {}),
        },
      }), providerId, action);
      setReceipt(value);
      setPreview(null);
      const nextLogin = asRecord(value.login);
      loginEpochRef.current += 1;
      setLogin(stringValue(nextLogin.loginId) ? nextLogin : null);
      setLoginStatusError('');
      void refetchCatalog();
    } catch (nextError) {
      setPreview(null);
      setError(errorText(nextError, apiKey));
    } finally {
      if (action === 'set_api_key') setApiKey('');
      setWorking(false);
    }
  }

  async function cancelLogin(): Promise<void> {
    const loginId = stringValue(login?.loginId);
    if (!loginId || !oauthCancelSupported) return;
    loginEpochRef.current += 1;
    setWorking(true);
    setError('');
    setLoginStatusError('');
    try {
      setLogin(parseOAuthStatus(await transport.request({
        pathId: 'agent.provider.oauth.cancel',
        body: { loginId },
      }), loginId));
    } catch (nextError) {
      setLoginStatusError(errorText(nextError));
    } finally {
      setWorking(false);
    }
  }

  async function refreshLoginStatus(): Promise<void> {
    const currentLoginId = stringValue(login?.loginId);
    if (!currentLoginId || !oauthStatusSupported) return;
    const epoch = loginEpochRef.current;
    setStatusChecking(true);
    setLoginStatusError('');
    try {
      const next = parseOAuthStatus(await transport.request({
        pathId: 'agent.provider.oauth.status',
        query: { loginId: currentLoginId },
      }), currentLoginId);
      if (epoch !== loginEpochRef.current) return;
      setLogin(next);
      if (stringValue(next.state) === 'completed') void refetchCatalog();
    } catch (nextError) {
      if (epoch === loginEpochRef.current) setLoginStatusError(errorText(nextError));
    } finally {
      if (epoch === loginEpochRef.current) setStatusChecking(false);
    }
  }

  async function retryLogin(): Promise<void> {
    const action = stringValue(login?.loginMethod) === 'device_code'
      ? 'oauth_device_code'
      : 'oauth_browser';
    loginEpochRef.current += 1;
    setLogin(null);
    setReceipt(null);
    setLoginStatusError('');
    setError('');
    await openPreview(action);
  }

  if (capabilities.isPending) {
    return <ManagementSection title="模型账号"><InlineNotice title="正在检查模型账号">正在确认这台 Mac 是否可以连接和管理远程模型账号。</InlineNotice></ManagementSection>;
  }
  if (capabilities.error) {
    return <ManagementSection title="模型账号"><InlineNotice title="模型账号检查失败" tone="danger">
      没有读到这台 Mac 的账号管理能力。请重新检查；如果仍然失败，可打开问题排查查看本机服务状态。
      <div className="mgmt-toolbar configuration-provider-recovery-actions">
        <Button leadingIcon={<RefreshCw size={15} />} loading={capabilities.isFetching} onClick={() => void capabilities.refetch()} size="small">重新检查</Button>
        {navigate ? <Button onClick={() => navigate('/diagnostics')} size="small" variant="quiet">打开问题排查</Button> : null}
      </div>
    </InlineNotice></ManagementSection>;
  }
  if (!supported) {
    return <ManagementSection title="模型账号"><InlineNotice title="模型账号管理仍不可用" tone="warning">
      当前本机服务未提供模型账号管理。已保存的模型连接不会因此丢失；重新检查后仍不可用时，可前往问题排查。
      <div className="mgmt-toolbar configuration-provider-recovery-actions">
        <Button leadingIcon={<RefreshCw size={15} />} loading={capabilities.isFetching} onClick={() => void capabilities.refetch()} size="small">重新检查</Button>
        {navigate ? <Button onClick={() => navigate('/diagnostics')} size="small" variant="quiet">打开问题排查</Button> : null}
      </div>
    </InlineNotice></ManagementSection>;
  }
  if (catalog.isPending) {
    return <ManagementSection title="模型账号"><InlineNotice title="正在读取">正在读取模型服务和可用模型。</InlineNotice></ManagementSection>;
  }
  if (catalog.error || envelope.ok === false) {
    return <ManagementSection title="模型账号"><InlineNotice title="读取失败" tone="danger">
      {errorText(catalog.error ?? envelope.error)}
      <div className="mgmt-toolbar configuration-provider-recovery-actions">
        <Button leadingIcon={<RefreshCw size={15} />} loading={catalog.isFetching} onClick={() => void catalog.refetch()} size="small">重新读取</Button>
        {navigate ? <Button onClick={() => navigate('/diagnostics')} size="small" variant="quiet">打开问题排查</Button> : null}
      </div>
    </InlineNotice></ManagementSection>;
  }
  if (envelope.available === false) {
    return <ManagementSection title="模型账号"><InlineNotice title="当前不可用" tone="warning">
      {catalogUnavailableText(envelope.unavailableReason)}
      <div className="mgmt-toolbar configuration-provider-recovery-actions">
        <Button leadingIcon={<RefreshCw size={15} />} loading={catalog.isFetching} onClick={() => void catalog.refetch()} size="small">重新检查</Button>
        {navigate ? <Button onClick={() => navigate('/diagnostics')} size="small" variant="quiet">打开问题排查</Button> : null}
      </div>
    </InlineNotice></ManagementSection>;
  }
  if (!providers.length) {
    return <ManagementSection title="模型账号"><InlineNotice title="还没有模型服务" tone="warning">
      请先在上方运行设置中配置模型服务，再重新读取账号列表。
      <div className="mgmt-toolbar configuration-provider-recovery-actions">
        <Button leadingIcon={<RefreshCw size={15} />} loading={catalog.isFetching} onClick={() => void catalog.refetch()} size="small">重新读取</Button>
      </div>
    </InlineNotice></ManagementSection>;
  }

  return (
    <ManagementSection title="模型账号" description="管理本机使用的模型服务。密钥和登录令牌不会显示在页面中。">
      <div className="mgmt-grid-2">
        <div className="mgmt-stack">
          <Field htmlFor="pi-provider" label="模型服务">
            <Select
              disabled={working || loginWaiting}
              id="pi-provider"
              onValueChange={selectProvider}
              options={providers.map((item) => ({ value: stringValue(item.id), label: providerDisplayName(item) }))}
              value={providerId}
            />
          </Field>
          <div className="mgmt-toolbar">
            <StatusBadge label={providerId === 'typesafe' ? (auth.configured === true ? '已配置' : '未配置') : (auth.configured === true ? '已连接' : '未连接')} tone={auth.configured === true ? 'success' : 'neutral'} />
            {stringValue(auth.type) ? <StatusBadge label={stringValue(auth.type) === 'oauth' ? 'ChatGPT 登录' : 'API 密钥'} tone="info" /> : null}
          </div>
          {providerId === 'typesafe' ? <InlineNotice title="Jev 工具审批" tone="info">配置密钥后，需要模型判断的工具审批优先使用 Jev；服务失败回退 Luna Max。当前权限模式仍决定是否需要审批。密钥保存在 macOS 钥匙串中，全局生效。</InlineNotice> : null}
          <Field description="输入内容只会在保存时交给本机安全存储；页面不会读回现有密钥。" htmlFor="pi-api-key" label="API 密钥">
            <Input autoComplete="new-password" disabled={!authChangesSupported || loginWaiting} id="pi-api-key" onChange={(event) => setApiKey(event.target.value)} placeholder={authChangesSupported ? '输入新的 API 密钥' : '当前版本仅支持查看状态'} type="password" value={apiKey} />
          </Field>
          <div className="mgmt-toolbar">
            <Button disabled={!authChangesSupported || !apiKey.trim() || loginWaiting} leadingIcon={<KeyRound size={15} />} loading={working} onClick={() => void openPreview('set_api_key')} size="small" variant="primary">{auth.configured === true ? '替换密钥' : '保存密钥'}</Button>
            {canUseBrowserOAuth ? <Button disabled={loginWaiting} leadingIcon={<UserRoundCheck size={15} />} loading={working} onClick={() => void openPreview('oauth_browser')} size="small">{auth.configured === true && stringValue(auth.type) === 'oauth' ? '重新连接 ChatGPT' : '连接 ChatGPT'}</Button> : null}
            {canUseDeviceOAuth ? <Button disabled={loginWaiting} loading={working} onClick={() => void openPreview('oauth_device_code')} size="small" variant="quiet">使用设备码</Button> : null}
            {auth.configured === true ? <Button disabled={!authChangesSupported || loginWaiting} leadingIcon={<LogOut size={15} />} loading={working} onClick={() => void openPreview('logout')} size="small" variant="quiet">断开账号</Button> : null}
            <Button leadingIcon={<RefreshCw size={15} />} loading={catalog.isFetching} onClick={refresh} size="small" variant="quiet">刷新</Button>
          </div>
          {!authChangesSupported ? <InlineNotice title="当前仅能查看" tone="warning">安全保存与退出功能尚未接入，所以不会发送凭据。</InlineNotice> : null}
        </div>
        {providerId === 'typesafe' ? (
          <section aria-label="Jev 应用场景" className="mgmt-stack configuration-jev-scenarios">
            <div><strong>Jev 在 OS 中做什么</strong><p className="mgmt-muted">结构化判断与排序。配置密钥不会自动开启所有场景。</p></div>
            <div className="mgmt-list">
              {arrayRecords(selected.scenarios).map((scenario) => (
                <div className="configuration-jev-scenario" key={stringValue(scenario.id)}>
                  <div className="mgmt-toolbar"><strong>{stringValue(scenario.name)}</strong><StatusBadge
                    label={scenario.status === 'configured' ? '已配置' : scenario.status === 'needs_key' ? '待配置密钥' : scenario.status === 'available' ? '可接入' : '尚未接通'}
                    tone={scenario.status === 'configured' ? 'success' : 'neutral'} /></div>
                  <p className="mgmt-muted">{stringValue(scenario.description)}</p>
                </div>
              ))}
            </div>
            <span className="mgmt-muted">已配置表示本机有密钥。每次实际调用与回退，以审批回执和知识库召回诊断为准。</span>
          </section>
        ) : (
        <div className="mgmt-stack">
          <div className="mgmt-toolbar">
            <strong>{providerDisplayName(selected)}</strong>
            <span className="mgmt-muted">
              {models.length > modelPreviewLimit
                ? `${modelsExpanded ? '已显示' : '当前'} ${loadedModels.length} / ${modelCatalogTruncated ? '已加载 ' : '共 '}${models.length}${modelCatalogTruncated ? ` · 共 ${modelTotal}` : ''} 个可用模型`
                : `${modelCatalogTruncated ? `已加载 ${models.length} · 共 ${modelTotal}` : modelTotal} 个可用模型`}
            </span>
          </div>
          {models.length ? (
            <div aria-live="polite" className="mgmt-list configuration-provider-models" data-expanded={modelsExpanded || undefined} id={modelListId}>
              {loadedModels.map((model) => (
                <div className="mgmt-list__row" key={stringValue(model.id)}>
                  <span>{modelDisplayName(model)}</span>
                  {model.reasoning === true ? <StatusBadge label="推理" tone="info" /> : null}
                  {model.imageInput === true ? <StatusBadge label="图片" tone="neutral" /> : null}
                </div>
              ))}
            </div>
          ) : <InlineNotice title="还没有可用模型">连接这个模型服务后刷新，即可看到实际可用的模型。</InlineNotice>}
          {hasAdditionalLoadedModels ? (
            <button
              aria-controls={modelListId}
              aria-expanded={modelsExpanded}
              className="configuration-provider-models__toggle"
              onClick={() => setModelsExpanded((current) => !current)}
              type="button"
            >
              {modelsExpanded ? `收起到最近 ${modelPreviewLimit} 个模型` : `显示全部 ${models.length} 个模型`}
            </button>
          ) : null}
          {modelCatalogTruncated ? <span className="mgmt-muted">当前目录已加载 {models.length} 项；服务报告共 {modelTotal} 项，刷新后可检查是否有新增可用模型。</span> : null}
        </div>
        )}
      </div>
      {error ? <InlineNotice title="操作没有完成" tone="danger">{error}</InlineNotice> : null}
      {receiptNotice ? (
        <InlineNotice title={receiptNotice.title} tone={receiptNotice.tone}>
          {receiptNotice.body}
        </InlineNotice>
      ) : null}
      {login ? (
        <OAuthStatus
          canCancel={oauthCancelSupported}
          canRefresh={oauthStatusSupported}
          error={loginStatusError}
          login={login}
          onCancel={cancelLogin}
          onRefresh={refreshLoginStatus}
          onRetry={retryLogin}
          working={working || statusChecking}
        />
      ) : null}

      <Dialog open={preview !== null} onOpenChange={(open) => { if (!open && !working) setPreview(null); }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{previewTitle(previewAction)}</DialogTitle>
            <DialogDescription>{previewDescription(previewAction)}</DialogDescription>
          </DialogHeader>
          <div className="mgmt-stack">
            {previewLines(previewAction).map((item) => <p className="mgmt-muted" key={item}>{item}</p>)}
            <InlineNotice title="生效时机">为了不打断当前对话，新设置会在当前回复结束后统一生效。</InlineNotice>
          </div>
          <DialogFooter>
            <Button disabled={working} onClick={() => setPreview(null)} variant="quiet">取消</Button>
            <Button disabled={previewAction === 'set_api_key' && !apiKey.trim()} loading={working} onClick={() => void applyPreview()} variant={previewAction === 'logout' ? 'danger' : 'primary'}>{previewAction === 'logout' ? '确认断开' : '确认继续'}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </ManagementSection>
  );
}

function finiteNonNegativeNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}

function OAuthStatus({
  canCancel,
  canRefresh,
  error,
  login,
  onCancel,
  onRefresh,
  onRetry,
  working,
}: {
  canCancel: boolean;
  canRefresh: boolean;
  error: string;
  login: Record<string, unknown>;
  onCancel: () => Promise<void>;
  onRefresh: () => Promise<void>;
  onRetry: () => Promise<void>;
  working: boolean;
}) {
  const state = stringValue(login.state);
  const waiting = isPendingLoginState(state);
  const canRetry = state === 'failed' || state === 'cancelled';
  return (
    <InlineNotice title={state === 'completed' ? 'ChatGPT 已连接' : state === 'failed' ? '登录失败' : state === 'cancelled' ? '登录已取消' : '等待完成 ChatGPT 登录'} tone={state === 'completed' ? 'success' : state === 'failed' ? 'danger' : state === 'cancelled' ? 'warning' : 'info'}>
      <div className="mgmt-stack">
        {stringValue(login.userCode) ? <strong>设备码：{stringValue(login.userCode)}</strong> : null}
        {safeLoginUrl(login.verificationUri) ? <a href={safeLoginUrl(login.verificationUri)} rel="noreferrer" target="_blank">{stringValue(login.userCode) ? '输入设备码' : '继续浏览器登录'} <ExternalLink aria-hidden="true" size={14} /></a> : null}
        {stringValue(login.error) ? <span>{errorText(login.error)}</span> : null}
        {error ? <span role="alert">{error}</span> : null}
        <div className="mgmt-toolbar">
          {waiting && canRefresh ? <Button leadingIcon={<RefreshCw size={15} />} loading={working} onClick={() => void onRefresh()} size="small" variant="quiet">检查状态</Button> : null}
          {waiting && canCancel ? <Button leadingIcon={<Unplug size={15} />} loading={working} onClick={() => void onCancel()} size="small" variant="quiet">取消登录</Button> : null}
          {canRetry ? <Button leadingIcon={<UserRoundCheck size={15} />} loading={working} onClick={() => void onRetry()} size="small">重新登录</Button> : null}
        </div>
      </div>
    </InlineNotice>
  );
}

function providerReceiptNotice(
  receipt: Record<string, unknown> | null,
  loginState: string,
): { title: string; body: string; tone: 'success' | 'info' } | null {
  if (!receipt) return null;
  const action = stringValue(receipt.action);
  if (stringValue(receipt.provider) === 'typesafe') {
    return {
      title: action === 'logout' ? 'Jev 密钥已移除' : 'Jev 密钥已保存',
      body: action === 'logout' ? '下一次工具审批将回退 Luna Max。' : '密钥已存入 macOS 钥匙串，下一次工具审批生效，无需重启。',
      tone: 'success',
    };
  }
  if (isOAuthAction(action)) {
    if (!isPendingLoginState(loginState)) return null;
    return {
      title: '登录流程已启动',
      body: '在浏览器完成登录后，再重新打开对话。当前回复不会被打断。',
      tone: 'info',
    };
  }
  if (action === 'logout') {
    return {
      title: '账号已断开',
      body: '本机已移除这个账号的授权。已有对话和伙伴不会被删除。',
      tone: 'success',
    };
  }
  return {
    title: 'API 密钥已保存',
    body: '密钥没有回显。结束当前回复后重新打开对话，新凭据会统一生效。',
    tone: 'success',
  };
}

function isPendingLoginState(state: string): boolean {
  return state === 'starting' || state === 'waiting_for_user';
}

function providerOrder(left: Record<string, unknown>, right: Record<string, unknown>): number {
  const score = (item: Record<string, unknown>) => Number(asRecord(item.auth).configured === true) * 4 + Number(item.configuredInCatalog === true) * 2 + Number(asRecord(item.auth).oauthSupported === true);
  return score(right) - score(left) || stringValue(left.name).localeCompare(stringValue(right.name));
}

function previewTitle(action: ProviderAction): string {
  if (action === 'logout') return '断开这个模型账号？';
  if (action === 'oauth_browser') return '连接 ChatGPT？';
  if (action === 'oauth_device_code') return '使用设备码连接 ChatGPT？';
  return '替换 API 密钥？';
}

function previewDescription(action: ProviderAction): string {
  if (action === 'logout') return '确认后会移除本机保存的账号授权，不会删除对话。';
  if (action === 'oauth_browser') return '确认后会打开浏览器，并通过本机回调完成登录；网页不会读取你的密码。';
  if (action === 'oauth_device_code') return '仅在本机回调不可用时使用；ChatGPT 必须已开启设备码授权。';
  return '新密钥只会交给本机安全保存，不会在确认页中回显。';
}

function previewLines(action: ProviderAction): string[] {
  if (action === 'logout') return ['这个模型服务的本机授权将被移除。', '已有对话和角色保持不变。'];
  if (action === 'oauth_browser') return ['默认使用 Pi 的浏览器 OAuth 与 localhost 回调。', '未完成授权前，登录状态不会改变。'];
  if (action === 'oauth_device_code') return ['这是无界面或本机回调不可用时的备用方式。', '设备码不会替代或改变你的 ChatGPT 套餐。'];
  return ['现有密钥不会被读取或显示。', '仅在确认后替换这个模型服务的密钥。'];
}

function parseProviderPreview(value: unknown, providerId: string, action: ProviderAction): Record<string, unknown> {
  const payload = asRecord(value);
  if (
    payload.ok !== true
    || !stringValue(payload.previewToken)
    || !stringValue(payload.requiredConfirm)
    || stringValue(payload.provider) !== providerId
    || stringValue(payload.action) !== action
  ) throw new Error('无法验证这次操作，请刷新后重试。');
  return payload;
}

function parseProviderApplyResult(value: unknown, providerId: string, action: ProviderAction): Record<string, unknown> {
  const payload = asRecord(value);
  const receiptState = stringValue(payload.receiptState);
  if (
    payload.ok !== true
    || !stringValue(payload.receiptId)
    || stringValue(payload.provider) !== providerId
    || stringValue(payload.action) !== action
    || (isOAuthAction(action) ? receiptState !== 'login_started' : receiptState !== 'applied')
  ) throw new Error('模型账号设置没有完成，请刷新后重试。');
  if (isOAuthAction(action)) {
    const login = asRecord(payload.login);
    if (!stringValue(login.loginId) || !isValidLoginState(stringValue(login.state))) {
      throw new Error('登录流程没有正确启动，请重新连接。');
    }
  }
  return payload;
}

function parseOAuthStatus(value: unknown, loginId: string): Record<string, unknown> {
  const payload = asRecord(value);
  if (
    stringValue(payload.loginId) !== loginId
    || !isValidLoginState(stringValue(payload.state))
  ) throw new Error('登录状态无法验证，请重新发起登录。');
  return payload;
}

function isValidLoginState(state: string): boolean {
  return ['starting', 'waiting_for_user', 'completed', 'failed', 'cancelled'].includes(state);
}

function providerDisplayName(provider: Record<string, unknown>): string {
  return stringValue(provider.name).trim() || '未命名模型服务';
}

function modelDisplayName(model: Record<string, unknown>): string {
  return stringValue(model.name).trim() || '未命名模型';
}

function catalogUnavailableText(value: unknown): string {
  const message = stringValue(value);
  return /path|runtime|provider|schema|token|credential|component/i.test(message)
    ? '当前版本还不能在网页中管理模型账号。'
    : message || '当前版本还不能在网页中管理模型账号。';
}

function safeLoginUrl(value: unknown): string {
  const raw = stringValue(value).trim();
  if (!raw) return '';
  try {
    const url = new URL(raw);
    return url.protocol === 'https:'
      && url.hostname === 'auth.openai.com'
      && ['/oauth/authorize', '/codex/device'].includes(url.pathname)
      && !url.username
      && !url.password
      && (!url.port || url.port === '443')
      ? url.toString()
      : '';
  } catch {
    return '';
  }
}

function errorText(error: unknown, secret = ''): string {
  const message = error instanceof Error ? error.message : typeof error === 'string' ? error : '';
  if (/enable device code authorization for codex in chatgpt security settings/i.test(message)) {
    return '请先在 ChatGPT「设置 → 安全」中开启设备码授权，然后重新连接。';
  }
  if (/browser callback port 1455 is unavailable/i.test(message)) {
    return '本机 OAuth 回调端口 1455 正在被占用；关闭占用程序后重试，或改用设备码。';
  }
  if (
    (secret && message.includes(secret))
    || /(?:sk|key|token|bearer)[-_A-Za-z0-9.]{8,}/i.test(message)
    || /(?:api.?key|authorization|token|secret|cookie)\s*[:=：]\s*\S+/i.test(message)
  ) return '操作没有完成，请检查输入后重试。';
  if (/pathId|receipt|sha|hash|schema|runtimeRevision|previewToken|providerId|loginId|work.?contract/i.test(message)) {
    return '操作没有完成，请刷新后重试。';
  }
  if (message && /[\u3400-\u9fff]/.test(message)) return message;
  return '操作失败，请刷新后重试。';
}

function isOAuthAction(action: string): action is 'oauth_browser' | 'oauth_device_code' {
  return action === 'oauth_browser' || action === 'oauth_device_code';
}
