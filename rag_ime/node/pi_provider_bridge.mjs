import { readFile } from 'node:fs/promises';
import { createServer } from 'node:net';
import { createRequire } from 'node:module';
import process from 'node:process';
import { pathToFileURL } from 'node:url';
import { CHATGPT_PROVIDER, chatGPTAccountStatus, loginChatGPT, logoutChatGPT, transferChatGPT } from './pi_chatgpt_accounts.mjs';

const MAX_REQUEST_BYTES = 64 * 1024;
const MAX_MODELS_PER_PROVIDER = 120;
const PROVIDER_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

function emit(value) {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}

function publicError(error) {
  const raw = error instanceof Error ? error.message : String(error);
  return raw
    .replace(/(?:sk|key|token|bearer)[-_A-Za-z0-9.]{8,}/gi, '[redacted]')
    .replace(/[\r\n\t]+/g, ' ')
    .slice(0, 300) || 'Pi provider operation failed';
}

async function readRequest() {
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > MAX_REQUEST_BYTES) throw new Error('request is too large');
    chunks.push(chunk);
  }
  const text = Buffer.concat(chunks).toString('utf8');
  const request = JSON.parse(text || '{}');
  if (!request || typeof request !== 'object' || Array.isArray(request)) {
    throw new Error('request must be an object');
  }
  return request;
}

function requiredString(request, key, maximum = 4096) {
  const value = typeof request[key] === 'string' ? request[key].trim() : '';
  if (!value || value.length > maximum) throw new Error(`${key} is invalid`);
  return value;
}

function providerId(request) {
  const value = requiredString(request, 'provider', 128);
  if (!PROVIDER_ID_PATTERN.test(value)) throw new Error('provider is invalid');
  return value;
}

async function loadPi(request) {
  const packageEntry = requiredString(request, 'packageEntry', 4096);
  const imported = await import(pathToFileURL(packageEntry).href);
  const agentDir = requiredString(request, 'agentDir', 4096);
  if (
    typeof imported.AuthStorage === 'function'
    && typeof imported.ModelRegistry === 'function'
    && typeof imported.ModelRegistry.create === 'function'
  ) {
    const auth = imported.AuthStorage.create(`${agentDir}/auth.json`);
    const registry = imported.ModelRegistry.create(auth, `${agentDir}/models.json`);
    return { agentDir, mode: 'legacy', auth, registry };
  }
  if (typeof imported.ModelRuntime === 'function' && typeof imported.ModelRuntime.create === 'function') {
    const { AuthStorage } = await import(new URL('./core/auth-storage.js', pathToFileURL(packageEntry)).href);
    const auth = AuthStorage.create(`${agentDir}/auth.json`);
    const runtime = await imported.ModelRuntime.create({
      credentials: auth,
      modelsPath: `${agentDir}/models.json`,
      allowModelNetwork: false,
    });
    let siwc;
    if (runtime.getProvider(CHATGPT_PROVIDER)) {
      const require = createRequire(pathToFileURL(packageEntry));
      const aiPackage = require.resolve('@earendil-works/pi-ai/package.json');
      siwc = await import(new URL('./dist/auth/oauth/openai-chatgpt.js', pathToFileURL(aiPackage)).href);
    }
    return { agentDir, mode: 'runtime', runtime, auth, siwc };
  }
  throw new Error('managed Pi package does not export credential services');
}

async function configuredProviderIds(request) {
  const ids = new Set();
  try {
    const raw = await readFile(`${requiredString(request, 'agentDir', 4096)}/models.json`, 'utf8');
    const parsed = JSON.parse(raw);
    const providers = parsed && typeof parsed === 'object' ? parsed.providers : undefined;
    if (providers && typeof providers === 'object' && !Array.isArray(providers)) {
      for (const id of Object.keys(providers)) ids.add(id);
    }
  } catch {
    // ModelRegistry reports a public load error; provider discovery stays fail-open.
  }
  return ids;
}

