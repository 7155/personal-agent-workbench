import { Activity, Archive, ChevronDown, ChevronRight, CircleAlert, FolderOpen, LoaderCircle, Orbit, Settings2, Users, X } from 'lucide-react';
import { lazy, memo, Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type DragEvent, type KeyboardEvent as ReactKeyboardEvent, type RefObject } from 'react';
import { usePawDesktopApi, usePawDesktopStore } from '../runtime/desktop-context';
import type { PawWayfinderIconPosition, PawWayfinderState } from '../runtime/desktop-store';
import { PAW_DESKTOP_GRID, pawDesktopGridEntries, pawDesktopMovePosition, pawDesktopOccupiedPositions, pawDesktopResolvePersistedPositions, pawDesktopSnapPosition, pawDesktopWorkOriginY, pawDesktopWorkPosition, usePawDesktopGridLayout } from './desktop-grid';
import { PAW_WORK_FILE_ACCENT, PawWorkFileIcon, PawWorkFolderIcon, pawWorkProjectAccent } from './PawWorkIcons';
import {
  projectWayfinderWork,
  bucketizeWayfinderWork,
  wayfinderWorkTime,
  type WayfinderWorkBucket,
  type WayfinderWorkItem,
  type WayfinderWorkProject,
  type WayfinderWorkView,
} from './wayfinder-work-projection';
import { usePawWorkDirectory } from './PawWorkDirectory';
import { SatelliteModelBadge } from '@/features/paw-os/SatelliteModelBadge';

const PawProjectGalaxy = lazy(() => import('./PawProjectGalaxy').then((module) => ({ default: module.PawProjectGalaxy })));
const PawProjectDocuments = lazy(() => import('./PawProjectDocuments').then((module) => ({ default: module.PawProjectDocuments })));

export const WAYFINDER_DRAG_MIME = 'application/x-paw-wayfinder-icon';
export const WAYFINDER_ICON_WIDTH = 96;
export const WAYFINDER_ICON_HEIGHT = 92;

function projectActivityLabel(project: Pick<WayfinderWorkProject, 'attentionCount' | 'runningCount'>): string {
  return [
    project.runningCount ? `${project.runningCount} 个进行中` : '',
    project.attentionCount ? `${project.attentionCount} 个需处理` : '',
  ].filter(Boolean).join(' · ');
}

function projectCompactActivityLabel(project: Pick<WayfinderWorkProject, 'attentionCount' | 'runningCount'>): string {
  return [
    project.runningCount ? `运行 ${project.runningCount}` : '',
    project.attentionCount ? `待处理 ${project.attentionCount}` : '',
  ].filter(Boolean).join(' · ');
}
/**
 * PawWayfinderWork — PAWOS' project folders and conversation files.
 *
 * The desktop plane is the contract: the projection is still read-only over
 * the Agent directory, but folders and dialogue files live on the same visual
 * canvas as App shortcuts. Dragging changes only PAWOS coordinates or folder
 * assignment; every click opens the canonical Agent
 * Session/Room window.
 *
 * The only props are the desktop selection channel — a stable callback and
 * the current icon-id set — so the panel stays a memo leaf for clock ticks,
 * window churn and context menus, re-rendering only when its own directory
 * read, query, expansion or the selection itself changes.
 */
