import { AlignLeft, ChevronDown, List, Type, X } from 'lucide-react';
import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { updateReadingPreferences, usePresentationMotion, useReadingPreferences } from '../../../conversation-ui/reading/reading-preferences';
import { ChatPresentationSettings } from '../../../conversation-ui/reading/ChatPresentationSettings';
import { useChatPresentation } from '../../../conversation-ui/reading/chat-presentation';
import './reading-surface.css';

type ReadingAnchor = { element: HTMLElement; label: string; level: number };
type ReadingIndex = { key: string; sections: ReadingAnchor[]; characters: number };
const EMPTY: ReadingIndex = { key: '', sections: [], characters: 0 };

/**
 * Navigation is derived from the rendered public answer, not an LLM summary.
 * The content stays mounted when menus change. Existing conversation owners
 * continue to own scrolling, streaming, restore and virtualisation.
 */
export function RichReadingSurface({ children, source, documentKey, streaming = false }: {
  children: ReactNode;
  source: string;
  documentKey: string;
  streaming?: boolean;
}) {
  const id = useId();
  const content = useRef<HTMLDivElement>(null);
  const controls = useRef<HTMLDivElement>(null);
  const opener = useRef<HTMLButtonElement | null>(null);
  const [menu, setMenu] = useState<'outline' | 'settings' | null>(null);
  const [index, setIndex] = useState<ReadingIndex>(EMPTY);
  const preferences = useReadingPreferences();
  const presentation = useChatPresentation();
  const motion = usePresentationMotion();
  const highlight = useRef<{ node: HTMLElement; timer: ReturnType<typeof setTimeout> } | null>(null);
  const focusCleanup = useRef<(() => void) | null>(null);
  const key = documentKey || id;

  useEffect(() => {
    if (streaming || !content.current) return;
    const root = content.current;
    let frame = 0;
    const scan = () => {
      frame = 0;
      // Deferred Markdown settlement can replace the final chunk after this
      // effect runs. Observe the settled subtree, never the live token tail.
      const belongs = (node: HTMLElement) => node.closest('.paw-reading-surface') === root.parentElement
        && !node.closest('pre, figure, details, [hidden]');
      const headings = [...root.querySelectorAll<HTMLElement>('.paw-rich-prose h1, .paw-rich-prose h2, .paw-rich-prose h3')].filter(belongs);
      const prose = [...root.querySelectorAll<HTMLElement>('.paw-rich-prose p')].filter(belongs);
      const next: ReadingIndex = { key, sections: headings.map(element => ({ element,
        label: element.textContent?.trim() || '未命名章节', level: Number(element.tagName.slice(1)) })),
        characters: prose.reduce((total, node) => total + (node.textContent?.length ?? 0), 0) };
      setIndex(previous => previous.key === next.key && previous.characters === next.characters
        && previous.sections.length === next.sections.length && previous.sections.every((item, i) =>
          item.element === next.sections[i]?.element && item.label === next.sections[i]?.label) ? previous : next);
    };
    const observer = new MutationObserver(() => { if (!frame) frame = requestAnimationFrame(scan); });
    observer.observe(root, { childList: true, subtree: true, characterData: true });
    scan();
    return () => { observer.disconnect(); cancelAnimationFrame(frame); };
  }, [source, key, streaming]);

  useEffect(() => { setMenu(null); }, [key, streaming]);
  useEffect(() => {
    if (!menu) return;
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !controls.current?.contains(event.target)) setMenu(null);
    };
    document.addEventListener('pointerdown', outside);
    return () => document.removeEventListener('pointerdown', outside);
  }, [menu]);
  useEffect(() => () => {
    if (highlight.current) {
      clearTimeout(highlight.current.timer);
      delete highlight.current.node.dataset.readingTarget;
    }
    focusCleanup.current?.();
  }, [key]);

  const visibleIndex = index.key === key ? index : EMPTY;
  const navigable = !streaming && (visibleIndex.sections.length >= 3 || visibleIndex.characters >= 1000);
  const closeMenu = (restore = true) => {
    setMenu(null);
    if (restore && opener.current?.isConnected) opener.current.focus({ preventScroll: true });
  };
  const openMenu = (next: 'outline' | 'settings', button: HTMLButtonElement) => {
    opener.current = button;
    setMenu(current => current === next ? null : next);
  };
  const jump = (anchor: ReadingAnchor) => {
    if (!anchor.element.isConnected) { closeMenu(); return; }
    closeMenu(false);
    const element = anchor.element;
    element.scrollIntoView({ block: 'start', behavior: motion ? 'smooth' : 'auto' });
    focusCleanup.current?.();
    const previousTabIndex = element.getAttribute('tabindex');
    element.setAttribute('tabindex', '-1');
    const restore = () => {
      if (previousTabIndex === null) element.removeAttribute('tabindex');
      else element.setAttribute('tabindex', previousTabIndex);
      element.removeEventListener('blur', restore);
    };
    focusCleanup.current = restore;
    element.addEventListener('blur', restore, { once: true });
    element.focus({ preventScroll: true });
    if (highlight.current) {
      clearTimeout(highlight.current.timer);
      delete highlight.current.node.dataset.readingTarget;
    }
    element.dataset.readingTarget = 'true';
    highlight.current = { node: element, timer: setTimeout(() => {
      delete element.dataset.readingTarget;
      highlight.current = null;
    }, 1100) };
  };

  return <div className="paw-reading-surface" data-reading-size={preferences.size}
    data-reading-spacing={preferences.spacing} data-reduce-motion={!motion || undefined}
    data-chat-presentation-version={presentation?.version}>
    {navigable ? <div className="paw-reading-bar" ref={controls}
      onKeyDown={event => { if (event.key === 'Escape' && menu) { event.preventDefault(); event.stopPropagation(); closeMenu(); } }}>
      <span className="paw-reading-bar__label"><AlignLeft size={13} aria-hidden />本段内容
        {visibleIndex.sections.length ? <small>{visibleIndex.sections.length} 节</small> : null}</span>
      <div className="paw-reading-bar__actions">
        {visibleIndex.sections.length > 0 ? <button type="button" aria-expanded={menu === 'outline'}
          aria-controls={`${id}-outline`} onClick={event => openMenu('outline', event.currentTarget)}>
          <List size={14} aria-hidden /><span>章节</span><ChevronDown size={12} aria-hidden /></button> : null}
        <button type="button" aria-expanded={menu === 'settings'} aria-controls={`${id}-settings`}
          onClick={event => openMenu('settings', event.currentTarget)}><Type size={14} aria-hidden /><span>阅读</span></button>
      </div>
      {menu === 'outline' ? <nav className="paw-reading-menu" id={`${id}-outline`} aria-label="本段章节导航">
        <header><strong>跳到章节</strong><button type="button" aria-label="关闭章节导航" onClick={() => closeMenu()}><X size={14} aria-hidden /></button></header>
        <ol>{visibleIndex.sections.map((anchor, position) => <li key={position} data-level={anchor.level}>
          <button type="button" onClick={() => jump(anchor)}><span>{String(position + 1).padStart(2, '0')}</span><strong>{anchor.label}</strong></button>
        </li>)}</ol>
        <p>只在你选择时跳转，不跟随新消息移动。</p>
      </nav> : null}
      {menu === 'settings' ? <section className="paw-reading-menu" id={`${id}-settings`} aria-label="阅读显示设置">
        <header><strong>让文字更好读</strong><button type="button" aria-label="关闭阅读设置" onClick={() => closeMenu()}><X size={14} aria-hidden /></button></header>
        <ChatPresentationSettings />
        <fieldset><legend>正文字号</legend><div>
          <button type="button" aria-pressed={preferences.size === 'standard'} onClick={() => updateReadingPreferences({ size: 'standard' })}>标准 <small>16</small></button>
          <button type="button" aria-pressed={preferences.size === 'large'} onClick={() => updateReadingPreferences({ size: 'large' })}>大字 <small>18</small></button>
        </div></fieldset>
        <fieldset><legend>段落间距</legend><div>
          <button type="button" aria-pressed={preferences.spacing === 'comfortable'} onClick={() => updateReadingPreferences({ spacing: 'comfortable' })}>舒展</button>
          <button type="button" aria-pressed={preferences.spacing === 'compact'} onClick={() => updateReadingPreferences({ spacing: 'compact' })}>紧凑</button>
        </div></fieldset>
        <label className="paw-reading-menu__motion"><span>减少动态效果<small>不影响任务执行与状态更新</small></span>
          <input type="checkbox" checked={preferences.motion === 'reduced'} onChange={event => updateReadingPreferences({ motion: event.target.checked ? 'reduced' : 'system' })} /></label>
        <p>阅读偏好在当前浏览器保留；系统减少动态设置始终优先。</p>
      </section> : null}
    </div> : null}
    <div className="paw-reading-surface__content" ref={content}>{children}</div>
  </div>;
}
