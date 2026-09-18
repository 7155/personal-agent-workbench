import {
  Archive,
  ArchiveRestore,
  FileText,
  Folder,
  FolderOpen,
  LoaderCircle,
  MoreHorizontal,
  PanelLeft,
  Plus,
  Search,
  Trash2,
} from 'lucide-react';
import { startTransition, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useControlTransport } from '@/app/control-transport';
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Disclosure,
  Menu,
  MenuCheckboxItem,
  MenuContent,
  MenuItem,
  MenuSeparator,
  MenuTrigger,
} from '@/components/primitives';
import type { AgentPersonaV1 } from '@/contracts/generated/agent-persona.v1';
import { parsePiModelCatalogOptions, type PiModelOption } from '@/features/agent/model-catalog-options';
import { roleItems, sessionItems, type SessionSummary } from '@/features/agent/types';
import { useAgentLiveStore } from '@/features/agent/state/live-store';
import { useRoomLiveStore } from '@/features/rooms/state/live-store';
import { selectActivePublicRoomTurn, selectPublicRoomTurnOrder } from '@/features/rooms/runtime/room-execution-lanes';
import { publicAgentErrorText } from '@/features/agent/public-error';
import { evidenceEchoFocusFromRoute } from '@/features/evidence-echo/evidence-echo';
import type { RoomSummary, RoomWorkItem } from '@/features/rooms/room-types';
import type { PawOsWindowTarget } from '@/features/paw-os/model/desktop';
import { usePawOsAppActive, usePawOsAppIdentity, usePawOsDesktop } from '@/features/paw-os/surface-context';
import { PawSessionWorkspace } from './PawSessionWorkspace';
import { PawRoomWorkspace } from './PawRoomWorkspace';
import { PawAgentHome } from './PawAgentHome';
import { readSessionCatalog } from './session-catalog';
import { PawWindowLeadingPortal, usePawWindowLeadingChromeTarget } from '../shell/PawWindowChrome';
import { TraceAgentHandoffButton, type TraceAgentHandoffInput } from '@/features/trace-agent/handoff';

type Selection =
  | { kind: 'new'; draft?: string }
  | { kind: 'session'; id: string; draft?: string }
  | { kind: 'room'; id: string; draft?: string; error?: string };