function legacyProviderCatalog(auth, registry, configuredIds) {
  const models = registry.getAll();
  const oauthProviders = new Map(auth.getOAuthProviders().map((provider) => [provider.id, provider]));
  const ids = new Set([
    ...models.map((model) => model.provider),
    ...auth.list(),
    ...oauthProviders.keys(),
    ...configuredIds,
  ]);
  const providers = [];
  for (const id of [...ids].sort((left, right) => left.localeCompare(right))) {
    const providerModels = models.filter((model) => model.provider === id);
    const available = providerModels.filter((model) => registry.hasConfiguredAuth(model));
    const credential = auth.get(id);
    const status = registry.getProviderAuthStatus(id);
    const oauth = oauthProviders.get(id);
    providers.push({
      id,
      name: registry.getProviderDisplayName(id),
      auth: {
        configured: Boolean(status.configured || available.length),
        type: credential?.type ?? '',
        source: status.source ?? '',
        sourceLabel: status.label ?? '',
        oauthSupported: Boolean(oauth),
        oauthBrowserSupported: id === 'openai-codex' && Boolean(oauth),
        oauthDeviceCodeSupported: id === 'openai-codex',
        ...(credential?.type === 'oauth'
          ? { expiresAtMs: Number.isFinite(credential.expires) ? credential.expires : 0 }
          : {}),
      },
      configuredInCatalog: configuredIds.has(id),
      modelCount: providerModels.length,
      availableModelCount: available.length,
      availableModels: available.slice(0, MAX_MODELS_PER_PROVIDER).map((model) => ({
        id: model.id,
        name: model.name,
        reasoning: Boolean(model.reasoning),
        imageInput: Array.isArray(model.input) && model.input.includes('image'),
      })),
      modelsTruncated: available.length > MAX_MODELS_PER_PROVIDER,
    });
  }
  return providers;
}

async function runtimeProviderCatalog(runtime, configuredIds, auth, siwc) {
  const credentialInfo = await runtime.listCredentials();
  const credentialTypes = new Map(
    credentialInfo.map((item) => [item.providerId, item.type]),
  );
  const runtimeProviders = new Map(
    runtime.getProviders().map((provider) => [provider.id, provider]),
  );
  const availableByProvider = new Map();
  for (const model of runtime.getAvailableSnapshot()) {
    const current = availableByProvider.get(model.provider) ?? [];
    current.push(model);
    availableByProvider.set(model.provider, current);
  }
  const ids = new Set([
    ...runtimeProviders.keys(),
    ...credentialTypes.keys(),
    ...configuredIds,
  ]);
  const providers = [];
  for (const id of [...ids].sort((left, right) => left.localeCompare(right))) {
    const provider = runtimeProviders.get(id);
    const models = runtime.getModels(id);
    const available = availableByProvider.get(id) ?? [];
    const status = runtime.getProviderAuthStatus(id);
    const chatgptStatus = id === CHATGPT_PROVIDER && siwc ? await chatGPTAccountStatus(auth, siwc) : {};
    providers.push({
      id,
      name: provider?.name ?? id,
      auth: {
        configured: Boolean(status?.configured || available.length),
        type: credentialTypes.get(id) ?? '',
        source: status?.source ?? '',
        sourceLabel: status?.label ?? '',
        oauthSupported: Boolean(provider?.auth?.oauth),
        oauthBrowserSupported: (id === CHATGPT_PROVIDER || id === 'openai-codex') && Boolean(provider?.auth?.oauth),
        oauthDeviceCodeSupported: id === 'openai-codex' && Boolean(provider?.auth?.oauth),
        apiKeySupported: Boolean(provider?.auth?.apiKey),
        ...chatgptStatus,
      },
      configuredInCatalog: configuredIds.has(id),
      modelCount: models.length,
      availableModelCount: available.length,
      availableModels: available.slice(0, MAX_MODELS_PER_PROVIDER).map((model) => ({
        id: model.id,
        name: model.name,
        reasoning: Boolean(model.reasoning),
        imageInput: Array.isArray(model.input) && model.input.includes('image'),
      })),
      modelsTruncated: available.length > MAX_MODELS_PER_PROVIDER,
    });
  }
  return providers;
}

