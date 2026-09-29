import { useEffect, useRef, useState } from 'react';
import { usePresentationMotion } from '../conversation-ui/reading/reading-preferences';

const EMPTY: ReadonlySet<string> = new Set();

/** Presentation only. Initial snapshots, history and reconnects stay still. */
export function useReceiptHighlight(
  scope: string,
  keys: readonly string[],
  observing: boolean,
  duration = 1200,
): ReadonlySet<string> {
  observing = usePresentationMotion(observing);
  const signature = JSON.stringify([...new Set(keys)].sort());
  const previous = useRef<{ scope: string; keys: Set<string> } | null>(null);
  const timers = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  const [highlight, setHighlight] = useState<{ scope: string; keys: ReadonlySet<string> }>({ scope: '', keys: EMPTY });

  useEffect(() => {
    const before = previous.current;
    const current = new Set<string>(JSON.parse(signature));
    previous.current = observing && scope ? { scope, keys: current } : null;
    if (!observing || !scope || before?.scope !== scope) {
      timers.current.forEach(clearTimeout);
      timers.current.clear();
      setHighlight(value => value.keys.size ? { scope: '', keys: EMPTY } : value);
      return;
    }
    // Unrelated stream updates do not cancel the lifetime of a highlight.
    for (const key of current) {
      if (before.keys.has(key)) continue;
      const timer = timers.current.get(key);
      if (timer) clearTimeout(timer);
      setHighlight(value => ({ scope, keys: new Set(value.scope === scope ? value.keys : []).add(key) }));
      timers.current.set(key, setTimeout(() => {
        timers.current.delete(key);
        setHighlight(value => {
          if (value.scope !== scope || !value.keys.has(key)) return value;
          const next = new Set(value.keys);
          next.delete(key);
          return { scope, keys: next };
        });
      }, duration));
    }
  }, [scope, signature, observing, duration]);

  useEffect(() => () => {
    timers.current.forEach(clearTimeout);
    timers.current.clear();
  }, []);

  return observing && highlight.scope === scope ? highlight.keys : EMPTY;
}
