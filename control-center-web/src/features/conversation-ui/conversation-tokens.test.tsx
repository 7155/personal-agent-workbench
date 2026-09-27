import { cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import css from './conversation-ui.css?raw';
import statusCss from './components/tool-status-mark.css?raw';
import { QueueTray } from './components/QueueTray';
import { createQueuedDraft } from './model/queue';
import type { QueuedDraft } from './model/types';
import type { ConversationQueueController } from './use-conversation-queue';

afterEach(cleanup);

/**
 * The `ccui-*` palette is declared on class roots rather than on `:root`, which
 * keeps the vendored surface themeable per host window. The cost is that any
 * element a host mounts *outside* the transcript has to be a token root of its
 * own — an undeclared custom property does not fall back, it invalidates the
 * whole declaration that reads it. The queue tray is exactly that case: Session
 * and Room both mount it beside their composer.
 */
describe('conversation-ui palette roots', () => {
  const declaredRoots = paletteRootSelectors();

  it('declares the palette once, on a selector list every host mount shares', () => {
    expect(declaredRoots).toContain('.ccui-conversation-surface');
    expect(declaredRoots.length).toBeGreaterThan(1);
  });

  it('covers every root the queue tray can render on its own', () => {
    for (const root of queueTrayRootClasses()) {
      expect(declaredRoots).toContain(`.${root}`);
    }
  });
});

describe('conversation-ui tool rows', () => {
  /* CJK labels have a one-character min-content width, so a shrinkable
     `strong` wraps vertically and stretches the whole row in narrow windows
     (planet satellites). The label stays on one line; the ellipsizing summary
     absorbs the squeeze. */
  it('keeps the tool label on one line and lets the summary absorb the squeeze', () => {
    expect(css).toContain('.ccui-tool-main strong { flex: 0 0 auto; font-size: 12px; font-weight: 620; white-space: nowrap; }');
    expect(css).toContain('.ccui-tool-meta { display: flex; gap: 6px; font-size: 11px; color: var(--ccui-faint); white-space: nowrap; }');
  });

  it('keeps readable tool text still and provides reduced-motion handling for activity marks', () => {
    expect(css).not.toContain('@keyframes ccui-text-sweep');
    expect(statusCss).toContain(".ccui-execution-mark[data-state='running'] svg");
    expect(statusCss).toContain(".ccui-execution-mark[data-active='false']");
    expect(statusCss).toContain('@media (prefers-reduced-motion: reduce)');
    expect(statusCss).toContain('animation: none;');
  });
});

/** The selector list of the rule that owns the palette. */
function paletteRootSelectors(): string[] {
  const block = /(?<selectors>[^{}]+)\{[^{}]*--ccui-surface:/u.exec(css.replace(/\/\*[\s\S]*?\*\//gu, ''));
  if (!block?.groups) throw new Error('conversation-ui.css no longer declares --ccui-surface');
  return block.groups.selectors.split(',').map((selector) => selector.trim()).filter(Boolean);
}

/** Every outermost class the tray actually renders, across all of its states:
 *  one held draft, several held drafts expanded, and a refused draft at cap. */
function queueTrayRootClasses(): string[] {
  const roots = new Set<string>();
  for (const queue of [[held('先补迁移脚本')], [held('先补迁移脚本'), held('再跑一次门禁')], []]) {
    const { container } = render(
      <QueueTray busy controller={queueController(queue, true)} />,
    );
    const collapsed = container.querySelector('.ccui-queue-collapsed');
    if (collapsed) fireEvent.click(collapsed);
    for (const element of container.children) {
      const root = [...element.classList].find((name) => name.startsWith('ccui-'));
      if (root) roots.add(root);
    }
    cleanup();
  }
  expect(roots.size).toBeGreaterThan(1);
  return [...roots];
}

let seed = 0;
function held(text: string): QueuedDraft {
  seed += 1;
  return createQueuedDraft({
    id: `queued-${seed}`,
    text,
    conversationId: 'conversation-under-test',
    busy: true,
    existingDepth: 0,
    now: 1_700_000_000_000,
  });
}

function queueController(queue: QueuedDraft[], capReached: boolean): ConversationQueueController {
  return {
    queue,
    capReached,
    enqueue: () => true,
    remove: () => undefined,
    edit: () => undefined,
    reorder: () => undefined,
    clear: () => undefined,
    sendNow: () => undefined,
    restoreToDraft: (draft: string) => draft,
  };
}