async function catalog(request) {
  const pi = await loadPi(request);
  let catalogError = '';
  if (pi.siwc) {
    const refreshed = await pi.runtime.refresh({ providers: [CHATGPT_PROVIDER], allowNetwork: true, force: true, signal: AbortSignal.timeout(12_000) });
    catalogError = refreshed.errors.get(CHATGPT_PROVIDER)?.message || '';
  }
  const configuredIds = await configuredProviderIds(request);
  return {
    event: 'result',
    ok: true,
    providers: pi.mode === 'runtime'
      ? await runtimeProviderCatalog(pi.runtime, configuredIds, pi.auth, pi.siwc)
      : legacyProviderCatalog(pi.auth, pi.registry, configuredIds),
    catalogError: (pi.mode === 'runtime' ? pi.runtime.getError() : pi.registry.getError()) || catalogError,
  };
}

async function knownProviderIds(request, pi) {
  const configuredIds = await configuredProviderIds(request);
  if (pi.mode === 'runtime') {
    return new Set([
      ...pi.runtime.getProviders().map((provider) => provider.id),
      ...configuredIds,
    ]);
  }
  return new Set([
    ...pi.registry.getAll().map((model) => model.provider),
    ...pi.auth.getOAuthProviders().map((item) => item.id),
    ...configuredIds,
  ]);
}

async function credentialType(pi, provider) {
  if (pi.mode === 'runtime') {
    const item = (await pi.runtime.listCredentials()).find(
      (credential) => credential.providerId === provider,
    );
    return item?.type ?? '';
  }
  return pi.auth.get(provider)?.type ?? '';
}

async function setApiKey(request) {
  const provider = providerId(request);
  const apiKey = requiredString(request, 'apiKey', 16 * 1024);
  const pi = await loadPi(request);
  const known = await knownProviderIds(request, pi);
  if (!known.has(provider)) throw new Error('provider is not in the Pi model catalog');
  const beforeType = await credentialType(pi, provider);
  if (pi.mode === 'runtime') {
    await pi.runtime.login(provider, 'api_key', {
      prompt: async () => apiKey,
      notify: () => {},
    });
  } else {
    pi.auth.set(provider, { type: 'api_key', key: apiKey });
    const errors = pi.auth.drainErrors();
    if (errors.length) throw errors[0];
  }
  return { event: 'result', ok: true, provider, beforeType, authType: 'api_key' };
}

async function logout(request) {
  const provider = providerId(request);
  const pi = await loadPi(request);
  if (provider === CHATGPT_PROVIDER && pi.siwc) return logoutChatGPT({ auth: pi.auth, siwc: pi.siwc, accountId: request.accountId, signal: AbortSignal.timeout(10_000) });
  if (!(await knownProviderIds(request, pi)).has(provider)) {
    throw new Error('provider is not in the Pi model catalog');
  }
  const beforeType = await credentialType(pi, provider);
  if (pi.mode === 'runtime') {
    await pi.runtime.logout(provider);
  } else {
    pi.auth.logout(provider);
    const errors = pi.auth.drainErrors();
    if (errors.length) throw errors[0];
  }
  return { event: 'result', ok: true, provider, beforeType, authType: '' };
}

async function assertBrowserCallbackAvailable() {
  const host = process.env.PI_OAUTH_CALLBACK_HOST?.trim() || '127.0.0.1';
  await new Promise((resolve, reject) => {
    const server = createServer();
    server.once('error', () => {
      reject(new Error('OpenAI Codex browser callback port 1455 is unavailable; use device-code fallback'));
    });
    server.listen(1455, host, () => {
      server.close((error) => {
        if (error) reject(error);
        else resolve();
      });
    });
  });
}

function waitForBrowserCallback(signal) {
  return new Promise((_resolve, reject) => {
    const aborted = () => reject(new Error('Browser callback prompt closed'));
    if (signal?.aborted) {
      aborted();
      return;
    }
    signal?.addEventListener('abort', aborted, { once: true });
  });
}