export function PawAgentApp({
  initialRoute = '',
  target,
}: {
  initialRoute?: string;
  target?: PawOsWindowTarget;
}) {
  const transport = useControlTransport();
  const desktop = usePawOsDesktop();
  const surfaceIdentity = usePawOsAppIdentity();
  const surfaceActive = usePawOsAppActive();
  /* The rail toggle reveals a leading-edge aside, so it docks in the leading
     titlebar slot and falls back inline only when that slot is absent. */
  const windowChromeTarget = usePawWindowLeadingChromeTarget();
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [rooms, setRooms] = useState<RoomSummary[]>([]);
  const [personas, setPersonas] = useState<AgentPersonaV1[]>([]);
  const [models, setModels] = useState<PiModelOption[]>([]);
  const [defaultModel, setDefaultModel] = useState('');
  const [selection, setSelection] = useState<Selection>(() => initialSelection(
    initialRoute,
    target?.kind,
    target?.id,
    target?.kind === 'participant' ? target.roomId : undefined,
  ));
  const [railOpen, setRailOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [actionError, setActionError] = useState('');
  const [actionTrace, setActionTrace] = useState<TraceAgentHandoffInput>();
  const [showArchived, setShowArchived] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<SessionSummary>();
  const [deleting, setDeleting] = useState(false);
  const [catalogRevision, setCatalogRevision] = useState(0);
  const railToggleRef = useRef<HTMLButtonElement>(null);
  const railSearchRef = useRef<HTMLInputElement>(null);
  const closeRail = useCallback(() => {
    setRailOpen(false);
    railToggleRef.current?.focus();
  }, []);
  const optimisticSessionsRef = useRef<Record<string, SessionSummary>>({});
  const optimisticRoomsRef = useRef<Record<string, RoomSummary>>({});
  /* Catalog hydration is deliberately cancellable. Opening Agent first commits
     the lightweight new-work shell; a superseded route, filter change or an
     unmounted window must never let an older catalog write into the new view. */
  const catalogRequestRef = useRef(0);
  const selectedSessionRequestRef = useRef(0);
  const targetKind = target?.kind;
  const targetId = target?.id;
  const targetRoomId = target?.kind === 'participant' ? target.roomId : undefined;
  const selectedSessionId = selection.kind === 'session' ? selection.id : '';
  /* 反向证据链只对它自己指名的那段 Session 生效；在同一扇窗里换一段
     Session 之后，落点就过期了，不该继续劫持视图。 */
  const evidenceFocus = useMemo(() => {
    const focus = evidenceEchoFocusFromRoute(initialRoute);
    if (!focus) return '';
    const routeSessionId = new URLSearchParams(initialRoute.split('?', 2)[1] ?? '').get('session') ?? '';
    return routeSessionId && selection.kind === 'session' && selection.id === routeSessionId ? focus.nodeId : '';
  }, [initialRoute, selection]);

  const toolPickerIntent = useMemo(() => {
    const params = new URLSearchParams(initialRoute.split('?', 2)[1] ?? '');
    const routeSessionId = params.get('session') || params.get('sessionId');
    const tools = params.get('tools');
    return routeSessionId === selectedSessionId && (tools === 'open' || tools === 'memory')
      ? { id: initialRoute, query: tools === 'memory' ? '记忆' : '' } : undefined;
  }, [initialRoute, selectedSessionId]);

  useEffect(() => {
    setSelection(initialSelection(initialRoute, targetKind, targetId, targetRoomId));
    setRailOpen(false);
  }, [initialRoute, targetId, targetKind, targetRoomId]);

  const loadCatalog = useCallback(async () => {
    const requestId = ++catalogRequestRef.current;
    setLoading(true);
    setLoadError('');
    const includeRooms = selection.kind !== 'session' || railOpen;
    const includeRoleModels = selection.kind === 'new';
    const [sessionResult, roomResult, roleResult, modelResult] = await Promise.allSettled([
      readSessionCatalog(transport, showArchived, () => catalogRequestRef.current === requestId),
      includeRooms
        ? transport.request({ pathId: 'agent.rooms.list', query: { limit: 100, ownerAppId: '' } })
        : Promise.resolve(undefined),
      transport.request({ pathId: 'agent.roles.list' }),
      includeRoleModels
        ? transport.request({ pathId: 'agent.role.models' })
        : Promise.resolve(undefined),
    ]);
    if (catalogRequestRef.current !== requestId) return;
    const failures = [sessionResult, roleResult,
      ...(includeRooms ? [roomResult] : []),
      ...(includeRoleModels ? [modelResult] : []),
    ]
      .filter((result) => result.status === 'rejected').length;
    /* A mock/local transport can resolve all four reads in the same turn. Put
     * the directory projection behind React's transition lane so it cannot
     * steal the click frame that opened the Agent window. */
    startTransition(() => {
      if (catalogRequestRef.current !== requestId) return;
      if (sessionResult.status === 'fulfilled') {
        /* Room Partner Sessions stay out of the ordinary work-record rail, but
         * a planet window must retain the one explicitly targeted Session so it
         * can render the same complete workspace as any other Session. */
        const listed = sessionItems(sessionResult.value, { includeAppOwned: true }).filter((item) => (
          !item.roomParticipant || item.id === selectedSessionId
        ));
        const listedIds = new Set(listed.map((item) => item.id));
        for (const id of Object.keys(optimisticSessionsRef.current)) {
          if (listedIds.has(id)) delete optimisticSessionsRef.current[id];
        }
        setSessions([
          ...Object.values(optimisticSessionsRef.current),
          ...listed.filter((item) => !optimisticSessionsRef.current[item.id]),
        ]);
      }
      if (roomResult.status === 'fulfilled' && roomResult.value !== undefined) {
        const listed = roomItems(roomResult.value).filter((item) => (
          !item.ownerAppId || item.id === (selection.kind === 'room' ? selection.id : '')
        ));
        const listedIds = new Set(listed.map((item) => item.id));
        for (const id of Object.keys(optimisticRoomsRef.current)) {
          if (listedIds.has(id)) delete optimisticRoomsRef.current[id];
        }
        setRooms([
          ...Object.values(optimisticRoomsRef.current),
          ...listed.filter((item) => !optimisticRoomsRef.current[item.id]),
        ]);
      }
      if (roleResult.status === 'fulfilled') setPersonas(roleItems(roleResult.value));
      if (modelResult.status === 'fulfilled' && modelResult.value !== undefined) {
        const catalog = parsePiModelCatalogOptions(modelResult.value);
        setModels(catalog.models);
        setDefaultModel(catalog.selectedReference);
      }
      if (failures) setLoadError(failures === 4 ? 'Agent 工作记录暂时无法读取。' : '部分 Agent 目录暂时不可用。');
      setLoading(false);
    });
  }, [railOpen, selectedSessionId, selection.kind, showArchived, transport]);

  useEffect(() => {
    if (surfaceActive === false || (selection.kind === 'session' && !railOpen)) {
      catalogRequestRef.current += 1;
      return;
    }
    let cancelled = false;
    /* Let PawAppProcess' boot surface and the Agent home commit first. The
     * catalog remains truthful, but no longer runs inside the Dock click's
     * first paint budget. */
    const frame = window.requestAnimationFrame(() => {
      if (!cancelled) void loadCatalog();
    });
    return () => {
      cancelled = true;
      window.cancelAnimationFrame(frame);
      catalogRequestRef.current += 1;
    };
  }, [catalogRevision, loadCatalog, railOpen, selection.kind, surfaceActive]);
  useEffect(() => {
    if (
      surfaceActive === false
      || selection.kind !== 'session'
      || railOpen
      || !selectedSessionId
    ) {
      return;
    }
    let cancelled = false;
    const frame = window.requestAnimationFrame(() => {
      const requestId = ++selectedSessionRequestRef.current;
      void transport.request({
        pathId: 'agent.sessions.list',
        query: { limit: 100, includeArchived: true },
      }).then((response) => {
        if (cancelled || requestId !== selectedSessionRequestRef.current) return;
        const canonical = sessionItems(response, { includeAppOwned: true })
          .find((item) => item.id === selectedSessionId);
        if (!canonical) return;
        setSessions((current) => {
          const existing = current.some((item) => item.id === canonical.id);
          return existing
            ? current.map((item) => item.id === canonical.id ? canonical : item)
            : [canonical, ...current];
        });
      }).catch(() => {
        // A direct Session can still render from its route identity. The
        // directory refresh remains the recovery path when the rail opens.
      });
    });
    return () => {
      cancelled = true;
      window.cancelAnimationFrame(frame);
      selectedSessionRequestRef.current += 1;
    };
  }, [railOpen, selectedSessionId, selection.kind, surfaceActive, transport]);

  useEffect(() => {
    if (!railOpen) return;
    const frame = window.requestAnimationFrame(() => railSearchRef.current?.focus());
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented) return;
      event.preventDefault();
      closeRail();
    };
    window.addEventListener('keydown', closeOnEscape);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener('keydown', closeOnEscape);
    };
  }, [closeRail, railOpen]);

  const normalizedQuery = query.trim().toLocaleLowerCase();
  const visibleSessions = sessions.filter((item) => (
    !item.roomParticipant && searchable(item.title, item.lastMessagePreview, normalizedQuery)
  ));
  const visibleRooms = rooms.filter((item) => searchable(item.title, item.description, normalizedQuery));
  const projectGroups = useMemo(
    () => projectWorkGroups(visibleSessions, visibleRooms),
    [visibleRooms, visibleSessions],
  );
  const projectRoots = useMemo(() => uniquePaths([
    ...sessions.flatMap((item) => item.workspaceRoots ?? []),
    ...rooms.flatMap((item) => item.workspaceRoots ?? []),
  ]).filter((root) => root !== '/'), [rooms, sessions]);
  const selectedSession = selection.kind === 'session'
    ? sessions.find((item) => item.id === selection.id)
    : undefined;
  const selectedSessionRecord = useMemo(() => (
    selectedSessionId
      ? selectedSession ?? provisionalSessionRecord(selectedSessionId, target?.title)
      : undefined
  ), [selectedSession, selectedSessionId, target?.title]);
  const selectedRoom = selection.kind === 'room'
    ? rooms.find((item) => item.id === selection.id)
    : undefined;

  useEffect(() => {
    if (!surfaceIdentity?.windowId || !desktop?.bindAgentMain) return;
    if (selection.kind === 'new') {
      desktop.bindAgentMain(surfaceIdentity.windowId);
      return;
    }
    if (selection.kind === 'session' && selectedSessionRecord) {
      desktop.bindAgentMain(surfaceIdentity.windowId, {
        kind: 'session',
        id: selectedSessionRecord.id,
        title: selectedSessionRecord.title,
      });
      return;
    }
    if (selection.kind === 'room' && selectedRoom) {
      desktop.bindAgentMain(surfaceIdentity.windowId, {
        kind: 'room',
        id: selectedRoom.id,
        title: selectedRoom.title,
        subtitle: selectedRoom.description,
      });
    }
  }, [desktop, selectedRoom, selectedSessionRecord, selection.kind, surfaceIdentity?.windowId]);

  async function archiveSession(session: SessionSummary): Promise<void> {
    const archived = session.status === 'archived';
    setActionError('');
    setActionTrace(undefined);
    try {
      await transport.request({
        pathId: 'agent.session.archive',
        params: { sessionId: session.id },
        body: { archived: !archived },
      });
      if (!archived && !showArchived) {
        setSessions((current) => current.filter((item) => item.id !== session.id));
        if (selection.kind === 'session' && selection.id === session.id) setSelection({ kind: 'new' });
      } else {
        setSessions((current) => current.map((item) => item.id === session.id
          ? { ...item, status: archived ? 'idle' : 'archived', updatedAtMs: Date.now() }
          : item));
      }
      setCatalogRevision((value) => value + 1);
    } catch (reason) {
      setActionError(errorText(reason));
      setActionTrace(agentDirectoryActionHandoff(session, archived ? 'restore' : 'archive', reason));
    }
  }

  async function deleteSession(): Promise<void> {
    if (!deleteTarget || deleting) return;
    setDeleting(true);
    setActionError('');
    setActionTrace(undefined);
    try {
      await transport.request({ pathId: 'agent.session.delete', params: { sessionId: deleteTarget.id } });
      useAgentLiveStore.getState().clear(deleteTarget.id);
      delete optimisticSessionsRef.current[deleteTarget.id];
      setSessions((current) => current.filter((item) => item.id !== deleteTarget.id));
      if (selection.kind === 'session' && selection.id === deleteTarget.id) setSelection({ kind: 'new' });
      setDeleteTarget(undefined);
    } catch (reason) {
      setActionError(errorText(reason));
      setActionTrace(agentDirectoryActionHandoff(deleteTarget, 'delete', reason));
    } finally {
      setDeleting(false);
    }
  }

  const railToggle = <button aria-controls="paw-agent-work-records" aria-expanded={railOpen} aria-label={railOpen ? '收起工作记录' : '打开工作记录'} className="paw-agent-rail-toggle" onClick={() => setRailOpen((open) => !open)} ref={railToggleRef} type="button"><PanelLeft size={16} /></button>;
  return (
    <section aria-label="Agent 工作台" className="paw-agent-app" data-rail-open={railOpen || undefined} data-selection={selection.kind} role="region">
      {windowChromeTarget ? <PawWindowLeadingPortal>{railToggle}</PawWindowLeadingPortal> : null}
      <aside aria-label="Agent 工作记录" className="paw-agent-rail" id="paw-agent-work-records" inert={!railOpen}>
        <header>
          <span><strong>工作记录</strong></span>
          <div className="paw-agent-rail__actions">
            <button aria-label="新建工作" onClick={() => { setSelection({ kind: 'new' }); closeRail(); }} type="button"><Plus size={17} /></button>
            <Menu>
              <MenuTrigger asChild><button aria-label="工作记录选项" type="button"><MoreHorizontal size={17} /></button></MenuTrigger>
              <MenuContent align="end">
                <MenuCheckboxItem checked={showArchived} onCheckedChange={(checked) => setShowArchived(checked === true)}>显示已归档 Session</MenuCheckboxItem>
              </MenuContent>
            </Menu>
          </div>
        </header>
        <label className="paw-agent-search">
          <Search size={14} />
          <input aria-label="搜索 Session 与 Room" onChange={(event) => setQuery(event.target.value)} placeholder="搜索" ref={railSearchRef} value={query} />
        </label>
        <div className="paw-agent-recents" aria-busy={loading || undefined}>
          {loading && !sessions.length && !rooms.length ? <RailNotice icon={<LoaderCircle className="ui-spin" size={15} />} text="正在读取工作记录" /> : null}
          {loadError ? (
            <RailNotice
              action={() => setCatalogRevision((value) => value + 1)}
              text={loadError}
              traceHandoff={{
                kind: 'generic',
                entityId: 'agent-catalog',
                title: 'Agent 工作记录读取失败',
                summary: loadError,
                error: loadError,
                sourceRoute: '/agent',
                refs: { operation: 'catalog-load', surface: 'agent-directory' },
              }}
            />
          ) : null}
          {actionError ? (
            <RailNotice
              action={() => { setActionError(''); setActionTrace(undefined); }}
              text={actionError}
              traceHandoff={actionTrace}
            />
          ) : null}
          {projectGroups.map((group) => (
            <ProjectFolder
              group={group}
              key={group.key}
              onOpenRoom={(id) => { setSelection({ kind: 'room', id }); closeRail(); }}
              onOpenSession={(id) => { setSelection({ kind: 'session', id }); closeRail(); }}
              onArchiveSession={(session) => void archiveSession(session)}
              onDeleteSession={(session) => { setActionError(''); setActionTrace(undefined); setDeleteTarget(session); }}
              selection={selection}
            />
          ))}
          {!loading && !loadError && !visibleSessions.length && !visibleRooms.length ? <RailNotice text={normalizedQuery ? '没有匹配的工作记录' : '还没有工作记录'} /> : null}
        </div>
      </aside>
      {windowChromeTarget ? null : railToggle}
      {railOpen ? <button aria-label="关闭工作记录" className="paw-agent-rail-backdrop" onClick={closeRail} type="button" /> : null}
      <section className="paw-agent-stage" inert={railOpen}>
        {selection.kind === 'new' ? (
          <PawAgentHome
            catalogError={loadError}
            catalogLoading={loading}
            defaultModel={defaultModel}
            initialDraft={selection.draft}
            key={`new:${selection.draft ?? ''}`}
            models={models}
            onOpenRoom={(id) => setSelection({ kind: 'room', id })}
            onOpenSession={(id) => setSelection({ kind: 'session', id })}
            onReloadCatalog={() => setCatalogRevision((value) => value + 1)}
            personas={personas}
            projectRoots={projectRoots}
            rooms={visibleRooms}
            sessions={visibleSessions}
            onCreated={(next, created, createdRoom) => {
              if (created) {
                optimisticSessionsRef.current[created.id] = created;
                setSessions((current) => [created, ...current.filter((item) => item.id !== created.id)]);
              }
              if (createdRoom) {
                optimisticRoomsRef.current[createdRoom.id] = createdRoom;
                setRooms((current) => [createdRoom, ...current.filter((item) => item.id !== createdRoom.id)]);
              }
              setSelection(next);
              setCatalogRevision((value) => value + 1);
            }}
          />
        ) : selection.kind === 'session' ? (
          <PawSessionWorkspace
            active={surfaceActive ?? true}
            key={`session:${selection.id}`}
            initialDraft={selection.draft}
            persona={personas.find((item) => item.roleId === sessions.find((session) => session.id === selection.id)?.roleId)}
            record={selectedSessionRecord}
            recordMetadataKnown={Boolean(selectedSession)}
            recordId={selection.id}
            traceFocusNodeId={evidenceFocus}
            toolPickerIntent={toolPickerIntent}
            onNewWork={() => setSelection({ kind: 'new' })}
            onSessionCreated={(created, draft) => {
              optimisticSessionsRef.current[created.id] = created;
              setSessions((current) => [created, ...current.filter((item) => item.id !== created.id)]);
              setSelection({ kind: 'session', id: created.id, ...(draft ? { draft } : {}) });
            }}
            onSessionUpdated={(updated) => {
              if (optimisticSessionsRef.current[updated.id]) optimisticSessionsRef.current[updated.id] = updated;
              setSessions((current) => current.map((item) => item.id === updated.id ? updated : item));
            }}
          />
        ) : selection.kind === 'room' ? (
          <PawRoomWorkspace
            active={surfaceActive ?? true}
            initialDraft={selection.draft}
            initialError={selection.error}
            key={`room:${selection.id}`}
            personas={personas}
            record={rooms.find((item) => item.id === selection.id)}
            recordId={selection.id}
            onRoomUpdated={(updated) => {
              if (optimisticRoomsRef.current[updated.id]) optimisticRoomsRef.current[updated.id] = updated;
              // A restored Room can receive its live snapshot before the
              // unfocused window loads the optional work-record directory.
              setRooms((current) => current.some((item) => item.id === updated.id)
                ? current.map((item) => item.id === updated.id ? updated : item)
                : [updated, ...current]);
            }}
          />
        ) : null}
      </section>
      <Dialog open={Boolean(deleteTarget)} onOpenChange={(open) => {
        if (!open && !deleting) {
          setActionError('');
          setDeleteTarget(undefined);
        }
      }}>
        <DialogContent className="paw-agent-delete-dialog">
          <DialogHeader>
            <DialogTitle>删除“{deleteTarget?.title ?? 'Session'}”</DialogTitle>
            <DialogDescription>将删除这段 Session 及其本地附件。暂时不想看到它，可以先归档。</DialogDescription>
          </DialogHeader>
          {actionError ? <p className="paw-agent-delete-dialog__error" role="alert">{actionError}</p> : null}
          <DialogFooter>
            <Button disabled={deleting} onClick={() => setDeleteTarget(undefined)} variant="quiet">取消</Button>
            <Button leadingIcon={<Trash2 size={15} />} loading={deleting} onClick={() => void deleteSession()} variant="danger">删除</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}

