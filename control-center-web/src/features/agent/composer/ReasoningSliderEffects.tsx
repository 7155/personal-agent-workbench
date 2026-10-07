/*!
MIT License

Copyright (c) 2026 Nachoneko_miao

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

*/
// Adapted visual code from client.js, DSH-Codex-reasoning-effort-slider
// commit 6e71fa408bc2fb5e9b20ed040b1ad42ed49f4333.
// PAW adaptations: native range geometry; real max, drag velocity (not Fake
// Fast); existing open/activity/pending gate; 30Hz draw cap and full cleanup.
import { useEffect, useRef, type RefObject } from 'react';

export type SliderVelocity = { speed: number; at: number };
const particleSeeds = [[17,15],[20,12],[28,18],[48,21],[69,23],[95,15],[112,20],
  [134,15],[138,24],[150,9],[165,20],[177,10],[190,22],[198,26],[217,15],
  [237,18],[254,12],[275,24],[290,14],[315,20]];

/** Decoration only: no catalog, selection callback, Pi or network owner. */
export function ReasoningSliderEffects({ enabled, maximum, dragging, velocity }: {
  enabled: boolean; maximum: boolean; dragging: boolean;
  velocity: RefObject<SliderVelocity>;
}) {
  const rootRef = useRef<HTMLDivElement>(null), canvasRef = useRef<HTMLCanvasElement>(null), burstRef = useRef<HTMLCanvasElement>(null);
  const previousMaximum = useRef(maximum);
  const latest = useRef({ maximum, dragging }); latest.current = { maximum, dragging };
  const motion = useRef({ speed: 0, from: 0, target: 0, changedAt: 0, travel: 0, burst: 0, frames: 0, bursts: 0 });
  useEffect(() => {
    const entered = maximum && !previousMaximum.current;
    previousMaximum.current = maximum;
    motion.current.burst = 0;
    if (!enabled || !entered) return;
    const timer = window.setTimeout(() => {
      motion.current.burst = performance.now();
      if (rootRef.current) rootRef.current.dataset.bursts = String(++motion.current.bursts);
    }, 220);
    return () => window.clearTimeout(timer);
  }, [enabled, maximum]);

  useEffect(() => {
    const root = rootRef.current, canvas = canvasRef.current, burst = burstRef.current;
    if (!root || !canvas || !burst || !enabled || (!maximum && !dragging)) return;
    const ctx = canvas.getContext('2d'), bctx = burst.getContext('2d');
    if (!ctx || !bctx) return;
    let frame = 0, last = 0, width = root.getBoundingClientRect().width || 248, disposed = false;
    const resize = (next: number) => {
      width = next || 248; canvas.width = Math.round(width * 2);
      root.style.setProperty('--particle-rail-width', `${width}px`);
    };
    resize(width);
    const observer = new ResizeObserver(entries => resize(entries[0].contentRect.width));
    observer.observe(root); root.dataset.running = 'true';
    function draw(time: number) {
      if (disposed) return;
      frame = window.requestAnimationFrame(draw);
      if (last && time - last < 1000 / 30) return;
      const delta = last ? Math.min(time - last, 50) / 1000 : 0;
      last = time;
      const current = latest.current, m = motion.current;
      // Upstream's visual Fast switch is deliberately absent. Only measured
      // pointer travel briefly accelerates the same upstream particle field.
      const speed = current.dragging && time - velocity.current.at < 250 ? velocity.current.speed : 0;
      const target = current.maximum ? (speed > .7 ? 1 : speed > .2 ? .5 : 0) : 0;
      if (target !== m.target) { m.from = m.speed; m.target = target; m.changedAt = time; }
      const p = Math.min(1, Math.max(0, (time - m.changedAt) / (m.target ? 600 : 750)));
      m.speed = m.from + (m.target - m.from) * p * p * (3 - 2 * p);
      if (current.maximum) m.travel += delta * m.speed;
      ctx!.clearRect(0, 0, canvas!.width, canvas!.height);
      bctx!.clearRect(0, 0, 144, 144);
      const t = time / 1000, particleSpeed = current.maximum ? m.speed : 0;
      particleSeeds.forEach(([px, py], i) => {
        const phase = i * 2.39;
        const offset = current.maximum ? m.travel * (145 + i % 6 * 17) : 0;
        const x = ((px / 346 * width - offset + Math.sin(t * 9 + phase) * .7 * (1 - particleSpeed)) % width + width) % width;
        const y = (py + Math.cos(t * 10 + phase) * .55 * (1 - particleSpeed) + Math.sin(t * 1.3 + phase) * .8 * particleSpeed) * 28 / 36;
        const idle = .55 + .3 * (.5 + .5 * Math.sin(t * 1.1 + phase));
        const moving = .3 + .45 * (.5 + .5 * Math.sin(t * 2.4 + phase));
        const radius = (i % 7 === 0 ? 2.1 : i % 4 === 0 ? 1.55 : 1.1) * 28 / 36;
        ctx!.beginPath(); ctx!.arc(x * 2, y * 2, radius * 2, 0, Math.PI * 2);
        ctx!.fillStyle = `rgba(255,255,255,${idle + (moving - idle) * particleSpeed})`; ctx!.fill();
      });
      if (m.burst && time - m.burst < 520) {
        const age = (time - m.burst) / 1000;
        for (let i = 0; i < 22; i++) {
          const angle = i * 2.399 + .18;
          const x = 72 + Math.cos(angle) * (43 + (48 + i % 5 * 12) * age * 2);
          const y = 72 + Math.sin(angle) * (43 + (48 + i % 4 * 10) * age * 2);
          bctx!.beginPath(); bctx!.arc(x, y, (i % 6 === 0 ? 2.3 : 1.35) * 2 * (1 - age), 0, Math.PI * 2);
          bctx!.fillStyle = `rgba(165,107,233,${.7 * (1 - age / .52)})`; bctx!.fill();
        }
      }
      root!.dataset.frames = String(++m.frames); root!.dataset.speed = m.speed.toFixed(3);
      root!.parentElement?.style.setProperty('--particle-speed', String(m.speed));
    }
    frame = window.requestAnimationFrame(draw);
    return () => {
      disposed = true; window.cancelAnimationFrame(frame); observer.disconnect();
      root.dataset.running = 'false'; motion.current.speed = motion.current.target = 0;
      root.parentElement?.style.setProperty('--particle-speed', '0');
      ctx.clearRect(0, 0, canvas.width, canvas.height); bctx.clearRect(0, 0, 144, 144);
    };
  }, [enabled, maximum, dragging, velocity]);
  return <div ref={rootRef} aria-hidden="true" className="agent-reasoning-effects"
    data-enabled={enabled} data-max={maximum} data-dragging={dragging} data-running="false" data-frames="0" data-bursts="0">
    <div className="agent-reasoning-effects__fill"><canvas ref={canvasRef} width={496} height={56}/></div>
    <canvas className="agent-reasoning-effects__burst" ref={burstRef} width={144} height={144}/>
  </div>;
}
