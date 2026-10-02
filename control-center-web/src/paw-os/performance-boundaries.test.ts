import { describe, expect, it, vi } from 'vitest';
import appSource from '@/app/App.tsx?raw';
import structuredRenderersSource from '@/features/agent/timeline/StructuredRenderers.tsx?raw';
import httpTransportSource from '@/platform/http-transport.ts?raw';
import nativeTransportSource from '@/platform/native-transport.ts?raw';
import pawOsSource from './PawOsApp.tsx?raw';
import appDispatcherSource from './apps/PawApps.tsx?raw';
import runtimeSource from './apps/PawAppsRuntime.tsx?raw';
import desktopSource from './shell/PawDesktop.tsx?raw';
import windowLayerSource from './shell/PawWindowLayer.tsx?raw';
import agentFxCss from './styles/paw-os-agent-fx.css?raw';

describe('PAWOS production loading boundaries', () => {
  it('loads one product shell without a parallel route implementation', () => {
    expect(appSource).not.toContain("import { RouterProvider } from 'react-router-dom'");
    expect(appSource).not.toContain("import { RouteLoading, router } from '@/app/router'");
    expect(appSource).not.toContain("import { FrontendShell } from './FrontendShell'");
    expect(appSource).not.toContain("LegacyProductApp");
    expect(appSource).not.toContain("frontend-product");
    expect(appSource).toContain("import('@/paw-os/PawOsApp')");
  });

  it('loads every leaf App through a dynamic boundary instead of the total dispatcher', () => {
    for (const staticOwner of [
      "from './PawAgentApp'",
      "from './PawBrowserApp'",
      "from './PawNativeApps'",
      "from '@/features/paw-os/PawOsSatelliteHost'",
      "from '@/features/paw-os/PawResultWindow'",
    ]) {
      expect(runtimeSource).not.toContain(staticOwner);
    }
    for (const lazyEntry of [
      './entries/PawAgentAppEntry',
      './entries/PawBrowserAppEntry',
      './entries/PawNativeAppEntry',
      './entries/PawSatelliteEntry',
      './entries/PawResultWindowEntry',
    ]) {
      expect(runtimeSource).toContain(`import('${lazyEntry}')`);
    }
  });

  it('warms the primary Agent boundary before the launch click reaches the main thread', () => {
    expect(appDispatcherSource).toContain('export function warmPawAppProcess');
    expect(runtimeSource).toContain('export function warmPawAppBody');
    expect(desktopSource).toContain("warmPawAppProcess('agent')");
    expect(desktopSource).toContain('onPointerEnter={() => warmPawAppProcess(appId)}');
    expect(desktopSource).toContain('onFocus={() => warmPawAppProcess(appId)}');
  });

  it('keeps App-only visual layers out of the first PAWOS shell stylesheet', () => {
    for (const appStyle of [
      'paw-os-agent-composition.css',
      'paw-os-agent-next.css',
      'paw-os-agent.css',
      'paw-os-agent-fx.css',
      'paw-os-room.css',
      'paw-os-room-focus.css',
      'paw-os-starfield.css',
      'paw-os-system-apps.css',
      'paw-os-tools-files.css',
    ]) {
      expect(pawOsSource).not.toContain(appStyle);
    }
  });

  it('defers the generated contract runtime and Room keepalive reducer beyond the first shell', () => {
    for (const transportSource of [nativeTransportSource, httpTransportSource]) {
      expect(transportSource).not.toContain("from '@/contracts/validators'");
      expect(transportSource).toContain('loadContractValidationRuntime');
    }
    expect(windowLayerSource).not.toContain("from '@/features/rooms/runtime/use-room-live-session'");
    expect(windowLayerSource).not.toContain("from '@/features/rooms/state/live-store'");
    expect(windowLayerSource).toContain("from '@/features/rooms/state/projection-bridge'");
    expect(windowLayerSource).toContain("lazy(() => import('./PawRoomProjectionKeeper'))");
  });

  it('keeps disclosure spacing and live progress updates off layout properties', () => {
    expect(agentFxCss).not.toMatch(/transition:\s*width/);
    expect(agentFxCss).toMatch(/\.fx-track \.fill\s*\{[^}]*transform:\s*scaleX\(var\(--fx-progress-scale, 0\)\);[^}]*transition:\s*transform/s);
    expect(structuredRenderersSource).toContain("'--fx-progress-scale': percent / 100");
  });

  it('does not evaluate unopened feature modules when the native App shell loads', async () => {
    const evaluated: string[] = [];
    const features: Record<string, string[]> = {
      '@/features/memory': ['MemoryFeature'],
      '@/features/knowledge': ['KnowledgeFeature'],
      '@/features/eval-lab': ['EvalLabFeature'],
      '@/features/voice': ['VoiceFeature'],
      '@/features/diagnostics': ['DiagnosticsFeature'],
      '@/features/configuration': ['ConfigurationFeature'],
      '@/features/trace-agent': ['TraceAgentFeature'],
      '@/features/context-debug': ['ContextDebugFeature'],
      '@/features/history': ['HistoryFeature'],
      '@/features/approvals': ['ApprovalsFeature'],
      '@/features/observability': ['ObservabilityFeature'],
      '@/features/input-method': ['InputMethodFeature', 'InputLexiconFeature'],
      '@/features/plugins': ['PluginsFeature'],
      '@/features/roles': ['ModelRoutingPanel'],
    };
    vi.resetModules();
    for (const [path, names] of Object.entries(features)) vi.doMock(path, () => {
      evaluated.push(path);
      return Object.fromEntries(names.map((name) => [name, () => null]));
    });
    try {
      await import('./apps/PawNativeApps');
      expect(evaluated).toEqual([]);
    } finally {
      for (const path of Object.keys(features)) vi.doUnmock(path);
      vi.resetModules();
    }
  });

  it('does not evaluate the actual contract runtime when importing the unopened HTTP window layer', async () => {
    const evaluated: string[] = [];
    vi.resetModules();
    // Match the production HTTP alias; preview transport is a different entry.
    vi.doMock('@/app/control-transport', () => import('@/app/control-transport.http'));
    vi.doMock('@/contracts/validators', async importOriginal => {
      evaluated.push('validators');
      return await importOriginal();
    });
    try {
      await import('./shell/PawWindowLayer');
      expect(evaluated).toEqual([]);
      // The deferred observer still evaluates the real validation owner.
      await import('./shell/PawBackgroundToolWindows');
      expect(evaluated).toEqual(['validators']);
    } finally {
      vi.doUnmock('@/contracts/validators');
      vi.doUnmock('@/app/control-transport');
      vi.resetModules();
    }
  });

});
