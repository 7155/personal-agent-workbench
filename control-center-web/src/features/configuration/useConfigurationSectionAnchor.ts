import { useEffect, type RefObject } from 'react';

/** Keep a deep link aligned while preceding settings finish loading. User
 * interaction ends the initial alignment, so reading and editing never snap back. */
export function useConfigurationSectionAnchor(ref: RefObject<HTMLElement | null>, highlighted: boolean) {
  useEffect(() => {
    const panel = ref.current;
    if (!highlighted || !panel) return;
    const content = panel.closest('.mgmt-page__body') ?? panel.parentElement;
    if (!content) return;
    let frame = 0;
    const align = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => panel.scrollIntoView({ block: 'start', behavior: 'instant' }));
    };
    const observer = new ResizeObserver(align);
    for (const section of content.children) observer.observe(section);
    const stop = () => {
      observer.disconnect();
      cancelAnimationFrame(frame);
      for (const event of ['pointerdown', 'wheel', 'keydown']) content.removeEventListener(event, stop, true);
    };
    for (const event of ['pointerdown', 'wheel', 'keydown']) content.addEventListener(event, stop, true);
    align();
    return stop;
  }, [ref, highlighted]);
}