function WorkGroup({ children, label }: { children: ReactNode; label: string }) {
  return <section className="paw-agent-group"><header>{label}</header>{children}</section>;
}

type ProjectWorkGroup = {
  key: string;
  label: string;
  roots: string[];
  sessions: SessionSummary[];
  rooms: RoomSummary[];
};

function ProjectFolder({
  group,
  onArchiveSession,
  onDeleteSession,
  onOpenRoom,
  onOpenSession,
  selection,
}: {
  group: ProjectWorkGroup;
  onArchiveSession: (session: SessionSummary) => void;
  onDeleteSession: (session: SessionSummary) => void;
  onOpenRoom: (id: string) => void;
  onOpenSession: (id: string) => void;
  selection: Selection;
}) {
  const [open, setOpen] = useState(true);
  const total = group.sessions.length + group.rooms.length;
  return (
    <Disclosure
      className={`paw-agent-project-folder${group.key === '__trace-agent__' ? ' paw-agent-project-folder--trace' : ''}`}
      defaultOpen
      onOpenChange={setOpen}
      summary={(
        <span className="paw-agent-project-folder__summary">
          {open ? <FolderOpen aria-hidden="true" size={15} /> : <Folder aria-hidden="true" size={15} />}
          <strong>{group.label}</strong>
          <small>{total} 个对话</small>
        </span>
      )}
      title={group.roots.length ? group.roots.join('\n') : '未绑定 workspaceRoots；不按标题归组'}
    >
      <div className="paw-agent-project-folder__contents">
        {group.sessions.length ? <WorkGroup label="Session">
          {group.sessions.map((session) => (
            <WorkRow
              active={selection.kind === 'session' && selection.id === session.id}
              key={session.id}
              projection={sessionFileProjection(session)}
              onClick={() => onOpenSession(session.id)}
              title={session.title}
              trailing={session.evaluationSnapshot ? null : <SessionActions onArchive={() => onArchiveSession(session)} onDelete={() => onDeleteSession(session)} session={session} />}
            />
          ))}
        </WorkGroup> : null}
        {group.rooms.length ? <WorkGroup label="Room">
          {group.rooms.map((room) => (
            <RoomWorkRow
              active={selection.kind === 'room' && selection.id === room.id}
              key={room.id}
              room={room}
              onClick={() => onOpenRoom(room.id)}
            />
          ))}
        </WorkGroup> : null}
      </div>
    </Disclosure>
  );
}

