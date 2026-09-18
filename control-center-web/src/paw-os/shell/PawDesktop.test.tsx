import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createPortal } from 'react-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { GlobalFeedbackProvider } from '@/components/feedback';
import { MotionProvider } from '@/design/motion';
import { ThemeProvider } from '@/design/themes';
import { MockControlTransport } from '@/test/mock-transport';
import { PawDesktopProvider } from '../runtime/desktop-context';
import { pawApps, type PawAppId } from '../runtime/app-registry';
import { isPawExtensionAppId, pawExtensionApps, registerLabExtensionApps } from '../extensions/registry';
import { PAW_EXTENSION_INSTALLATION_CHANGED_EVENT } from '../extensions/installation';
import { PawDesktop } from './PawDesktop';
import desktopSource from './PawDesktop.tsx?raw';
import wayfinderWorkSource from './PawWayfinderWork.tsx?raw';

// The shell owns window selection and routes; App body behavior has its own suites.
vi.mock('../apps/PawApps', () => ({
  PawAppProcess: ({ appId, initialRoute }: { appId: string; initialRoute?: string }) => (
    <output aria-label={`${appId} current page`}>{initialRoute ?? ''}</output>
  ),
  warmPawAppProcess: vi.fn(),
}));

const pointerFixture = vi.hoisted(() => ({ role: undefined as string | undefined, portalled: false }));

vi.mock('./PawWindowLayer', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./PawWindowLayer')>();
  const ActualPawWindowLayer = actual.PawWindowLayer;
  return {
    ...actual,
    PawWindowLayer: () => {
      const target = pointerFixture.role ? <div aria-label="Desktop pointer regression" role={pointerFixture.role}><span>Pointer target</span></div> : null;
      return <><ActualPawWindowLayer />{target && pointerFixture.portalled ? createPortal(target, document.body) : target}</>;
    },
  };
});

const originalAnimate = Object.getOwnPropertyDescriptor(Element.prototype, 'animate');
const originalGetAnimations = Object.getOwnPropertyDescriptor(Element.prototype, 'getAnimations');

beforeEach(() => {
  pointerFixture.role = undefined;
  pointerFixture.portalled = false;
  window.localStorage.clear();
  Object.defineProperty(Element.prototype, 'animate', { configurable: true, value: vi.fn(() => ({ cancel: vi.fn(), finished: Promise.resolve() })) });
  Object.defineProperty(Element.prototype, 'getAnimations', { configurable: true, value: vi.fn(() => []) });
  vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })));
});
afterEach(() => {
  cleanup();
  window.location.hash = '';
  registerLabExtensionApps({ ok: true, items: [] });
  delete window.pawBrowserHost;
  vi.unstubAllGlobals();
  restoreElementMethod('animate', originalAnimate);
  restoreElementMethod('getAnimations', originalGetAnimations);
});