export const PawWayfinderWork = memo(function PawWayfinderWork({ onArchive, onSelectIcon, selectedIcons }: {
  onArchive?: (iconIds: readonly string[], label: string) => void;
  onSelectIcon?: (iconId: string, additive: boolean) => void;
  selectedIcons?: ReadonlySet<string>;
}) {
  const api = usePawDesktopApi();
  const wayfinder = usePawDesktopStore((state) => state.wayfinder);
  useEffect(() => {
    // Preserve positions, archive choices and manual assignments from keys
    // that used to include the extra filesystem access root.
    const projectId = (id: string) => id.split('\u001f').filter((root, _, roots) => root !== '/' || roots.length === 1).join('\u001f');
    const iconId = (id: string) => id.startsWith('project:') ? `project:${projectId(id.slice(8))}` : id;
    const positions = { ...wayfinder.iconPositions };
    let changed = false;
    for (const [oldId, position] of Object.entries(wayfinder.iconPositions)) {
      const nextId = iconId(oldId);
      if (nextId === oldId) continue;
      positions[nextId] ??= position;
      delete positions[oldId];
      changed = true;
    }
    const archived = [...new Set(wayfinder.archived.map((id) => {
      const nextId = iconId(id);
      changed ||= nextId !== id;
      return nextId;
    }))];
    const assignments = Object.fromEntries(Object.entries(wayfinder.projectAssignments).map(([id, target]) => {
      const nextId = projectId(target);
      changed ||= nextId !== target;
      return [id, nextId];
    }));
    if (!changed) return;
    // Let the desktop provider subscribe before publishing the migration.
    const timer = window.setTimeout(() => api.setState({
      wayfinder: { ...wayfinder, iconPositions: positions, archived, projectAssignments: assignments },
    }), 0);
    return () => window.clearTimeout(timer);
  }, [api, wayfinder]);

  const {
    failed,
    loaded: loadedOnce,
    loading,
    refresh,
    roomStatusFresh,
    rooms,
    sessionStatusFresh,
    sessions,
  } = usePawWorkDirectory();
  const [expandedBuckets, setExpandedBuckets] = useState<ReadonlySet<string>>(new Set());
  const [expandedRepeats, setExpandedRepeats] = useState<ReadonlySet<string>>(new Set());
  /* An expanded folder owns one anchored content panel. Keeping a single id
     is important on the desktop coordinate plane: two neighbouring folders
     may be icons, but their scrollable panels must never occupy the same
     pixels. The desktop rests closed like macOS Finder: single click selects,
     double click opens, and `null` is the explicit fold-all state. */
  const [expandedProjectId, setExpandedProjectId] = useState<string | null>(null);
  const [galaxyProjectId, setGalaxyProjectId] = useState<string | null>(null);
  const [draggingKey, setDraggingKey] = useState<string | null>(null);
  const [contextProjectId, setContextProjectId] = useState<string | null>(null);
  const contextTriggerRef = useRef<HTMLElement | null>(null);
  const contextSheetRef = useRef<HTMLElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLDivElement>(null);
  const fullView = useMemo(
    () => projectWayfinderWork({ nowMs: Date.now(), roomStatusFresh, rooms, sessionStatusFresh, sessions }),
    [roomStatusFresh, rooms, sessionStatusFresh, sessions],
  );
  const sourceView = fullView;
  const view = useMemo(
    () => applyWayfinderUiState(sourceView, wayfinder, fullView.projects),
    [fullView.projects, sourceView, wayfinder],
  );
  const searching = false;
  const visibleExpandedProjectId = expandedProjectId;
  const contextProject = contextProjectId
    ? view.projects.find((project) => project.id === contextProjectId) ?? fullView.projects.find((project) => project.id === contextProjectId) ?? null
    : null;
  const galaxyProject = galaxyProjectId ? view.projects.find((project) => project.id === galaxyProjectId) ?? null : null;

  useEffect(() => {
    if (galaxyProjectId && !galaxyProject) setGalaxyProjectId(null);
  }, [galaxyProject, galaxyProjectId]);

  useEffect(() => {
    if (contextProjectId && !contextProject) setContextProjectId(null);
  }, [contextProject, contextProjectId]);

  useEffect(() => {
    if (!contextProject) return;
    contextSheetRef.current?.querySelector<HTMLButtonElement>('[data-wayfinder-context-close]')?.focus();
  }, [contextProject]);

  useEffect(() => {
    if (contextProjectId !== null) return;
    contextTriggerRef.current?.focus();
  }, [contextProjectId]);

  useEffect(() => {
    if (!contextProject) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      setContextProjectId(null);
    };
    document.addEventListener('keydown', closeOnEscape);
    return () => document.removeEventListener('keydown', closeOnEscape);
  }, [contextProject]);

  useEffect(() => {
    if (!contextProject) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (contextSheetRef.current?.contains(target) || contextTriggerRef.current?.contains(target)) return;
      setContextProjectId(null);
    };
    document.addEventListener('pointerdown', closeOnOutsidePointer, true);
    return () => document.removeEventListener('pointerdown', closeOnOutsidePointer, true);
  }, [contextProject]);

  const openItem = useCallback((kind: 'session' | 'room', id: string, title: string) => {
    const state = api.getState();
    state.openApp('agent', {
      entityId: id,
      initialRoute: `/agent?${kind === 'room' ? 'room' : 'session'}=${encodeURIComponent(id)}`,
      target: { kind, id, title },
      title: 'Agent',
    });
  }, [api]);

  const openSystemSettings = useCallback(() => {
    api.getState().openApp('system-settings', { initialRoute: '/configuration' });
  }, [api]);

  const openProjectRootInFiles = useCallback((root: string, sessionId: string) => {
    const leaf = root.split('/').filter(Boolean).pop() ?? root;
    api.getState().openApp('files', {
      entityId: `${sessionId}:${root}`,
      initialRoute: `/files?session=${encodeURIComponent(sessionId)}&path=${encodeURIComponent(root)}`,
      title: `Files · ${leaf}`,
    });
  }, [api]);

  const openObservability = useCallback(() => {
    api.getState().openApp('system-monitor', { initialRoute: '/observability' });
  }, [api]);

  const gridLayout = usePawDesktopGridLayout();
  const iconPosition = useCallback((iconId: string, index: number): PawWayfinderIconPosition => (
    wayfinder.iconPositions[iconId]
      ?? pawDesktopWorkPosition(index, gridLayout.columns, gridLayout.originY)
  ), [gridLayout.columns, gridLayout.originY, wayfinder.iconPositions]);
  useLayoutEffect(() => {
    const canvas = canvasRef.current;
    const plane = canvas?.closest<HTMLElement>('.paw-wayfinder') ?? canvas;
    if (!plane || Object.keys(wayfinder.iconPositions).length === 0) return;
    api.getState().setWayfinderIconPositions(pawDesktopResolvePersistedPositions(
      pawDesktopGridEntries(plane),
      wayfinder.iconPositions,
      gridLayout.columns,
    ));
  }, [api, gridLayout.columns, view.looseItems.length, view.projects.length, wayfinder.iconPositions]);

  const startDrag = useCallback((event: DragEvent<HTMLElement>, iconId: string) => {
    event.stopPropagation();
    setDraggingKey(iconId);
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData(WAYFINDER_DRAG_MIME, iconId);
    event.dataTransfer.setData('text/plain', iconId);
  }, []);

  const endDrag = useCallback(() => setDraggingKey(null), []);

  const dropKey = useCallback((event: DragEvent<HTMLElement>): string => (
    event.dataTransfer.getData(WAYFINDER_DRAG_MIME) || event.dataTransfer.getData('text/plain')
  ), []);

  const dropOnCanvas = useCallback((event: DragEvent<HTMLDivElement>) => {
    const iconId = dropKey(event);
    if (!iconId || (!isDialogueIcon(iconId) && !isProjectIcon(iconId))) return;
    event.preventDefault();
    event.stopPropagation();
    const canvas = canvasRef.current;
    if (canvas) {
      const rect = canvas.getBoundingClientRect();
      const x = event.clientX - rect.left + canvas.scrollLeft - WAYFINDER_ICON_WIDTH / 2;
      const y = event.clientY - rect.top + canvas.scrollTop - WAYFINDER_ICON_HEIGHT / 2;
      const plane = canvas.closest<HTMLElement>('.paw-wayfinder') ?? canvas;
      const snapped = pawDesktopSnapPosition(
        { x, y },
        gridLayout.columns,
        pawDesktopOccupiedPositions(plane, iconId),
      );
      api.getState().setWayfinderIconPosition(iconId, clampWayfinderIconPosition(snapped, canvas));
    }
    if (isDialogueIcon(iconId)) api.getState().setWayfinderProjectAssignment(iconId, null);
    api.getState().setWayfinderArchived(iconId, false);
    setDraggingKey(null);
  }, [api, dropKey, gridLayout.columns]);

  const dropOnProject = useCallback((event: DragEvent<HTMLElement>, projectId: string) => {
    const iconId = dropKey(event);
    if (!iconId || !isDialogueIcon(iconId)) return;
    event.preventDefault();
    event.stopPropagation();
    api.getState().setWayfinderProjectAssignment(iconId, projectId);
    api.getState().setWayfinderArchived(iconId, false);
    setExpandedProjectId(projectId);
    setDraggingKey(null);
  }, [api, dropKey]);

  // Desktop files can be freely positioned, so arrow keys follow their visual
  // coordinates instead of an unrelated DOM order. Home/End intentionally
  // retain the stable document-order escape hatch.
  const walkRows = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (!['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    const host = listRef.current;
    if (!host) return;
    const rows = Array.from(host.querySelectorAll<HTMLElement>(
      'summary[data-wayfinder-project], button[data-wayfinder-loose], button[data-wayfinder-row]',
    ));
    const current = rows.indexOf(document.activeElement as HTMLElement);
    if (current === -1) return;
    event.preventDefault();
    if (event.altKey && event.key.startsWith('Arrow')) {
      const owner = rows[current]!.closest<HTMLElement>('[data-wayfinder-grid-position]');
      const iconId = owner?.dataset.wayfinderGridPosition;
      const plane = owner?.closest<HTMLElement>('.paw-wayfinder') ?? canvasRef.current;
      const entry = plane && iconId
        ? pawDesktopGridEntries(plane).find((candidate) => candidate.id === iconId)
        : undefined;
      if (plane && iconId && entry) {
        api.getState().setWayfinderIconPosition(iconId, pawDesktopMovePosition(
          entry.position,
          event.key as 'ArrowDown' | 'ArrowUp' | 'ArrowLeft' | 'ArrowRight',
          gridLayout.columns,
          pawDesktopOccupiedPositions(plane, iconId),
        ));
      }
      return;
    }
    if (event.key === 'Home' || event.key === 'End') {
      rows[event.key === 'Home' ? 0 : rows.length - 1]?.focus();
      return;
    }
    const next = spatialWayfinderTarget(
      rows,
      rows[current]!,
      event.key as 'ArrowDown' | 'ArrowUp' | 'ArrowLeft' | 'ArrowRight',
    );
    next?.focus();
  };

  const hasRows = view.rowCount > 0;
  const canvasMinHeight = Math.max(
    360,
    pawDesktopWorkOriginY(gridLayout.columns, gridLayout.originY)
      + Math.ceil((view.projects.length + view.looseItems.length) / gridLayout.columns) * PAW_DESKTOP_GRID.pitchY
      + 34,
  );
  return (
    <section aria-label="最近工作" className="paw-wayfinder-work" data-paw-desktop-work-files>
      <div
        aria-busy={loading || undefined}
        className="paw-wayfinder-work__list paw-wayfinder-work__canvas"
        data-wayfinder-canvas
        onDragOver={(event) => { event.preventDefault(); event.dataTransfer.dropEffect = 'move'; }}
        onDrop={dropOnCanvas}
        onKeyDown={walkRows}
        ref={(node) => { listRef.current = node; canvasRef.current = node; }}
        style={{ minHeight: `${canvasMinHeight}px` }}
      >
        {loading && !loadedOnce ? (
          <p className="paw-wayfinder-work__state" role="status"><LoaderCircle className="paw-wayfinder-work__spin" size={13} />正在读取</p>
        ) : failed ? (
          <p className="paw-wayfinder-work__state" role="alert">
            <CircleAlert size={13} />工作记录暂时无法读取
            <button onClick={() => void refresh()} type="button">重试</button>
          </p>
        ) : !hasRows ? (
          <p className="paw-wayfinder-work__state">
            {searching ? '没有匹配的工作' : '还没有工作记录'}
            {searching ? null : (
              <button onClick={() => openAgentHome(api)} type="button">开始一件事</button>
            )}
          </p>
        ) : view.projects.map((project, index) => {
          const folderExpanded = visibleExpandedProjectId === project.id;
          return (
            <ProjectFolder
              expandedBuckets={expandedBuckets}
              expandedRepeats={expandedRepeats}
              expanded={folderExpanded}
              iconPosition={iconPosition(`project:${project.id}`, index)}
              onDragEnd={endDrag}
              onDragStart={startDrag}
              key={project.id}
              onContext={(trigger) => {
                contextTriggerRef.current = trigger;
                setExpandedProjectId(null);
                setContextProjectId(project.id);
              }}
              onDrop={(event) => dropOnProject(event, project.id)}
              onOpen={openItem}
              onOpenGalaxy={() => setGalaxyProjectId(project.id)}
              onSelect={onSelectIcon}
              onToggle={() => setExpandedProjectId(folderExpanded ? null : project.id)}
              onToggleBucket={(key) => setExpandedBuckets((current) => toggled(current, key))}
              onToggleRepeats={(key) => setExpandedRepeats((current) => toggled(current, key))}
              project={project}
              searching={searching}
              selected={selectedIcons?.has(projectIconId(project.id)) ?? false}
            />
          );
        })}
        {/* Loose conversation files: dragged out of their folder onto the
            desktop, they keep their own coordinates and stay loose until they
            are dropped on a folder again. They flow on the same grid right
            after the project folders. */}
        {view.looseItems.map((item, looseIndex) => (
          <LooseWorkFile
            iconPosition={iconPosition(item.key, view.projects.length + looseIndex)}
            item={item}
            key={item.key}
            onDragEnd={endDrag}
            onDragStart={startDrag}
            onOpen={openItem}
            onSelect={onSelectIcon}
            selected={selectedIcons?.has(item.key) ?? false}
          />
        ))}
        {draggingKey ? <p className="paw-wayfinder-work__drop-hint">拖到空白处放回项目桌面，拖到项目文件夹归类</p> : null}
      </div>
      {contextProject ? (
        <ProjectContextSheet
          onArchive={() => {
            const iconId = projectIconId(contextProject.id);
            if (onArchive) onArchive([iconId], contextProject.label);
            else api.getState().setWayfinderArchived(iconId, true);
            contextTriggerRef.current = document.querySelector<HTMLElement>('.paw-desktop-viewport');
            setContextProjectId(null);
          }}
          onBack={() => setContextProjectId(null)}
          onOpen={openItem}
          onOpenInFiles={openProjectRootInFiles}
          onOpenObservability={openObservability}
          onOpenSystemSettings={openSystemSettings}
          project={contextProject}
          sheetRef={contextSheetRef}
        />
      ) : null}
      {galaxyProject ? <Suspense fallback={<p role="status">正在打开项目星系…</p>}>
        <PawProjectGalaxy
          project={galaxyProject}
          modelDetails={(item) => item.kind === 'session' ? <SatelliteModelBadge key={item.id} sessionId={item.id} inline /> : <span>各行星使用独立模型</span>}
          onClose={() => setGalaxyProjectId(null)}
          onShowList={() => {
            setExpandedProjectId(galaxyProject.id);
            setGalaxyProjectId(null);
          }}
          onOpen={(item) => {
            setGalaxyProjectId(null);
            setExpandedProjectId(null);
            openItem(item.kind, item.id, item.title);
          }}
          onOpenProject={() => {
            setGalaxyProjectId(null);
            setExpandedProjectId(null);
            setContextProjectId(galaxyProject.id);
          }}
          documents={<PawProjectDocuments
            project={galaxyProject}
            sessions={sessions}
            onOpenFile={(path, sessionId) => {
              setGalaxyProjectId(null);
              setExpandedProjectId(null);
              openProjectRootInFiles(path, sessionId);
            }}
            onOpenWork={(item) => {
              setGalaxyProjectId(null);
              setExpandedProjectId(null);
              openItem(item.kind, item.id, item.title);
            }}
          />}
        />
      </Suspense> : null}
    </section>
  );
});