type WorkFileProjection = {
  detail: string;
  meta: string;
  state: 'attention' | 'complete' | 'neutral' | 'working';
};

function RoomWorkRow({ active, onClick, room }: { active: boolean; onClick: () => void; room: RoomSummary }) {
  const live = useRoomLiveStore((state) => state.projections[room.id]);
  const projection = roomFileProjection(room);
  const latest = live && !live.needsSnapshot
    ? live.turnsById[selectPublicRoomTurnOrder(live).at(-1) ?? '']
    : undefined;
  if (live && latest && room.status !== 'archived') {
    const running = Boolean(selectActivePublicRoomTurn(live));
    const status = running ? '协作中'
      : latest.status === 'aborted' ? '本轮已停止'
      : latest.status === 'failed' ? '本轮失败'
      : latest.status === 'completed' ? '本轮已完成'
      : latest.status === 'queued' ? '等待启动'
      : '等待汇合';
    projection.meta = `${status} · ${room.participants.length} 位伙伴 · ${relativeTime(room.updatedAtMs)}`;
    projection.state = running ? 'working' : latest.status === 'failed' ? 'attention'
      : ['completed', 'aborted'].includes(latest.status) ? 'complete' : 'neutral';
  }
  return <WorkRow active={active} onClick={onClick} projection={projection} title={room.title} />;
}

