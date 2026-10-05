import { describe, expect, it } from 'vitest';
import type { SessionSummary } from '@/features/agent/types';
import { projectPetDirectory } from './desktop-pet-directory-projection';

function session(id: string, status: SessionSummary['status'] = 'idle'): SessionSummary {
  return { id, title: `对话 ${id}`, status, mode: 'assistant', roleId: '', roleVersion: '', roleBookRevisionId: '',
    workspaceRoots: ['/private'], updatedAtMs: 1, lastMessagePreview: 'secret transcript' };
}
describe('small conversation directory projection', () => {
  it('uses existing stellar status semantics without forwarding history or paths', () => {
    const value = projectPetDirectory([session('running', 'busy'), session('problem', 'faulted'), session('idle')], true, true);
    expect(value.counts).toMatchObject({ running: 1, attention: 1, idle: 1 });
    expect(value.conversations.map(item => item.id)).toEqual(['idle', 'problem', 'running']);
    expect(JSON.stringify(value)).not.toMatch(/private|secret transcript|workspaceRoots/);
  });
  it('bounds the list, prioritizes useful states and leaves an honest remaining count', () => {
    const value = projectPetDirectory([...Array.from({ length: 20 }, (_, i) => session(`idle-${i}`)), session('running', 'busy')], true, true);
    expect(value.conversations).toHaveLength(8);
    expect(value.conversations.some(item => item.id === 'running')).toBe(true);
    expect(Object.values(value.counts).reduce((sum, count) => sum + count, 0)).toBe(21);
  });
  it('keeps identity order stable across input reordering and never runs stale planets', () => {
    const rows = [session('b', 'busy'), session('a')];
    expect(projectPetDirectory(rows, true, true)).toEqual(projectPetDirectory([...rows].reverse(), true, true));
    const stale = projectPetDirectory(rows, false, true);
    expect(stale.freshness).toBe('recovering'); expect(stale.counts.running).toBe(0);
    expect(stale.conversations.every(item => item.state === 'unknown')).toBe(true);
  });
  it('clips labels and excludes archived or unsafe identities', () => {
    const value = projectPetDirectory([{ ...session('a'), title: '长'.repeat(80) }, session('old', 'archived'), session('invalid id')], true, true);
    expect(value.conversations).toHaveLength(1); expect([...value.conversations[0].label]).toHaveLength(48);
    expect(projectPetDirectory([], false, false).freshness).toBe('unavailable');
  });
});
