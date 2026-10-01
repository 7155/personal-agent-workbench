import { describe, expect, it } from 'vitest';
import {
  codeModeDetailsFromPayload,
  codeModeOutputFromPayload,
  codeModeOutputText,
  codeModeSourceFromInput,
  parseCodeModeDetails,
} from './codemode';

describe('codemode receipt projection', () => {
  it('merges native arguments by child identity instead of preferring compact display previews', () => {
    const script = 'text("完整脚本");'.repeat(200);
    expect(codeModeDetailsFromPayload({ result: {
      details: { calls: [{ id: 'p/1', name: 'browser', args: 'script: text(...)', status: 'ok', durationMs: 42 }] },
      nestedCalls: { complete: true, calls: [{ id: 'p/1', name: 'browser', arguments: { script }, status: 'ok' }] },
    } })).toEqual({ calls: [{ id: 'p/1', name: 'browser', args: JSON.stringify({ script }), status: 'ok', durationMs: 42 }], nestedCallsComplete: true });
  });

  it('retains native cancellation and flags missing rows instead of claiming an empty complete receipt', () => {
    expect(codeModeDetailsFromPayload({ result: {
      details: { calls: [{ id: 'p/1', name: 'read', args: '{}', status: 'cancelled' }] },
      nestedCalls: { complete: false, calls: [{ id: 'p/1', name: 'read', arguments: {}, status: 'error' }] },
    } })?.calls[0]?.status).toBe('cancelled');
    expect(parseCodeModeDetails({ complete: true, calls: [{ name: 'read', status: 'ok' }] })?.nestedCallsComplete).toBe(false);
  });
  it('accepts Pi nested call details and keeps optional evidence fields', () => {
    expect(parseCodeModeDetails({
      calls: [
        { id: 'call/1', name: 'read', args: '{}', status: 'ok', durationMs: 12, cost: 0.001 },
        { id: 'call/2', name: 'bash', args: '{}', status: 'error', error: 'failed' },
      ],
      fullOutputPath: '/tmp/output.txt',
    })).toEqual({
      calls: [
        { id: 'call/1', name: 'read', args: '{}', status: 'ok', durationMs: 12, cost: 0.001 },
        { id: 'call/2', name: 'bash', args: '{}', status: 'error', error: 'failed' },
      ],
      fullOutputPath: '/tmp/output.txt',
    });
  });

  it('ignores malformed nested rows without inventing a successful receipt', () => {
    expect(parseCodeModeDetails({ calls: [{ name: 'read', status: 'ok' }, { id: 'call/2', name: 'bash', args: '{}', status: 'running' }] }))
      .toEqual({ calls: [{ id: 'call/2', name: 'bash', args: '{}', status: 'running' }], nestedCallsComplete: false });
    expect(parseCodeModeDetails({ calls: 'not-an-array' })).toBeUndefined();
  });

  it('reads details from the Pi result envelope and strips only its presentation header', () => {
    const payload = {
      result: {
        details: { calls: [{ id: 'call/1', name: 'read', args: '{}', status: 'ok' }] },
      },
    };
    expect(codeModeDetailsFromPayload(payload)?.calls[0]?.id).toBe('call/1');
    expect(codeModeSourceFromInput('{"code":"await tools.read({})"}')).toBe('await tools.read({})');
    expect(codeModeOutputText('Script completed\nWall time 0.2 seconds\nOutput:\nhello')).toBe('hello');
    expect(codeModeOutputText('Script error: bad')).toBe('Script error: bad');
    expect(codeModeOutputFromPayload({
      result: { content: [{ type: 'text', text: 'Script completed\nWall time 1ms\nOutput:\nfinal' }] },
    })).toBe('final');
  });

  it('accepts the public nestedCalls fallback when a host lifts it out of result.details', () => {
    expect(codeModeDetailsFromPayload({
      nestedCalls: [{ id: 'call/1', name: 'read', args: { path: 'README.md' }, status: 'ok' }],
      nestedCallsComplete: false,
    })).toEqual({ calls: [{ id: 'call/1', name: 'read', args: '{"path":"README.md"}', status: 'ok' }], nestedCallsComplete: false });
    expect(codeModeDetailsFromPayload({
      nestedCalls: { calls: [{ id: 'call/2', name: 'bash', args: '{}', status: 'error', error: 'exit 1' }] },
    })).toEqual({ calls: [{ id: 'call/2', name: 'bash', args: '{}', status: 'error', error: 'exit 1' }] });
    expect(codeModeDetailsFromPayload({
      nestedCalls: [{ id: 'call/3', name: 'read', args: '{}', status: 'ok' }],
    })).not.toHaveProperty('nestedCallsComplete');
  });
});