function WorkRow({ active, onClick, projection, title, trailing }: { active: boolean; onClick: () => void; projection: WorkFileProjection; title: string; trailing?: ReactNode }) {
  return <div className="paw-agent-row-shell" data-active={active || undefined} data-work-state={projection.state}><button aria-current={active ? 'page' : undefined} className="paw-agent-row" onClick={onClick} title={title} type="button"><FileText aria-hidden="true" size={15} /><span><strong>{title}</strong><small>{projection.meta}</small><small className="paw-agent-row__detail">{projection.detail}</small></span></button>{trailing}</div>;
}

function SessionActions({ onArchive, onDelete, session }: { onArchive: () => void; onDelete: () => void; session: SessionSummary }) {
  const archived = session.status === 'archived';
  return <Menu><MenuTrigger asChild><button aria-label={`更多“${session.title}”操作`} className="paw-agent-row-menu" type="button"><MoreHorizontal size={14} /></button></MenuTrigger><MenuContent align="end"><MenuItem onSelect={onArchive}>{archived ? <ArchiveRestore size={15} /> : <Archive size={15} />}{archived ? '恢复 Session' : '归档 Session'}</MenuItem><MenuSeparator /><MenuItem className="paw-agent-row-menu__danger" onSelect={onDelete}><Trash2 size={15} />删除 Session</MenuItem></MenuContent></Menu>;
}

