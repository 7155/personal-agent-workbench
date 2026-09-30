import assert from 'node:assert/strict';
import { chmod, mkdtemp, readFile, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { accountKey, chatGPTAccountStatus, loginChatGPT, logoutChatGPT, transferChatGPT } from '../rag_ime/node/pi_chatgpt_accounts.mjs';

const registration = (clientId = 'client-a') => ({ type: 'oauth', protocol: 'siwc-v1', issuer: 'https://auth.openai.com',
  clientId, subject: 'same-subject', email: 'same@example.invalid', access: `access-${clientId}`, refresh: `refresh-${clientId}`,
  idToken: `identity-${clientId}`, expires: Date.now() + 3600000, scopes: ['chatgpt.tokens.use.direct', 'resource.invoke'], sessionState: 'active' });
const siwc = {
  isChatGPTRegistration: item => item?.protocol === 'siwc-v1',
  chatGPTPlanEnabled: item => item?.sessionState === 'active' && item.scopes.includes('chatgpt.tokens.use.direct'),
  getOrCreateChatGPTHostId: async () => 'vm-host-id',
  clearChatGPTSession: item => ({ ...item, access: '', refresh: '', idToken: '', expires: 0, scopes: [], sessionState: 'signed_out' }),
  revokeChatGPTSession: async () => true,
};
function storage(initial) {
  let value = initial;
  return { read: async () => value, modify: async (_id, fn) => { value = await fn(value) ?? value; return value; } };
}
const signal = new AbortController().signal;
const runtime = { refresh: async () => ({ errors: new Map() }) };

test('same-email registrations remain distinct and status never exposes credentials', async () => {
  const a = registration(), b = registration('client-b');
  const auth = storage({ ...a, savedAccounts: [b] });
  const status = await chatGPTAccountStatus(auth, siwc);
  assert.equal(status.accounts.length, 2);
  assert.notEqual(status.accounts[0].id, status.accounts[1].id);
  assert.equal(status.activeAccount, accountKey(a));
  assert.doesNotMatch(JSON.stringify(status), /access-client|refresh-client|identity-client/);
});

test('switching reauthorizes the selected registration before replacing the active one', async () => {
  const a = registration(), b = registration('client-b');
  const auth = storage({ ...a, savedAccounts: [b] });
  const events = [];
  await loginChatGPT({ auth, runtime, agentDir: '/fixture', accountId: accountKey(b), signal, emit: item => events.push(item),
    siwc: { ...siwc, createOpenAIChatGPTOAuth: options => {
      assert.equal(options.agentName, 'Personal Agent Workbench');
      assert.equal(options.hostId, 'vm-host-id');
      assert.equal(options.registration.clientId, 'client-b');
      return { login: async () => ({ ...b, access: 'new-b' }) };
    } },
  });
  const current = await auth.read();
  assert.equal(current.clientId, 'client-b');
  assert.equal(current.savedAccounts[0].clientId, 'client-a');
  assert.equal(events.at(-1).firstSignIn, false);
});

test('failure, cancellation and late completion preserve the active account', async () => {
  const a = registration(), b = registration('client-b');
  const auth = storage(a);
  await assert.rejects(loginChatGPT({ auth, runtime, agentDir: '/fixture', accountId: 'new', signal, emit: () => {},
    siwc: { ...siwc, createOpenAIChatGPTOAuth: () => ({ login: async () => { throw new Error('declined'); } }) },
  }));
  assert.deepEqual(await auth.read(), a);
  await assert.rejects(loginChatGPT({ auth, runtime, agentDir: '/fixture', accountId: 'new', signal, emit: () => {},
    siwc: { ...siwc, createOpenAIChatGPTOAuth: () => ({ login: async () => {
      await auth.modify('', async () => ({ ...a, accountEpoch: 'changed-during-login' }));
      return b;
    } }) },
  }), /changed during sign-in/);
  assert.equal((await auth.read()).clientId, a.clientId);
});

test('sign-out clears only the selected tokens, retains registration and reports revocation failure', async () => {
  const a = registration(), b = registration('client-b');
  const auth = storage({ ...a, savedAccounts: [b] });
  const result = await logoutChatGPT({ auth, accountId: accountKey(b), signal, siwc: { ...siwc, revokeChatGPTSession: async () => false } });
  assert.equal(result.remoteRevocationConfirmed, false);
  const current = await auth.read();
  assert.equal(current.access, a.access);
  assert.equal(current.savedAccounts[0].clientId, b.clientId);
  assert.equal(current.savedAccounts[0].refresh, '');
});

test('secure VM transfer exports one registration and preserves the VM host and other accounts', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'paw-siwc-transfer-'));
  try {
    const a = registration(), b = registration('client-b');
    const path = join(directory, 'account.json');
    await transferChatGPT({ action: 'chatgpt_export', auth: storage({ ...a, savedAccounts: [b] }), siwc, agentDir: directory, path });
    assert.equal((await stat(path)).mode & 0o777, 0o600);
    assert.equal(JSON.parse(await readFile(path, 'utf8')).savedAccounts, undefined);
    const destination = storage(b);
    const result = await transferChatGPT({ action: 'chatgpt_import', auth: destination, siwc, agentDir: directory, path });
    assert.equal(result.hostId, 'vm-host-id');
    assert.equal((await destination.read()).savedAccounts[0].clientId, b.clientId);
    await chmod(path, 0o644);
    await assert.rejects(transferChatGPT({ action: 'chatgpt_import', auth: destination, siwc, agentDir: directory, path }), /owner-only/);
  } finally { await rm(directory, { recursive: true }); }
});