export function spatialWayfinderTarget(
  rows: readonly HTMLElement[],
  current: HTMLElement,
  key: 'ArrowDown' | 'ArrowUp' | 'ArrowLeft' | 'ArrowRight',
): HTMLElement | null {
  const currentCenter = wayfinderElementCenter(current);
  if (!currentCenter) return null;
  const currentX = currentCenter.x;
  const currentY = currentCenter.y;
  const horizontal = key === 'ArrowLeft' || key === 'ArrowRight';
  const direction = key === 'ArrowLeft' || key === 'ArrowUp' ? -1 : 1;

  let best: { row: HTMLElement; score: number } | null = null;
  for (const row of rows) {
    if (row === current) continue;
    const center = wayfinderElementCenter(row);
    // Closed folder descendants and other non-rendered rows have no box or
    // top-level grid coordinate and must not intercept desktop navigation.
    if (!center) continue;
    const deltaX = center.x - currentX;
    const deltaY = center.y - currentY;
    const primary = horizontal ? deltaX : deltaY;
    if (primary * direction <= 0) continue;
    const cross = horizontal ? deltaY : deltaX;
    // Crossing a row/column is deliberately costlier than distance along the
    // requested axis, so a nearly aligned icon wins over a diagonal shortcut.
    const score = Math.abs(primary) + Math.abs(cross) * 2;
    if (!best || score < best.score) best = { row, score };
  }
  return best?.row ?? null;
}