function RailNotice({ action, icon, text, traceHandoff }: {
  action?: () => void;
  icon?: ReactNode;
  text: string;
  traceHandoff?: TraceAgentHandoffInput;
}) {
  return <div className="paw-agent-rail-notice">
    {icon}<span>{text}</span>{action ? <button onClick={action} type="button">重试</button> : null}
    {traceHandoff ? <TraceAgentHandoffButton handoff={traceHandoff} /> : null}
  </div>;
}

function initialSelection(
  initialRoute: string,
  targetKind?: PawOsWindowTarget['kind'],
  targetId?: string,
  targetRoomId?: string,
): Selection {
  const query = new URLSearchParams(initialRoute.split('?', 2)[1] ?? '');
  const routeDraft = query.get('draft');
  const draft = routeDraft?.trim() ? routeDraft : undefined;
  const draftSelection = draft === undefined ? {} : { draft };
  if (targetKind === 'session' && targetId) return { kind: 'session', id: targetId };
  if (targetKind === 'room' && targetId) return { kind: 'room', id: targetId, ...draftSelection };
  if (targetKind === 'participant' && targetRoomId) return { kind: 'room', id: targetRoomId, ...draftSelection };
  if (initialRoute.startsWith('/rooms')) {
    const roomId = query.get('room');
    return roomId ? { kind: 'room', id: roomId, ...draftSelection } : { kind: 'new' };
  }
  const roomId = query.get('room');
  if (roomId) return { kind: 'room', id: roomId, ...draftSelection };
  const sessionId = query.get('session') || query.get('sessionId');
  if (draft) return { kind: 'new', draft };
  return sessionId ? { kind: 'session', id: sessionId } : { kind: 'new' };
}

