import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { TooltipProvider } from '@/components/primitives';
import agentCss from '../agent.css?raw';
import agentMigratedCss from '@/paw-os/styles/paw-os-agent.css?raw';
import { CodeContentBlock } from './CodeDiffRenderers';

afterEach(cleanup);

function renderBlock(props: Parameters<typeof CodeContentBlock>[0]) {
  return render(
    <TooltipProvider>
      <CodeContentBlock {...props} />
    </TooltipProvider>,
  );
}

describe('conversation code highlighting', () => {
  it('highlights a settled code block with shiki tokens while the paired surface owner keeps the pre', async () => {
    renderBlock({ code: 'const ready: boolean = true;', language: 'ts' });

    await waitFor(() => {
      expect(document.querySelector('.agent-code-block__highlight pre.shiki')).not.toBeNull();
    });
    const reader = document.querySelector<HTMLElement>('.agent-code-block__highlight');
    expect(reader).toHaveAttribute('role', 'region');
    expect(reader).toHaveAttribute('tabindex', '0');
    expect(reader).toHaveAccessibleName('ts 代码内容');
    // The root pre cedes its inline surface to --color-code-bg/--color-code-text.
    const shikiPre = document.querySelector<HTMLElement>('.agent-code-block__highlight pre');
    expect(shikiPre?.getAttribute('style')).toBeNull();
    expect(document.querySelector('.agent-code-block__highlight span[style*="color:"]')).not.toBeNull();
  });

  it('keeps the streaming tail on the plain incremental reader without an async highlight swap', async () => {
    renderBlock({ code: 'const streaming = true;', language: 'ts', streamingTail: true });

    expect(document.querySelector('.agent-code-block__content')).not.toBeNull();
    await new Promise((resolve) => setTimeout(resolve, 40));
    expect(document.querySelector('.agent-code-block__highlight')).toBeNull();
    expect(screen.getByRole('region', { name: 'ts 代码内容' }).tagName).toBe('PRE');
  });

  it('keeps unknown languages on the plain readable reader', async () => {
    renderBlock({ code: 'opaque content', language: 'not-a-real-language' });

    await new Promise((resolve) => setTimeout(resolve, 40));
    expect(document.querySelector('.agent-code-block__highlight')).toBeNull();
    expect(document.querySelector('.agent-code-block__content')).toHaveTextContent('opaque content');
  });

  it('keeps both conversation code readers on the paired code surface in the installed separated flow', () => {
    // The highlighted reader is the same bounded scroll box as the plain pre.
    expect(agentCss).toMatch(
      /\.agent-code-block__highlight\s*\{[^}]*max-height:\s*min\(440px, 64dvh\);[^}]*overflow:\s*auto;/s,
    );
    expect(agentCss).toMatch(
      /\.agent-code-block__highlight pre\s*\{[^}]*background:\s*transparent;[^}]*color:\s*inherit;/s,
    );
    // The separated-flow paper rule guards pre.agent-code-block__content, but
    // the content class sits on code; both conversation readers must therefore
    // be listed in the paired code-surface owner after that paper rule.
    const paperRule = agentMigratedCss.indexOf("pre:not(.agent-code-block__content)");
    const codeSurfaceRule = agentMigratedCss.search(
      /:is\([^)]*\.agent-code-block pre,[^)]*\.agent-code-block__highlight pre[^)]*\)\s*\{[^}]*background:\s*var\(--color-code-bg\);[^}]*color:\s*var\(--color-code-text\);/s,
    );
    expect(paperRule).toBeGreaterThan(-1);
    expect(codeSurfaceRule).toBeGreaterThan(paperRule);
  });
});
