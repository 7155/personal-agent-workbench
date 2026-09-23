import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { MockControlTransport, type MockControlTransportOptions } from '@/test/mock-transport';
import type { ControlRequest } from '@/platform/transport';
import { PawDesktopProvider } from '../runtime/desktop-context';
import {
  PawWayfinderWork,
  WAYFINDER_DRAG_MIME,
  placeWayfinderProjectPanel,
} from './PawWayfinderWork';
import { PawWorkDirectoryProvider } from './PawWorkDirectory';

/* The desktop panel is a projection, not a manager: it must fold the raw
 * directory to desktop density, keep its own scroll, and hand every click to
 * the Agent window. These tests drive it through the same transport seam the
 * desktop uses, with the exact clone/repeat shapes that once flooded the
 * screen. */

vi.mock('./PawProjectGalaxyScene', () => ({
  PawProjectGalaxyScene: ({ model, onPick }: { model: import('./project-galaxy-model').ProjectGalaxyModel; onPick(id: string): void }) => <div>{model.bodies.map((body) => <button key={body.id} onClick={() => onPick(body.id)}>{body.title}，{body.subtitle}</button>)}</div>,
}));

const NOW = Date.now();

beforeEach(() => window.localStorage.clear());
afterEach(() => cleanup());

describe('PawWayfinderWork', () => {
  it('opens the folder list by default and offers the galaxy explicitly', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.sessions.list': { ok: true, items: [sessionRecord('s-galaxy', '星系中的工作')] },
      'agent.rooms.list': { ok: true, items: [] },
    } });
    render(<ControlTransportProvider transport={transport}><PawDesktopProvider>
      <PawWorkDirectoryProvider initialPollDelayMs={0} pollIntervalMs={60_000}><PawWayfinderWork /></PawWorkDirectoryProvider>
    </PawDesktopProvider></ControlTransportProvider>);
    const panel = await screen.findByRole('region', { name: '最近工作' });
    await waitFor(() => expect(panel.querySelector('[data-wayfinder-project]')).toBeTruthy());
    fireEvent.doubleClick(panel.querySelector('[data-wayfinder-project]')!);
    expect(await screen.findByRole('dialog', { name: '未绑定项目 项目窗口' })).toBeInTheDocument();
    expect(screen.queryByRole('dialog', { name: '未绑定项目 · 项目星系' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '全屏星系' }));
    const galaxy = await screen.findByRole('dialog', { name: '未绑定项目 · 项目星系' });
    expect(within(galaxy).getByRole('button', { name: '星系中的工作，就绪' })).toBeInTheDocument();
    fireEvent.click(within(galaxy).getByRole('button', { name: '项目 docs' }));
    expect(await within(galaxy).findByText('尚未绑定工作区')).toBeInTheDocument();
    expect(transport.requests.some(({ request }) => request.pathId.startsWith('agent.session.workspace.'))).toBe(false);
    fireEvent.click(within(galaxy).getByRole('button', { name: '文字列表' }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '未绑定项目 · 项目星系' })).not.toBeInTheDocument());
    expect(screen.getByRole('dialog', { name: '未绑定项目 项目窗口' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '全屏星系' }));
    const reopened = await screen.findByRole('dialog', { name: '未绑定项目 · 项目星系' });
    expect(within(reopened).getByRole('button', { name: '星系中的工作，就绪' })).toBeInTheDocument();
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.sessions.create')).toBe(false);
  });
  it('preserves archived folders and assignments when removing the access-root identity', async () => {
    const legacy = '/\u001f/work/paw';
    window.localStorage.setItem('pawos.desktop.v1', JSON.stringify({
      wayfinder: {
        layoutVersion: 3,
        archived: [`project:${legacy}`],
        iconPositions: { [`project:${legacy}`]: { x: 136, y: 140 } },
        projectAssignments: { 'session:moved': legacy },
      },
    }));
    renderPanel({ routes: {
      'agent.sessions.list': { ok: true, items: [
        sessionRecord('a', '同一项目', { workspaceRoots: ['/work/paw', '/'] }),
      ] },
      'agent.rooms.list': { ok: true, items: [] },
    } });
    await waitFor(() => {
      const saved = JSON.parse(window.localStorage.getItem('pawos.desktop.v1')!);
      expect(saved.wayfinder.archived).toEqual(['project:/work/paw']);
      expect(saved.wayfinder.projectAssignments['session:moved']).toBe('/work/paw');
      expect(saved.wayfinder.iconPositions[`project:${legacy}`]).toBeUndefined();
    });
    expect(screen.getByRole('region', { name: '最近工作' }).querySelector('[data-project-folder]')).toBeNull();
  });

  it('does not show a Room row when only partner Sessions exist', async () => {
    renderPanel({
      routes: {
        'agent.sessions.list': { ok: true, items: [
          sessionRecord('s-1', '迁移作战室 · Agent 1', { roomParticipant: { roomId: 'room-9', participantId: 'p1', status: 'active' } }),
          sessionRecord('s-2', '迁移作战室 · Agent 2', { roomParticipant: { roomId: 'room-9', participantId: 'p2', status: 'active' } }),
          sessionRecord('s-3', '迁移作战室 · Agent 3', { roomParticipant: { roomId: 'room-9', participantId: 'p3', status: 'active' } }),
        ] },
        'agent.rooms.list': { ok: true, items: [] },
      },
    });

    const panel = await screen.findByRole('region', { name: '最近工作' });
    expect(within(panel).getByText('还没有工作记录')).toBeInTheDocument();
    expect(within(panel).queryByText('迁移作战室 · Agent 1')).not.toBeInTheDocument();
  });

  it('collapses repeated goal copy and reveals older runs only on demand', async () => {
    renderPanel({
      routes: {
        'agent.sessions.list': { ok: true, items: [
          sessionRecord('s-new', '重构 Wayfinder 列表', { updatedAtMs: NOW - 60_000 }),
          sessionRecord('s-old', '重构 Wayfinder 列表', { updatedAtMs: NOW - 3_600_000 }),
        ] },
        'agent.rooms.list': { ok: true, items: [] },
      },
    });

    const panel = await screen.findByRole('region', { name: '最近工作' });
    await openProjectFolder(panel);
    expect(await screen.findAllByRole('button', { name: /重构 Wayfinder 列表/ })).toHaveLength(1);
    expect(screen.queryByRole('button', { name: /较早一段/ })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '同名工作还有 1 段' }));
    expect(screen.getByRole('button', { name: /较早一段/ })).toBeInTheDocument();
  });

  it('keeps desktop files directly on the plane without a project-dashboard toolbar', async () => {
    renderPanel({
      routes: {
        'agent.sessions.list': { ok: true, items: [
          sessionRecord('s-1', '重构列表'),
          sessionRecord('s-2', '整理知识库'),
        ] },
        'agent.rooms.list': { ok: true, items: [] },
      },
    });

    const panel = await screen.findByRole('region', { name: '最近工作' });
    await waitFor(() => expect(panel.querySelector('[data-project-folder]')).toBeInTheDocument());
    expect(panel.querySelector('[data-project-folder][open]')).toBeNull();
    await openProjectFolder(panel);
    expect(within(panel).getByRole('button', { name: /重构列表/ })).toBeInTheDocument();
    expect(within(panel).getByRole('button', { name: /整理知识库/ })).toBeInTheDocument();
    expect(panel.querySelectorAll('[data-dialogue-file] .paw-work-glyph__tile')).toHaveLength(2);
    expect(within(panel).queryByRole('searchbox', { name: '搜索最近工作' })).not.toBeInTheDocument();
    expect(within(panel).queryByText('项目桌面')).not.toBeInTheDocument();
    expect(panel.querySelector('[data-wayfinder-archive]')).toBeNull();
  });

  it('reveals the rest of a busy project through one clearly named list control', async () => {
    renderPanel({
      routes: {
        'agent.sessions.list': {
          ok: true,
          items: Array.from({ length: 7 }, (_, index) => sessionRecord(
            `session-${index}`,
            `对话 ${index + 1}`,
            { updatedAtMs: NOW - index * 1_000 },
          )),
        },
        'agent.rooms.list': { ok: true, items: [] },
      },
    });

    const panel = await screen.findByRole('region', { name: '最近工作' });
    await openProjectFolder(panel);
    expect(panel.querySelectorAll('[data-dialogue-file]')).toHaveLength(5);

    const reveal = within(panel).getByRole('button', { name: '显示其余 2 个对话' });
    expect(reveal).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(reveal);

    expect(panel.querySelectorAll('[data-dialogue-file]')).toHaveLength(7);
    expect(within(panel).queryByRole('button', { name: '显示其余 2 个对话' })).not.toBeInTheDocument();
  });

  it('rests the 更早 bucket collapsed so stale history never fills the first screen', async () => {
    renderPanel({
      routes: {
        'agent.sessions.list': { ok: true, items: [
          sessionRecord('s-now', '今天的工作'),
          sessionRecord('s-old-1', '上个月的工作 A', { updatedAtMs: NOW - 30 * 86_400_000 }),
          sessionRecord('s-old-2', '上个月的工作 B', { updatedAtMs: NOW - 31 * 86_400_000 }),
        ] },
        'agent.rooms.list': { ok: true, items: [] },
      },
    });

    const panel = await screen.findByRole('region', { name: '最近工作' });
    await openProjectFolder(panel);
    await screen.findByRole('button', { name: /今天的工作/ });
    expect(screen.queryByRole('button', { name: /上个月的工作/ })).not.toBeInTheDocument();

    const stale = screen.getByRole('button', { name: /更早/ });
    expect(stale).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(stale);
    expect(screen.getAllByRole('button', { name: /上个月的工作/ })).toHaveLength(2);
  });

  it('shows a bounded preview when a project contains only older conversations', async () => {
    renderPanel({ routes: {
      'agent.sessions.list': { ok: true, items: Array.from({ length: 7 }, (_, i) =>
        sessionRecord(`old-${i}`, `历史对话 ${i}`, { updatedAtMs: NOW - (30 + i) * 86_400_000 })) },
      'agent.rooms.list': { ok: true, items: [] },
    } });
    const panel = await screen.findByRole('region', { name: '最近工作' });
    await openProjectFolder(panel);
    expect(panel.querySelectorAll('[data-dialogue-file]')).toHaveLength(5);
    fireEvent.click(within(panel).getByRole('button', { name: '显示其余 2 个对话' }));
    expect(panel.querySelectorAll('[data-dialogue-file]')).toHaveLength(7);
  });

  it('opens a row straight into the Agent window projection', async () => {
    renderPanel({
      routes: {
        'agent.sessions.list': { ok: true, items: [sessionRecord('s-42', '继续修复投影')] },
        'agent.rooms.list': { ok: true, items: [] },
      },
    });

    const panel = await screen.findByRole('region', { name: '最近工作' });
    await openProjectFolder(panel);
    fireEvent.click(await screen.findByRole('button', { name: /继续修复投影/ }));

    // Desktop persistence is a trailing debounce, so the snapshot lands one
    // beat after the interaction instead of inside it.
    await waitFor(() => {
      const snapshot = JSON.parse(window.localStorage.getItem('pawos.desktop.v1') ?? '{}') as {
        windows?: Record<string, { appId?: string; initialRoute?: string; target?: { kind?: string; id?: string } }>;
      };
      expect(snapshot.windows?.['agent:s-42']).toMatchObject({
        appId: 'agent',
        initialRoute: '/agent?session=s-42',
        target: { kind: 'session', id: 's-42' },
      });
    });
  });

  it('groups a project in place, shows public progress, and opens the canonical Room', async () => {
    renderPanel({
      routes: {
        'agent.sessions.list': { ok: true, items: [sessionRecord('s-1', 'Trace 地基', {
          status: 'busy',
          lastMessagePreview: '正在建立 Trace 关联',
          workspaceRoots: ['/work/paw'],
        })] },
        'agent.rooms.list': { ok: true, items: [{
          id: 'room-1', title: '协作检查', status: 'active', updatedAtMs: NOW,
          workspaceRoots: ['/work/paw'], participants: [{ displayName: 'Builder', ordinal: 1 }],
          workItems: [{ state: 'blocked', blocker: { reason: '等待沙盒授权' }, updatedAtMs: NOW }],
        }] },
      },
    });

    const panel = await screen.findByRole('region', { name: '最近工作' });
    await openProjectFolder(panel);
    expect(within(panel).getAllByText('paw').length).toBeGreaterThanOrEqual(1);
    expect(within(panel).getByText('当前公开内容：正在建立 Trace 关联')).toBeInTheDocument();
    expect(within(panel).getByText('阻塞：等待沙盒授权')).toBeInTheDocument();

    fireEvent.click(within(panel).getByRole('button', { name: /协作检查/ }));
    await waitFor(() => {
      const snapshot = JSON.parse(window.localStorage.getItem('pawos.desktop.v1') ?? '{}') as {
        windows?: Record<string, { initialRoute?: string; target?: { kind?: string; id?: string } }>;
      };
      expect(snapshot.windows?.['agent:room-1']).toMatchObject({
        initialRoute: '/agent?room=room-1',
        target: { kind: 'room', id: 'room-1' },
      });
    });
  });

  it('keeps only one project folder expanded so content panels cannot overlap', async () => {
    renderPanel({
      routes: {
        'agent.sessions.list': { ok: true, items: [
          sessionRecord('s-paw', 'PAW 工作', { workspaceRoots: ['/work/paw'] }),
          sessionRecord('s-learn', 'LearnA 工作', { workspaceRoots: ['/work/learnA'] }),
        ] },
        'agent.rooms.list': { ok: true, items: [] },
      },
    });

    const panel = await screen.findByRole('region', { name: '最近工作' });
    await waitFor(() => expect(panel.querySelectorAll('[data-project-folder]')).toHaveLength(2));
    const folders = panel.querySelectorAll<HTMLDetailsElement>('[data-project-folder]');
    expect(folders).toHaveLength(2);
    expect(panel.querySelectorAll<HTMLDetailsElement>('[data-project-folder][open]')).toHaveLength(0);

    fireEvent.keyDown(folders[0]!.querySelector('summary')!, { key: ' ' });
      expect(panel.querySelectorAll<HTMLDetailsElement>('[data-project-folder][open]')).toHaveLength(1);
    fireEvent.keyDown(folders[1]!.querySelector('summary')!, { key: 'Enter' });
      expect(panel.querySelectorAll<HTMLDetailsElement>('[data-project-folder][open]')).toHaveLength(1);
    expect(folders[1]).toHaveAttribute('open');
    expect(folders[0]).not.toHaveAttribute('open');
  });

  it('walks the freely positioned desktop icons in all four spatial directions', async () => {
    renderPanel({
      routes: {
        'agent.sessions.list': { ok: true, items: [
          sessionRecord('s-a', '项目 A', { workspaceRoots: ['/work/a'] }),
          sessionRecord('s-b', '项目 B', { workspaceRoots: ['/work/b'] }),
          sessionRecord('s-c', '项目 C', { workspaceRoots: ['/work/c'] }),
          sessionRecord('s-d', '项目 D', { workspaceRoots: ['/work/d'] }),
        ] },
        'agent.rooms.list': { ok: true, items: [] },
      },
    });

    const panel = await screen.findByRole('region', { name: '最近工作' });
    await waitFor(() => expect(panel.querySelectorAll('[data-wayfinder-project]')).toHaveLength(4));
    const canvas = panel.querySelector('[data-wayfinder-canvas]') as HTMLElement;
    const icons = Array.from(panel.querySelectorAll<HTMLElement>('[data-wayfinder-project]'));
    const rects = [
      domRect(20, 20, 80, 80),
      domRect(140, 20, 80, 80),
      domRect(20, 140, 80, 80),
      domRect(140, 140, 80, 80),
    ];
    icons.forEach((icon, index) => Object.defineProperty(icon, 'getBoundingClientRect', {
      configurable: true,
      value: () => rects[index],
    }));

    icons[0]!.focus();
    fireEvent.keyDown(canvas, { key: 'ArrowRight' });
    expect(document.activeElement).toBe(icons[1]);
    fireEvent.keyDown(canvas, { key: 'ArrowDown' });
    expect(document.activeElement).toBe(icons[3]);
    fireEvent.keyDown(canvas, { key: 'ArrowLeft' });
    expect(document.activeElement).toBe(icons[2]);
    fireEvent.keyDown(canvas, { key: 'ArrowUp' });
    expect(document.activeElement).toBe(icons[0]);
  });

  it('names simultaneous running and attention state without hiding either fact', async () => {
    renderPanel({
      routes: {
        'agent.sessions.list': { ok: true, items: [sessionRecord('partner-1', '协作修复 · Agent 1', {
          status: 'busy',
          roomParticipant: { roomId: 'room-mixed' },
          workspaceRoots: ['/work/paw'],
        })] },
        'agent.rooms.list': { ok: true, items: [{
          id: 'room-mixed',
          title: '协作修复',
          status: 'active',
          updatedAtMs: NOW,
          workspaceRoots: ['/work/paw'],
          participants: [],
          workItems: [{ state: 'blocked', blocker: { reason: '等待确认' }, updatedAtMs: NOW }],
        }] },
      },
    });

    const panel = await screen.findByRole('region', { name: '最近工作' });
    const state = await within(panel).findByText('运行 1 · 待处理 1');
    expect(state).toHaveAccessibleName('1 个进行中 · 1 个需处理');
  });

  it('flips and clamps an expanded project panel inside the canvas at the right-bottom edge', () => {
    const canvas = domRect(100, 80, 800, 600);
    const shell = domRect(780, 560, 96, 92);
    const placement = placeWayfinderProjectPanel({
      canvasRect: canvas,
      shellRect: shell,
      panelWidth: 520,
      panelHeight: 400,
    });

    expect(placement.horizontal).toBe('left');
    expect(placement.vertical).toBe('above');
    const panelLeft = shell.left + placement.x;
    const panelTop = shell.top + placement.y;
    expect(panelLeft).toBeGreaterThanOrEqual(canvas.left + 8);
    expect(panelLeft + 520).toBeLessThanOrEqual(canvas.right - 8);
    expect(panelTop).toBeGreaterThanOrEqual(canvas.top + 8);
    expect(panelTop + 400).toBeLessThanOrEqual(canvas.bottom - 8);
  });

  it('reserves the resident Dock when clamping an expanded project panel', () => {
    const canvas = domRect(0, 34, 390, 810);
    const shell = domRect(248, 522, 96, 86);
    const dockInset = 74;
    const placement = placeWayfinderProjectPanel({
      canvasRect: canvas,
      shellRect: shell,
      panelWidth: 366,
      panelHeight: 555,
      bottomInset: dockInset,
    });

    const panelTop = shell.top + placement.y;
    expect(panelTop).toBeGreaterThanOrEqual(canvas.top + 8);
    expect(panelTop + 555).toBeLessThanOrEqual(canvas.bottom - dockInset - 8);
  });

  it('re-measures the open project window on resize and writes an in-place flipped placement', async () => {
    renderPanel({
      routes: {
        'agent.sessions.list': { ok: true, items: [sessionRecord('s-edge', '右下角项目', { workspaceRoots: ['/work/edge'] })] },
        'agent.rooms.list': { ok: true, items: [] },
      },
    });

    const panel = await screen.findByRole('region', { name: '最近工作' });
    await openProjectFolder(panel);
    const canvas = panel.querySelector('[data-wayfinder-canvas]') as HTMLDivElement;
    const shell = panel.querySelector('[data-project-folder]')?.parentElement as HTMLDivElement;
    const projectWindow = within(panel).getByRole('dialog', { name: 'edge 项目窗口' });
    Object.defineProperty(canvas, 'getBoundingClientRect', {
      configurable: true,
      value: () => domRect(100, 80, 800, 600),
    });
    Object.defineProperty(shell, 'getBoundingClientRect', {
      configurable: true,
      value: () => domRect(780, 560, 96, 92),
    });
    Object.defineProperty(projectWindow, 'offsetWidth', { configurable: true, value: 520 });
    Object.defineProperty(projectWindow, 'offsetHeight', { configurable: true, value: 400 });
    Object.defineProperty(projectWindow, 'getBoundingClientRect', {
      configurable: true,
      value: () => domRect(780, 662, 520, 400),
    });

    fireEvent(window, new Event('resize'));

    await waitFor(() => {
      expect(shell.style.getPropertyValue('--wayfinder-panel-x')).toBe('-424px');
      expect(shell.style.getPropertyValue('--wayfinder-panel-y')).toBe('-410px');
    });
  });

  it('opens a non-modal project context sheet with real roots, status and canonical shortcuts', async () => {
    renderPanel({
      routes: {
        'agent.sessions.list': { ok: true, items: [sessionRecord('s-context', 'Trace 地基', {
          status: 'busy',
          workspaceRoots: ['/work/paw', '/work/shared'],
        })] },
        'agent.rooms.list': { ok: true, items: [{
          id: 'room-context', title: '协作检查', status: 'active', updatedAtMs: NOW,
          workspaceRoots: ['/work/shared', '/work/paw'],
          participants: [{ displayName: 'Builder', ordinal: 1 }],
          workItems: [{ state: 'blocked', blocker: { reason: '等待沙盒授权' }, updatedAtMs: NOW }],
        }] },
      },
    });

    const panel = await screen.findByRole('region', { name: '最近工作' });
    const contextTrigger = panel.querySelector<HTMLElement>('[data-wayfinder-project]')!;
    fireEvent.contextMenu(contextTrigger);

    const sheet = within(panel).getByRole('dialog', { name: 'paw 项目详情' });
    expect(sheet).toHaveAttribute('aria-modal', 'false');
    expect(panel.querySelector('[data-wayfinder-canvas]')).not.toHaveAttribute('hidden');
    expect(contextTrigger).toBeVisible();
    expect(within(sheet).getByText('/work/paw')).toBeInTheDocument();
    expect(within(sheet).getByText('/work/shared')).toBeInTheDocument();
    expect(within(sheet).getByText('Session')).toBeInTheDocument();
    expect(within(sheet).getByText('Room')).toBeInTheDocument();
    expect(within(sheet).getByText('协作检查')).toBeInTheDocument();
    expect(within(sheet).getByText(/等待沙盒授权/)).toBeInTheDocument();
    expect(within(sheet).queryByText('Builder')).not.toBeInTheDocument();
    expect(within(sheet).getByText(/不会创建 Finder\/Git 文件/)).toBeInTheDocument();

    fireEvent.click(within(sheet).getByRole('button', { name: /Trace 地基/ }));
    await waitFor(() => {
      const snapshot = JSON.parse(window.localStorage.getItem('pawos.desktop.v1') ?? '{}') as {
        windows?: Record<string, { initialRoute?: string; target?: { kind?: string; id?: string } }>;
      };
      expect(snapshot.windows?.['agent:s-context']).toMatchObject({
        initialRoute: '/agent?session=s-context',
        target: { kind: 'session', id: 's-context' },
      });
    });

    fireEvent.click(within(sheet).getByRole('button', { name: '系统设置' }));
    fireEvent.click(within(sheet).getByRole('button', { name: 'Trace / Eval' }));
    await waitFor(() => {
      const snapshot = JSON.parse(window.localStorage.getItem('pawos.desktop.v1') ?? '{}') as {
        windows?: Record<string, { appId?: string; initialRoute?: string }>;
      };
      expect(snapshot.windows?.['system-settings']).toMatchObject({ appId: 'system-settings', initialRoute: '/configuration' });
      expect(snapshot.windows?.['system-monitor']).toMatchObject({ appId: 'system-monitor', initialRoute: '/observability' });
    });

    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => expect(within(panel).queryByRole('dialog', { name: 'paw 项目详情' })).not.toBeInTheDocument());
    expect(document.activeElement).toBe(contextTrigger);
  });

  it('opens a project workspace root in the Files app from the context sheet', async () => {
    renderPanel({
      routes: {
        'agent.sessions.list': { ok: true, items: [sessionRecord('s-files', 'Trace 地基', {
          workspaceRoots: ['/work/paw'],
        })] },
        'agent.rooms.list': { ok: true, items: [] },
      },
    });

    const panel = await screen.findByRole('region', { name: '最近工作' });
    fireEvent.contextMenu(panel.querySelector<HTMLElement>('[data-wayfinder-project]')!);

    const sheet = within(panel).getByRole('dialog', { name: 'paw 项目详情' });
    fireEvent.click(within(sheet).getByRole('button', { name: '在 Files 中打开 /work/paw' }));

    await waitFor(() => {
      const snapshot = JSON.parse(window.localStorage.getItem('pawos.desktop.v1') ?? '{}') as {
        windows?: Record<string, { appId?: string; initialRoute?: string }>;
      };
      expect(snapshot.windows?.['files:s-files:/work/paw']).toMatchObject({
        appId: 'files',
        initialRoute: `/files?session=s-files&path=${encodeURIComponent('/work/paw')}`,
      });
    });
  });

  it('keeps room-only workspace roots as plain paths without a Files action', async () => {
    renderPanel({
      routes: {
        'agent.sessions.list': { ok: true, items: [] },
        'agent.rooms.list': { ok: true, items: [{
          id: 'room-only', title: '协作检查', status: 'active', updatedAtMs: NOW,
          workspaceRoots: ['/work/room'],
          participants: [{ displayName: 'Builder', ordinal: 1 }],
          workItems: [],
        }] },
      },
    });

    const panel = await screen.findByRole('region', { name: '最近工作' });
    fireEvent.contextMenu(panel.querySelector<HTMLElement>('[data-wayfinder-project]')!);

    const sheet = within(panel).getByRole('dialog', { name: 'room 项目详情' });
    expect(within(sheet).getByText('/work/room')).toBeInTheDocument();
    expect(within(sheet).queryByRole('button', { name: /在 Files 中打开/ })).not.toBeInTheDocument();
  });

  it('keeps the project context action operable after the folder is folded', async () => {
    renderPanel({
      routes: {
        'agent.sessions.list': { ok: true, items: [sessionRecord('s-folded', '折叠后仍可见', { workspaceRoots: ['/work/paw'] })] },
        'agent.rooms.list': { ok: true, items: [] },
      },
    });

    const panel = await screen.findByRole('region', { name: '最近工作' });
    const folder = panel.querySelector('[data-project-folder]');
    const contextTrigger = folder!.querySelector<HTMLElement>('summary')!;
    expect(folder).not.toBeNull();

    fireEvent.click(folder!.querySelector('summary')!);
    expect(folder).not.toHaveAttribute('open');
    fireEvent.keyDown(contextTrigger, { key: 'F10', shiftKey: true });

    expect(within(panel).getByRole('dialog', { name: 'paw 项目详情' })).toBeInTheDocument();
    expect(folder).not.toHaveAttribute('open');
  });

  it('keeps a deliberately folded project closed on the direct desktop plane', async () => {
    renderPanel({
      routes: {
        'agent.sessions.list': { ok: true, items: [sessionRecord('s-1', 'Trace 地基', {
          workspaceRoots: ['/work/paw'],
        })] },
        'agent.rooms.list': { ok: true, items: [] },
      },
    });

    const panel = await screen.findByRole('region', { name: '最近工作' });
    const folder = panel.querySelector('[data-project-folder]');
    expect(folder).not.toBeNull();
    fireEvent.click(folder!.querySelector('summary')!);
    expect(folder).not.toHaveAttribute('open');

    expect(folder).not.toHaveAttribute('open');
    expect(within(panel).queryByText('Trace 地基')).not.toBeInTheDocument();
  });

  it('moves a project folder on the desktop coordinate plane', async () => {
    renderPanel({
      routes: {
        'agent.sessions.list': { ok: true, items: [sessionRecord('s-drag', '一个需要完整显示的长对话名称', { workspaceRoots: ['/work/paw'] })] },
        'agent.rooms.list': { ok: true, items: [] },
      },
    });

    const panel = await screen.findByRole('region', { name: '最近工作' });
    await openProjectFolder(panel);
    const folder = panel.querySelector('[data-project-folder]') as HTMLDetailsElement;
    const project = panel.querySelector('[data-wayfinder-project]') as HTMLElement;
    const row = panel.querySelector('[data-dialogue-file]') as HTMLButtonElement;
    const canvas = panel.querySelector('[data-wayfinder-canvas]') as HTMLDivElement;

    expect(project).toHaveAttribute('draggable', 'true');
    expect(project).toHaveAttribute('title', 'paw · 1 个对话\n/work/paw');
    expect(row).toHaveAttribute('draggable', 'true');
    expect(row).toHaveAttribute('title', expect.stringContaining('一个需要完整显示的长对话名称'));
    expect(folder).toHaveAttribute('open');

    Object.defineProperty(canvas, 'clientWidth', { configurable: true, value: 800 });
    Object.defineProperty(canvas, 'clientHeight', { configurable: true, value: 600 });
    Object.defineProperty(canvas, 'scrollWidth', { configurable: true, value: 800 });
    Object.defineProperty(canvas, 'scrollHeight', { configurable: true, value: 600 });
    Object.defineProperty(canvas, 'getBoundingClientRect', {
      configurable: true,
      value: () => domRect(10, 20, 800, 600),
    });

    const projectTransfer = dragTransfer();
    fireEvent.dragStart(project, { dataTransfer: projectTransfer });
    fireDrop(canvas, projectTransfer, 220, 190);

    await waitFor(() => {
      const snapshot = JSON.parse(window.localStorage.getItem('pawos.desktop.v1') ?? '{}') as {
        wayfinder?: { iconPositions?: Record<string, { x: number; y: number }> };
      };
      expect(snapshot.wayfinder?.iconPositions?.['project:/work/paw']).toEqual({ x: 136, y: 140 });
    });

    expect(panel.querySelector('[data-dialogue-file]')).toBeInTheDocument();
  });

  it('does not reintroduce an archive tray above project folders', async () => {
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': { ok: true, items: [sessionRecord('s-project-archive', '项目内对话', { workspaceRoots: ['/work/archive-project'] })] },
        'agent.rooms.list': { ok: true, items: [] },
      },
    });
    render(
      <ControlTransportProvider transport={transport}>
        <PawDesktopProvider><PawWorkDirectoryProvider initialPollDelayMs={0} pollIntervalMs={60_000}><PawWayfinderWork /></PawWorkDirectoryProvider></PawDesktopProvider>
      </ControlTransportProvider>,
    );

    const panel = await screen.findByRole('region', { name: '最近工作' });
    expect(panel.querySelector('[data-project-folder]')).toBeInTheDocument();
    expect(panel.querySelector('[data-wayfinder-archive]')).toBeNull();
    expect(panel.querySelector('[data-wayfinder-archive-tray]')).toBeNull();
    expect(transport.requests.map(({ request }) => request.pathId)).toEqual([
      'agent.sessions.list',
      'agent.rooms.list',
      'agent.memoryMaintenance.run',
    ]);
  });

  it('keeps a dialogue file visible after moving it into another project folder', async () => {
    renderPanel({
      routes: {
        'agent.sessions.list': { ok: true, items: [
          sessionRecord('s-source', '来源对话', { workspaceRoots: ['/work/source'] }),
          sessionRecord('s-target', '目标对话', { workspaceRoots: ['/work/target'] }),
        ] },
        'agent.rooms.list': { ok: true, items: [] },
      },
    });

    const panel = await screen.findByRole('region', { name: '最近工作' });
    await waitFor(() => expect(panel.querySelectorAll('[data-project-folder]')).toHaveLength(2));
    const folders = panel.querySelectorAll<HTMLDetailsElement>('[data-project-folder]');
    expect(folders).toHaveLength(2);
    fireEvent.doubleClick(folders[0]!.querySelector('summary')!);
      const sourceFile = within(folders[0]!).getByRole('button', { name: /来源对话/ });
    const targetFolder = folders[1]!;
    const transfer = dragTransfer();

    fireEvent.dragStart(sourceFile, { dataTransfer: transfer });
    fireDrop(targetFolder, transfer);

    await waitFor(() => {
      expect(targetFolder).toHaveAttribute('open');
      expect(within(targetFolder).getByRole('button', { name: /来源对话/ })).toBeInTheDocument();
    });
    await waitFor(() => {
      const snapshot = JSON.parse(window.localStorage.getItem('pawos.desktop.v1') ?? '{}') as {
        wayfinder?: { projectAssignments?: Record<string, string> };
      };
      expect(snapshot.wayfinder?.projectAssignments?.['session:s-source']).toBe('/work/target');
    });
  });

  it('drops a dialogue file onto the canvas as a loose desktop file and files it back', async () => {
    /* The desktop owns the selection set; this harness wires it the way
     * PawDesktop does so the loose file's click-to-select is exercised. */
    function SelectionHarness() {
      const [selected, setSelected] = useState<ReadonlySet<string>>(() => new Set());
      return (
        <PawWayfinderWork
          onSelectIcon={(iconId, additive) => setSelected((current) => {
            if (!additive) return new Set([iconId]);
            const next = new Set(current);
            if (next.has(iconId)) next.delete(iconId);
            else next.add(iconId);
            return next;
          })}
          selectedIcons={selected}
        />
      );
    }
    render(
      <ControlTransportProvider transport={new MockControlTransport({
        routes: {
          'agent.sessions.list': { ok: true, items: [
            sessionRecord('s-loose', '桌面上的散文件', { workspaceRoots: ['/work/paw'] }),
            sessionRecord('s-stays', '留在文件夹里的对话', { workspaceRoots: ['/work/paw'] }),
          ] },
          'agent.rooms.list': { ok: true, items: [] },
        },
      })}>
        <PawDesktopProvider><PawWorkDirectoryProvider initialPollDelayMs={0} pollIntervalMs={60_000}><SelectionHarness /></PawWorkDirectoryProvider></PawDesktopProvider>
      </ControlTransportProvider>,
    );

    const panel = await screen.findByRole('region', { name: '最近工作' });
    await openProjectFolder(panel);
    await screen.findByRole('button', { name: /桌面上的散文件/ });
    const row = within(panel).getByRole('button', { name: /桌面上的散文件/ });
    const canvas = panel.querySelector('[data-wayfinder-canvas]') as HTMLDivElement;
    Object.defineProperty(canvas, 'clientWidth', { configurable: true, value: 800 });
    Object.defineProperty(canvas, 'clientHeight', { configurable: true, value: 600 });
    Object.defineProperty(canvas, 'scrollWidth', { configurable: true, value: 800 });
    Object.defineProperty(canvas, 'scrollHeight', { configurable: true, value: 600 });
    Object.defineProperty(canvas, 'getBoundingClientRect', {
      configurable: true,
      value: () => domRect(10, 20, 800, 600),
    });

    const transfer = dragTransfer();
    fireEvent.dragStart(row, { dataTransfer: transfer });
    fireDrop(canvas, transfer, 220, 190);

    const loose = await waitFor(() => {
      const element = panel.querySelector('[data-wayfinder-loose]');
      expect(element).toBeTruthy();
      return element as HTMLButtonElement;
    });
    expect(loose).toHaveTextContent('桌面上的散文件');
    expect(loose.querySelector('.paw-wayfinder-work__file-art svg')).not.toBeNull();
    expect(panel.querySelectorAll('[data-dialogue-file]')).toHaveLength(1);
    await waitFor(() => {
      const snapshot = JSON.parse(window.localStorage.getItem('pawos.desktop.v1') ?? '{}') as {
        wayfinder?: { iconPositions?: Record<string, unknown>; projectAssignments?: Record<string, string> };
      };
      expect(snapshot.wayfinder?.iconPositions?.['session:s-loose']).toBeTruthy();
      expect(snapshot.wayfinder?.projectAssignments?.['session:s-loose']).toBeUndefined();
    });

    fireEvent.click(loose);
    expect(loose).toHaveAttribute('aria-pressed', 'true');
    fireEvent.keyDown(loose, { key: 'Enter' });
    await waitFor(() => {
      const snapshot = JSON.parse(window.localStorage.getItem('pawos.desktop.v1') ?? '{}') as {
        windows?: Record<string, unknown>;
      };
      expect(snapshot.windows?.['agent:s-loose']).toBeTruthy();
    });

    const folder = panel.querySelector('[data-project-folder]') as HTMLDetailsElement;
    const backTransfer = dragTransfer();
    fireEvent.dragStart(loose, { dataTransfer: backTransfer });
    fireDrop(folder, backTransfer);

    await waitFor(() => {
      expect(panel.querySelector('[data-wayfinder-loose]')).toBeNull();
      expect(panel.querySelectorAll('[data-dialogue-file]')).toHaveLength(2);
    });

    const projectSummary = folder.querySelector('summary')!;
    fireEvent.click(projectSummary);
    expect(projectSummary).toHaveAttribute('data-selected');
    expect(projectSummary).toHaveAttribute('aria-description', '已选择');
    expect(projectSummary).not.toHaveAttribute('aria-pressed');
  });

  it('reports an unreadable directory with a retry instead of an empty desktop', async () => {
    renderPanel({ routes: {} });

    expect(await screen.findByRole('alert')).toHaveTextContent('工作记录暂时无法读取');
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument();
  });

  it('keeps the desktop directory read bounded without rendering a load-more panel', async () => {
    const sessions = Array.from({ length: 101 }, (_, index) => sessionRecord(
      `session-${index}`,
      index === 100 ? '最早 Session 对话' : `Session ${index}`,
      { updatedAtMs: NOW - index, workspaceRoots: ['/work/paw'] },
    ));
    const rooms = Array.from({ length: 101 }, (_, index) => ({
      id: `room-${index}`,
      title: index === 100 ? '最早 Room 协作' : `Room ${index}`,
      status: 'active',
      updatedAtMs: NOW - index,
      workspaceRoots: ['/work/paw'],
      participants: [],
      workItems: [],
    }));
    const transport = new MockControlTransport({ routes: {
      'agent.sessions.list': (request: ControlRequest) => ({
        ok: true,
        items: sessions.slice(0, Number(request.query?.limit ?? 100)),
      }),
      'agent.rooms.list': (request: ControlRequest) => ({
        ok: true,
        items: rooms.slice(0, Number(request.query?.limit ?? 100)),
      }),
    } });
    render(
      <ControlTransportProvider transport={transport}>
        <PawDesktopProvider><PawWorkDirectoryProvider initialPollDelayMs={0} pollIntervalMs={60_000}><PawWayfinderWork /></PawWorkDirectoryProvider></PawDesktopProvider>
      </ControlTransportProvider>,
    );

    const panel = await screen.findByRole('region', { name: '最近工作' });
    expect(within(panel).queryByText('最早 Session 对话')).not.toBeInTheDocument();
    expect(within(panel).queryByText('最早 Room 协作')).not.toBeInTheDocument();
    expect(within(panel).queryByRole('button', { name: '加载更多工作记录' })).not.toBeInTheDocument();
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.sessions.list' && request.query?.limit === 100)).toBe(true);
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.rooms.list' && request.query?.limit === 100)).toBe(true);
  });
});