function provisionalSessionRecord(id: string, title = ''): SessionSummary {
  return {
    id,
    title: title.trim() && title !== id ? title : 'Session',
    mode: 'assistant',
    status: 'idle',
    roleId: '',
    roleVersion: '',
    roleBookRevisionId: '',
    updatedAtMs: 0,
    workspaceRoots: [],
  };
}

function roomItems(value: unknown): RoomSummary[] {
  const envelope = record(value);
  const source = Array.isArray(envelope.items) ? envelope.items : Array.isArray(envelope.rooms) ? envelope.rooms : [];
  return source.filter((item): item is RoomSummary => {
    const room = record(item);
    return typeof room.id === 'string' && typeof room.title === 'string' && Array.isArray(room.participants);
  });
}

function searchable(title: string, detail: string | undefined, query: string): boolean {
  return !query || `${title}\n${detail ?? ''}`.toLocaleLowerCase().includes(query);
}

function uniquePaths(values: string[]): string[] {
  return values.map((value) => value.trim()).filter((value, index, all) => value.startsWith('/') && all.indexOf(value) === index).slice(0, 24);
}

function projectWorkGroups(
  sessions: readonly SessionSummary[],
  rooms: readonly RoomSummary[],
): ProjectWorkGroup[] {
  const groups = new Map<string, ProjectWorkGroup>();
  const add = (item: SessionSummary | RoomSummary, kind: 'session' | 'room') => {
    const roots = workspaceBindingRoots(item.workspaceRoots);
    // Trace diagnostics are Agent sessions technically, but they are a
    // separate work stream. Keeping them in their own folder prevents a
    // diagnostic conversation from hiding among ordinary project chats.
    const traceConversation = kind === 'session' && isTraceConversation(item as SessionSummary);
    const key = traceConversation ? '__trace-agent__' : roots.length ? roots.join('\u001f') : '__unbound__';
    const group = groups.get(key) ?? {
      key,
      label: traceConversation ? 'Trace Agent 对话' : roots.length ? pathName(roots[0]) : '未绑定项目',
      roots: traceConversation ? [] : roots,
      sessions: [],
      rooms: [],
    };
    if (kind === 'session') group.sessions.push(item as SessionSummary);
    else group.rooms.push(item as RoomSummary);
    groups.set(key, group);
  };
  sessions.forEach((session) => add(session, 'session'));
  rooms.forEach((room) => add(room, 'room'));
  return [...groups.values()];
}

function isTraceConversation(session: SessionSummary): boolean {
  const owner = `${session.ownerAppId ?? ''} ${session.surfaceKey ?? ''}`.toLocaleLowerCase();
  return owner.includes('trace-agent') || owner.includes('trace_agent') || owner.includes('traceagent');
}

function workspaceBindingRoots(roots: readonly string[] | undefined): string[] {
  return [...new Set((roots ?? []).map((root) => root.trim()).filter(Boolean))]
    .map((root) => root.length > 1 ? root.replace(/\/+$/u, '') : root)
    .sort();
}

function pathName(path: string): string {
  return path.split('/').filter(Boolean).at(-1) ?? path;
}

function projectName(paths: string[] | undefined): string {
  return paths?.[0] ? pathName(paths[0]) : '无项目';
}

function sessionFileProjection(session: SessionSummary): WorkFileProjection {
  const base = `${projectName(session.workspaceRoots)} · ${relativeTime(session.updatedAtMs)}`;
  if (session.evaluationSnapshot) {
    return {
      detail: '冻结 JSONL · 不可继续提问、改写或删除',
      meta: `评测快照 · 只读 · ${relativeTime(session.updatedAtMs)}`,
      state: 'complete',
    };
  }
  const state = session.status === 'archived' ? '已归档'
    : session.status === 'busy' ? '进行中'
    : session.status === 'faulted' ? '需要处理'
    : '就绪';
  const preview = boundedPublicText(session.lastMessagePreview);
  const detail = session.status === 'busy'
    ? preview ? `当前公开内容：${preview}` : '当前进度不可用'
    : session.status === 'faulted'
      ? preview ? `故障原因不可用 · 最近公开内容：${preview}` : '故障原因不可用'
      : preview ? `最近公开内容：${preview}` : '暂无公开进度';
  return {
    detail,
    meta: `${state} · ${base}`,
    state: session.status === 'faulted' ? 'attention'
      : session.status === 'busy' ? 'working'
      : session.status === 'archived' ? 'complete'
      : 'neutral',
  };
}

