import { describe, expect, it } from 'vitest';
import { roomCancellationOutcome } from './room-cancellation';

describe('Room cancellation receipts', () => {
  it('distinguishes applied cancellation from a Root that already ended', () => {
    expect(roomCancellationOutcome({ ok: true, status: 'terminated' })).toBe('terminated');
    expect(roomCancellationOutcome({ ok: true, status: 'already_terminal' })).toBe('already_terminal');
  });

  it.each([
    { ok: true },
    { ok: true, status: 'cancellation_pending' },
    { ok: false, status: 'terminated' },
    { ok: false, status: 'already_terminal' },
  ])('keeps unverified cancellation pending: %j', receipt => {
    expect(roomCancellationOutcome(receipt)).toBe('pending');
  });
});
