import { Maximize2, Minimize2 } from 'lucide-react';
import { useLayoutEffect, useRef, type RefObject } from 'react';
import { IconButton } from '@/components/primitives';
import { useChatPresentation } from '@/features/conversation-ui/reading/chat-presentation';

const EDITOR_BUDGET = '--composer-editor-available-height';

function boundedComposer(input: HTMLTextAreaElement) {
  if (!input.closest('.agent-composer[data-chat-presentation-version="v2"]')) return null;
  const dock = input.closest<HTMLElement>('.paw-session-workspace__composer, .paw-room-workspace__composer');
  const host = dock?.parentElement;
  if (!dock || !host || !host.matches('.paw-session-workspace__primary, .paw-room-workspace__main')) return null;
  if (!dock.closest('.paw-session-workspace[data-design="workbench"]:not([data-appearance="embedded"]), .paw-room-workspace--conversation[data-window-chrome="portal"]')) return null;
  return { dock, host };
}

function availableEditorHeight(input: HTMLTextAreaElement, maximum: number) {
  const bounded = boundedComposer(input);
  if (!bounded) return null;
  // Width-only wrapping can spend the entire height of a restored short
  // window on toolbar rows. Compact that chrome before measuring its budget.
  if (bounded.host.clientHeight > 0 && bounded.host.clientHeight < 220) bounded.dock.dataset.composerHeight = 'compact';
  else delete bounded.dock.dataset.composerHeight;
  if (bounded.host.clientHeight <= 0) return null;
  // Subtract the textarea border box, leaving only the dock's actual chrome.
  const chrome = Math.max(0, bounded.dock.scrollHeight - input.offsetHeight);
  const gap = Number.parseFloat(getComputedStyle(bounded.host).getPropertyValue('--workspace-composer-gap')) || 8;
  return Math.min(maximum, Math.max(0, bounded.host.clientHeight - chrome - gap));
}

function fitEditor(input: HTMLTextAreaElement, expanded: boolean, animate = false, resizeOnly = false) {
  const previousHeight = input.style.height;
  const cap = expanded ? 360 : 156;
  const available = availableEditorHeight(input, cap);
  // Unchanged observer deliveries must not interrupt an expansion animation.
  if (resizeOnly && available !== null && input.style.getPropertyValue(EDITOR_BUDGET) === `${available}px`
    && Number.parseFloat(previousHeight) <= available) return;
  // Remove only our prior limits before reading the stylesheet's current floor.
  if (input.style.getPropertyValue(EDITOR_BUDGET)) {
    input.style.removeProperty(EDITOR_BUDGET);
    input.style.removeProperty('min-height');
    input.style.removeProperty('max-height');
  }
  input.style.transition = 'none';
  input.style.height = 'auto';
  const minimum = Number.parseFloat(getComputedStyle(input).minHeight) || 40;
  // scrollHeight includes banners, attachments, toolbar, wrapper and padding;
  // the textarea's own overflow stays inside it. Its height cancels out.
  const maximum = availableEditorHeight(input, cap) ?? cap;
  if (available !== null) {
    input.style.setProperty(EDITOR_BUDGET, `${maximum}px`);
    // A CSS minimum wins over a smaller height/max-height. Bound that floor as
    // well; short windows must not be pushed back to 210/220px by expansion.
    input.style.minHeight = `${Math.min(minimum, maximum)}px`;
    input.style.maxHeight = `${maximum}px`;
  }
  const height = Math.min(Math.max(input.scrollHeight, expanded ? 220 : minimum), maximum);
  if (animate && previousHeight) {
    input.style.height = previousHeight;
    void input.offsetHeight;
    input.style.transition = 'height 220ms cubic-bezier(.2,.8,.2,1)';
  }
  input.style.height = `${height}px`;
}

export function useComposerEditor(ref: RefObject<HTMLTextAreaElement | null>, draft: string, expanded: boolean) {
  const version = useChatPresentation()?.version ?? 'v1';
  const previousExpanded = useRef(expanded);
  useLayoutEffect(() => {
    const input = ref.current;
    if (!input) return;
    const toggled = previousExpanded.current !== expanded;
    previousExpanded.current = expanded;
    fitEditor(input, expanded, toggled);
  }, [draft, expanded, ref, version]);

  useLayoutEffect(() => {
    const input = ref.current;
    if (!input) return;
    const bounded = boundedComposer(input);
    if (!bounded) return;
    const update = () => fitEditor(input, expanded, false, true);
    // The host has a fixed available box. Only sibling branches outside the
    // editor are observed, so changing the textarea does not observe itself.
    const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(update) : null;
    const retarget = () => {
      observer?.disconnect();
      observer?.observe(bounded.host);
      for (let branch: Element = input; branch !== bounded.dock && branch.parentElement; branch = branch.parentElement) {
        for (const sibling of branch.parentElement.children) if (sibling !== branch) observer?.observe(sibling);
      }
      update();
    };
    retarget();
    const mutations = new MutationObserver(retarget);
    mutations.observe(bounded.dock, { childList: true, subtree: true });
    window.addEventListener('resize', update);
    return () => {
      observer?.disconnect();
      mutations.disconnect();
      window.removeEventListener('resize', update);
      delete bounded.dock.dataset.composerHeight;
    };
  }, [expanded, ref, version]);
}

export function ComposerExpandButton({ expanded, onToggle }: { expanded: boolean; onToggle: () => void }) {
  return <IconButton className="composer-editor__expand" label={expanded ? '收起长文本编辑' : '展开长文本编辑'} icon={expanded ? <Minimize2 size={15} /> : <Maximize2 size={15} />} aria-expanded={expanded} onClick={onToggle} tooltip />;
}