function wayfinderElementCenter(element: HTMLElement): { x: number; y: number } | null {
  const rect = element.getBoundingClientRect();
  if (rect.width > 0 || rect.height > 0) {
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
  }
  /* JSDOM and pre-layout keyboard focus have no client rect yet; the desktop
   * still has an authoritative inline grid coordinate on the positioned
   * shell, so navigation need not fall back to unrelated DOM order. */
  const positioned = element.closest<HTMLElement>('[data-wayfinder-grid-position]');
  if (!positioned) return null;
  const x = Number.parseFloat(positioned.style.getPropertyValue('--wayfinder-x'));
  const y = Number.parseFloat(positioned.style.getPropertyValue('--wayfinder-y'));
  return Number.isFinite(x) && Number.isFinite(y)
    ? { x: x + WAYFINDER_ICON_WIDTH / 2, y: y + WAYFINDER_ICON_HEIGHT / 2 }
    : null;
}

function ProjectFolder({ expanded, expandedBuckets, expandedRepeats, iconPosition, onContext, onDragEnd, onDragStart, onDrop, onOpen, onOpenGalaxy, onSelect, onToggle, onToggleBucket, onToggleRepeats, project, searching, selected }: {
  expanded: boolean;
  expandedBuckets: ReadonlySet<string>;
  expandedRepeats: ReadonlySet<string>;
  iconPosition: PawWayfinderIconPosition;
  onContext: (trigger: HTMLElement) => void;
  onDragEnd: () => void;
  onDragStart: (event: DragEvent<HTMLElement>, iconId: string) => void;
  onDrop: (event: DragEvent<HTMLElement>) => void;
  onOpen: (kind: 'session' | 'room', id: string, title: string) => void;
  onOpenGalaxy: () => void;
  onSelect?: (iconId: string, additive: boolean) => void;
  onToggle: () => void;
  onToggleBucket: (key: string) => void;
  onToggleRepeats: (key: string) => void;
  project: WayfinderWorkProject;
  searching: boolean;
  selected: boolean;
}) {
  const displayedOpen = expanded;
  const shellRef = useRef<HTMLDivElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const [panelPlacement, setPanelPlacement] = useState<WayfinderProjectPanelPlacement | null>(null);

  useLayoutEffect(() => {
    if (!expanded) {
      setPanelPlacement(null);
      return undefined;
    }
    const shell = shellRef.current;
    const panel = panelRef.current;
    const canvas = shell?.closest<HTMLElement>('[data-wayfinder-canvas]');
    if (!shell || !panel || !canvas) return undefined;

    const measure = () => {
      const canvasRect = canvas.getBoundingClientRect();
      const shellRect = shell.getBoundingClientRect();
      const panelRect = panel.getBoundingClientRect();
      const panelWidth = panel.offsetWidth || panelRect.width;
      const panelHeight = panel.offsetHeight || panelRect.height;
      if (!panelWidth || !panelHeight) return;
      const desktopRoot = shell.closest<HTMLElement>('.paw-desktop-root');
      const dock = desktopRoot?.querySelector<HTMLElement>('.paw-dock');
      const dockRect = dock?.getBoundingClientRect();
      const configuredDockHeight = desktopRoot
        ? Number.parseFloat(getComputedStyle(desktopRoot).getPropertyValue('--paw-dock-h'))
        : 0;
      /* The Dock floats inside the same viewport rather than consuming layout
       * height. Treat its whole bottom shelf as a reserved placement inset so
       * an opened project never clamps against the screen edge underneath it.
       * The token fallback also keeps the placement stable while a maximized
       * App temporarily unmounts the Dock. */
      const bottomInset = dockRect && dockRect.height > 0
        ? Math.max(0, canvasRect.bottom - dockRect.top)
        : Math.max(0, (Number.isFinite(configuredDockHeight) ? configuredDockHeight : 0) + 12);
      const next = placeWayfinderProjectPanel({ canvasRect, shellRect, panelWidth, panelHeight, bottomInset });
      setPanelPlacement((current) => (
        current && current.x === next.x && current.y === next.y && current.horizontal === next.horizontal && current.vertical === next.vertical
          ? current
          : next
      ));
    };

    measure();
    const resizeObserver = typeof ResizeObserver === 'function' ? new ResizeObserver(measure) : null;
    resizeObserver?.observe(canvas);
    resizeObserver?.observe(panel);
    canvas.addEventListener('scroll', measure, { passive: true });
    window.addEventListener('resize', measure);
    return () => {
      resizeObserver?.disconnect();
      canvas.removeEventListener('scroll', measure);
      window.removeEventListener('resize', measure);
    };
  }, [expanded, expandedBuckets, expandedRepeats, project.id, project.items.length, searching]);

  const style = {
    '--wayfinder-x': `${iconPosition.x}px`,
    '--wayfinder-y': `${iconPosition.y}px`,
    ...(panelPlacement ? {
      '--wayfinder-panel-x': `${panelPlacement.x}px`,
      '--wayfinder-panel-y': `${panelPlacement.y}px`,
    } : {}),
  } as CSSProperties;
  return (
    <div className="paw-wayfinder-work__project-shell" data-wayfinder-grid-position={projectIconId(project.id)} ref={shellRef} style={style}>
      <details
        className="paw-wayfinder-work__project"
        data-project-folder
        onDragOver={(event) => { event.preventDefault(); event.stopPropagation(); event.dataTransfer.dropEffect = 'move'; }}
        onDrop={onDrop}
        open={displayedOpen}
      >
        <summary
          aria-description={selected ? '已选择' : undefined}
          aria-expanded={displayedOpen}
          aria-keyshortcuts="Shift+F10 Alt+ArrowUp Alt+ArrowDown Alt+ArrowLeft Alt+ArrowRight"
          data-selected={selected || undefined}
          data-wayfinder-icon={projectIconId(project.id)}
          data-wayfinder-project
          draggable
          onClick={(event) => {
            event.preventDefault();
            const additive = event.shiftKey || event.metaKey || event.ctrlKey;
            onSelect?.(projectIconId(project.id), additive);
          }}
          onDoubleClick={(event) => { event.preventDefault(); event.currentTarget.focus(); onToggle(); }}
          onContextMenu={(event) => {
            event.preventDefault();
            event.stopPropagation();
            onContext(event.currentTarget);
          }}
          onKeyDown={(event) => {
            if ((event.shiftKey && event.key === 'F10') || event.key === 'ContextMenu') {
              event.preventDefault();
              event.stopPropagation();
              onContext(event.currentTarget);
              return;
            }
            if (event.key !== 'Enter' && event.key !== ' ') return;
            event.preventDefault();
            event.currentTarget.focus();
            onToggle();
          }}
          onDragEnd={onDragEnd}
          onDragStart={(event) => onDragStart(event, projectIconId(project.id))}
          title={`${project.label} · ${project.items.length} 个对话${project.workspaceRoots.length ? `\n${project.workspaceRoots.join("\n")}` : ""}`}
        >
          <span
            className="paw-wayfinder-work__folder-art"
            data-attention={project.attentionCount > 0 || undefined}
            data-running={project.runningCount > 0 || undefined}
            style={{ '--paw-work-accent': pawWorkProjectAccent(project.id) } as CSSProperties}
          >
            <PawWorkFolderIcon open={displayedOpen} />
            <i aria-hidden="true" />
          </span>
          <span className="paw-wayfinder-work__project-copy">
            <strong><span className="paw-wayfinder-work__label-ink">{project.label}</span></strong>
            {project.runningCount || project.attentionCount ? (
              <small
                aria-label={projectActivityLabel(project)}
                data-state={project.attentionCount ? 'attention' : 'running'}
              >
                {projectCompactActivityLabel(project)}
              </small>
            ) : null}
          </span>
          <small className="paw-wayfinder-work__project-count">{project.items.length} 个文件</small>
        </summary>
        {displayedOpen ? <div
          aria-label={`${project.label} 项目窗口`}
          className="paw-wayfinder-work__project-content"
          data-paw-desktop-ui
          data-wayfinder-project-window
          role="dialog"
          ref={panelRef}
        >
          <header className="paw-wayfinder-work__project-content-head">
            <div>
              <span>项目文件夹</span>
              <strong>{project.label}</strong>
              <small>{project.items.length} 个对话 · {projectActivityLabel(project) || '已准备好'}</small>
            </div>
            <div className="paw-wayfinder-work__project-content-actions">
              <button className="paw-wayfinder-work__project-content-galaxy" onClick={(event) => { event.preventDefault(); event.stopPropagation(); onOpenGalaxy(); }} type="button" title="以全屏星系选择对话与文档">
                <Orbit aria-hidden="true" size={15} /><span>全屏星系</span>
              </button>
              <button
                aria-label={`打开 ${project.label} 项目设置`}
                className="paw-wayfinder-work__project-content-context"
                data-wayfinder-context
                onClick={(event) => { event.preventDefault(); event.stopPropagation(); onContext(event.currentTarget); }}
                title="项目设置与上下文"
                type="button"
              >
                <Settings2 aria-hidden="true" size={14} />
              </button>
              <button aria-label={`关闭 ${project.label} 项目窗口`} onClick={(event) => { event.preventDefault(); event.stopPropagation(); onToggle(); }} type="button">
                <X aria-hidden="true" size={15} />
              </button>
            </div>
          </header>
          <div className="paw-wayfinder-work__project-content-scroll">
            {project.buckets.map((bucket) => (
              <WorkBucket
                bucket={project.buckets.length === 1 && bucket.previewCount === 0
                  ? { ...bucket, previewCount: 5 }
                  : bucket}
                expanded={searching || expandedBuckets.has(`${project.id}:${bucket.id}`)}
                expandedRepeats={expandedRepeats}
                onDragEnd={onDragEnd}
                onDragStart={onDragStart}
                key={bucket.id}
                onOpen={onOpen}
                onToggle={() => onToggleBucket(`${project.id}:${bucket.id}`)}
                onToggleRepeats={onToggleRepeats}
                searching={searching}
              />
            ))}
          </div>
        </div> : null}
      </details>
    </div>
  );
}

