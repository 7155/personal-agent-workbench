// PAW owns account selection; Pi owns the SIWC protocol and provider execution.
import { createHash, randomUUID } from 'node:crypto';
import { lstat, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

export const CHATGPT_PROVIDER = 'openai-chatgpt';

export function accountKey(registration) {
  return createHash('sha256').update(JSON.stringify([registration.issuer, registration.clientId, registration.subject])).digest('hex');
}

function withoutSavedAccounts(value) {
  const { savedAccounts: _savedAccounts, ...registration } = value;
  return registration;
}

function registrations(current, siwc) {
  const accounts = Array.isArray(current?.savedAccounts)
    ? current.savedAccounts.filter(siwc.isChatGPTRegistration).map(withoutSavedAccounts) : [];
  if (siwc.isChatGPTRegistration(current)) accounts.unshift(withoutSavedAccounts(current));
  return [...new Map(accounts.map(item => [accountKey(item), item])).values()];
}

export async function chatGPTAccountStatus(auth, siwc) {
  const current = await auth.read(CHATGPT_PROVIDER);
  const accounts = registrations(current, siwc);
  const activeKey = siwc.isChatGPTRegistration(current) ? accountKey(current) : '';
  return {
    configured: siwc.isChatGPTRegistration(current) && current.sessionState === 'active',
    planEnabled: siwc.chatGPTPlanEnabled(current),
    sessionState: siwc.isChatGPTRegistration(current) ? current.sessionState : 'signed_out',
    activeAccount: activeKey,
    accounts: accounts.map(item => ({
      id: accountKey(item),
      label: `${item.email || 'ChatGPT account'} · ${accountKey(item).slice(0, 8)}`,
      active: accountKey(item) === activeKey && item.sessionState === 'active',
      sessionState: item.sessionState,
    })),
  };
}

export async function loginChatGPT({ auth, runtime, siwc, agentDir, accountId, requestConsent, emit, signal }) {
  const before = await auth.read(CHATGPT_PROVIDER, { signal });
  const accounts = registrations(before, siwc);
  const selection = accountId || (siwc.isChatGPTRegistration(before) ? accountKey(before) : 'new');
  const registration = selection === 'new' ? undefined : accounts.find(item => accountKey(item) === selection);
  if (selection !== 'new' && !registration) throw new Error('ChatGPT account registration was not found');
  const hostId = await siwc.getOrCreateChatGPTHostId(join(agentDir, 'chatgpt-host-id'));
  const credential = await siwc.createOpenAIChatGPTOAuth({
    agentName: 'Personal Agent Workbench', hostId, registration, requestConsent: requestConsent === true,
  }).login({ signal, prompt: async () => { throw new Error('Interactive input is unavailable'); }, notify: event => emit({ ...event, event: event.type }) });
  signal.throwIfAborted();
  await auth.modify(CHATGPT_PROVIDER, async current => {
    signal.throwIfAborted();
    // A cancelled/disconnected or switched account must not be restored by a late callback.
    if (accountVersion(current, siwc) !== accountVersion(before, siwc)) throw new Error('ChatGPT account changed during sign-in; try again');
    return {
      ...credential,
      accountEpoch: randomUUID(),
      savedAccounts: registrations(current, siwc).filter(item => accountKey(item) !== accountKey(credential)),
    };
  }, { signal });
  let catalogWarning = '';
  if (siwc.chatGPTPlanEnabled(credential)) {
    const refreshed = await runtime.refresh({ providers: [CHATGPT_PROVIDER], allowNetwork: true, force: true, signal });
    if (refreshed.aborted || refreshed.errors.has(CHATGPT_PROVIDER)) catalogWarning = 'ChatGPT 登录已保存，但模型目录暂时不可用；请刷新后再选模型。';
  }
  emit({ event: 'completed', ok: true, provider: CHATGPT_PROVIDER,
    planEnabled: siwc.chatGPTPlanEnabled(credential), firstSignIn: !registration, catalogWarning });
}

function accountVersion(value, siwc) {
  return siwc.isChatGPTRegistration(value) ? `${accountKey(value)}:${value.sessionState}:${value.accountEpoch || ''}` : '';
}

export async function logoutChatGPT({ auth, siwc, accountId, signal }) {
  let remoteRevocationConfirmed = true;
  await auth.modify(CHATGPT_PROVIDER, async current => {
    const accounts = registrations(current, siwc);
    const selection = accountId || (siwc.isChatGPTRegistration(current) ? accountKey(current) : '');
    const selected = accounts.find(item => accountKey(item) === selection);
    if (!selected) throw new Error('ChatGPT account registration was not found');
    remoteRevocationConfirmed = await siwc.revokeChatGPTSession(selected, signal);
    const cleared = siwc.clearChatGPTSession(selected);
    if (siwc.isChatGPTRegistration(current) && accountKey(current) === selection) {
      return { ...cleared, accountEpoch: randomUUID(), savedAccounts: accounts.filter(item => accountKey(item) !== selection) };
    }
    return { ...current, accountEpoch: randomUUID(), savedAccounts: accounts.filter(item => accountKey(item) !== accountKey(current)).map(item => accountKey(item) === selection ? cleared : item) };
  });
  return { event: 'result', ok: true, provider: CHATGPT_PROVIDER, beforeType: 'oauth', authType: '',
    remoteRevocationConfirmed,
    warning: remoteRevocationConfirmed ? '' : '本机已退出，但未确认远程撤销；请到 ChatGPT 设置中断开此应用。',
  };
}

// Local CLI only. These actions are intentionally absent from the HTTP control routes.
export async function transferChatGPT({ action, auth, siwc, agentDir, path, accountId }) {
  const hostId = await siwc.getOrCreateChatGPTHostId(join(agentDir, 'chatgpt-host-id'));
  if (action === 'chatgpt_prepare_host') return { event: 'result', ok: true, hostId };
  if (typeof path !== 'string' || !path.trim()) throw new Error('Protected credential file path is required');
  if (action === 'chatgpt_export') {
    const current = await auth.read(CHATGPT_PROVIDER);
    const selectedId = accountId || (siwc.isChatGPTRegistration(current) ? accountKey(current) : '');
    const selected = registrations(current, siwc).find(item => accountKey(item) === selectedId);
    if (!selected || selected.sessionState !== 'active') throw new Error('Select a signed-in ChatGPT registration before export');
    await writeFile(path, JSON.stringify(withoutSavedAccounts(selected)), { flag: 'wx', mode: 0o600 });
    return { event: 'result', ok: true, exported: true };
  }
  if (action !== 'chatgpt_import') throw new Error('Unknown credential transfer action');
  const info = await lstat(path);
  if (!info.isFile() || info.isSymbolicLink() || info.size > 64 * 1024
    || (process.platform !== 'win32' && (info.mode & 0o077) !== 0)) throw new Error('Credential import requires an owner-only regular file');
  const selected = JSON.parse(await readFile(path, 'utf8'));
  if (!siwc.isChatGPTRegistration(selected) || selected.sessionState !== 'active' || !selected.refresh) throw new Error('Invalid SIWC registration file; legacy Codex tokens cannot be imported');
  const credential = withoutSavedAccounts(selected);
  await auth.modify(CHATGPT_PROVIDER, async current => ({ ...credential, accountEpoch: randomUUID(),
    savedAccounts: registrations(current, siwc).filter(item => accountKey(item) !== accountKey(credential)),
  }));
  return { event: 'result', ok: true, imported: true, hostId, requiresAgentRestart: true };
}