function renderPanel(options: MockControlTransportOptions) {
  return render(
    <ControlTransportProvider transport={new MockControlTransport(options)}>
      <PawDesktopProvider>
        <PawWorkDirectoryProvider initialPollDelayMs={0} pollIntervalMs={60_000}><PawWayfinderWork /></PawWorkDirectoryProvider>
      </PawDesktopProvider>
    </ControlTransportProvider>,
  );
}

async function openProjectFolder(panel: HTMLElement, index = 0): Promise<HTMLDetailsElement> {
  await waitFor(() => expect(panel.querySelectorAll('[data-project-folder]').length).toBeGreaterThan(index));
  const folder = panel.querySelectorAll<HTMLDetailsElement>('[data-project-folder]')[index]!;
  fireEvent.doubleClick(folder.querySelector('summary')!);
  expect(folder).toHaveAttribute('open');
  return folder;
}

function sessionRecord(id: string, title: string, overrides: Record<string, unknown> = {}) {
  return {
    id,
    title,
    mode: 'assistant',
    status: 'idle',
    roleId: 'default',
    roleVersion: '1',
    roleBookRevisionId: 'r1',
    updatedAtMs: NOW - 60_000,
    workspaceRoots: [],
    ...overrides,
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
    types: [WAYFINDER_DRAG_MIME, 'text/plain'],
  } as unknown as DataTransfer;
}

function fireDrop(element: HTMLElement, dataTransfer: DataTransfer, clientX = 0, clientY = 0): void {
  const event = new Event('drop', { bubbles: true, cancelable: true });
  Object.defineProperties(event, {
    clientX: { configurable: true, value: clientX },
    clientY: { configurable: true, value: clientY },
    dataTransfer: { configurable: true, value: dataTransfer },
  });
  fireEvent(element, event);
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