/* A conversation file living directly on the desktop: dragged out of its
 * folder, it keeps the document glyph, the kind/status line and its own
 * coordinates. Single click selects it like any desktop icon; double-click
 * opens the canonical Agent window. */
function LooseWorkFile({ iconPosition, item, onDragEnd, onDragStart, onOpen, onSelect, selected }: {
  iconPosition: PawWayfinderIconPosition;
  item: WayfinderWorkItem;
  onDragEnd: () => void;
  onDragStart: (event: DragEvent<HTMLElement>, iconId: string) => void;
  onOpen: (kind: 'session' | 'room', id: string, title: string) => void;
  onSelect?: (iconId: string, additive: boolean) => void;
  selected: boolean;
}) {
  const style = {
    '--wayfinder-x': `${iconPosition.x}px`,
    '--wayfinder-y': `${iconPosition.y}px`,
    '--paw-work-accent': PAW_WORK_FILE_ACCENT[item.kind],
  } as CSSProperties;
  const state = item.activity === 'running' ? 'running' : item.activity === 'attention' ? 'attention' : undefined;
  return (
    <div className="paw-wayfinder-work__loose-shell" data-wayfinder-grid-position={item.key} style={style}>
      <button
        aria-keyshortcuts="Alt+ArrowUp Alt+ArrowDown Alt+ArrowLeft Alt+ArrowRight"
        aria-pressed={selected || undefined}
        className="paw-wayfinder-work__loose-file"
        data-activity={item.activity}
        data-kind={item.kind}
        data-running={item.runtimeRunning || undefined}
        data-wayfinder-icon={item.key}
        data-wayfinder-loose
        draggable
        onClick={(event) => onSelect?.(item.key, event.shiftKey || event.metaKey || event.ctrlKey)}
        onDoubleClick={() => onOpen(item.kind, item.id, item.title)}
        onKeyDown={(event) => {
          if (event.key !== 'Enter') return;
          event.preventDefault();
          onOpen(item.kind, item.id, item.title);
        }}
        onDragEnd={onDragEnd}
        onDragStart={(event) => onDragStart(event, item.key)}
        title={`${item.title} · ${item.kind === 'room' ? 'Room' : 'Session'} · ${item.statusLabel}`}
        type="button"
      >
        <span aria-hidden="true" className="paw-wayfinder-work__file-art" data-kind={item.kind}>
          <PawWorkFileIcon kind={item.kind} />
          <i />
        </span>
        <span className="paw-wayfinder-work__project-copy">
          <strong><span className="paw-wayfinder-work__label-ink">{item.title}</span></strong>
          <small data-state={state}>{item.kind === 'room' ? 'Room' : 'Session'} · {item.statusLabel}</small>
        </span>
      </button>
    </div>
  );
}

