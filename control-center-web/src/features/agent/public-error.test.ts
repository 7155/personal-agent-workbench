import { describe, expect, it } from 'vitest';

import {
  agentCommandReceiptFailure,
  isAmbiguousAgentPromptFailure,
  MODEL_QUOTA_EXHAUSTED_TEXT,
  MODEL_AUTH_FAILURE_TEXT,
  isModelAuthError,
  publicAgentErrorText,
} from './public-error';

describe('Agent command receipt public recovery', () => {
  it('explains provider overload and active-turn conflicts', () => {
    expect(publicAgentErrorText(new Error('Codex error: Our servers are currently overloaded. Please try again later.')))
      .toContain('模型服务当前繁忙');
    expect(publicAgentErrorText({ payload: { code: 'AGENT_COMMAND_FAILED',
      commandReceipt: { state: 'failed', clientMessageId: 'busy', causeCode: 'AGENT_TURN_CONFLICT' } } }))
      .toContain('仍在处理或自动重试');
  });
  it('recognizes invalidated OAuth without treating a network failure as a login failure', () => {
    expect(publicAgentErrorText(new Error('Encountered invalidated oauth token for user, failing request')))
      .toBe(MODEL_AUTH_FAILURE_TEXT);
    expect(publicAgentErrorText(new Error('refresh_token_reused'))).toBe(MODEL_AUTH_FAILURE_TEXT);
    expect(isModelAuthError('fetch failed')).toBe(false);
  });
  it('explains a Room participant reservation without a generic resend loop', () => {
    const expected = '目标伙伴正在处理另一条请求；草稿已保留，待当前请求结束后可再次发送。';
    expect(publicAgentErrorText({
      payload: {
        code: 'AGENT_COMMAND_FAILED',
        commandReceipt: { state: 'failed', clientMessageId: 'busy-1', causeCode: 'ROOM_PARTICIPANT_BUSY' },
      },
    })).toBe(expected);
    expect(publicAgentErrorText(new Error('Agent 3 is currently busy'))).toBe(expected);
    expect(publicAgentErrorText(new Error('Room participants are currently busy: Agent 3'))).toBe(expected);
  });

  it('explains a definitive team worker rejection without marking delivery uncertain', () => {
    const error = Object.assign(
      new Error('team worker unavailable'),
      {
        status: 409,
        payload: {
          ok: false,
          errorCode: 'team_worker_unavailable',
          error: '团队尚未配置执行服务，请联系管理员配置模型与隔离运行环境。',
        },
      },
    );

    expect(publicAgentErrorText(error)).toBe('团队尚未配置执行服务，请联系管理员配置模型与隔离运行环境。');
    expect(isAmbiguousAgentPromptFailure(error)).toBe(false);

    const serverFailure = Object.assign(new Error('worker gateway failed'), {
      status: 500,
      payload: error.payload,
    });
    expect(isAmbiguousAgentPromptFailure(serverFailure)).toBe(true);
  });

  it('turns an optional memory budget failure into a non-blocking message', () => {
    expect(publicAgentErrorText({
      payload: {
        errorCode: 'memory_bootstrap_budget_exceeded',
        error: 'memory bootstrap exceeded its strict character budget',
      },
    })).toBe('记忆召回本轮已跳过，消息仍可继续；下次会重新尝试。');
    expect(publicAgentErrorText(new Error('memory bootstrap exceeded its strict character budget')))
      .toBe('记忆召回本轮已跳过，消息仍可继续；下次会重新尝试。');
  });

  it('does not present a provider quota exhaustion as a local memory overflow', () => {
    expect(publicAgentErrorText(new Error('Codex error: The usage limit has been reached')))
      .toBe(MODEL_QUOTA_EXHAUSTED_TEXT);
    expect(publicAgentErrorText(new Error('memory curation packet exceeds the managed Session input limit')))
      .toBe('记忆召回本轮已跳过，消息仍可继续；下次会重新尝试。');
  });

  it('keeps the typed new-command recovery state and gives a direct resend action', () => {
    const error = {
      payload: {
        code: 'AGENT_COMMAND_CONFLICT',
        commandReceipt: {
          state: 'conflict',
          clientMessageId: 'local-command-id',
          causeCode: 'PAYLOAD_MISMATCH',
          recoveryState: 'new_command_required',
        },
      },
    };

    expect(agentCommandReceiptFailure(error)?.recoveryState)
      .toBe('new_command_required');
    expect(publicAgentErrorText(error))
      .toBe('这次发送内容已经变化，输入已保留；请直接重新发送一次。');
  });

  it('never exposes transport identity details for a generic command conflict', () => {
    const error = {
      payload: {
        code: 'AGENT_COMMAND_CONFLICT',
        commandReceipt: {
          state: 'conflict',
          clientMessageId: 'internal-id-must-stay-hidden',
          causeCode: 'PAYLOAD_MISMATCH',
        },
      },
    };

    const message = publicAgentErrorText(error);
    expect(message).toBe('这条消息没有发送；输入已保留，请直接重新发送一次。');
    expect(message).not.toContain('标识');
    expect(message).not.toContain('internal-id');
    expect(message).not.toContain('刷新');
  });
});
