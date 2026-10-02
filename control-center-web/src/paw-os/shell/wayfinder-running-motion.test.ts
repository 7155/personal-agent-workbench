import { describe, expect, it } from 'vitest';
import shellCss from '../styles/paw-os-shell.css?raw';
import wayfinderSource from './PawWayfinderWork.tsx?raw';

describe('Wayfinder running desktop-file motion', () => {
  it('projects real running state onto project folders and loose dialogue files', () => {
    expect(wayfinderSource).toContain('data-running={project.runningCount > 0 || undefined}');
    expect(wayfinderSource).toContain('data-activity={item.activity}');
    expect(wayfinderSource).toContain('data-running={item.runtimeRunning || undefined}');
  });

  it('uses one compositor-only state signal for running desktop files', () => {
    expect(shellCss).toMatch(/\.paw-wayfinder-work__folder-art\[data-running\]::after[\s\S]*?animation:\s*paw-wayfinder-running-pulse 1\.2s var\(--paw-ease-in-out\) infinite/);
    expect(shellCss).toMatch(/\.paw-wayfinder-work__loose-file\[data-running\][\s\S]*?\.paw-wayfinder-work__file-art::after[\s\S]*?animation:\s*paw-wayfinder-running-pulse/);
    const keyframes = shellCss.match(/@keyframes paw-wayfinder-running-pulse\s*\{(?<body>[\s\S]*?)\n\}/)?.groups?.body ?? '';
    expect(keyframes).toContain('opacity:');
    expect(keyframes).toContain('transform:');
    expect(keyframes).not.toMatch(/filter|width|height|margin|padding|top|left/);
  });

  it('stills the signal for reduced motion and while desktop interaction owns the frame', () => {
    expect(shellCss).toMatch(/\.paw-desktop-root\[data-window-interaction\][\s\S]*?animation-play-state:\s*paused/);
    expect(shellCss).toMatch(/data-ambient-paused[\s\S]*?animation-play-state:\s*paused/);
    expect(shellCss).toMatch(/@media \(prefers-reduced-motion: reduce\)[\s\S]*?paw-wayfinder-work__[\s\S]*?animation:\s*none/);
  });
});