/** The Files App only browses Session-authorized workspace roots, so a root
 * is openable there exactly when one of the project's Sessions names it. */
function filesSessionForRoot(project: WayfinderWorkProject, root: string): string | null {
  const item = project.items.find((candidate) => candidate.kind === 'session' && candidate.workspaceRoots.includes(root));
  return item?.id ?? null;
}

function ProjectContextSheet({ onArchive, onBack, onOpen, onOpenInFiles, onOpenObservability, onOpenSystemSettings, project, sheetRef }: {
  onArchive: () => void;
  onBack: () => void;
  onOpen: (kind: 'session' | 'room', id: string, title: string) => void;
  onOpenInFiles: (root: string, sessionId: string) => void;
  onOpenObservability: () => void;
  onOpenSystemSettings: () => void;
  project: WayfinderWorkProject;
  sheetRef: RefObject<HTMLElement | null>;
}) {
  return (
    <aside
      aria-label={`${project.label} 项目详情`}
      aria-modal="false"
      className="paw-wayfinder-work__context-sheet"
      data-paw-desktop-ui
      data-wayfinder-context-sheet
      ref={sheetRef}
      role="dialog"
    >
      <header className="paw-wayfinder-work__context-head">
        <div>
          <small>项目详情</small>
          <h2>{project.label}</h2>
        </div>
        <button aria-label={`关闭 ${project.label} 项目详情`} className="paw-wayfinder-work__context-close" data-wayfinder-context-close onClick={onBack} type="button">
          <X aria-hidden="true" size={14} />
        </button>
      </header>

      <dl className="paw-wayfinder-work__context-stats">
        <div><dt>Session</dt><dd>{project.sessionCount}</dd></div>
        <div><dt>Room</dt><dd>{project.roomCount}</dd></div>
        <div><dt>进行中</dt><dd>{project.runningCount}</dd></div>
        <div data-attention={project.attentionCount > 0 || undefined}><dt>需处理</dt><dd>{project.attentionCount}</dd></div>
      </dl>

      <section className="paw-wayfinder-work__context-roots">
        <h3>工作区</h3>
        {project.workspaceRoots.length ? project.workspaceRoots.map((root) => {
          const sessionId = filesSessionForRoot(project, root);
          return (
            <div className="paw-wayfinder-work__context-root" key={root}>
              <code>{root}</code>
              {sessionId ? (
                <button
                  aria-label={`在 Files 中打开 ${root}`}
                  onClick={() => onOpenInFiles(root, sessionId)}
                  title="在 Files 中打开这个工作区"
                  type="button"
                >
                  <FolderOpen aria-hidden="true" size={13} />
                  <span>在 Files 中打开</span>
                </button>
              ) : null}
            </div>
          );
        }) : <p>未绑定工作区</p>}
      </section>

      <section className="paw-wayfinder-work__context-conversations">
        <header>
          <h3>当前对话</h3>
          <small>{project.items.length}</small>
        </header>
        <div className="paw-wayfinder-work__context-items">
          {project.items.map((item) => (
            <button
              className="paw-wayfinder-work__context-item"
              data-activity={item.activity}
              data-wayfinder-context-item
              key={item.key}
              onClick={() => onOpen(item.kind, item.id, item.title)}
              type="button"
            >
              <i aria-hidden="true" className="paw-wayfinder-work__dot" />
              <span>
                <strong>{item.title}</strong>
                <small>{item.kind === 'room' ? 'Room' : 'Session'} · {item.statusLabel}</small>
                <small>{item.detail}</small>
              </span>
            </button>
          ))}
        </div>
      </section>

      <nav aria-label="项目快捷入口" className="paw-wayfinder-work__context-shortcuts">
        <button data-wayfinder-context-shortcut="settings" onClick={onOpenSystemSettings} type="button">
          <Settings2 aria-hidden="true" size={13} />系统设置
        </button>
        <button data-wayfinder-context-shortcut="observability" onClick={onOpenObservability} type="button">
          <Activity aria-hidden="true" size={13} />Trace / Eval
        </button>
        <button aria-label={`移到归档 ${project.label}`} data-wayfinder-context-shortcut="archive" onClick={onArchive} type="button">
          <Archive aria-hidden="true" size={13} />移到归档
        </button>
      </nav>

      <p className="paw-wayfinder-work__context-note">这是 PAWOS 的显示上下文，不会创建 Finder/Git 文件，也不负责管理 ProjectSettings。</p>
    </aside>
  );
}