describe('PAWOS desktop', () => {
  it('opens an enabled dynamic Lab App from Launchpad while inventory refresh is pending', async () => {
    const id = 'extension:lab-33333333333333333333333333333333';
    const route = '/extensions/lab-33333333333333333333333333333333';
    const item = { appId: id, projectId: 'project-refresh', activeVersion: 7, installation: {
      schemaVersion: 'pawos.lab-app.v1', id, version: '0.7.0', label: '刷新中的极地研究', shortLabel: '极地', tagline: '已启用的研究应用',
      route, presentation: 'workspace', accent: 'green', icon: { symbol: 'assistant', background: '#22876A' }, packageId: id,
      bindingSha256: 'a'.repeat(64), hosting: { kind: 'lab-html', appId: id, projectId: 'project-refresh', version: 7 },
    } };
    let reads = 0; let finishRefresh: (() => void) | undefined;
    const transport = new MockControlTransport({ routes: {
      'agent.extensions.list': { ok: true, runtimeAvailable: true, items: [] },
      'agent.eval-lab.apps.get': async () => {
        reads += 1;
        if (reads > 1) await new Promise<void>((resolve) => { finishRefresh = resolve; });
        return { ok: true, items: [item] };
      },
    } });
    renderDesktop('eval-lab', transport, '/eval-lab?project=project-refresh');
    await waitFor(() => expect(document.querySelector(`[data-desktop-app="${id}"]`)).toBeInTheDocument());
    const lab = document.querySelector('[data-paw-window-id="eval-lab"]');
    expect(lab).toBeInTheDocument();
    act(() => window.dispatchEvent(new Event(PAW_EXTENSION_INSTALLATION_CHANGED_EVENT)));
    await waitFor(() => expect(reads).toBe(2));
    fireEvent.click(screen.getByRole('button', { name: '打开全部 App' }));
    fireEvent.click(within(screen.getByRole('dialog', { name: '全部 App' })).getByRole('button', { name: /刷新中的极地研究/ }));
    expect(document.querySelector(`[data-paw-window-id="${id}"]`)).toBeInTheDocument();
    expect(document.querySelector('[data-paw-window-id="eval-lab"]')).toBe(lab);
    expect(screen.getByLabelText('eval-lab current page')).toHaveTextContent('/eval-lab?project=project-refresh');
    await act(async () => finishRefresh?.());
    expect(document.querySelector(`[data-paw-window-id="${id}"]`)).toBeInTheDocument();
  });

  it.each(['desktop', 'launcher'])('preserves an App subpage in the reload URL when returning through %s', async (entry) => {
    const transport = new MockControlTransport();
    const page = renderDesktop('app-center', transport, '/plugins?view=skills');
    expect(screen.getByLabelText('app-center current page')).toHaveTextContent('/plugins?view=skills');
    const shortcuts = screen.getByLabelText('桌面 App');
    fireEvent.keyDown(within(shortcuts).getByRole('button', { name: 'Agent' }), { key: 'Enter' });
    expect(window.location.hash).toBe('#/agent');

    if (entry === 'desktop') {
      fireEvent.keyDown(within(shortcuts).getByRole('button', { name: 'App Center' }), { key: 'Enter' });
    } else {
      fireEvent.click(screen.getByRole('button', { name: '打开全部 App' }));
      fireEvent.click(within(screen.getByRole('dialog', { name: '全部 App' })).getByRole('button', { name: /App Center/ }));
    }
    expect(screen.getByLabelText('app-center current page')).toHaveTextContent('/plugins?view=skills');
    expect(window.location.hash).toBe('#/plugins?view=skills');
    const reloadRoute = window.location.hash.slice(1);
    page.unmount();
    renderDesktop('app-center', transport, reloadRoute);
    expect(screen.getByLabelText('app-center current page')).toHaveTextContent('/plugins?view=skills');
  });

  it('opens and closes the App launcher with the advertised keyboard shortcut', () => {
    renderDesktop();

    fireEvent.keyDown(window, { key: 'k', metaKey: true });
    expect(screen.getByRole('dialog', { name: '全部 App' })).toBeInTheDocument();

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByRole('dialog', { name: '全部 App' })).not.toBeInTheDocument();
  });

  it('keeps keyboard focus inside Launchpad and returns it to the opener', () => {
    renderDesktop();
    const opener = screen.getByRole('button', { name: '打开全部 App' });
    opener.focus();
    fireEvent.click(opener);

    const launcher = screen.getByRole('dialog', { name: '全部 App' });
    expect(within(launcher).getByRole('searchbox', { name: '搜索 App' })).toHaveFocus();
    fireEvent.keyDown(launcher, { key: 'Tab', shiftKey: true });
    expect(within(launcher).getAllByRole('button').at(-1)).toHaveFocus();

    fireEvent.keyDown(launcher, { key: 'Escape' });
    expect(screen.queryByRole('dialog', { name: '全部 App' })).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it('closes the launcher after opening an App', () => {
    renderDesktop();

    fireEvent.click(screen.getByRole('button', { name: '全部 App' }));
    const launcher = screen.getByRole('dialog', { name: '全部 App' });
    fireEvent.click(launcher.querySelector<HTMLButtonElement>('[data-app="agent"]')!);

    expect(screen.queryByRole('dialog', { name: '全部 App' })).not.toBeInTheDocument();
  });

  it('renders every registered App in Launchpad with its original SVG identity', () => {
    const { container } = renderDesktop();
    fireEvent.click(screen.getByRole('button', { name: '全部 App' }));
    const launcher = screen.getByRole('dialog', { name: '全部 App' });

    for (const app of pawApps) {
      const appButton = launcher.querySelector<HTMLButtonElement>(`[data-app="${app.id}"]`)!;
      expect(appButton).toBeInTheDocument();
      const icon = appButton.querySelector(`[data-paw-app-icon="${app.id}"]`);
      expect(icon).toBeInTheDocument();
      expect(icon).toHaveAttribute('aria-hidden', 'true');
    }
    expect(container.querySelectorAll('.paw-launchpad [data-paw-app-icon]')).toHaveLength(pawApps.length);
    expect(launcher.querySelector('.paw-os-app-icon, .paw-app-glyph')).toBeNull();
    expect(launcher.querySelector('[data-lucide]')).toBeNull();
  });

  it('pairs the current App label with the same original identity in the top bar', () => {
    renderDesktop('agent');

    expect(screen.getByRole('button', { name: '打开全部 App' })).toHaveTextContent('PAW');
    expect(document.querySelector('[data-paw-product-version]')).toHaveTextContent('v0.1.0');
    expect(document.querySelector('[data-paw-product-version]')).toHaveAttribute(
      'title',
      expect.stringContaining('构建 dev'),
    );
    expect(document.querySelector('.paw-menu-bar > strong')).toBeNull();
    const current = document.querySelector('.paw-menu-app') as HTMLElement;
    expect(current).toHaveTextContent('Agent');
    expect(current.querySelector('[data-paw-app-icon="agent"]')).toHaveAttribute('aria-hidden', 'true');
    expect(current.querySelector('[data-lucide], .paw-os-app-icon, .paw-app-glyph')).toBeNull();
  });

  it('names the menu bar 桌面 when no window is focused instead of borrowing Workbench', () => {
    renderDesktop();
    const current = document.querySelector('.paw-menu-app') as HTMLElement;
    expect(current).toHaveAttribute('data-idle');
    expect(current).toHaveTextContent('桌面');
    expect(current.querySelector('[data-paw-app-icon]')).toBeNull();
  });

  it('opens hide and overview commands from the menu bar App name', () => {
    renderDesktop('agent');
    fireEvent.click(screen.getByRole('button', { name: 'Agent 菜单' }));
    expect(screen.getByRole('menu', { name: 'Agent 菜单' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('menuitem', { name: '隐藏窗口' }));
    expect(document.querySelector('[data-paw-window-id="agent"]')).toBeNull();
    const agentDock = within(screen.getByRole('navigation', { name: 'PAWOS 工具架' })).getByRole('button', { name: 'Agent' });
    expect(agentDock).toHaveAttribute('data-open');
    expect(agentDock).toHaveAttribute('data-minimized');
  });

  it('hides the Dock only while the active App window is maximized', () => {
    renderDesktop('agent');
    expect(screen.getByRole('navigation', { name: 'PAWOS 工具架' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '最大化窗口' }));
    expect(screen.queryByRole('navigation', { name: 'PAWOS 工具架' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '还原窗口' }));
    expect(screen.getByRole('navigation', { name: 'PAWOS 工具架' })).toBeInTheDocument();
  });

  it('pins a desktop App by dropping it on the Dock and unpins it by dragging it back out', () => {
    renderDesktop();
    const dock = screen.getByRole('navigation', { name: 'PAWOS 工具架' });
    const settings = document.querySelector<HTMLElement>('[data-desktop-app="system-settings"]')!;
    expect(within(dock).queryByRole('button', { name: 'System Settings' })).not.toBeInTheDocument();

    const intoDock = dragTransfer();
    fireEvent.dragStart(settings, { dataTransfer: intoDock });
    fireEvent.dragOver(dock, { dataTransfer: intoDock });
    fireEvent.drop(dock, { dataTransfer: intoDock });
    const pinned = within(dock).getByRole('button', { name: 'System Settings' });
    expect(pinned).toHaveAttribute('draggable', 'true');

    const outOfDock = dragTransfer();
    fireEvent.dragStart(pinned, { dataTransfer: outOfDock });
    fireEvent.drop(screen.getByLabelText('项目场'), { clientX: 260, clientY: 220, dataTransfer: outOfDock });
    expect(within(dock).queryByRole('button', { name: 'System Settings' })).not.toBeInTheDocument();
    expect(document.querySelector('[data-paw-window-id="system-settings"]')).toBeNull();
  });

  it('filters Launchpad Apps from the archive search field', () => {
    renderDesktop();
    fireEvent.click(screen.getByRole('button', { name: '全部 App' }));
    const launcher = screen.getByRole('dialog', { name: '全部 App' });
    fireEvent.change(within(launcher).getByRole('searchbox', { name: '搜索 App' }), { target: { value: 'Terminal' } });
    expect(within(launcher).getByRole('button', { name: /Terminal/ })).toBeInTheDocument();
    expect(within(launcher).queryByRole('button', { name: /Agent/ })).not.toBeInTheDocument();
    expect(within(launcher).getByRole('heading', { name: '工具' })).toBeInTheDocument();
  });

  it('groups the Launchpad archive the way the machine is laid out', () => {
    renderDesktop();
    fireEvent.click(screen.getByRole('button', { name: '全部 App' }));
    const launcher = screen.getByRole('dialog', { name: '全部 App' });

    // Where work happens, where what came out of it is kept, what the work
    // reaches for, what runs underneath. Every App lands in exactly one band,
    // so the Apps read as one full shelf instead of lone tiles on rows of
    // their own.
    const bands = new Map<string, string[]>();
    let current = '';
    for (const node of launcher.querySelectorAll('.paw-launchpad-group, .paw-launchpad [data-app]')) {
      if (node.classList.contains('paw-launchpad-group')) {
        current = node.textContent ?? '';
        bands.set(current, []);
      } else {
        bands.get(current)?.push(node.getAttribute('data-app') ?? '');
      }
    }
    expect([...bands.keys()]).toEqual(['工作', '记忆与知识', '工具', '系统']);
    expect(bands.get('工作')).toEqual([
      'agent',
      'eval-lab',
      'project-workbench',
      'agent-capsule',
      ...pawApps.filter((app) => isPawExtensionAppId(app.id) && app.kind === 'agent').map((app) => app.id),
    ]);
    expect(bands.get('工具')).toEqual(expect.arrayContaining(
      pawApps.filter((app) => isPawExtensionAppId(app.id) && app.kind !== 'agent').map((app) => app.id),
    ));
    expect(bands.get('记忆与知识')).toEqual(['memory', 'knowledge']);
    expect(bands.get('系统')).toEqual(['system-monitor', 'system-settings']);
    expect([...bands.values()].flat()).toHaveLength(pawApps.length);
  });

  it('gives every Launchpad group header its own cascade beat ahead of its tiles', () => {
    renderDesktop();
    fireEvent.click(screen.getByRole('button', { name: '全部 App' }));
    const launcher = screen.getByRole('dialog', { name: '全部 App' });

    const beats = Array.from(launcher.querySelectorAll<HTMLElement>('[style*="--paw-tile-i"]'))
      .map((element) => ({
        header: element.classList.contains('paw-launchpad-group'),
        beat: Number.parseInt(element.style.getPropertyValue('--paw-tile-i'), 10),
      }));
    expect(beats.length).toBeGreaterThan(pawApps.length);
    // One shared clock in document order: header, then its tiles, then the
    // next header — every element knows exactly one beat.
    beats.forEach(({ beat }, index) => expect(beat).toBe(index));
    expect(beats[0]?.header).toBe(true);
    expect(beats.filter(({ header }) => header).length).toBeGreaterThanOrEqual(3);
  });

  it('puts every registered App on the desktop', () => {
    renderDesktop();
    const shortcuts = screen.getByLabelText('桌面 App');
    const rows = within(shortcuts).getAllByRole('button');
    // One compact row per identity — label only, no taglines or marketing
    // copy competing with the fog field.
    const builtinApps = pawApps.filter((app) => !isPawExtensionAppId(app.id));
    expect(rows.map((row) => row.dataset.desktopApp).sort()).toEqual(
      builtinApps.map((app) => app.id).sort(),
    );
    for (const app of builtinApps) {
      expect(rows.find((row) => row.dataset.desktopApp === app.id)!).toHaveTextContent(app.label);
    }
    expect(rows).toHaveLength(builtinApps.length);
    for (const row of rows) {
      expect(row.querySelector('[data-paw-app-icon]')).toBeInTheDocument();
    }
  });

  it('projects an enabled Extension App onto the desktop and Dock', async () => {
    const extension = pawExtensionApps[0]!;
    const transport = new MockControlTransport({ routes: {
      'agent.extensions.list': {
        ok: true,
        runtimeAvailable: true,
        items: [{
          id: extension.packageId,
          version: extension.version,
          installed: true,
          enabled: true,
          capabilities: [`pawos.extension.binding.${extension.bindingSha256.slice(0, 40)}`],
          extensionApp: {
            id: extension.id,
            packageId: extension.packageId,
            version: extension.version,
            bindingSha256: extension.bindingSha256,
            bindingCapability: `pawos.extension.binding.${extension.bindingSha256.slice(0, 40)}`,
            skillRef: extension.skillRef,
            skillSha256: extension.skillSha256,
            verticalSuiteId: extension.verticalSuiteId,
            verticalSuiteRevision: extension.verticalSuiteRevision,
            sandbox: extension.sandbox,
          },
        }],
      },
    } });
    window.localStorage.setItem('pawos.desktop.v1', JSON.stringify({
      windows: {},
      stack: [],
      activeWindowId: null,
      dockAppIds: [extension.id],
      wayfinder: { layoutVersion: 3, iconPositions: {}, archived: [], projectAssignments: {} },
    }));

    renderDesktop(undefined, transport);

    await waitFor(() => expect(document.querySelector(`[data-desktop-app="${extension.id}"]`)).toBeInTheDocument());
    expect(within(screen.getByRole('navigation', { name: 'PAWOS 工具架' }))
      .getByRole('button', { name: extension.label })).toBeInTheDocument();
  });

  it('keeps a closed Extension App closed across background inventory refreshes until the user opens it', async () => {
    vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
      matches: true,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })));
    const extension = pawExtensionApps[0]!;
    const inventory = {
      ok: true,
      runtimeAvailable: true,
      items: [{
        id: extension.packageId,
        version: extension.version,
        installed: true,
        enabled: true,
        capabilities: [`pawos.extension.binding.${extension.bindingSha256.slice(0, 40)}`],
        extensionApp: {
          id: extension.id,
          packageId: extension.packageId,
          version: extension.version,
          bindingSha256: extension.bindingSha256,
          bindingCapability: `pawos.extension.binding.${extension.bindingSha256.slice(0, 40)}`,
          skillRef: extension.skillRef,
          skillSha256: extension.skillSha256,
          verticalSuiteId: extension.verticalSuiteId,
          verticalSuiteRevision: extension.verticalSuiteRevision,
          sandbox: extension.sandbox,
        },
      }],
    };
    let requestCount = 0;
    let finishRefresh: (() => void) | undefined;
    const transport = new MockControlTransport({ routes: {
      'agent.extensions.list': async () => {
        requestCount += 1;
        if (requestCount > 1) await new Promise<void>((resolve) => { finishRefresh = resolve; });
        return inventory;
      },
    } });
    window.location.hash = extension.route;

    renderDesktop(extension.id, transport);

    await waitFor(() => expect(document.querySelector(`[data-paw-window-id="${extension.id}"]`)).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: '关闭窗口' }));
    expect(document.querySelector(`[data-paw-window-id="${extension.id}"]`)).toBeNull();

    act(() => window.dispatchEvent(new Event(PAW_EXTENSION_INSTALLATION_CHANGED_EVENT)));
    await waitFor(() => expect(transport.requests.filter(({ request }) => request.pathId === 'agent.extensions.list')).toHaveLength(2));
    await act(async () => finishRefresh?.());
    expect(document.querySelector(`[data-paw-window-id="${extension.id}"]`)).toBeNull();

    fireEvent.doubleClick(document.querySelector(`[data-desktop-app="${extension.id}"]`)!);
    expect(document.querySelector(`[data-paw-window-id="${extension.id}"]`)).toBeInTheDocument();
  });

  it('removes disabled Extension App windows and Dock entries while keeping core Apps', async () => {
    const extension = pawExtensionApps[0]!;
    const transport = new MockControlTransport({ routes: {
      'agent.extensions.list': {
        ok: true,
        runtimeAvailable: true,
        items: [{ id: extension.packageId, installed: true, enabled: false }],
      },
    } });
    window.localStorage.setItem('pawos.desktop.v1', JSON.stringify({
      windows: {
        [extension.id]: {
          id: extension.id,
          appId: extension.id,
          title: extension.label,
          minimized: false,
          bounds: { x: 80, y: 80, width: 640, height: 480 },
        },
      },
      stack: [extension.id],
      activeWindowId: extension.id,
      dockAppIds: [extension.id],
      wayfinder: { layoutVersion: 3, iconPositions: {}, archived: [], projectAssignments: {} },
    }));

    renderDesktop(undefined, transport);

    await waitFor(() => {
      expect(document.querySelector(`[data-desktop-app="${extension.id}"]`)).toBeNull();
      expect(document.querySelector(`[data-paw-window-id="${extension.id}"]`)).toBeNull();
    });
    expect(document.querySelector('[data-desktop-app="agent"]')).toBeInTheDocument();
    expect(within(screen.getByRole('navigation', { name: 'PAWOS 工具架' }))
      .queryByRole('button', { name: extension.label })).toBeNull();
  });

  it('keeps an uninstalled Extension App in Launchpad and routes it to App Center', async () => {
    const extension = pawExtensionApps[0]!;
    const transport = new MockControlTransport({ routes: {
      'agent.extensions.list': { ok: true, runtimeAvailable: true, items: [] },
    } });
    renderDesktop(undefined, transport);

    fireEvent.click(screen.getByRole('button', { name: '全部 App' }));
    const launcher = screen.getByRole('dialog', { name: '全部 App' });
    const candidate = within(launcher).getByRole('button', { name: new RegExp(extension.label) });
    await waitFor(() => expect(candidate).toHaveAttribute('data-extension-installation', 'uninstalled'));

    fireEvent.click(candidate);

    expect(screen.queryByRole('dialog', { name: '全部 App' })).not.toBeInTheDocument();
    await waitFor(() => expect(document.querySelector('[data-paw-window-id="app-center"]')).toBeInTheDocument());
    await waitFor(() => {
      const snapshot = JSON.parse(window.localStorage.getItem('pawos.desktop.v1') ?? '{}') as {
        windows?: Record<string, { initialRoute?: string }>;
      };
      expect(snapshot.windows?.['app-center']?.initialRoute)
        .toBe(`/plugins?packageId=${encodeURIComponent(extension.packageId)}`);
    });
  });

  it('labels an installed Extension App with stale binding evidence as needing an update', async () => {
    const extension = pawExtensionApps[0]!;
    const transport = new MockControlTransport({ routes: {
      'agent.extensions.list': {
        ok: true,
        runtimeAvailable: true,
        items: [{ id: extension.packageId, version: '0.0.0', installed: true, enabled: true }],
      },
    } });
    renderDesktop(undefined, transport);

    fireEvent.click(screen.getByRole('button', { name: '全部 App' }));
    const launcher = screen.getByRole('dialog', { name: '全部 App' });
    const candidate = within(launcher).getByRole('button', { name: new RegExp(extension.label) });
    await waitFor(() => expect(candidate).toHaveAttribute('data-extension-installation', 'update-required'));
    expect(within(candidate).getByText('需要更新')).toBeInTheDocument();
  });

  it('fails closed for an Extension App deep link when Runtime is unavailable', async () => {
    const extension = pawExtensionApps[0]!;
    const transport = new MockControlTransport({ routes: {
      'agent.extensions.list': { ok: true, runtimeAvailable: false, items: [{ id: extension.packageId, installed: true, enabled: true }] },
    } });

    renderDesktop(extension.id, transport);

    await waitFor(() => {
      expect(document.querySelector(`[data-paw-window-id="${extension.id}"]`)).toBeNull();
      expect(document.querySelector(`[data-desktop-app="${extension.id}"]`)).toBeNull();
    });
  });

  it('projects the Dock running language onto Wayfinder rows without renaming them', () => {
    // Reduced motion lets minimize complete synchronously, as in the Dock test.
    vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
      matches: true,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })));
    renderDesktop('agent');
    const shortcuts = screen.getByLabelText('桌面 App');
    const agentRow = within(shortcuts).getByRole('button', { name: 'Agent' });
    expect(agentRow).toHaveAttribute('data-open');
    expect(agentRow).not.toHaveAttribute('data-minimized');
    expect(agentRow).toHaveAttribute('aria-description', '运行中');
    expect(within(shortcuts).getByRole('button', { name: 'Files' })).not.toHaveAttribute('data-open');

    fireEvent.click(screen.getByRole('button', { name: '最小化窗口' }));
    expect(agentRow).toHaveAttribute('data-open');
    expect(agentRow).toHaveAttribute('data-minimized');
    expect(agentRow).toHaveAttribute('aria-description', '已最小化，按回车打开');
    // The description carries state while the stable accessible name remains
    // the bare App label used by voice control and exact-name queries.
    expect(agentRow).toHaveAttribute('title', 'Agent · 已最小化');
  });

  it('walks the visible desktop grid spatially with roving arrow keys', () => {
    const originalWidth = window.innerWidth;
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 390 });
    try {
      renderDesktop();
      const shortcuts = screen.getByLabelText('桌面 App');
      const buttons = within(shortcuts).getAllByRole('button');
      expect(buttons.length).toBeGreaterThanOrEqual(5);

      buttons[0]!.focus();
      fireEvent.keyDown(shortcuts, { key: 'ArrowDown' });
      expect(document.activeElement).toBe(buttons[3]);
      fireEvent.keyDown(shortcuts, { key: 'ArrowRight' });
      expect(document.activeElement).toBe(buttons[4]);
      fireEvent.keyDown(shortcuts, { key: 'ArrowUp' });
      expect(document.activeElement).toBe(buttons[1]);
      fireEvent.keyDown(shortcuts, { key: 'ArrowLeft' });
      expect(document.activeElement).toBe(buttons[0]);
      fireEvent.keyDown(shortcuts, { key: 'End' });
      expect(document.activeElement).toBe(buttons.at(-1));
    } finally {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: originalWidth });
    }
  });

  it('follows actual icon coordinates after Apps have been rearranged', () => {
    renderDesktop();
    const shortcuts = screen.getByLabelText('桌面 App');
    const agent = within(shortcuts).getByRole('button', { name: 'Agent' });
    const memory = within(shortcuts).getByRole('button', { name: 'Memory' });
    const browser = within(shortcuts).getByRole('button', { name: 'Browser' });
    for (const button of within(shortcuts).getAllByRole('button')) {
      Object.defineProperty(button, 'getBoundingClientRect', {
        configurable: true,
        value: () => domRect(600, 600, 80, 72),
      });
    }
    Object.defineProperty(agent, 'getBoundingClientRect', { configurable: true, value: () => domRect(160, 80, 80, 72) });
    Object.defineProperty(memory, 'getBoundingClientRect', { configurable: true, value: () => domRect(40, 80, 80, 72) });
    Object.defineProperty(browser, 'getBoundingClientRect', { configurable: true, value: () => domRect(280, 80, 80, 72) });

    agent.focus();
    fireEvent.keyDown(shortcuts, { key: 'ArrowLeft' });
    expect(memory).toHaveFocus();
    agent.focus();
    fireEvent.keyDown(shortcuts, { key: 'ArrowRight' });
    expect(browser).toHaveFocus();
  });

  it('moves a focused App by one free grid cell with Alt+Arrow', async () => {
    window.localStorage.setItem('pawos.desktop.v1', JSON.stringify({
      windows: {}, stack: [], activeWindowId: null,
      wayfinder: {
        layoutVersion: 3,
        iconPositions: { 'app:agent': { x: 24, y: 720 } },
        archived: [], projectAssignments: {},
      },
    }));
    renderDesktop();
    const shortcuts = screen.getByLabelText('桌面 App');
    const agent = within(shortcuts).getByRole('button', { name: 'Agent' });
    agent.focus();

    fireEvent.keyDown(shortcuts, { key: 'ArrowRight', altKey: true });

    expect(agent).toHaveFocus();
    await waitFor(() => {
      const snapshot = JSON.parse(window.localStorage.getItem('pawos.desktop.v1') ?? '{}') as {
        wayfinder?: { iconPositions?: Record<string, { x: number; y: number }> };
      };
      expect(snapshot.wayfinder?.iconPositions?.['app:agent']).toEqual({ x: 136, y: 720 });
    });
  });

  it('opens System Settings from the advertised keyboard shortcut', () => {
    renderDesktop();
    fireEvent.keyDown(window, { key: ',', metaKey: true });
    expect(document.querySelector('[data-paw-window-id="system-settings"]')).toBeInTheDocument();
  });

  it('replaces the browser context menu with desktop and App commands', () => {
    renderDesktop();

    const desktop = screen.getByRole('main');
    const desktopMenu = new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: 120, clientY: 90 });
    fireEvent(desktop, desktopMenu);

    expect(desktopMenu.defaultPrevented).toBe(true);
    expect(screen.getByRole('menu', { name: '桌面菜单' })).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: '新建 Agent 工作' })).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: '打开 Browser' })).toBeInTheDocument();

    const shortcuts = screen.getByLabelText('桌面 App');
    fireEvent.contextMenu(within(shortcuts).getByRole('button', { name: 'Agent' }), { clientX: 240, clientY: 180 });
    expect(screen.getByRole('menu', { name: 'Agent 菜单' })).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: '打开 Agent' })).toBeInTheDocument();
  });

  it('removes an App shortcut from the desktop without uninstalling it and restores it from the desktop menu', async () => {
    renderDesktop();

    const shortcuts = screen.getByLabelText('桌面 App');
    const agent = within(shortcuts).getByRole('button', { name: 'Agent' });
    fireEvent.contextMenu(agent, { clientX: 240, clientY: 180 });
    fireEvent.click(screen.getByRole('menuitem', { name: '从桌面移除 Agent' }));

    await waitFor(() => expect(within(shortcuts).queryByRole('button', { name: 'Agent' })).not.toBeInTheDocument());
    await waitFor(() => expect(JSON.parse(window.localStorage.getItem('pawos.desktop.v1') ?? '{}').wayfinder?.archived).toContain('app:agent'));

    fireEvent.click(screen.getByRole('button', { name: '打开全部 App' }));
    const launchpad = screen.getByRole('dialog', { name: '全部 App' });
    expect(launchpad.querySelector('[data-app="agent"]')).toBeInTheDocument();
    fireEvent.click(within(launchpad).getByRole('button', { name: '完成' }));

    fireEvent.contextMenu(screen.getByRole('main'), { clientX: 120, clientY: 90 });
    fireEvent.click(screen.getByRole('menuitem', { name: '恢复 1 个桌面图标' }));
    await waitFor(() => expect(within(shortcuts).getByRole('button', { name: 'Agent' })).toBeInTheDocument());
  });

  it('returns focus to the context-menu opener after Escape', async () => {
    renderDesktop();
    const agent = within(screen.getByLabelText('桌面 App')).getByRole('button', { name: 'Agent' });
    agent.focus();
    fireEvent.contextMenu(agent, { clientX: 240, clientY: 180 });
    const menu = screen.getByRole('menu', { name: 'Agent 菜单' });
    expect(within(menu).getByRole('menuitem', { name: '打开 Agent' })).toHaveFocus();

    fireEvent.keyDown(menu, { key: 'Escape' });

    await waitFor(() => expect(agent).toHaveFocus());
  });

  it('puts Overview and Launchpad first on a compact Dock and exposes the dialog relationship', () => {
    vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
      matches: query === '(max-width: 820px)',
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })));
    renderDesktop();
    const dock = screen.getByRole('navigation', { name: 'PAWOS 工具架' });
    const buttons = Array.from(dock.children).filter((child): child is HTMLButtonElement => child instanceof HTMLButtonElement);

    expect(buttons.slice(0, 2).map((button) => button.getAttribute('aria-label'))).toEqual(['窗口总览', '全部 App']);
    expect(buttons[1]).toHaveAttribute('aria-haspopup', 'dialog');
    expect(buttons[1]).toHaveAttribute('aria-controls', 'paw-launchpad-dialog');
    expect(buttons[1]).toHaveAttribute('aria-expanded', 'false');
  });

  it('arranges previously dragged desktop icons from the desktop context menu', async () => {
    window.localStorage.setItem('pawos.desktop.v1', JSON.stringify({
      windows: {},
      stack: [],
      activeWindowId: null,
      wayfinder: {
        layoutVersion: 3,
        iconPositions: { 'app:agent': { x: 731, y: 418 } },
        archived: [],
        projectAssignments: {},
      },
    }));
    renderDesktop();

    fireEvent.contextMenu(screen.getByRole('main'), { clientX: 120, clientY: 90 });
    fireEvent.click(screen.getByRole('menuitem', { name: '整理图标' }));

    await waitFor(() => {
      const snapshot = JSON.parse(window.localStorage.getItem('pawos.desktop.v1') ?? '{}') as {
        wayfinder?: { iconPositions?: Record<string, unknown> };
      };
      expect(snapshot.wayfinder?.iconPositions).toEqual({});
    });
  });

  it('repairs persisted desktop coordinates onto visible non-overlapping grid cells', async () => {
    const originalWidth = window.innerWidth;
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 420 });
    window.localStorage.setItem('pawos.desktop.v1', JSON.stringify({
      windows: {},
      stack: [],
      activeWindowId: null,
      wayfinder: {
        layoutVersion: 3,
        iconPositions: {
          'app:agent': { x: 731, y: 418 },
          'app:memory': { x: 731, y: 418 },
        },
        archived: [],
        projectAssignments: {},
      },
    }));
    try {
      renderDesktop();
      await waitFor(() => {
        const snapshot = JSON.parse(window.localStorage.getItem('pawos.desktop.v1') ?? '{}') as {
          wayfinder?: { iconPositions?: Record<string, { x: number; y: number }> };
        };
        const agent = snapshot.wayfinder?.iconPositions?.['app:agent'];
        const memory = snapshot.wayfinder?.iconPositions?.['app:memory'];
        expect(agent).toBeTruthy();
        expect(memory).toBeTruthy();
        expect(agent).not.toEqual(memory);
        expect(agent!.x + 96).toBeLessThanOrEqual(420);
        expect(memory!.x + 96).toBeLessThanOrEqual(420);
        expect((agent!.x - 24) % 112).toBe(0);
        expect((memory!.x - 24) % 112).toBe(0);
      });
    } finally {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: originalWidth });
    }
  });

  it('clears the Dock drop target when a non-App desktop file is rejected', () => {
    renderDesktop();
    const dock = screen.getByRole('navigation', { name: 'PAWOS 工具架' });
    const transfer = dragTransfer();
    transfer.setData('application/x-paw-wayfinder-icon', 'session:s1');
    transfer.setData('text/plain', 'session:s1');

    fireEvent.dragOver(dock, { dataTransfer: transfer });
    expect(dock).toHaveAttribute('data-drop-target');
    fireEvent.drop(dock, { dataTransfer: transfer });
    expect(dock).not.toHaveAttribute('data-drop-target');
  });

  it('keeps the desktop visible while inspecting a project and offers immediate archive undo', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.sessions.list': { ok: true, items: [{
        id: 'session-archive',
        title: '待归档对话',
        mode: 'assistant',
        status: 'idle',
        roleId: 'default',
        roleVersion: '1',
        roleBookRevisionId: 'r1',
        updatedAtMs: 20,
        workspaceRoots: ['/work/paw'],
      }] },
      'agent.rooms.list': { ok: true, items: [] },
      'agent.memoryMaintenance.run': { ok: true, projection: { running: false } },
    } });
    renderDesktop(undefined, transport);
    const folder = await waitFor(() => {
      const element = document.querySelector<HTMLElement>('[data-wayfinder-project]');
      expect(element).toBeTruthy();
      return element!;
    }, { timeout: 2_500 });

    fireEvent.contextMenu(folder, { clientX: 220, clientY: 160 });
    const sheet = screen.getByRole('dialog', { name: /paw.*项目详情/i });
    expect(screen.queryByRole('menu', { name: /paw.*菜单/i })).not.toBeInTheDocument();
    expect(folder).toBeVisible();
    expect(folder.closest('[data-wayfinder-canvas]')).not.toHaveAttribute('hidden');
    fireEvent.click(within(sheet).getByRole('button', { name: /移到归档 paw/ }));
    await waitFor(() => expect(document.querySelector('[data-wayfinder-project]')).toBeNull());
    expect(screen.getByRole('main')).toHaveFocus();
    expect(screen.getByRole('status', { name: '桌面移除结果' })).toHaveTextContent('已移到归档：paw');
    expect(screen.getByRole('button', { name: '撤销移除 paw' })).toHaveAttribute('aria-keyshortcuts', 'Meta+Z Control+Z');
    fireEvent.click(screen.getByRole('button', { name: '关闭移除提示' }));
    expect(screen.queryByRole('status', { name: '桌面移除结果' })).not.toBeInTheDocument();

    fireEvent.keyDown(document, { key: 'z', metaKey: true });
    await waitFor(() => expect(document.querySelector('[data-wayfinder-project]')).toBeInTheDocument());
    expect(screen.queryByRole('status', { name: '桌面移除结果' })).not.toBeInTheDocument();
  });

  it('routes Dock right-click to the App menu and closes every window for that App', () => {
    renderDesktop('agent');
    const dock = screen.getByRole('navigation', { name: 'PAWOS 工具架' });
    const agentDockButton = within(dock).getByRole('button', { name: 'Agent' });

    fireEvent.contextMenu(agentDockButton, { clientX: 240, clientY: 680 });

    expect(screen.getByRole('menu', { name: 'Agent 菜单' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('menuitem', { name: '关闭 Agent 的全部窗口' }));
    expect(document.querySelector('[data-paw-window-id="agent"]')).toBeNull();
  });

  it('routes an expanded project dialogue row to its own work context menu', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.sessions.list': { ok: true, items: [{
        id: 'session-row-menu', title: '行内对话', mode: 'assistant', status: 'idle', roleId: 'default', roleVersion: '1', roleBookRevisionId: 'r1', updatedAtMs: Date.now(), workspaceRoots: ['/work/paw'],
      }] },
      'agent.rooms.list': { ok: true, items: [] },
      'agent.memoryMaintenance.run': { ok: true, projection: { running: false } },
    } });
    renderDesktop(undefined, transport);
    const folder = await waitFor(() => {
      const element = document.querySelector<HTMLElement>('[data-wayfinder-project]');
      expect(element).toBeTruthy();
      return element!;
    }, { timeout: 2_500 });
    fireEvent.doubleClick(folder);
    const row = await waitFor(() => {
      const element = document.querySelector<HTMLButtonElement>('button[data-wayfinder-row]');
      expect(element).toBeTruthy();
      return element!;
    });

    fireEvent.contextMenu(row, { clientX: 260, clientY: 210 });

    expect(screen.getByRole('menu', { name: /行内对话.*菜单/ })).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: /移到归档 行内对话/ })).toBeInTheDocument();
  });

  it('marks a Dock App that only has minimized windows and restores it on click', () => {
    // Prefer reduced-motion so minimize completes synchronously for Dock state.
    vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
      matches: true,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })));
    renderDesktop('agent');
    const dock = screen.getByRole('navigation', { name: 'PAWOS 工具架' });
    const agentDockButton = within(dock).getByRole('button', { name: 'Agent' });
    expect(agentDockButton).toHaveAttribute('aria-description', '运行中');
    expect(agentDockButton).toHaveAttribute('data-open');
    expect(agentDockButton).not.toHaveAttribute('data-minimized');

    fireEvent.click(screen.getByRole('button', { name: '最小化窗口' }));
    expect(agentDockButton).toHaveAttribute('data-open');
    expect(agentDockButton).toHaveAttribute('data-minimized');
    expect(agentDockButton).toHaveAttribute('aria-description', '已最小化，按回车恢复');
    expect(agentDockButton).toHaveAttribute('title', 'Agent 已最小化，点击恢复');

    fireEvent.click(agentDockButton);
    expect(agentDockButton).not.toHaveAttribute('data-minimized');
    expect(document.querySelector('[data-paw-window-id="agent"]')).toBeInTheDocument();
  });

  it('gives every Dock control a hover name label that stays out of the accessible name', () => {
    renderDesktop('agent');
    const dock = screen.getByRole('navigation', { name: 'PAWOS 工具架' });

    // The exact-name queries above only stay stable if the visual label is
    // aria-hidden; the button keeps its aria-label as the accessible name.
    const agentTip = within(dock).getByRole('button', { name: 'Agent' }).querySelector('.paw-dock-tip');
    expect(agentTip).toHaveAttribute('aria-hidden', 'true');
    expect(agentTip).toHaveTextContent('Agent');
    expect(within(dock).getByRole('button', { name: '窗口总览' }).querySelector('.paw-dock-tip')).toHaveTextContent('窗口总览');
    expect(within(dock).getByRole('button', { name: '全部 App' }).querySelector('.paw-dock-tip')).toHaveTextContent('全部 App');
  });

  it('closes all PAWOS windows from the desktop menu in one projection-only action', () => {
    renderDesktop('agent');
    const desktop = screen.getByRole('main');

    fireEvent.contextMenu(desktop, { clientX: 120, clientY: 90 });
    fireEvent.click(screen.getByRole('menuitem', { name: '关闭全部窗口' }));

    expect(document.querySelectorAll('[data-paw-window-id]')).toHaveLength(0);
  });

  it('routes a Browser host command into PAWOS before the Browser App is mounted', async () => {
    let commandListener: ((command: { action: 'new_tab'; commandId: string; url: string }) => void) | undefined;
    window.pawBrowserHost = desktopBrowserHost((listener) => {
      commandListener = listener;
      return () => { commandListener = undefined; };
    });
    renderDesktop();

    act(() => commandListener?.({
      action: 'new_tab',
      commandId: 'command-1',
      url: 'https://inside.example',
    }));

    expect(document.querySelector('[data-paw-window-id="browser"]')).toBeInTheDocument();
    // Persistence lands one debounce beat after the interaction.
    await waitFor(() => {
      const snapshot = JSON.parse(window.localStorage.getItem('pawos.desktop.v1') ?? '{}') as {
        windows?: Record<string, { target?: { commandId?: string; url?: string } }>;
      };
      expect(snapshot.windows?.browser?.target).toMatchObject({
        commandId: 'command-1',
        url: 'https://inside.example',
      });
    });
  });

  it('pauses the ambient wallpaper while an App window owns focus and resumes on an empty desktop', () => {
    // Reduced motion lets minimize complete synchronously, as in the Dock test.
    vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
      matches: true,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })));
    renderDesktop('agent');
    const desktop = document.querySelector('.paw-desktop') as HTMLElement;
    // A focused window covers the field: every weather clock freezes.
    expect(desktop).toHaveAttribute('data-ambient-paused');

    fireEvent.click(screen.getByRole('button', { name: '最小化窗口' }));
    // The bare desktop is the only surface anyone can watch the scenery on.
    expect(desktop).not.toHaveAttribute('data-ambient-paused');
  });

  it('pauses the ambient wallpaper under the Launchpad veil and while the document is hidden', () => {
    renderDesktop();
    const desktop = document.querySelector('.paw-desktop') as HTMLElement;
    expect(desktop).not.toHaveAttribute('data-ambient-paused');

    // The Launchpad's full-screen backdrop blur must never re-blur a stepping
    // wallpaper underneath it.
    fireEvent.keyDown(window, { key: 'k', metaKey: true });
    expect(desktop).toHaveAttribute('data-ambient-paused');
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(desktop).not.toHaveAttribute('data-ambient-paused');

    try {
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
      fireEvent(document, new Event('visibilitychange'));
      expect(desktop).toHaveAttribute('data-ambient-paused');
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' });
      fireEvent(document, new Event('visibilitychange'));
      expect(desktop).not.toHaveAttribute('data-ambient-paused');
    } finally {
      delete (document as unknown as Record<string, unknown>).visibilityState;
    }
  });

  it('never subscribes shell chrome to the whole windows record', () => {
    // Bounds commits after every drag/resize/viewport refit and runtime
    // title/target binds replace state.windows; menu bar, Wayfinder, Dock and
    // Launchpad must not re-render on that churn. Menus subscribe to the
    // structural signature (id, App, placement) instead, and the half-minute
    // clock tick owns its own leaf so it re-renders one <span>.
    expect(desktopSource).not.toMatch(/usePawDesktopStore\(\(state\) => state\.windows\)/);
    expect(desktopSource).toContain('const menuSignature = usePawDesktopStore');
    expect(desktopSource).toContain('function PawMenuClock()');
    expect(desktopSource).toMatch(/<PawMenuClock \/>/);
  });

  it('still reflects placement changes in the window menu through the structural signature', () => {
    renderDesktop('agent');
    const windowShell = document.querySelector('[data-paw-window-id="agent"]') as HTMLElement;
    fireEvent.contextMenu(windowShell, { clientX: 300, clientY: 200 });
    fireEvent.click(screen.getByRole('menuitem', { name: '最大化' }));

    fireEvent.contextMenu(windowShell, { clientX: 300, clientY: 200 });
    expect(screen.getByRole('menuitem', { name: '还原窗口' })).toBeInTheDocument();
  });

  it('coalesces desktop persistence into one trailing write instead of one per mutation', async () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem');
    renderDesktop();
    const desktopViewport = document.querySelector<HTMLElement>('.paw-desktop-viewport')!;

    // A burst of desktop mutations: open two Apps back to back.
    fireEvent.keyDown(window, { key: ',', metaKey: true });
    fireEvent.contextMenu(desktopViewport, { clientX: 120, clientY: 90 });
    fireEvent.click(screen.getByRole('menuitem', { name: '新建 Agent 工作' }));

    // Nothing serializes on the interaction path itself…
    const writesBefore = setItem.mock.calls.filter(([key]) => key === 'pawos.desktop.v1');
    expect(writesBefore).toHaveLength(0);

    // …and the whole burst lands as one trailing snapshot.
    await waitFor(() => {
      expect(setItem.mock.calls.filter(([key]) => key === 'pawos.desktop.v1')).toHaveLength(1);
    });
    const snapshot = JSON.parse(window.localStorage.getItem('pawos.desktop.v1') ?? '{}') as {
      windows?: Record<string, unknown>;
    };
    expect(Object.keys(snapshot.windows ?? {})).toEqual(expect.arrayContaining(['system-settings', 'agent']));
    setItem.mockRestore();
  });

  it.each(['menuitem', 'presentation'])('does not prevent or capture pointer presses from a body Portal (%s) in the desktop React tree', (role) => {
    pointerFixture.role = role;
    pointerFixture.portalled = true;
    renderDesktop();
    const viewport = screen.getByRole('main');
    const target = screen.getByText('Pointer target');
    const capture = vi.fn();
    Object.defineProperty(viewport, 'setPointerCapture', { value: capture });
    expect(document.body).toContainElement(target);
    expect(viewport).not.toContainElement(target);

    const accepted = fireEvent.pointerDown(target, { button: 0, pointerId: 8, bubbles: true, cancelable: true });
    fireEvent.pointerUp(window, { pointerId: 8 });

    expect({ prevented: !accepted, pointerCaptureCalls: capture.mock.calls }).toEqual({ prevented: false, pointerCaptureCalls: [] });
    expect(screen.queryByTestId('paw-selection-lasso')).not.toBeInTheDocument();
  });

  it.each(['menuitem', 'listbox', 'dialog'])('does not start a lasso from an inline %s interaction', (role) => {
    pointerFixture.role = role;
    renderDesktop();
    const viewport = screen.getByRole('main');
    const target = screen.getByText('Pointer target');
    const capture = vi.fn();
    Object.defineProperty(viewport, 'setPointerCapture', { value: capture });
    expect(viewport).toContainElement(target);

    const accepted = fireEvent.pointerDown(target, { button: 0, pointerId: 9, bubbles: true, cancelable: true });
    fireEvent.pointerUp(window, { pointerId: 9 });

    expect({ prevented: !accepted, pointerCaptureCalls: capture.mock.calls }).toEqual({ prevented: false, pointerCaptureCalls: [] });
    expect(screen.queryByTestId('paw-selection-lasso')).not.toBeInTheDocument();
  });

  it('selects desktop Apps with a lasso instead of webpage text selection', () => {
    const frames: FrameRequestCallback[] = [];
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      frames.push(callback);
      return frames.length;
    });
    vi.stubGlobal('cancelAnimationFrame', (handle: number) => {
      frames[handle - 1] = () => undefined;
    });
    renderDesktop();
    const viewport = screen.getByRole('main');
    const shortcuts = screen.getByLabelText('桌面 App');
    const agent = within(shortcuts).getByRole('button', { name: 'Agent' });
    const browser = within(shortcuts).getByRole('button', { name: 'Browser' });
    Object.defineProperty(viewport, 'getBoundingClientRect', {
      value: () => domRect(0, 0, 900, 700),
    });
    Object.defineProperty(agent, 'getBoundingClientRect', {
      value: () => domRect(680, 80, 78, 70),
    });
    Object.defineProperty(browser, 'getBoundingClientRect', {
      value: () => domRect(680, 180, 78, 70),
    });

    fireEvent.pointerDown(viewport, { button: 0, clientX: 650, clientY: 50, pointerId: 4 });
    fireEvent.pointerMove(window, { clientX: 790, clientY: 270, pointerId: 4 });
    act(() => {
      const queued = frames.splice(0);
      queued.forEach((callback) => callback(performance.now()));
    });

    expect(screen.getByTestId('paw-selection-lasso')).toBeInTheDocument();
    expect(agent).toHaveAttribute('aria-pressed', 'true');
    expect(browser).toHaveAttribute('aria-pressed', 'true');

    fireEvent.pointerUp(window, { clientX: 790, clientY: 270, pointerId: 4 });
    expect(screen.queryByTestId('paw-selection-lasso')).not.toBeInTheDocument();
  });

  it('opts the magnetic Dock out while a window gesture owns the pointer', () => {
    expect(desktopSource).toContain('windowGestureOwnsPointer');
    expect(desktopSource).toMatch(/dataset\.windowInteraction/);
    expect(desktopSource).toMatch(/requestAnimationFrame\(apply\)/);
  });

  it('measures desktop identities once per lasso instead of on every frame', () => {
    const frames: FrameRequestCallback[] = [];
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      frames.push(callback);
      return frames.length;
    });
    vi.stubGlobal('cancelAnimationFrame', (handle: number) => {
      frames[handle - 1] = () => undefined;
    });
    renderDesktop();
    const viewport = screen.getByRole('main');
    const shortcuts = screen.getByLabelText('桌面 App');
    const agent = within(shortcuts).getByRole('button', { name: 'Agent' });
    let measurements = 0;
    Object.defineProperty(viewport, 'getBoundingClientRect', { value: () => domRect(0, 0, 900, 700) });
    Object.defineProperty(agent, 'getBoundingClientRect', {
      value: () => {
        measurements += 1;
        return domRect(680, 80, 78, 70);
      },
    });

    fireEvent.pointerDown(viewport, { button: 0, clientX: 650, clientY: 50, pointerId: 9 });
    expect(measurements).toBe(1);

    for (const clientY of [180, 260, 340]) {
      fireEvent.pointerMove(window, { clientX: 790, clientY, pointerId: 9 });
      act(() => {
        frames.splice(0).forEach((callback) => callback(performance.now()));
      });
    }

    // Identities cannot move while the band is drawn, so the gesture reads
    // their boxes exactly once — no forced layout per sampled frame.
    expect(measurements).toBe(1);
    expect(agent).toHaveAttribute('aria-pressed', 'true');
    fireEvent.pointerUp(window, { clientX: 790, clientY: 340, pointerId: 9 });
  });

  it('keeps the Wayfinder, its work panel and the Dock out of desktop re-renders', () => {
    expect(desktopSource).toMatch(/const Wayfinder = memo\(function Wayfinder/);
    expect(desktopSource).toMatch(/const PawDock = memo\(function PawDock/);
    expect(wayfinderWorkSource).toMatch(/export const PawWayfinderWork = memo\(function PawWayfinderWork/);
    // Memo only pays off when the props are stable identities.
    expect(desktopSource).toMatch(/const openApp = useCallback\(/);
    expect(desktopSource).toMatch(/const toggleLaunchpad = useCallback\(/);
    expect(desktopSource).toMatch(/const toggleOverview = useCallback\(/);
    // …and when a lasso frame that crosses no new identity keeps its Set.
    expect(desktopSource).toMatch(/sameIconSelection\(current, next\) \? current : next/);
  });
});

function renderDesktop(initialAppId?: PawAppId, transport = new MockControlTransport(), initialRoute?: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <ControlTransportProvider transport={transport}>
        <ThemeProvider>
          <MotionProvider>
            <GlobalFeedbackProvider>
              <PawDesktopProvider initialAppId={initialAppId} initialRoute={initialRoute}>
                <PawDesktop />
              </PawDesktopProvider>
            </GlobalFeedbackProvider>
          </MotionProvider>
        </ThemeProvider>
      </ControlTransportProvider>
    </QueryClientProvider>,
  );
}

function domRect(x: number, y: number, width: number, height: number): DOMRect {
  return {
    x,
    y,
    width,
    height,
    top: y,
    right: x + width,
    bottom: y + height,
    left: x,
    toJSON: () => ({}),
  };
}

function dragTransfer(): DataTransfer {
  const values = new Map<string, string>();
  return {
    clearData: (format?: string) => {
      if (format) values.delete(format);
      else values.clear();
    },
    dropEffect: 'move',
    effectAllowed: 'move',
    files: [] as unknown as FileList,
    getData: (format: string) => values.get(format) ?? '',
    items: [] as unknown as DataTransferItemList,
    setData: (format: string, value: string) => values.set(format, value),
    setDragImage: () => undefined,
    get types() { return [...values.keys()]; },
  } as unknown as DataTransfer;
}

function restoreElementMethod(name: 'animate' | 'getAnimations', descriptor?: PropertyDescriptor): void {
  if (descriptor) Object.defineProperty(Element.prototype, name, descriptor);
  else delete (Element.prototype as unknown as Record<string, unknown>)[name];
}

function desktopBrowserHost(
  onCommand: NonNullable<typeof window.pawBrowserHost>['onCommand'],
): NonNullable<typeof window.pawBrowserHost> {
  return {
    kind: 'electron-webview',
    partition: 'persist:paw-browser',
    activate: () => undefined,
    clearBrowsingData: async (action) => ({ action, after: 0, before: 0, completedAt: 1 }),
    clearHistory: async () => [],
    getHistory: async () => [],
    getSettings: async () => ({ cacheBytes: 0, cookieCount: 0, downloadPath: '/tmp', extensionCount: 0, extensionsPath: '/tmp/Extensions', partition: 'persist:paw-browser', permissionMode: 'site-request', startPage: 'about:blank' }),
    listExtensions: async () => [],
    loadUnpackedExtension: async () => null,
    openExtensionsFolder: async () => ({ opened: true, path: '/tmp/Extensions' }),
    openDownloads: async () => ({ opened: true, path: '/tmp' }),
    register: () => undefined,
    removeExtension: async () => [],
    removeHistoryEntry: async () => [],
    setStartPage: async (startPage) => ({ startPage }),
    takeScreenshot: async () => ({ path: '/tmp/page.png', saved: true }),
    onCommand,
    onGuestClosed: () => () => undefined,
    onHistoryChanged: () => () => undefined,
    onOpenUrl: () => () => undefined,
    onSelectTab: () => () => undefined,
  };
}
