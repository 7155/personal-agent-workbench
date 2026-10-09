import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';
import { TooltipProvider } from '@/components/primitives';
import { DiffPreview } from './DiffPreview';

afterEach(cleanup);

const PATCH = `diff --git a/src/a.ts b/src/a.ts
--- a/src/a.ts
+++ b/src/a.ts
@@ -1,3 +1,4 @@
 line1
-old
+new
+added
 line3
`;

describe('DiffPreview', () => {
  it('summarizes real added/removed counts per patch and per file', () => {
    render(<TooltipProvider><DiffPreview content={PATCH} /></TooltipProvider>);
    expect(screen.getByText('1 个文件 · +2 −1')).toBeInTheDocument();
    expect(screen.getByText('修改 · +2 −1')).toBeInTheDocument();
  });

  it('keeps distinct full paths, change counts and hunks when switching a long-path patch to split view', async () => {
    const user = userEvent.setup();
    const path = `workspace/${'long-directory-'.repeat(24)}/result.txt`;
    const deletedPath = 'workspace/other-directory/result.txt';
    const patch = `--- a/${path}\n+++ b/${path}\n@@ -1 +1 @@\n-old exact content\n+new exact content\n--- a/${deletedPath}\n+++ /dev/null\n@@ -1 +0,0 @@\n-deleted exact content\n`;
    const { container } = render(<TooltipProvider><DiffPreview content={patch} /></TooltipProvider>);
    const headers = () => Array.from(container.querySelectorAll('.agent-diff-file > header strong'), node => node.textContent);
    expect(headers()).toEqual([path, deletedPath]);
    expect(screen.getByText('修改 · +1 −1')).toBeInTheDocument();
    expect(screen.getByText('删除 · +0 −1')).toBeInTheDocument();
    await user.click(screen.getByRole('radio', { name: '并排' }));
    expect(headers()).toEqual([path, deletedPath]);
    expect(screen.getByRole('region', { name: `${path} 变更内容` })).toHaveAttribute('tabindex', '0');
    expect(screen.getByText('old exact content')).toBeInTheDocument();
    expect(screen.getByText('new exact content')).toBeInTheDocument();
    expect(screen.getByText('deleted exact content')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '复制补丁原文' }));
    await expect(navigator.clipboard.readText()).resolves.toBe(patch);
  });

  it('copies the exact received patch text', async () => {
    const user = userEvent.setup();
    render(<TooltipProvider><DiffPreview content={PATCH} /></TooltipProvider>);
    await user.click(screen.getByRole('button', { name: '复制补丁原文' }));
    expect(await screen.findByRole('button', { name: '复制补丁原文：已复制' })).toBeInTheDocument();
    await expect(navigator.clipboard.readText()).resolves.toBe(PATCH);
  });
});
