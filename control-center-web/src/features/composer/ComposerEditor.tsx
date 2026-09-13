import { Maximize2, Minimize2 } from 'lucide-react';
import { useLayoutEffect, useRef, type RefObject } from 'react';
import { IconButton } from '@/components/primitives';

export function useComposerEditor(ref: RefObject<HTMLTextAreaElement | null>, draft: string, expanded: boolean) {
  const previousExpanded = useRef(expanded);
  useLayoutEffect(() => {
    const input = ref.current;
    if (!input) return;
    const toggled = previousExpanded.current !== expanded;
    previousExpanded.current = expanded;
    const previousHeight = input.style.height;
    input.style.transition = 'none';
    input.style.height = 'auto';
    const height = Math.min(Math.max(input.scrollHeight, expanded ? 220 : 54), expanded ? 360 : 156);
    if (toggled && previousHeight) {
      input.style.height = previousHeight;
      void input.offsetHeight;
      input.style.transition = 'height 220ms cubic-bezier(.2,.8,.2,1)';
    }
    input.style.height = `${height}px`;
  }, [draft, expanded, ref]);
}

export function ComposerExpandButton({ expanded, onToggle }: { expanded: boolean; onToggle: () => void }) {
  return <IconButton className="composer-editor__expand" label={expanded ? '收起长文本编辑' : '展开长文本编辑'} icon={expanded ? <Minimize2 size={15} /> : <Maximize2 size={15} />} aria-expanded={expanded} onClick={onToggle} tooltip />;
}
