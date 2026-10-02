import { describe, expect, it } from 'vitest';
import workbenchCss from './paw-os-workbench.css?raw';

// One PAWOS UI contract: a single cobalt accent, not a competing purple
// flourish layered on top of it. Project Workbench previously mixed its
// cobalt identity (--paw-wb-accent, matching [data-app='project-workbench']
// in paw-os.css) with an unrelated violet (#6850d9) across four decorative
// gradients — a second, dead-var-only hue that read as a second theme.
describe('Project Workbench stays on one cobalt accent', () => {
  it('never declares or references the retired violet decoration token', () => {
    expect(workbenchCss).not.toMatch(/--paw-wb-violet/);
    expect(workbenchCss).not.toContain('6850d9');
    expect(workbenchCss).not.toContain('104 80 217');
  });

  it('keeps reading surfaces on their theme token and confines the cobalt identity to small details', () => {
    expect(workbenchCss).toMatch(
      /\.paw-wb-now\s*\{[^}]*background:\s*var\(--paw-wb-surface\);/s,
    );
    expect(workbenchCss).not.toContain('--paw-wb-gauge-angle');
    expect(workbenchCss).toMatch(
      /\.paw-wb-ledger__mark\s*\{[^}]*background:\s*linear-gradient\(135deg, rgb\(49 94 172 \/ 16%\), rgb\(49 94 172 \/ 11%\)\);/s,
    );
    expect(workbenchCss).toMatch(
      /\.paw-wb-ledger::before\s*\{[^}]*background:\s*linear-gradient\(90deg, transparent 2%, rgb\(49 94 172 \/ 45%\) 30%, rgb\(49 94 172 \/ 34%\) 70%, transparent 98%\);/s,
    );
  });
});

// Window logic, not page logic: every Workbench page is fixed chrome around a
// flexible band, and the band hands its scroll to the pane that owns the work.
describe('Project Workbench lays out as a window, not a document', () => {
  it('keeps the overview a fixed shell whose panes own their own scroll', () => {
    expect(workbenchCss).toMatch(
      /\.paw-wb-overview\s*\{[^}]*display:\s*flex;[^}]*flex-direction:\s*column;[^}]*overflow:\s*hidden;/s,
    );
    expect(workbenchCss).toMatch(
      /\.paw-wb-overview__workspace\s*\{[^}]*flex:\s*1 1 auto;[^}]*grid-template-rows:\s*minmax\(0, 1fr\);/s,
    );
    expect(workbenchCss).toMatch(
      /\.paw-wb-pane > :is\(\.paw-wb-pane__list, \.paw-wb-pane__rows, \.paw-wb-pane__goals, \.paw-wb-empty\)\s*\{[^}]*flex:\s*1 1 auto;[^}]*overflow:\s*auto;/s,
    );
    // The retired hero plate and stat rail must not come back above the work.
    expect(workbenchCss).not.toContain('.paw-wb-project-lead');
    expect(workbenchCss).not.toContain('.paw-wb-overview__columns');
  });

  it('keeps the WorkDocument reader header fixed above one scrolling body', () => {
    expect(workbenchCss).toMatch(
      /\.paw-wb-document-reader\s*\{[^}]*display:\s*flex;[^}]*flex-direction:\s*column;[^}]*overflow:\s*hidden;/s,
    );
    expect(workbenchCss).toMatch(
      /\.paw-wb-document-reader__body\s*\{[^}]*flex:\s*1 1 auto;[^}]*overflow:\s*auto;/s,
    );
    expect(workbenchCss).toMatch(
      /\.paw-wb-document-index > ol\s*\{[^}]*flex:\s*1 1 auto;[^}]*overflow:\s*auto;/s,
    );
  });

  it('draws dependency lane ownership and keeps the lane header on the vertical scroll', () => {
    expect(workbenchCss).toMatch(/\.paw-wb-graph__band\s*\{[^}]*transform:\s*translateX\(calc\(var\(--paw-wb-lane-x\) - 12px\)\);/s);
    expect(workbenchCss).toMatch(/\.paw-wb-graph__lanes\s*\{[^}]*position:\s*sticky;[^}]*top:\s*0;/s);
    // Sticky on the block axis only, or the header stops naming its column.
    expect(workbenchCss).not.toMatch(/\.paw-wb-graph__lanes\s*\{[^}]*left:\s*0;/s);
  });

  it('keeps every decorative motion bounded and reduced-motion safe', () => {
    const keyframes = [...workbenchCss.matchAll(/@keyframes\s+([\w-]+)/g)].map(([, name]) => name);
    expect(keyframes).toEqual([
      'paw-wb-edge-flow',
      'paw-wb-packet-visible',
      'paw-wb-spin',
      'paw-wb-node-enter',
      'paw-wb-pane-rise',
    ]);
    expect(workbenchCss).toMatch(
      /@media \(prefers-reduced-motion: reduce\)[\s\S]*?animation-duration:\s*\.01ms\s*!important;[\s\S]*?\.paw-wb-flow-packet\s*\{\s*display:\s*none;/,
    );
  });
});