function WorkBucket({ bucket, expanded, expandedRepeats, onDragEnd, onDragStart, onOpen, onToggle, onToggleRepeats, searching }: {
  bucket: WayfinderWorkBucket;
  expanded: boolean;
  expandedRepeats: ReadonlySet<string>;
  onDragEnd: () => void;
  onDragStart: (event: DragEvent<HTMLElement>, iconId: string) => void;
  onOpen: (kind: 'session' | 'room', id: string, title: string) => void;
  onToggle: () => void;
  onToggleRepeats: (key: string) => void;
  searching: boolean;
}) {
  // 更早 rests fully collapsed; 今天/本周 preview a short slice and only then
  // offer the rest inside the panel's own scroll area.
  const restingCount = Math.min(bucket.previewCount, bucket.items.length);
  const visibleItems = expanded ? bucket.items : bucket.items.slice(0, restingCount);
  const hiddenCount = bucket.items.length - visibleItems.length;
  const collapsedWholeBucket = !searching && bucket.previewCount === 0;
  return (
    <section className="paw-wayfinder-work__bucket" data-bucket={bucket.id}>
      {collapsedWholeBucket ? (
        <button
          aria-expanded={expanded}
          className="paw-wayfinder-work__bucket-toggle"
          onClick={onToggle}
          type="button"
        >
          {expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
          <span>{bucket.label}</span>
          <small>{bucket.items.length}</small>
        </button>
      ) : (
        <h3 className="paw-wayfinder-work__bucket-label">{bucket.label}<small>{bucket.items.length}</small></h3>
      )}
      {(!collapsedWholeBucket || expanded) ? visibleItems.map((item) => (
        <WorkRow
          expandedRepeats={expandedRepeats}
          item={item}
          onDragEnd={onDragEnd}
          onDragStart={onDragStart}
          key={item.key}
          onOpen={onOpen}
          onToggleRepeats={onToggleRepeats}
        />
      )) : null}
      {!collapsedWholeBucket && hiddenCount > 0 ? (
        <button aria-expanded={expanded} className="paw-wayfinder-work__more" onClick={onToggle} type="button">
          <ChevronDown aria-hidden="true" size={13} />
          <span>显示其余 {hiddenCount} 个对话</span>
        </button>
      ) : null}
    </section>
  );
}

function WorkRow({ expandedRepeats, item, onDragEnd, onDragStart, onOpen, onToggleRepeats }: {
  expandedRepeats: ReadonlySet<string>;
  item: WayfinderWorkItem;
  onDragEnd: () => void;
  onDragStart: (event: DragEvent<HTMLElement>, iconId: string) => void;
  onOpen: (kind: 'session' | 'room', id: string, title: string) => void;
  onToggleRepeats: (key: string) => void;
}) {
  const repeatsOpen = expandedRepeats.has(item.key);
  const meta = [item.project, wayfinderWorkTime(item.updatedAtMs, Date.now())].filter(Boolean).join(' · ');
  return (
    <div className="paw-wayfinder-work__row-shell">
      <button
        className="paw-wayfinder-work__row"
        data-activity={item.activity}
        data-dialogue-file
        data-kind={item.kind}
        data-wayfinder-icon={item.key}
        data-wayfinder-row
        draggable
        onClick={() => onOpen(item.kind, item.id, item.title)}
        onDragEnd={onDragEnd}
        onDragStart={(event) => onDragStart(event, item.key)}
        title={`${item.title} · ${meta}`}
        type="button"
      >
        <span
          aria-hidden="true"
          className="paw-wayfinder-work__file-icon"
          data-kind={item.kind}
          style={{ '--paw-work-accent': PAW_WORK_FILE_ACCENT[item.kind] } as CSSProperties}
        >
          <PawWorkFileIcon kind={item.kind} />
          <i className="paw-wayfinder-work__dot" />
        </span>
        <span className="paw-wayfinder-work__copy">
          <strong>{item.title}</strong>
          <small>
            {item.kind === 'room' ? <Users aria-hidden="true" size={11} /> : null}
            <span className="paw-wayfinder-work__kind" data-kind={item.kind}>{item.kind === 'room' ? 'Room' : 'Session'}</span>
            {item.agents.length ? (
              <span aria-label={`${item.agents.length} 位伙伴`} className="paw-wayfinder-work__agents">
                {item.agents.slice(0, 4).map((agent) => <b key={agent}>{agent}</b>)}
                {item.agents.length > 4 ? <b>+{item.agents.length - 4}</b> : null}
              </span>
            ) : null}
            <span>{item.statusLabel}{meta ? ` · ${meta}` : ''}</span>
          </small>
          <small className="paw-wayfinder-work__detail">{item.detail}</small>
        </span>
      </button>
      {item.repeats.length ? (
        <button
          aria-expanded={repeatsOpen}
          aria-label={`同名工作还有 ${item.repeats.length} 段`}
          className="paw-wayfinder-work__repeats-toggle"
          onClick={() => onToggleRepeats(item.key)}
          title={`同名工作还有 ${item.repeats.length} 段`}
          type="button"
        >
          ×{item.repeats.length + 1}
        </button>
      ) : null}
      {repeatsOpen ? item.repeats.map((repeat) => (
        <button
          className="paw-wayfinder-work__repeat"
          data-wayfinder-icon={`${repeat.kind}:${repeat.id}`}
          data-wayfinder-row
          draggable
          key={`${repeat.kind}:${repeat.id}`}
          onClick={() => onOpen(repeat.kind, repeat.id, item.title)}
          onDragEnd={onDragEnd}
          onDragStart={(event) => onDragStart(event, `${repeat.kind}:${repeat.id}`)}
          title={`${item.title} · 较早一段`}
          type="button"
        >
          <span>较早一段</span>
          <small>{wayfinderWorkTime(repeat.updatedAtMs, Date.now())}</small>
        </button>
      )) : null}
    </div>
  );
}

function applyWayfinderUiState(
  source: WayfinderWorkView,
  state: PawWayfinderState,
  baseProjects: readonly WayfinderWorkProject[],
): WayfinderWorkView & { looseItems: WayfinderWorkItem[] } {
  const archived = new Set(state.archived);
  const projectById = new Map(baseProjects.map((project) => [project.id, project]));

  /* A dialogue file with its own desktop coordinates and no folder assignment
   * is a loose desktop file: it left its project folder deliberately (dropped
   * on bare canvas) and renders as a standalone icon until it is dropped on a
   * folder again. Everything else groups into project folders as before. */
  const looseItems: WayfinderWorkItem[] = [];
  const grouped = new Map<string, WayfinderWorkItem[]>();
  for (const sourceProject of source.projects) {
    for (const item of sourceProject.items) {
      if (archived.has(item.key)) continue;
      if (!state.projectAssignments[item.key] && state.iconPositions[item.key]) {
        if (!archived.has(projectIconId(sourceProject.id))) looseItems.push(item);
        continue;
      }
      const target = state.projectAssignments[item.key]
        ? projectById.get(state.projectAssignments[item.key]!) ?? sourceProject
        : sourceProject;
      if (archived.has(projectIconId(target.id))) continue;
      const projected = target.id === item.projectKey && target.label === item.project
        ? item
        : { ...item, projectKey: target.id, project: target.label };
      const items = grouped.get(target.id) ?? [];
      items.push(projected);
      grouped.set(target.id, items);
    }
  }
  looseItems.sort((left, right) => right.updatedAtMs - left.updatedAtMs);

  const projects = [...grouped.entries()]
    .map(([id, items]) => {
      const sourceProject = projectById.get(id) ?? source.projects.find((project) => project.id === id);
      /* `items` may include a file dragged here from another project. Build
         buckets from the resulting destination set instead of filtering the
         old project's buckets, otherwise the destination summary updates but
         its opened folder renders no moved file. */
      const buckets = bucketizeWayfinderWork(items, Date.now());
      return {
        ...(sourceProject ?? {
          id,
          label: items[0]?.project ?? '未绑定项目',
          workspaceRoots: items.flatMap((item) => item.workspaceRoots),
          sessionCount: 0,
          roomCount: 0,
          runningCount: 0,
          attentionCount: 0,
          items: [],
          buckets: [],
        }),
        id,
        items,
        buckets,
        sessionCount: items.filter((item) => item.kind === 'session').length,
        roomCount: items.filter((item) => item.kind === 'room').length,
        runningCount: items.filter((item) => item.runtimeRunning).length,
        attentionCount: items.filter((item) => item.activity === 'attention').length,
      } satisfies WayfinderWorkProject;
    })
    .sort((left, right) => (right.items[0]?.updatedAtMs ?? 0) - (left.items[0]?.updatedAtMs ?? 0));

  const visibleKeys = new Set(projects.flatMap((project) => project.items.map((item) => item.key)));
  for (const item of looseItems) visibleKeys.add(item.key);
  return {
    ...source,
    buckets: source.buckets
      .map((bucket) => ({ ...bucket, items: bucket.items.filter((item) => visibleKeys.has(item.key)) }))
      .filter((bucket) => bucket.items.length > 0),
    projects,
    rowCount: visibleKeys.size,
    looseItems,
  };
}

type WayfinderProjectPanelRect = Pick<DOMRect, 'left' | 'top' | 'right' | 'bottom'>;

export type WayfinderProjectPanelPlacement = {
  x: number;
  y: number;
  horizontal: 'left' | 'right';
  vertical: 'above' | 'below';
};

/**
 * Resolve an expanded project window against the visible desktop canvas.
 * The icon remains the anchor, but the window flips to the opposite side when
 * the preferred side has no room and is finally clamped to an 8px safe inset.
 * Coordinates are relative to the project shell because the window is an
 * in-place absolutely positioned child of that shell.
 */
export function placeWayfinderProjectPanel({
  canvasRect,
  shellRect,
  panelWidth,
  panelHeight,
  bottomInset = 0,
  inset = 8,
  gap = 10,
}: {
  canvasRect: WayfinderProjectPanelRect;
  shellRect: WayfinderProjectPanelRect;
  panelWidth: number;
  panelHeight: number;
  bottomInset?: number;
  inset?: number;
  gap?: number;
}): WayfinderProjectPanelPlacement {
  const safeInset = Math.max(0, Number.isFinite(inset) ? inset : 8);
  const safeBottomInset = Math.max(0, Number.isFinite(bottomInset) ? bottomInset : 0);
  const panelW = Math.max(0, Number.isFinite(panelWidth) ? panelWidth : 0);
  const panelH = Math.max(0, Number.isFinite(panelHeight) ? panelHeight : 0);
  const canvasLeft = canvasRect.left + safeInset;
  const canvasRight = canvasRect.right - safeInset;
  const canvasTop = canvasRect.top + safeInset;
  const canvasBottom = canvasRect.bottom - safeInset - safeBottomInset;

  const clampStart = (value: number, size: number, start: number, end: number) => {
    const latestStart = Math.max(start, end - size);
    return Math.min(Math.max(value, start), latestStart);
  };

  const rightAlignedLeft = shellRect.left;
  const leftAlignedLeft = shellRect.right - panelW;
  const hasRoomOnRight = rightAlignedLeft + panelW <= canvasRight;
  const hasRoomOnLeft = leftAlignedLeft >= canvasLeft;
  const pageLeft = hasRoomOnRight
    ? rightAlignedLeft
    : hasRoomOnLeft
      ? leftAlignedLeft
      : clampStart(rightAlignedLeft, panelW, canvasLeft, canvasRight);
  const horizontal = pageLeft < shellRect.left ? 'left' : 'right';

  const belowAlignedTop = shellRect.bottom + gap;
  const aboveAlignedTop = shellRect.top - panelH - gap;
  const hasRoomBelow = belowAlignedTop + panelH <= canvasBottom;
  const hasRoomAbove = aboveAlignedTop >= canvasTop;
  const pageTop = hasRoomBelow
    ? belowAlignedTop
    : hasRoomAbove
      ? aboveAlignedTop
      : clampStart(belowAlignedTop, panelH, canvasTop, canvasBottom);
  const vertical = pageTop < shellRect.bottom ? 'above' : 'below';

  return {
    x: Math.round(pageLeft - shellRect.left),
    y: Math.round(pageTop - shellRect.top),
    horizontal,
    vertical,
  };
}

export function clampWayfinderIconPosition(position: PawWayfinderIconPosition, canvas: HTMLElement): PawWayfinderIconPosition {
  const width = Math.max(canvas.clientWidth, canvas.scrollWidth);
  const height = Math.max(canvas.clientHeight, canvas.scrollHeight);
  const x = Number.isFinite(position.x) ? position.x : 8;
  const y = Number.isFinite(position.y) ? position.y : 8;
  return {
    x: Math.round(Math.max(8, Math.min(Math.max(8, width - WAYFINDER_ICON_WIDTH - 8), x))),
    y: Math.round(Math.max(8, Math.min(Math.max(8, height - WAYFINDER_ICON_HEIGHT - 8), y))),
  };
}

function isDialogueIcon(iconId: string): boolean {
  return iconId.startsWith('session:') || iconId.startsWith('room:');
}

function isProjectIcon(iconId: string): boolean {
  return iconId.startsWith('project:');
}

function projectIconId(projectId: string): string {
  return `project:${projectId}`;
}

function openAgentHome(api: ReturnType<typeof usePawDesktopApi>): void {
  api.getState().openApp('agent', { title: 'Agent' });
}

function toggled(current: ReadonlySet<string>, key: string): ReadonlySet<string> {
  const next = new Set(current);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  return next;
}
