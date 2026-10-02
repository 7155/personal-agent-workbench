import { describe, expect, it } from 'vitest';
import agentAppSource from './apps/PawAgentApp.tsx?raw';
import browserAppSource from './apps/PawBrowserApp.tsx?raw';
import roomWorkspaceSource from './apps/PawRoomWorkspace.tsx?raw';
import sessionWorkspaceSource from './apps/PawSessionWorkspace.tsx?raw';
import workbenchSource from './apps/PawWorkbench.tsx?raw';
import desktopSource from './shell/PawDesktop.tsx?raw';
import contextDebugSource from '@/features/context-debug/index.tsx?raw';
import filesSource from '@/features/files/PawOsFilesApp.tsx?raw';
import knowledgeSource from '@/features/knowledge/index.tsx?raw';
import managementSource from '@/features/overview/management-ui.tsx?raw';
import resultSource from '@/features/paw-os/PawResultWindow.tsx?raw';
import terminalSource from '@/features/terminal/PawOsTerminalApp.tsx?raw';

const windowOnlySurfaceSources = [
  ['Agent', agentAppSource],
  ['Session', sessionWorkspaceSource],
  ['Room', roomWorkspaceSource],
  ['Browser', browserAppSource],
  ['Files', filesSource],
  ['Terminal', terminalSource],
  ['Workbench', workbenchSource],
  ['Result', resultSource],
] as const;

const adaptiveSurfaceSources = [
  ['Context Debug', contextDebugSource],
  ['Knowledge', knowledgeSource],
  ['Management', managementSource],
] as const;

function mainTags(source: string): string[] {
  const tags: string[] = [];
  const openingTag = /<main\b/g;
  let match: RegExpExecArray | null;
  while ((match = openingTag.exec(source))) {
    let quote = '';
    let braces = 0;
    let end = match.index + match[0].length;
    for (; end < source.length; end += 1) {
      const char = source[end];
      if (quote) {
        if (char === quote && source[end - 1] !== '\\') quote = '';
        continue;
      }
      if (char === '"' || char === "'") {
        quote = char;
        continue;
      }
      if (char === '{') {
        braces += 1;
        continue;
      }
      if (char === '}') {
        braces = Math.max(0, braces - 1);
        continue;
      }
      if (char === '>' && braces === 0) break;
    }
    tags.push(source.slice(match.index, Math.min(end + 1, source.length)));
  }
  return tags;
}

describe('PAWOS landmark ownership', () => {
  it('keeps the desktop viewport as the only native main and app surfaces as named regions', () => {
    const desktopMains = mainTags(desktopSource);
    expect(desktopMains).toHaveLength(1);
    expect(desktopMains[0]).toContain('paw-desktop-viewport');
    expect(desktopMains[0]).not.toMatch(/\brole\s*=/);

    for (const [name, source] of windowOnlySurfaceSources) {
      expect(mainTags(source), `${name} window surface must not use the native main tag`).toHaveLength(0);
      expect(source, `${name} window surface must use section`).toMatch(/<section\b/);
      expect(source, `${name} window surface must expose region semantics`).toMatch(/\brole=(?:"region"|'region'|\{)/);
      expect(source, `${name} region must have an accessible name`).toMatch(/\baria-(?:label|labelledby)=/);
    }

    for (const [name, source] of adaptiveSurfaceSources) {
      expect(source, `${name} must choose section inside PAWOS and main on the web route`).toMatch(/appSurface\s*\?\s*['"]section['"]\s*:\s*['"]main['"]/);
    }
  });

  it('keeps Terminal tabpanel semantics while making its empty state a region', () => {
    expect(terminalSource).toMatch(/role=\{selected \? ['"]tabpanel['"] : ['"]region['"]\}/);
    expect(terminalSource).toMatch(/aria-labelledby=\{selectedTabId\}/);
  });
});