async function oauthLogin(request) {
  const provider = providerId(request);
  if (provider === CHATGPT_PROVIDER) {
    if (request.action !== 'oauth_browser') throw new Error('SIWC does not offer the legacy Codex device-code flow');
    const pi = await loadPi(request);
    if (!pi.siwc) throw new Error('Update the managed Pi runtime to use Sign in with ChatGPT');
    const controller = new AbortController();
    const cancel = () => controller.abort();
    process.once('SIGTERM', cancel);
    process.once('SIGINT', cancel);
    try { await loginChatGPT({ auth: pi.auth, runtime: pi.runtime, siwc: pi.siwc, agentDir: pi.agentDir,
      accountId: request.accountId, requestConsent: request.requestConsent, emit, signal: controller.signal }); }
    finally { process.off('SIGTERM', cancel); process.off('SIGINT', cancel); }
    return;
  }
  if (provider !== 'openai-codex') throw new Error('OAuth login is unavailable for this provider');
  const method = request.action === 'oauth_browser' ? 'browser' : 'device_code';
  if (method === 'browser') await assertBrowserCallbackAvailable();
  const pi = await loadPi(request);
  emit({ event: 'state', state: 'starting', provider });
  if (pi.mode === 'runtime') {
    const runtimeProvider = pi.runtime.getProvider(provider);
    if (!runtimeProvider?.auth?.oauth) {
      throw new Error('OAuth provider is unavailable in the managed Pi package');
    }
    await pi.runtime.login(provider, 'oauth', {
      prompt: async (prompt) => {
        if (
          prompt?.type === 'select'
          && Array.isArray(prompt.options)
          && prompt.options.some((option) => option.id === method)
        ) {
          return method;
        }
        if (method === 'browser' && prompt?.type === 'manual_code') {
          return waitForBrowserCallback(prompt.signal);
        }
        throw new Error('interactive OAuth input is unavailable');
      },
      notify: (event) => emit({ ...event, event: event.type }),
    });
  } else {
    const oauthProvider = pi.auth.getOAuthProviders().find((item) => item.id === provider);
    if (!oauthProvider) throw new Error('OAuth provider is unavailable in the managed Pi package');
    await pi.auth.login(provider, {
      onAuth: (info) => emit({ event: 'auth_url', url: info.url, instructions: info.instructions || '' }),
      onDeviceCode: (info) => emit({
        event: 'device_code',
        userCode: info.userCode,
        verificationUri: info.verificationUri,
        intervalSeconds: info.intervalSeconds ?? 0,
        expiresInSeconds: info.expiresInSeconds ?? 0,
      }),
      onPrompt: async (prompt) => {
        if (method === 'browser') return waitForBrowserCallback(prompt?.signal);
        throw new Error('interactive OAuth input is unavailable');
      },
      onProgress: (message) => emit({ event: 'progress', message: String(message).slice(0, 200) }),
      onSelect: async () => method,
    });
    const errors = pi.auth.drainErrors();
    if (errors.length) throw errors[0];
  }
  emit({ event: 'completed', ok: true, provider });
}

async function main() {
  const request = await readRequest();
  switch (request.action) {
    case 'chatgpt_prepare_host':
    case 'chatgpt_import':
    case 'chatgpt_export': {
      const pi = await loadPi(request);
      if (!pi.siwc) throw new Error('Update the managed Pi runtime to use Sign in with ChatGPT');
      emit(await transferChatGPT({ action: request.action, auth: pi.auth, siwc: pi.siwc,
        agentDir: pi.agentDir, path: request.path, accountId: request.accountId }));
      return;
    }
    case 'catalog':
      emit(await catalog(request));
      return;
    case 'set_api_key':
      emit(await setApiKey(request));
      return;
    case 'logout':
      emit(await logout(request));
      return;
    case 'oauth_device_code':
    case 'oauth_browser':
      await oauthLogin(request);
      return;
    default:
      throw new Error('action is not allowed');
  }
}

main().catch((error) => {
  emit({ event: 'failed', ok: false, error: publicError(error) });
  process.exitCode = 1;
});
