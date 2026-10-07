import assert from 'node:assert/strict';
import { test } from 'node:test';
import { assistantLaunchIntent, assistantSessionRoute } from './assistant-launch.mjs';

test('cold and second-instance argv identify the exact Session', () => {
  const argv = ['/Applications/PAW', '--paw-session=session:one/two'];
  assert.deepEqual(assistantLaunchIntent(argv), { kind: 'session', sessionId: 'session:one/two' });
  assert.equal(assistantSessionRoute('session:one/two'), '/agent?session=session%3Aone%2Ftwo');
});

test('capture carries only a bounded app identity', () => {
  assert.deepEqual(assistantLaunchIntent(['--paw-capture', '--paw-source-app=com.example.Editor']), {
    kind: 'capture', sourceAppBundleId: 'com.example.Editor',
  });
  assert.equal(assistantLaunchIntent(['--paw-capture', '--paw-source-app=bad\napp']).sourceAppBundleId, '');
  assert.equal(assistantLaunchIntent(['--paw-session=bad\nname']), null);
  assert.equal(assistantLaunchIntent(['--some-other-switch']), null);
  assert.deepEqual(assistantLaunchIntent(['--paw-session=']), { kind: 'session', sessionId: '' });
});

test('the assistant launcher opens StarMate while explicit Sessions stay in conversations', () => {
  assert.equal(assistantSessionRoute(''), '/agent-controller');
  assert.equal(assistantSessionRoute('agent:original'), '/agent?session=agent%3Aoriginal');
});