function roomFileProjection(room: RoomSummary): WorkFileProjection {
  const workItems = room.workItems;
  const activeWork = (workItems ?? []).filter((item) => ['queued', 'active', 'review', 'blocked'].includes(item.state)).length;
  const progress = activeWork ? ` · ${activeWork} 项任务` : '';
  const focus = workItems?.slice().sort(compareWorkFilePriority)[0];
  const state = focus?.state === 'blocked' || focus?.state === 'failed' ? 'attention'
    : focus?.state === 'done' || room.status === 'archived' ? 'complete'
    : 'neutral';
  // A WorkItem records delivery, not Pi execution. Only a hydrated Room
  // projection above can claim that a Root is running or terminal.
  const status = room.status === 'archived' ? '已归档'
    : state === 'attention' ? '需要处理'
    : activeWork ? '有待办'
    : state === 'complete' ? '已有交付'
    : focus?.state === 'cancelled' ? '任务已取消'
    : '就绪';
  const detail = workItems === undefined ? '任务进度不可用'
    : !focus ? '尚无任务'
    : workItemFileDetail(focus);
  return {
    detail,
    meta: `${status} · ${room.participants.length} 位伙伴${progress} · ${relativeTime(room.updatedAtMs)}`,
    state,
  };
}

function compareWorkFilePriority(left: RoomWorkItem, right: RoomWorkItem): number {
  const priorities: Record<RoomWorkItem['state'], number> = {
    blocked: 0,
    active: 1,
    review: 2,
    queued: 3,
    failed: 4,
    done: 5,
    cancelled: 6,
  };
  return priorities[left.state] - priorities[right.state] || right.updatedAtMs - left.updatedAtMs;
}

function workItemFileDetail(item: RoomWorkItem): string {
  const objective = boundedPublicText(item.objective);
  const result = boundedPublicText(item.resultSummary);
  if (item.state === 'blocked') {
    const reason = boundedPublicText(record(item.blocker).reason);
    return reason ? `阻塞：${reason}` : '阻塞原因不可用';
  }
  if (item.state === 'active') return objective ? `当前任务：${objective}` : '当前进度不可用';
  if (item.state === 'review') return objective ? `待复核：${objective}` : '复核内容不可用';
  if (item.state === 'queued') return objective ? `待开始：${objective}` : '待开始任务内容不可用';
  if (item.state === 'failed') return result ? `失败结果：${result}` : '失败原因不可用';
  if (item.state === 'done') return result ? `最近结果：${result}` : '结果摘要不可用';
  return objective ? `已取消：${objective}` : '已取消任务内容不可用';
}

function boundedPublicText(value: unknown, limit = 96): string {
  if (typeof value !== 'string') return '';
  const normalized = value.replace(/\s+/gu, ' ').trim();
  if (normalized.length <= limit) return normalized;
  return `${normalized.slice(0, limit - 1).trimEnd()}…`;
}

function relativeTime(timestamp: number): string {
  const delta = Date.now() - timestamp;
  if (delta < 60_000) return '刚刚';
  if (delta < 3_600_000) return `${Math.max(1, Math.floor(delta / 60_000))} 分钟前`;
  if (delta < 86_400_000) return `${Math.max(1, Math.floor(delta / 3_600_000))} 小时前`;
  return new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric' }).format(new Date(timestamp));
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function errorText(error: unknown): string {
  const raw = error instanceof Error && error.message ? error.message : '工作没有成功开始，请重试。';
  return publicAgentErrorText(error, raw);
}

function agentDirectoryActionHandoff(
  session: SessionSummary,
  operation: 'archive' | 'restore' | 'delete',
  reason: unknown,
): TraceAgentHandoffInput {
  const message = errorText(reason);
  const operationLabel = operation === 'archive' ? '归档' : operation === 'restore' ? '恢复' : '删除';
  return {
    kind: 'session',
    entityId: session.id,
    title: `Session ${operationLabel}失败`,
    summary: message,
    error: reason instanceof Error ? reason.message : message,
    sessionId: session.id,
    sourceRoute: `/agent?session=${encodeURIComponent(session.id)}`,
    refs: { operation, surface: 'agent-directory', sessionStatus: session.status },
  };
}
