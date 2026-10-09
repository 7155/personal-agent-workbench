import {
  ArrowDown,
  ArrowUp,
  Home,
  Check,
  ChevronLeft,
  ChevronRight,
  ClipboardCopy,
  Copy,
  File,
  FileCode2,
  FileImage,
  FileJson2,
  FileSymlink,
  FileText,
  Folder,
  FolderOpen,
  FolderTree,
  LoaderCircle,
  RefreshCw,
  ScanSearch,
  Search,
  TriangleAlert,
  Wrench,
  X,
} from 'lucide-react';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type FocusEvent, type KeyboardEvent, type ReactNode } from 'react';
import { Popover, PopoverClose, PopoverContent, PopoverTrigger } from '@/components/primitives';
import { useControlTransport } from '@/app/control-transport';
import type { ControlTransport } from '@/platform/transport';
import { recoveryScope } from '@/features/semantic-workspace/workspace-recovery';
import { useMotionActivity } from '@/design/motion';
import { CodePreview } from '@/features/agent/file-preview/CodePreview';
import { DiffPreview } from '@/features/agent/file-preview/DiffPreview';
import { MarkdownPreview } from '@/features/agent/file-preview/MarkdownPreview';
import { RichHtmlPreview } from '@/features/agent/file-preview/RichHtmlPreview';
import '@/features/agent/file-preview/file-preview.css';
import { sessionItems, type SessionSummary } from '@/features/agent/types';
import { EvidenceEchoUsage } from '@/features/evidence-echo/EvidenceEchoUsage';
import { AppSidebarToggle, useAppSidebar } from '@/paw-os/apps/app-sidebar';
import { PawWindowChromePortal, usePawWindowChromeTarget } from '@/paw-os/shell/PawWindowChrome';
import { writeClipboardText } from '@/platform/clipboard';
import { SvgFilePreview } from './SvgFilePreview';
import { canonicalWorkspaceReadPath, useWorkspaceTextEditor, type EditableWorkspacePreview } from './WorkspaceTextEditor';
import { FileCollaborationPanel } from './FileCollaborationPanel';
import './paw-os-files-app.css';

interface WorkspaceEntry {
  path: string;
  name: string;
  kind: 'directory' | 'file' | 'symlink';
  byteSize?: number;
}

interface SelectedWorkspaceFile extends WorkspaceEntry {
  sessionId: string;
}

interface WorkspaceListing {
  items: WorkspaceEntry[];
  /** The directory holds more entries than the bounded list request returned. */
  limited: boolean;
  nextOffset?: number;
}

interface WorkspacePreview extends EditableWorkspacePreview {
  path: string;
  content: string;
  /** Total file size reported by the read route, in UTF-8 bytes. */
  byteSize: number;
  /** Bytes loaded so far from offset 0 — the read route's nextOffset. */
  loadedBytes: number;
  /** More bytes remain beyond loadedBytes. */
  truncated: boolean;
}

interface VisibleTreeNode {
  path: string;
  parentPath: string | null;
  kind: 'root' | WorkspaceEntry['kind'];
}

interface PathCrumb {
  label: string;
  path: string;
  kind: 'root' | 'directory';
}

/** One bounded read request — the transport caps workspace reads at 64 KB. */
const PREVIEW_CHUNK_BYTES = 65_536;
/** Honest in-App reading window; longer files belong to Terminal/Agent tools. */
const PREVIEW_MAX_BYTES = 524_288;
/** Bounded filter projection so one broad query cannot flood the pane. */
const FILTER_MATCH_LIMIT = 120;

/** Recognition families drive glyphs and hue chips; unknown extensions stay
    a neutral document instead of pretending to be classified. */
const FILE_FAMILY: Record<string, string> = {
  c: 'code', cc: 'code', cjs: 'code', cpp: 'code', cs: 'code', css: 'code', go: 'code', h: 'code',
  htm: 'code', html: 'code', java: 'code', js: 'code', jsx: 'code', kt: 'code', mjs: 'code',
  php: 'code', py: 'code', rb: 'code', rs: 'code', scss: 'code', sh: 'code', sql: 'code',
  swift: 'code', ts: 'code', tsx: 'code', vue: 'code', zsh: 'code',
  markdown: 'doc', md: 'doc', mdx: 'doc', rst: 'doc', rtf: 'doc', txt: 'doc',
  csv: 'data', env: 'data', ini: 'data', json: 'data', jsonl: 'data', lock: 'data',
  plist: 'data', toml: 'data', tsv: 'data', xml: 'data', yaml: 'data', yml: 'data',
  avif: 'media', bmp: 'media', gif: 'media', heic: 'media', ico: 'media', jpeg: 'media',
  jpg: 'media', mp3: 'media', mp4: 'media', png: 'media', svg: 'media', webp: 'media',
  diff: 'diff', patch: 'diff',
};

// Reveal the actual focused descendant, rather than its wider copy-action
// wrapper. Keep all corrections local to the crumbs' horizontal client box.
export function nextFileCrumbScrollLeft(owner: {
  left: number; width: number; offsetWidth: number; clientLeft: number;
  clientWidth: number; scrollWidth: number; scrollLeft: number;
}, target: { left: number; right: number }): number {
  const scale = owner.width / owner.offsetWidth;
  if (!Object.values(owner).every(Number.isFinite) || !Number.isFinite(target.left) || !Number.isFinite(target.right)
    || !(scale > 0) || !(owner.clientWidth > 0)) return owner.scrollLeft;
  const left = owner.left + owner.clientLeft * scale;
  const right = left + owner.clientWidth * scale;
  const gap = target.right - target.left >= right - left
    ? target.left - left
    : target.left < left ? target.left - left : target.right > right ? target.right - right : 0;
  // Chromium can quantize scrollLeft: round away from zero so fractional gaps
  // cannot leave the focused button partially clipped after the correction.
  const delta = gap < 0 ? Math.floor(gap / scale) : Math.ceil(gap / scale);
  return Math.max(0, Math.min(owner.scrollWidth - owner.clientWidth, owner.scrollLeft + delta));
}

export function PawOsFilesApp({ initialRoute = '' }: { initialRoute?: string } = {}) {
  const transport = useControlTransport();
  const motionActive = useMotionActivity();
  const sidebar = useAppSidebar('files');
  const [treeVisible, setTreeVisible] = useState(!sidebar.collapsed);
  const [treeRevealed, setTreeRevealed] = useState(false);
  const windowChromeTarget = usePawWindowChromeTarget();
  const selectionIntent = useMemo(() => {
    const caller = requestedWorkspaceFile(initialRoute);
    if (caller.sessionId || caller.path) return { requested: caller, restored: false };
    const rememberedRoute = readFilesSelectionRoute(transport);
    return { requested: requestedWorkspaceFile(rememberedRoute), restored: Boolean(rememberedRoute) };
  }, [initialRoute, transport]);
  const requested = selectionIntent.requested;
  const requestedKey = JSON.stringify([requested.sessionId, requested.path]);
  const generationRef = useRef(0);
  const locationGenerationRef = useRef(0);
  const manualLocationRef = useRef(false);
  const [homePath, setHomePath] = useState('');
  const [location, setLocation] = useState<{ requestKey: string; path: string; selectedPath: string } | null>(null);
  const [locationInput, setLocationInput] = useState('');
  const locationInputEditRef = useRef(0);
  const locationInputSyncRef = useRef(0);
  const [locationLoading, setLocationLoading] = useState(false);
  const [locationError, setLocationError] = useState('');
  const directoryGenerationRef = useRef(0);
  const sessionsGenerationRef = useRef(0);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [sessionSelection, setSessionSelection] = useState({ requestKey: requestedKey, sessionId: '' });
  // A new file intent cannot borrow the previous selection while its Session
  // catalog is loading. Manual selection remains valid for the same intent.
  const selectedSessionId = sessionSelection.requestKey === requestedKey ? sessionSelection.sessionId : '';
  const [, setSessionsLoading] = useState(true);
  const [sessionError, setSessionError] = useState('');
  const [entries, setEntries] = useState<Record<string, WorkspaceListing>>({});
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [loadingPaths, setLoadingPaths] = useState<Set<string>>(new Set());
  const [pathErrors, setPathErrors] = useState<Record<string, string>>({});
  const [selectedFile, setSelectedFile] = useState<SelectedWorkspaceFile | null>(null);
  const [expandedIdentityKey, setExpandedIdentityKey] = useState('');
  const [openToolsKey, setOpenToolsKey] = useState('');
  const toolsTriggerRef = useRef<HTMLButtonElement>(null);
  const locationFieldRef = useRef<HTMLInputElement>(null);
  const returnToolsFocusRef = useRef(true);
  const currentIdentityRef = useRef('');
  const [preview, setPreview] = useState<WorkspacePreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState('');
  const [previewMoreLoading, setPreviewMoreLoading] = useState(false);
  const [previewMoreError, setPreviewMoreError] = useState('');
  const [treeFocusPath, setTreeFocusPath] = useState('');
  const [filterQuery, setFilterQuery] = useState('');
  const [copiedAction, setCopiedAction] = useState<'' | 'path' | 'content'>('');
  const [copyError, setCopyError] = useState('');
  const treeItemRefs = useRef(new Map<string, HTMLButtonElement>());
  const treeRef = useRef<HTMLElement | null>(null);
  const workspaceRef = useRef<HTMLDivElement | null>(null);
  const backButtonRef = useRef<HTMLButtonElement | null>(null);
  const crumbFocusFrameRef = useRef<number | null>(null);
  // Whether this App currently holds keyboard focus. A focusout that names no
  // new target means the focused element was hidden, not that someone moved
  // away, so the flag survives exactly the case the layout swap creates.
  const holdsFocusRef = useRef(false);
  const pendingFocusPathRef = useRef('');
  const pendingFocusIntentRef = useRef<Element | null>(null);
  const locationFocusIntentRef = useRef<Element | null>(null);
  const typeaheadRef = useRef({ text: '', at: 0 });
  const selectedSession = sessions.find((session) => session.id === selectedSessionId) ?? null;
  const roots = useMemo(() => location?.requestKey === requestedKey ? [location.path]
    : authorizedRoots(selectedSession).length ? authorizedRoots(selectedSession) : homePath ? [homePath] : [], [location, requestedKey, selectedSession, homePath]);
  const visibleTreeNodes = useMemo(
    () => flattenVisibleTree(roots, entries, expanded),
    [entries, expanded, roots],
  );
  const visibleEntryCount = useMemo(() => {
    const paths = new Set<string>();
    for (const listing of Object.values(entries)) {
      for (const entry of listing.items) paths.add(entry.path);
    }
    return paths.size;
  }, [entries]);
  // The filter only sees directories that have already been read; the result
  // meta states that coverage truthfully instead of implying a full-disk find.
  const normalizedFilter = filterQuery.trim().toLocaleLowerCase();
  const filterMatches = useMemo(() => {
    if (!normalizedFilter) return [];
    const seen = new Set<string>();
    const matches: WorkspaceEntry[] = [];
    for (const listing of Object.values(entries)) {
      for (const entry of listing.items) {
        if (seen.has(entry.path) || !entry.name.toLocaleLowerCase().includes(normalizedFilter)) continue;
        seen.add(entry.path);
        matches.push(entry);
      }
    }
    return matches.sort((first, second) => first.path.localeCompare(second.path, undefined, { numeric: true, sensitivity: 'base' }));
  }, [entries, normalizedFilter]);
  const shownMatches = filterMatches.slice(0, FILTER_MATCH_LIMIT);
  const filterActive = Boolean(normalizedFilter);
  const previewIsBinary = preview ? isProbablyBinary(preview.content) : false;
  const previewCapped = Boolean(preview?.truncated && preview.loadedBytes >= PREVIEW_MAX_BYTES);
  const selectedCrumbs = useMemo(
    () => (selectedFile ? pathCrumbs(selectedFile.path, roots) : []),
    [roots, selectedFile],
  );
  const previewReady = Boolean(preview && !previewLoading && !previewError);
  const currentSessionIdRef = useRef(selectedSessionId);
  useLayoutEffect(() => { currentSessionIdRef.current = selectedSessionId; }, [selectedSessionId]);
  const onFileSaved = useCallback((snapshot: EditableWorkspacePreview & { sessionId: string }) => {
    if (currentSessionIdRef.current !== snapshot.sessionId) return;
    const matchesPath = (path: string) => path === snapshot.path || path === snapshot.canonicalPath;
    setEntries((current) => Object.fromEntries(Object.entries(current).map(([directory, listing]) => [directory, {
      ...listing,
      items: listing.items.map((entry) => matchesPath(entry.path) ? { ...entry, byteSize: snapshot.byteSize } : entry),
    }])));
    setSelectedFile((current) => current?.sessionId === snapshot.sessionId && matchesPath(current.path)
      ? { ...current, byteSize: snapshot.byteSize }
      : current);
  }, []);
  const editor = useWorkspaceTextEditor(
    selectedSessionId && selectedFile?.sessionId === selectedSessionId ? selectedFile : null,
    previewReady && preview?.path === selectedFile?.path ? preview : null,
    onFileSaved,
  );
  useEffect(() => {
    if (!selectedFile || selectedFile.sessionId !== selectedSessionId
      || selectedSessionId && !sessions.some(session => session.id === selectedSessionId)) return;
    rememberFilesSelectionRoute(transport, selectedSessionId, selectedFile.path);
  }, [selectedFile?.sessionId, selectedFile?.path, selectedSessionId, sessions, transport]);
  // Loaded-line readout: honest for exactly the bytes on screen, never a
  // whole-file claim while the read window is still partial. A complete editor
  // owns its current text independently of the earlier bounded preview.
  const previewLineCount = useMemo(() => {
    const content = editor.copyContent ?? preview?.content;
    if (!content || (editor.copyContent === null && previewIsBinary)) return 0;
    const lines = content.split('\n').length;
    return content.endsWith('\n') ? lines - 1 : lines;
  }, [editor.copyContent, preview?.content, previewIsBinary]);
  const previewRenderer = previewReady && preview ? rendererLabel(preview) : '';
  const directoriesRead = Object.keys(entries).length;
  const workspacePending = !roots.length && locationLoading;
  const emptyPreview = workspacePending
    ? { title: '正在打开文件夹', detail: '正在读取本机文件，无需选择 Session。' }
    : locationError && !roots.length
      ? { title: '文件夹暂时无法读取', detail: '检查路径后重试，或从主目录重新打开。' }
      : { title: '选择要检查的文件', detail: '从目录树打开文件，也可以在地址栏输入任意文件或文件夹路径。' };

  const openLocation = useCallback(async (path: string, manual = true, association = '', focusIntent: Element | null = document.activeElement) => {
    const generation = ++locationGenerationRef.current;
    const inputEdit = locationInputEditRef.current;
    const focusInputValue = focusIntent === locationFieldRef.current ? locationFieldRef.current?.value : undefined;
    if (manual) manualLocationRef.current = true;
    setLocationLoading(true); setLocationError('');
    try {
      const response = await transport.request({ pathId: 'files.list', query: { ...(path.trim() ? { path: path.trim() } : {}), limit: 240 } });
      if (generation !== locationGenerationRef.current) return;
      if (!isRecord(response) || response.ok !== true || typeof response.path !== 'string' || !response.path.startsWith('/') || response.path.includes('\0')) throw new Error('目录服务返回了无法识别的数据。');
      workspaceListing(response);
      locationInputSyncRef.current = inputEdit;
      if (typeof response.homePath === 'string') setHomePath(response.homePath);
      if (manual) {
        locationFocusIntentRef.current = locationInputEditRef.current !== inputEdit || focusIntent === locationFieldRef.current && locationFieldRef.current?.value !== focusInputValue ? null : focusIntent;
        setSessionSelection({ requestKey: requestedKey, sessionId: association });
        rememberFilesSelectionRoute(transport, association, typeof response.selectedPath === 'string' ? response.selectedPath || response.path : response.path);
        setLocation({ requestKey: requestedKey, path: response.path, selectedPath: typeof response.selectedPath === 'string' ? response.selectedPath : '' });
        if (locationInputEditRef.current === inputEdit) setLocationInput(response.path);
        setTreeRevealed(true); sidebar.setCollapsed(false);
      } else setHomePath(response.path);
    } catch (error) {
      if (generation === locationGenerationRef.current) setLocationError(publicError(error, '文件夹读取失败。'));
    } finally { if (generation === locationGenerationRef.current) setLocationLoading(false); }
  }, [requestedKey, transport, sidebar.setCollapsed]);
  useEffect(() => {
    if (!homePath) void openLocation('', false);
    // Home discovery does not wait for the optional Session catalog.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [transport]);
  useEffect(() => {
    manualLocationRef.current = selectionIntent.restored && !requested.sessionId;
    // A remembered Session/path is only an intent. Let the current catalog
    // and authorized roots resolve it; never promote old state to a grant.
    if ((!selectionIntent.restored || !requested.sessionId)
      && (requested.path.startsWith('/') || requested.path.startsWith('~/'))) {
      void openLocation(requested.path, true, requested.sessionId, null);
    }
    return () => { locationGenerationRef.current += 1; };
    // Absolute deep links can be opened even when their Session is gone.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestedKey]);
  useEffect(() => {
    // A canonical location may change the actual tree/Session while a newer
    // address draft remains in the field. Both sync paths share this boundary.
    if (roots[0] && locationInputEditRef.current === locationInputSyncRef.current) setLocationInput(roots[0]);
  }, [roots.join('\0')]);

  const loadSessions = useCallback(async () => {
    const requestGeneration = ++sessionsGenerationRef.current;
    const inputEdit = locationInputEditRef.current;
    setSessionsLoading(true);
    setSessionError('');
    try {
      const response = await transport.request({
        pathId: 'agent.sessions.list',
        query: { limit: 100, includeArchived: false },
      });
      if (requestGeneration !== sessionsGenerationRef.current) return;
      const next = sessionItems(response, { includeAppOwned: true });
      setSessions(next);
      if (!manualLocationRef.current) locationInputSyncRef.current = inputEdit;
      const activeId = isRecord(response) && typeof response.activeSessionId === 'string'
        ? response.activeSessionId
        : '';
      setSessionSelection((current) => {
        if (manualLocationRef.current) return current.sessionId && !next.some(session => session.id === current.sessionId) ? { ...current, sessionId: '' } : current;
        if (current.requestKey === requestedKey && next.some((session) => session.id === current.sessionId)) return current;
        // A named Session supplies relative-link resolution and editor identity.
        // Do not resolve its relative links against an unrelated workspace.
        if (requested.sessionId) {
          return { requestKey: requestedKey, sessionId: next.find((session) => session.id === requested.sessionId)?.id ?? '' };
        }
        const sessionId = next.some((session) => session.id === current.sessionId)
          ? current.sessionId
          : next.find((session) => session.id === activeId && authorizedRoots(session).length)?.id
            ?? next.find((session) => authorizedRoots(session).length)?.id ?? next[0]?.id ?? '';
        return { requestKey: requestedKey, sessionId };
      });
    } catch (error) {
      if (requestGeneration !== sessionsGenerationRef.current) return;
      setSessionError(publicError(error, 'Session 列表读取失败。'));
    } finally {
      if (requestGeneration === sessionsGenerationRef.current) setSessionsLoading(false);
    }
  }, [requested.sessionId, requestedKey, transport]);

  const loadDirectory = useCallback(async (path: string, force = false, offset = 0) => {
    if (!force && (loadingPaths.has(path) || entries[path])) return;
    const requestGeneration = directoryGenerationRef.current;
    setLoadingPaths((current) => new Set(current).add(path));
    setPathErrors((current) => omitKey(current, path));
    try {
      const response = await transport.request({
        pathId: 'files.list',
        query: { path, offset, limit: 240 },
      });
      if (requestGeneration !== directoryGenerationRef.current) return;
      const listing = workspaceListing(response);
      setEntries((current) => ({ ...current, [path]: offset ? { ...listing,
        items: sortedEntries([...new Map([...(current[path]?.items ?? []), ...listing.items].map(item => [item.path, item])).values()]) } : listing }));
    } catch (error) {
      if (requestGeneration !== directoryGenerationRef.current) return;
      setPathErrors((current) => ({ ...current, [path]: publicError(error, '目录读取失败。') }));
    } finally {
      if (requestGeneration === directoryGenerationRef.current) {
        setLoadingPaths((current) => {
          const next = new Set(current);
          next.delete(path);
          return next;
        });
      }
    }
  }, [entries, loadingPaths, transport]);

  useEffect(() => {
    void loadSessions();
    return () => { sessionsGenerationRef.current += 1; };
  }, [loadSessions]);

  useEffect(() => {
    generationRef.current += 1;
    directoryGenerationRef.current += 1;
    setEntries({});
    setLoadingPaths(new Set());
    setPathErrors({});
    setExpanded(new Set(roots));
    setSelectedFile(null);
    setPreview(null);
    setPreviewLoading(false);
    setPreviewError('');
    setPreviewMoreError('');
    setPreviewMoreLoading(false);
    setFilterQuery('');
    for (const root of roots) void loadDirectory(root, true);
    // Directory state resets on location or optional editor association changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedSessionId, roots.join('\u0000')]);

  /* 绝对路径独立打开；相对证据路径以关联工作区为起点。展开目录链只走一次，
     随后保留用户自己的浏览位置。 */
  const openedRequestRef = useRef('');
  useEffect(() => { openedRequestRef.current = ''; }, [requestedKey]);
  useEffect(() => {
    const independent = location?.requestKey === requestedKey;
    if (!independent && requested.sessionId && (selectedSessionId !== requested.sessionId || !authorizedRoots(selectedSession).length)) return;
    const path = independent ? location.selectedPath || location.path : resolveRequestedWorkspacePath(requested.path, roots);
    if (!path || !roots.length) return;
    const chain = ancestorDirectories(path, roots);
    if (!chain.length) return;
    const request = `${selectedSessionId}\u0000${path}`;
    if (openedRequestRef.current === request) return;
    openedRequestRef.current = request;
    setExpanded((current) => new Set([...current, ...chain]));
    for (const directory of chain) if (!roots.includes(directory)) void loadDirectory(directory, true);
    if (roots.includes(path)) {
      // 深链指名的是一个工作区根目录（项目桌面的“在 Files 中打开”走这里）。
      // 目录不是文件，不进入读取链：展开它、把目录树焦点交给它，预览面板
      // 保持空态，由翻看的人自己选文件。
      setSelectedFile(null);
      setPreview(null);
      setPreviewError('');
      queueTreeFocus(path, independent ? locationFocusIntentRef.current : null);
      return;
    }
    setSelectedFile({ path, name: pathName(path), kind: 'file', sessionId: selectedSessionId });
    // loadDirectory changes identity with every listing; the one-shot guard,
    // not the dependency list, is what keeps this from re-opening the file.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestedKey, roots.join('\u0000'), selectedSessionId, location]);

  useEffect(() => {
    setTreeFocusPath((current) => {
      if (visibleTreeNodes.some((node) => node.path === current)) return current;
      if (selectedFile && visibleTreeNodes.some((node) => node.path === selectedFile.path)) return selectedFile.path;
      return visibleTreeNodes[0]?.path ?? '';
    });
  }, [selectedFile, visibleTreeNodes]);

  const cancelCrumbFocusReveal = useCallback(() => {
    if (crumbFocusFrameRef.current !== null) cancelAnimationFrame(crumbFocusFrameRef.current);
    crumbFocusFrameRef.current = null;
  }, []);
  const revealFocusedCrumb = useCallback((event: FocusEvent<HTMLElement>) => {
    cancelCrumbFocusReveal();
    const owner = event.currentTarget;
    const target = event.target;
    if (!(target instanceof HTMLElement) || target === owner) return;
    const identity = currentIdentityRef.current;
    // Measure once after native focus reveal, before the next paint. A newer
    // focus/file intent wins; never promote focus or move any vertical reader.
    crumbFocusFrameRef.current = requestAnimationFrame(() => {
      crumbFocusFrameRef.current = null;
      if (!owner.isConnected || !target.isConnected || !owner.contains(target)
        || document.activeElement !== target || currentIdentityRef.current !== identity) return;
      const rect = owner.getBoundingClientRect();
      const next = nextFileCrumbScrollLeft({
        left: rect.left, width: rect.width, offsetWidth: owner.offsetWidth, clientLeft: owner.clientLeft,
        clientWidth: owner.clientWidth, scrollWidth: owner.scrollWidth, scrollLeft: owner.scrollLeft,
      }, target.getBoundingClientRect());
      if (next !== owner.scrollLeft) owner.scrollLeft = next;
    });
  }, [cancelCrumbFocusReveal]);
  useEffect(() => cancelCrumbFocusReveal, [cancelCrumbFocusReveal]);

  // The App opens as rail + reader with the rail as the working object: once
  // the first listing lands, keyboard focus starts on the tree so ↑/↓/→/Enter
  // work immediately. One shot only, and never stolen from another window or
  // from a field the person is already typing in.
  const initialTreeFocusDone = useRef(false);
  useEffect(() => {
    if (initialTreeFocusDone.current || !visibleTreeNodes.length) return;
    initialTreeFocusDone.current = true;
    // A queued explicit location owns its own handoff. Otherwise only promote
    // neutral body/own-shell focus; menus and controls retain the user's intent.
    if (pendingFocusPathRef.current || !canFocusTree()) return;
    const node = treeItemRefs.current.get(visibleTreeNodes[0]?.path ?? '');
    if (!node || window.getComputedStyle(node).display === 'none') return;
    setTreeFocusPath(visibleTreeNodes[0]?.path ?? '');
    node.focus();
  }, [visibleTreeNodes]);

  const loadPreview = useCallback(async (file: SelectedWorkspaceFile) => {
    if (file.sessionId !== selectedSessionId) return;
    const generation = ++generationRef.current;
    setPreviewLoading(true);
    setPreviewMoreLoading(false);
    setPreviewError('');
    setPreviewMoreError('');
    setCopiedAction('');
    setCopyError('');
    try {
      const response = await transport.request({
        pathId: 'files.read',
        query: { path: file.path, offset: 0, limit: PREVIEW_CHUNK_BYTES, ...(selectedSessionId ? { sessionId: selectedSessionId } : {}) },
      });
      if (generation !== generationRef.current) return;
      const chunk = workspaceFileChunk(response, file.path, file.sessionId);
      const snapshot = {
        path: file.path,
        canonicalPath: chunk.canonicalPath,
        content: chunk.content,
        byteSize: chunk.byteSize,
        loadedBytes: chunk.nextOffset,
        truncated: chunk.truncated,
        resourceRevision: chunk.resourceRevision,
        editability: chunk.editability,
      };
      setPreview(snapshot);
      onFileSaved({ ...snapshot, sessionId: file.sessionId });
    } catch (error) {
      if (generation === generationRef.current) {
        setPreview(null);
        setPreviewError(publicError(error, '文件读取失败。'));
      }
    } finally {
      if (generation === generationRef.current) setPreviewLoading(false);
    }
  }, [onFileSaved, selectedSessionId, transport]);

  const loadMorePreview = useCallback(async () => {
    const current = preview;
    if (!current || !current.truncated || previewLoading || previewMoreLoading) return;
    if (current.loadedBytes >= PREVIEW_MAX_BYTES) return;
    const generation = generationRef.current;
    setPreviewMoreLoading(true);
    setPreviewMoreError('');
    try {
      const response = await transport.request({
        pathId: 'files.read',
        query: { path: current.path, offset: current.loadedBytes, limit: PREVIEW_CHUNK_BYTES, ...(selectedSessionId ? { sessionId: selectedSessionId } : {}) },
      });
      if (generation !== generationRef.current) return;
      const chunk = workspaceFileChunk(response, current.path, selectedSessionId);
      if (chunk.canonicalPath !== current.canonicalPath || (current.resourceRevision && (chunk.resourceRevision !== current.resourceRevision || chunk.byteSize !== current.byteSize))) {
        throw new Error('读取期间文件已变化，请刷新后重新读取。');
      }
      setPreview((existing) => existing && existing.path === current.path
        ? {
          ...existing,
          content: existing.content + chunk.content,
          byteSize: chunk.byteSize || existing.byteSize,
          loadedBytes: chunk.nextOffset,
          truncated: chunk.truncated,
        }
        : existing);
    } catch (error) {
      if (generation === generationRef.current) setPreviewMoreError(publicError(error, '继续读取失败。'));
    } finally {
      if (generation === generationRef.current) setPreviewMoreLoading(false);
    }
  }, [preview, previewLoading, previewMoreLoading, selectedSessionId, transport]);

  useEffect(() => {
    if (!selectedFile) return;
    void loadPreview(selectedFile);
    // Save receipts update metadata without starting another file read.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadPreview, selectedFile?.sessionId, selectedFile?.path]);

  function toggleDirectory(path: string): void {
    const willExpand = !expanded.has(path);
    setExpanded((current) => {
      const next = new Set(current);
      if (willExpand) next.add(path);
      else next.delete(path);
      return next;
    });
    if (willExpand) void loadDirectory(path);
  }

  function refresh(): void {
    // A refresh is a new directory snapshot. In-flight responses from the
    // previous snapshot must not repopulate the tree after the user asked for
    // current state (this was especially visible when a slow list returned
    // after a quick double refresh).
    directoryGenerationRef.current += 1;
    const pathsToRefresh = new Set([...roots, ...expanded]);
    setEntries({});
    setPathErrors({});
    setLoadingPaths(new Set());
    // Keep the reader selection, but make its content honest while the new
    // read is in flight instead of showing a stale document beside a fresh
    // directory tree.
    const file = selectedFile;
    setPreview(null);
    setPreviewError('');
    setPreviewMoreError('');
    for (const path of pathsToRefresh) void loadDirectory(path, true);
    if (file) void loadPreview(file);
  }

  function queueTreeFocus(path: string, intent: Element | null = document.activeElement): void {
    pendingFocusPathRef.current = path;
    pendingFocusIntentRef.current = intent;
  }

  function canFocusTree(intent: Element | null = null): boolean {
    const active = document.activeElement;
    const ownShell = treeRef.current?.closest('.paw-window-shell');
    if (ownShell && !ownShell.hasAttribute('data-active')) return false;
    if (active === document.body || active === document.documentElement || active === ownShell) return true;
    // The exact original navigation control can hand off after a late listing.
    // A newer menu, field, control or window selection must never be borrowed.
    if (!intent || active !== intent) return false;
    const ownApp = treeRef.current?.closest('.paw-files-app');
    const ownTools = locationFieldRef.current?.closest('.paw-files-tools-popover');
    return Boolean(ownApp?.contains(active) || ownTools?.contains(active));
  }

  function focusTreeItem(path: string): void {
    setTreeFocusPath(path);
    treeItemRefs.current.get(path)?.focus();
  }

  function treeHidden(): boolean {
    const tree = treeRef.current;
    return Boolean(tree && window.getComputedStyle(tree).display === 'none');
  }

  function goBackToTree(): void {
    if (!selectedFile) return;
    sidebar.setCollapsed(false);
    queueTreeFocus(selectedFile.path);
    setSelectedFile(null);
  }

  function toggleTree(): void {
    if (sidebar.collapsed || treeHidden()) {
      sidebar.setCollapsed(false);
      // The narrow pane can reveal the same mounted tree without throwing
      // away the selected file, its preview, or an in-progress editor draft.
      setTreeRevealed(true);
    } else {
      sidebar.setCollapsed(true);
      setTreeRevealed(false);
    }
  }

  /** Expand a directory's ancestor chain and hand tree focus to it. In the
      narrow reader layout the tree is hidden, so revealing also returns to
      the list; the wide layout keeps the open file beside the located row. */
  function revealInTree(path: string): void {
    setExpanded((current) => {
      const next = new Set(current);
      for (const ancestor of ancestorDirectories(path, roots)) next.add(ancestor);
      next.add(path);
      return next;
    });
    void loadDirectory(path);
    queueTreeFocus(path);
    if (treeHidden()) {
      sidebar.setCollapsed(false);
      setSelectedFile(null);
    }
  }

  function openFilterMatch(entry: WorkspaceEntry): void {
    if (entry.kind !== 'directory') {
      setSelectedFile({ ...entry, sessionId: selectedSessionId });
      return;
    }
    revealInTree(entry.path);
    setFilterQuery('');
  }

  async function copyPreviewText(action: 'path' | 'content'): Promise<void> {
    if (!selectedFile) return;
    const value = action === 'path' ? selectedFile.path : editor.copyContent ?? preview?.content ?? '';
    if (!value) return;
    setCopyError('');
    try {
      await writeClipboardText(value);
      setCopiedAction(action);
      window.setTimeout(() => setCopiedAction((current) => current === action ? '' : current), 1_500);
    } catch {
      setCopiedAction('');
      setCopyError(action === 'path'
        ? '无法访问剪贴板，文件路径没有复制。'
        : '无法访问剪贴板，文件内容没有复制。');
    }
  }

  // When the narrow layout swaps the tree for the reader, focus travels with
  // the content; going back restores focus to the row that opened the file.
  useLayoutEffect(() => { setTreeRevealed(false); }, [selectedFile]);
  useLayoutEffect(() => { setTreeVisible(!treeHidden()); }, [selectedFile, sidebar.collapsed, treeRevealed]);
  useEffect(() => {
    if (selectedFile && treeHidden()) backButtonRef.current?.focus();
  }, [selectedFile]);

  // A window drag-resize can cross the single-pane breakpoint while a file is
  // open. The rail is hidden by CSS, which strands whatever it held outside
  // the focus order; this hands focus to the return path instead, so changing
  // the window size never costs the person their place. Focus is only ever
  // reclaimed from this App's own hidden rail, never from another window.
  useEffect(() => {
    if (!selectedFile) return;
    const repairStrandedRailFocus = (): void => {
      setTreeVisible(!treeHidden());
      const rail = treeRef.current;
      if (!rail || !holdsFocusRef.current || !treeHidden()) return;
      const active = document.activeElement;
      const stranded = active === document.body || (active instanceof HTMLElement && rail.contains(active));
      if (stranded) backButtonRef.current?.focus();
    };
    const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(repairStrandedRailFocus) : null;
    if (observer && workspaceRef.current) observer.observe(workspaceRef.current);
    window.addEventListener('resize', repairStrandedRailFocus);
    return () => {
      observer?.disconnect();
      window.removeEventListener('resize', repairStrandedRailFocus);
    };
  }, [selectedFile]);

  useEffect(() => {
    const path = pendingFocusPathRef.current;
    if (!path) return;
    if (selectedFile && treeHidden()) {
      pendingFocusPathRef.current = '';
      pendingFocusIntentRef.current = null;
      return;
    }
    const node = treeItemRefs.current.get(path);
    if (!node) return;
    pendingFocusPathRef.current = '';
    const intent = pendingFocusIntentRef.current;
    pendingFocusIntentRef.current = null;
    if (!canFocusTree(intent)) return;
    setTreeFocusPath(path);
    node.focus();
  });

  function onTreeKeyDown(event: KeyboardEvent<HTMLButtonElement>, path: string): void {
    const index = visibleTreeNodes.findIndex((node) => node.path === path);
    const node = visibleTreeNodes[index];
    if (!node) return;

    if (event.key.length === 1 && event.key !== ' ' && !event.metaKey && !event.ctrlKey && !event.altKey) {
      const now = Date.now();
      const buffer = now - typeaheadRef.current.at < 700 ? typeaheadRef.current.text + event.key : event.key;
      typeaheadRef.current = { text: buffer, at: now };
      const query = (/^(.)\1+$/.test(buffer) ? buffer.charAt(0) : buffer).toLowerCase();
      const ordered = [...visibleTreeNodes.slice(index + 1), ...visibleTreeNodes.slice(0, index + 1)];
      const match = ordered.find((candidate) => pathName(candidate.path).toLowerCase().startsWith(query));
      if (match) {
        event.preventDefault();
        focusTreeItem(match.path);
      }
      return;
    }

    let nextPath = '';

    if (event.key === 'ArrowDown') nextPath = visibleTreeNodes[Math.min(index + 1, visibleTreeNodes.length - 1)]?.path ?? '';
    if (event.key === 'ArrowUp') nextPath = visibleTreeNodes[Math.max(index - 1, 0)]?.path ?? '';
    if (event.key === 'Home') nextPath = visibleTreeNodes[0]?.path ?? '';
    if (event.key === 'End') nextPath = visibleTreeNodes.at(-1)?.path ?? '';
    if (event.key === 'ArrowRight' && (node.kind === 'root' || node.kind === 'directory')) {
      if (!expanded.has(path)) toggleDirectory(path);
      else nextPath = visibleTreeNodes.find((candidate) => candidate.parentPath === path)?.path ?? '';
    }
    if (event.key === 'ArrowLeft') {
      if ((node.kind === 'root' || node.kind === 'directory') && expanded.has(path)) toggleDirectory(path);
      else nextPath = node.parentPath ?? '';
    }
    if (!nextPath && !['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    if (!nextPath && !((node.kind === 'root' || node.kind === 'directory') && ['ArrowLeft', 'ArrowRight'].includes(event.key))) return;
    event.preventDefault();
    if (nextPath) focusTreeItem(nextPath);
  }

  function renderChildren(parent: string, depth: number): ReactNode {
    const listing = entries[parent];
    if (loadingPaths.has(parent) && !listing) return <TreeState loading>正在读取目录…</TreeState>;
    if (pathErrors[parent] && !listing) {
      return <TreeState error={pathErrors[parent]} onRetry={() => void loadDirectory(parent, true)} />;
    }
    if (!listing?.items.length) return <TreeState>空目录</TreeState>;
    return (
      <>
        <ul role="group">
          {listing.items.map((entry) => {
            const directory = entry.kind === 'directory';
            const symlink = entry.kind === 'symlink';
            const open = directory && expanded.has(entry.path);
            const childListing = directory ? entries[entry.path] : undefined;
            return (
              <li key={entry.path} role="none">
                <button
                  aria-expanded={directory ? open : undefined}
                  aria-label={directory ? `${open ? '收起' : '展开'}目录 ${entry.name}` : symlink ? `打开符号链接 ${entry.name}` : `打开文件 ${entry.name}`}
                  aria-level={depth + 1}
                  aria-selected={!directory ? entry.path === selectedFile?.path : undefined}
                  className="paw-files-tree__row"
                  data-family={entryFamily(entry)}
                  data-kind={directory ? 'directory' : symlink ? 'symlink' : undefined}
                  data-selected={!directory && entry.path === selectedFile?.path || undefined}
                  onClick={() => directory ? toggleDirectory(entry.path) : setSelectedFile({ ...entry, sessionId: selectedSessionId })}
                  onFocus={() => setTreeFocusPath(entry.path)}
                  onKeyDown={(event) => onTreeKeyDown(event, entry.path)}
                  ref={(node) => {
                    if (node) treeItemRefs.current.set(entry.path, node);
                    else treeItemRefs.current.delete(entry.path);
                  }}
                  role="treeitem"
                  style={{ '--paw-files-depth': depth } as CSSProperties}
                  tabIndex={treeFocusPath === entry.path ? 0 : -1}
                  title={entry.path}
                  type="button"
                >
                  {directory
                    ? <ChevronRight className="paw-files-tree__disclosure" data-open={open || undefined} size={13} />
                    : <i aria-hidden="true" className="paw-files-tree__disclosure" />}
                  <i aria-hidden="true" className="paw-files-tree__glyph">
                    {directory ? (open ? <FolderOpen size={14} /> : <Folder size={14} />) : symlink ? <FileSymlink size={14} /> : fileGlyph(entry.name)}
                  </i>
                  <span className="paw-files-tree__label"><span>{directory ? entry.name : entryLabel(entry.name)}</span></span>
                  {directory && childListing
                    ? <small>{childListing.items.length}{childListing.limited ? '+' : ''} 项</small>
                    : !directory && entry.byteSize !== undefined
                      ? <small>{formatBytes(entry.byteSize)}</small>
                      : null}
                </button>
                {directory && open ? renderChildren(entry.path, depth + 1) : null}
              </li>
            );
          })}
        </ul>
        {listing.limited ? (
          <div className="paw-files-tree__limit" role="note" style={{ '--paw-files-depth': depth } as CSSProperties}>
            已加载 {listing.items.length} 项。
            {listing.nextOffset !== undefined ? <button type="button" disabled={loadingPaths.has(parent)} onClick={() => void loadDirectory(parent, true, listing.nextOffset)}>继续加载目录</button> : <span>目录条目已达显示上限。</span>}
          </div>
        ) : null}
      </>
    );
  }

  const identityKey = selectedFile ? JSON.stringify([selectedFile.sessionId, selectedFile.path]) : '';
  useLayoutEffect(() => {
    currentIdentityRef.current = identityKey;
    setOpenToolsKey('');
  }, [identityKey]);
  const identityExpanded = editor.editing && expandedIdentityKey === identityKey && Boolean(identityKey);
  const identityBackAction = <button aria-label="返回文件列表" className="paw-files-preview__back" onClick={goBackToTree} ref={backButtonRef} title="返回文件列表（Esc）" type="button"><ChevronLeft size={15} /><span>返回</span></button>;
  const identityCopyActions = (
    <span className="paw-files-preview__actions">
    <button
      aria-label={copiedAction === 'content' ? '已复制文件内容' : editor.editing ? '复制编辑内容' : '复制文件内容'}
      className="paw-files-preview__action"
      data-copied={copiedAction === 'content' || undefined}
      disabled={editor.copyContent !== null ? !editor.copyContent : !copyableContent(preview, previewLoading, previewError)}
      onClick={() => void copyPreviewText('content')}
      title={editor.editing ? '复制当前编辑内容' : copyContentTitle(preview, previewLoading, previewError, copiedAction === 'content')}
      type="button"
    >
      {copiedAction === 'content' ? <Check size={14} /> : <ClipboardCopy size={14} />}
    </button>
    <button
      aria-label={copiedAction === 'path' ? '已复制文件路径' : '复制文件路径'}
      className="paw-files-preview__action"
      data-copied={copiedAction === 'path' || undefined}
      onClick={() => void copyPreviewText('path')}
      title={copiedAction === 'path' ? '已复制完整路径' : '复制完整路径'}
      type="button"
    >
      {copiedAction === 'path' ? <Check size={14} /> : <Copy size={14} />}
    </button>
  </span>
  );

  const fileCollaboration = selectedFile?.sessionId ? <FileCollaborationPanel sessionId={selectedFile.sessionId} path={editor.resourcePath ?? selectedFile.path} fileName={selectedFile.name}>
    <EvidenceEchoUsage appId="files" entityId={editor.resourcePath ?? selectedFile.path} entityLabel={selectedFile.name} />
  </FileCollaborationPanel> : null;

  const locationTools = (
      <form className="paw-files-location" onSubmit={(event) => { event.preventDefault(); void openLocation(locationInput); }}>
        <button type="button" aria-label="打开主目录" onClick={() => void openLocation('')}><Home size={14} /></button>
        <button type="button" aria-label="上一级文件夹" disabled={!roots[0] || roots[0] === '/'} onClick={() => void openLocation(roots[0]?.replace(/\/[^/]+\/?$/, '') || '/')}><ArrowUp size={14} /></button>
        <input ref={locationFieldRef} aria-label="文件或文件夹路径" value={locationInput} onChange={(event) => { locationInputEditRef.current += 1; setLocationInput(event.target.value); }} placeholder="输入路径，如 ~/Documents 或 /Volumes" spellCheck={false} />
        <button type="submit" disabled={locationLoading} aria-label="打开路径">{locationLoading ? <LoaderCircle className="ui-spin" size={14} /> : <ChevronRight size={14} />}</button>
      </form>
  );

  const filesTools = (
    <div className="paw-files-app__toolbar" data-motion-active={motionActive} data-window-chrome={windowChromeTarget ? true : undefined}>
      <AppSidebarToggle className="paw-files-tree-toggle" collapsed={sidebar.collapsed || !treeVisible} controlsId={sidebar.controlsId} label="文件目录" onToggle={toggleTree} toggleRef={sidebar.toggleRef} />
      <label className="paw-files-scope">
        <FolderTree aria-hidden="true" size={13} />
        <span className="sr-only">Session</span>
        <select
          aria-label="选择文件所属 Session"
          onChange={(event) => {
            manualLocationRef.current = true; locationGenerationRef.current += 1;
            locationInputSyncRef.current = locationInputEditRef.current;
            setLocationLoading(false); setLocationError(''); setLocation(null);
            setSessionSelection({ requestKey: requestedKey, sessionId: event.target.value });
            rememberFilesSelectionRoute(transport, event.target.value, '');
            if (!event.target.value) void openLocation('');
          }}
          value={selectedSessionId}
        >
          <option value="">本机文件（无需 Session）</option>
          {selectedSessionId && !sessions.some(session => session.id === selectedSessionId) ? <option value={selectedSessionId}>关联 Session · 正在读取</option> : null}
          {sessions.map((session) => (
            <option key={session.id} title={session.title} value={session.id}>
              {session.title}{authorizedRoots(session).length ? '' : ' · 无工作区'}
            </option>
          ))}
        </select>
      </label>
      <label className="paw-files-filter">
        <Search aria-hidden="true" size={13} />
        <input
          aria-label="筛选已加载的文件"
          disabled={!roots.length}
          onChange={(event) => setFilterQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Escape') setFilterQuery('');
          }}
          placeholder="筛选已加载的文件"
          spellCheck={false}
          type="search"
          value={filterQuery}
        />
        {filterActive ? <span aria-hidden="true" className="paw-files-filter__count">{filterMatches.length}</span> : null}
      </label>
      <button aria-busy={loadingPaths.size ? true : undefined} aria-label="刷新文件" className="paw-files-refresh" disabled={!roots.length || loadingPaths.size > 0} onClick={refresh} type="button"><RefreshCw className={loadingPaths.size ? 'ui-spin' : undefined} size={14} /><span>刷新</span></button>
    </div>
  );

  return (
    <>
      {windowChromeTarget ? <PawWindowChromePortal>{filesTools}</PawWindowChromePortal> : null}
      <section
        className="paw-files-app"
        data-motion-active={motionActive}
        data-editing={editor.editing || undefined}
        onBlur={(event) => {
          if (event.relatedTarget && !event.currentTarget.contains(event.relatedTarget)) holdsFocusRef.current = false;
        }}
        onFocus={() => { holdsFocusRef.current = true; }}
      >
        <h1 className="paw-files-app__title">文件</h1>
        {windowChromeTarget ? null : filesTools}
        {!editor.editing ? <details className="paw-files-location-disclosure" open>
          <summary>文件夹位置</summary>
          {locationTools}
        </details> : null}
        {locationError ? <div className="paw-native-app__error" role="alert"><TriangleAlert size={16} />{locationError}<button onClick={() => void openLocation(locationInput)} type="button">重试打开</button></div> : null}
        {sessionError ? <div className="paw-files-session-notice" role="status">Session 快捷入口暂时无法读取，本机文件仍可浏览。<button type="button" onClick={() => void loadSessions()}>重试 Session 列表</button></div> : null}
        <div className="paw-files-app__workspace" data-file-open={selectedFile ? true : undefined} data-sidebar-collapsed={sidebar.collapsed} data-tree-revealed={treeRevealed || undefined} ref={workspaceRef}>
        <aside className="paw-files-tree" aria-label="文件目录" {...sidebar.contentProps} ref={(node) => { treeRef.current = node; sidebar.contentProps.ref(node); }}>
          {roots.length ? (
            <header className="paw-files-tree__head">
              <strong>目录</strong>
              {loadingPaths.size ? (
                <span className="paw-files-tree__head-scan">
                  <LoaderCircle aria-hidden="true" className="ui-spin" size={11} />
                  正在读取 {loadingPaths.size} 个目录
                </span>
              ) : (
                <span className="paw-files-tree__head-count">{directoriesRead} 目录 · {visibleEntryCount} 项</span>
              )}
            </header>
          ) : null}
          <div className="paw-files-tree__scroll">
            {workspacePending ? <TreeState loading>正在打开主目录…</TreeState> : null}
            {roots.length && filterActive ? (
              <div className="paw-files-filter-results">
                <p aria-live="polite" className="paw-files-filter-results__meta">
                  在已加载的 {visibleEntryCount} 项中匹配 {filterMatches.length} 项
                  {filterMatches.length > shownMatches.length ? `，仅显示前 ${shownMatches.length} 项` : ''}
                </p>
                {shownMatches.length ? (
                  <ul aria-label="筛选结果">
                    {shownMatches.map((entry) => {
                      const directory = entry.kind === 'directory';
                      const symlink = entry.kind === 'symlink';
                      return (
                        <li key={entry.path}>
                          <button
                            aria-label={directory ? `在目录树中展开 ${entry.name}` : `打开文件 ${entry.name}`}
                            className="paw-files-tree__row"
                            data-family={entryFamily(entry)}
                            data-kind={directory ? 'directory' : symlink ? 'symlink' : undefined}
                            data-selected={!directory && entry.path === selectedFile?.path || undefined}
                            onClick={() => openFilterMatch(entry)}
                            onFocus={() => setTreeFocusPath(entry.path)}
                            ref={(node) => {
                              if (node) treeItemRefs.current.set(entry.path, node);
                              else treeItemRefs.current.delete(entry.path);
                            }}
                            title={entry.path}
                            type="button"
                          >
                            <i aria-hidden="true" className="paw-files-tree__disclosure" />
                            <i aria-hidden="true" className="paw-files-tree__glyph">
                              {directory ? <Folder size={14} /> : symlink ? <FileSymlink size={14} /> : fileGlyph(entry.name)}
                            </i>
                            <span className="paw-files-tree__label"><span>{highlightMatch(entry.name, normalizedFilter)}</span></span>
                            <small>{rootRelativeParent(entry.path, roots)}</small>
                          </button>
                        </li>
                      );
                    })}
                  </ul>
                ) : <TreeState>没有匹配已加载的条目。</TreeState>}
              </div>
            ) : null}
            {roots.length && !filterActive ? (
              <nav aria-label="项目文件"><ul aria-label="项目文件" role="tree">
                {roots.map((root) => {
                  const rootListing = entries[root];
                  return (
                    <li className="paw-files-tree__root" key={root} role="none">
                      <button
                        aria-expanded={expanded.has(root)}
                        aria-label={`${expanded.has(root) ? '收起' : '展开'}工作区 ${pathName(root)}，路径 ${root}`}
                        aria-level={1}
                        className="paw-files-tree__root-button"
                        onClick={() => toggleDirectory(root)}
                        onFocus={() => setTreeFocusPath(root)}
                        onKeyDown={(event) => onTreeKeyDown(event, root)}
                        ref={(node) => {
                          if (node) treeItemRefs.current.set(root, node);
                          else treeItemRefs.current.delete(root);
                        }}
                        role="treeitem"
                        tabIndex={treeFocusPath === root ? 0 : -1}
                        title={root}
                        type="button"
                      >
                        <ChevronRight className="paw-files-tree__disclosure" data-open={expanded.has(root) || undefined} size={14} />
                        <i aria-hidden="true" className="paw-files-tree__root-glyph">
                          {expanded.has(root) ? <FolderOpen size={15} /> : <Folder size={15} />}
                        </i>
                        <span className="paw-files-tree__root-id"><strong>{pathName(root)}</strong><small>{root}</small></span>
                        {rootListing ? <span aria-hidden="true" className="paw-files-tree__root-count">{rootListing.items.length}{rootListing.limited ? '+' : ''}</span> : null}
                      </button>
                      {expanded.has(root) ? renderChildren(root, 1) : null}
                    </li>
                  );
                })}
              </ul></nav>
            ) : null}
          </div>
        </aside>
        <section aria-label="文件预览" className="paw-files-preview" onKeyDown={(event) => { if (event.key === 'Escape' && treeHidden()) goBackToTree(); }} role="region">
          {!selectedFile ? (
            <div aria-busy={workspacePending || undefined} className="paw-files-preview__empty">
              <div aria-hidden="true" className="paw-files-preview__empty-art">
                <i />
                <i />
                <span><ScanSearch size={17} /></span>
              </div>
              <strong>{emptyPreview.title}</strong>
              <span>{emptyPreview.detail}</span>
              {roots.length ? <span className="paw-files-preview__empty-keys">
                <span><kbd>↑</kbd><kbd>↓</kbd><span>移动</span></span>
                <span><kbd>→</kbd><span>展开目录</span></span>
                <span><kbd>Enter</kbd><span>打开</span></span>
              </span> : null}
            </div>
          ) : (
            <>
              <header key={`header:${selectedFile.path}`}>
                {!identityExpanded ? identityBackAction : null}
                <span aria-hidden="true" className="paw-files-preview__badge" data-family={entryFamily(selectedFile)}>
                  {fileExtension(selectedFile.name)
                    ? fileExtension(selectedFile.name).slice(0, 4).toUpperCase()
                    : selectedFile.kind === 'symlink' ? <FileSymlink size={15} /> : <File size={15} />}
                </span>
                <details
                  className="paw-files-preview__id"
                  open={!editor.editing || identityExpanded}
                >
                  <summary onClick={(event) => {
                    if (!editor.editing) return;
                    // Native Enter/Space activates this summary through click as
                    // well. Reflow the controls and disclosure in one React commit.
                    event.preventDefault();
                    setExpandedIdentityKey(identityExpanded ? '' : identityKey);
                  }}><h2 title={pathName(selectedFile.path)}>{pathName(selectedFile.path)}</h2><ChevronRight size={14} aria-hidden="true" /></summary>
                  <small className="paw-files-crumbs" onFocus={identityExpanded ? revealFocusedCrumb : undefined} onBlur={(event) => {
                    if (!event.currentTarget.contains(event.relatedTarget)) cancelCrumbFocusReveal();
                  }} title={selectedFile.path} role="group" aria-label="文件路径与属性" tabIndex={editor.editing ? 0 : undefined}>
                    {selectedCrumbs.length ? selectedCrumbs.map((crumb) => (
                      <button
                        aria-label={`在目录树中定位 ${crumb.label}`}
                        className="paw-files-crumbs__segment"
                        data-root={crumb.kind === 'root' || undefined}
                        key={crumb.path}
                        onClick={() => revealInTree(crumb.path)}
                        title={crumb.path}
                        type="button"
                      >
                        {crumb.label}
                      </button>
                    )) : <span className="paw-files-crumbs__plain">{selectedFile.path}</span>}
                    <span className="paw-files-crumbs__meta">
                      {previewRenderer
                        ? <b className="paw-files-renderer" data-family={entryFamily(selectedFile)}>{previewRenderer}</b>
                        : <span>{fileExtension(selectedFile.name).toUpperCase() || '文件'}</span>}
                      {selectedFile.byteSize !== undefined ? <span>{formatBytes(selectedFile.byteSize)}</span> : null}
                      {(editor.editing || previewReady) && previewLineCount > 0
                        ? <span>{!editor.editing && preview?.truncated ? `已载 ${previewLineCount} 行` : `${previewLineCount} 行`}</span>
                        : null}
                    </span>
                    {identityExpanded ? <>{identityCopyActions}{identityBackAction}</> : null}
                  </small>
                </details>
                {!editor.editing && !identityExpanded ? identityCopyActions : null}
                {editor.editing ? (
                  <Popover key={identityKey} open={openToolsKey === identityKey} onOpenChange={(open) => {
                    if (open) returnToolsFocusRef.current = true;
                    setOpenToolsKey(open ? identityKey : '');
                  }}>
                    <PopoverTrigger asChild>
                      <button aria-label="文件工具" title="位置、复制与协作" className="paw-files-preview__action paw-files-tools-trigger" ref={toolsTriggerRef} type="button"><Wrench size={16} /></button>
                    </PopoverTrigger>
                    <PopoverContent align="start" side="right" className="paw-files-tools-popover" aria-label="文件工具" data-motion-active={motionActive}
                      onOpenAutoFocus={(event) => { event.preventDefault(); locationFieldRef.current?.focus(); }}
                      onInteractOutside={(event) => {
                        const target = event.target;
                        // Explicit outside controls own their focus, including
                        // Save/discard and another file or window selection.
                        if (target instanceof Element) {
                          const ownWindow = toolsTriggerRef.current?.closest('.paw-window-shell');
                          const ownScope = ownWindow ?? toolsTriggerRef.current?.closest('.paw-files-app');
                          const outsideWindow = target.closest('.paw-window-shell');
                          // Whole-window/desktop tabIndex=-1 containers do not
                          // turn neutral descendants into local controls.
                          const outsideControl = target.closest('button, input, textarea, select, a[href], summary, [role="button"], [role="link"], [contenteditable="true"], [contenteditable=""], [tabindex]:not(.paw-window-shell)');
                          const localControl = outsideControl && ownScope?.contains(outsideControl);
                          // A direct explicit desktop control still owns its
                          // click intent; never return focus from a dock/menu.
                          const explicitControl = target.closest('button, input, textarea, select, a[href], summary, [role="button"], [role="link"], [contenteditable="true"], [contenteditable=""]');
                          const externalControlIntent = explicitControl && !ownScope?.contains(target);
                          if (localControl || externalControlIntent || outsideWindow && outsideWindow !== ownWindow) returnToolsFocusRef.current = false;
                        }
                      }}
                      onCloseAutoFocus={(event) => {
                        event.preventDefault();
                        if (returnToolsFocusRef.current && currentIdentityRef.current === identityKey && toolsTriggerRef.current?.isConnected) toolsTriggerRef.current.focus();
                      }}
                      onKeyDown={(event) => { if (event.key === 'Escape') event.stopPropagation(); }}
                    >
                      <div className="paw-files-tools-popover__heading"><strong>位置与协作</strong><PopoverClose asChild><button className="paw-files-preview__action" aria-label="关闭文件工具" type="button"><X size={16} /></button></PopoverClose></div>
                      {locationTools}
                      {!identityExpanded ? <div className="paw-files-tools-popover__copy"><span>复制</span>{identityCopyActions}</div> : null}
                      {fileCollaboration}
                    </PopoverContent>
                  </Popover>
                ) : null}
              </header>
              {copyError ? (
                <div className="paw-files-preview__copy-error" role="alert">
                  <TriangleAlert aria-hidden="true" size={14} />
                  <span>{copyError}</span>
                </div>
              ) : null}
              <div className="paw-files-preview__body" key={`body:${selectedFile.path}`}>
                {editor.panel}
                {previewLoading ? (
                  <div className="paw-files-preview__state paw-files-preview__state--loading" role="status">
                    <span className="paw-files-preview__state-line"><LoaderCircle className="ui-spin" size={16} />正在读取文件…</span>
                    <span aria-hidden="true" className="paw-files-skeleton paw-files-skeleton--reader"><i /><i /><i /><i /><i /><i /></span>
                  </div>
                ) : null}
                {previewError ? <div className="paw-files-preview__state" role="alert"><TriangleAlert size={18} /><span>{previewError}</span><button onClick={() => void loadPreview(selectedFile)} type="button">重试</button></div> : null}
                {editor.draftPreview ? renderPreview(editor.draftPreview) : !editor.editing && !previewLoading && !previewError && preview ? renderPreview(preview) : null}
              </div>
              {!editor.editing && preview && !previewLoading && !previewError && preview.truncated && !previewIsBinary ? (
                <footer className="paw-files-preview__more">
                  <div className="paw-files-preview__range">
                    <span className="paw-files-preview__range-readout">
                      <span>已显示前 {formatBytes(preview.loadedBytes)} · 共 {formatBytes(preview.byteSize)}</span>
                      <em aria-hidden="true">{honestShare(preview)}%</em>
                    </span>
                    <span
                      aria-label={`已读取 ${formatBytes(preview.loadedBytes)}，共 ${formatBytes(preview.byteSize)}`}
                      aria-valuemax={100}
                      aria-valuemin={0}
                      aria-valuenow={honestShare(preview)}
                      className="paw-files-preview__meter"
                      data-busy={previewMoreLoading || undefined}
                      role="progressbar"
                      style={meterStyle(preview)}
                    >
                      <i />
                      {capMarkerShare(preview) ? <b aria-hidden="true" title={`${formatBytes(PREVIEW_MAX_BYTES)} 预览上限`} /> : null}
                    </span>
                  </div>
                  {previewMoreError ? <em role="alert">{previewMoreError}</em> : null}
                  {previewCapped ? (
                    <em>已达 {formatBytes(PREVIEW_MAX_BYTES)} 预览上限，更长内容请用 Terminal 或 Agent 工具查看。</em>
                  ) : (
                    <button aria-busy={previewMoreLoading || undefined} disabled={previewMoreLoading} onClick={() => void loadMorePreview()} type="button">
                      {previewMoreLoading ? <LoaderCircle className="ui-spin" size={13} /> : <ArrowDown size={13} />}
                      <span>继续读取 {formatBytes(Math.min(PREVIEW_CHUNK_BYTES, Math.max(1, preview.byteSize - preview.loadedBytes)))}</span>
                    </button>
                  )}
                </footer>
              ) : null}
              {!editor.editing ? fileCollaboration : null}
            </>
          )}
        </section>
        </div>
        <footer className="paw-files-statusbar" aria-live="polite">
          <span>已加载 {visibleEntryCount} 项</span>
          {filterActive ? <><i aria-hidden="true" /><span>匹配 {filterMatches.length} 项</span></> : null}
          {selectedFile ? <><i aria-hidden="true" /><span className="paw-files-statusbar__selection" title={`${selectedFile.path}${selectedFile.byteSize !== undefined ? ` · ${formatBytes(selectedFile.byteSize)}` : ''}`}>已选 {selectedFile.name}{selectedFile.byteSize !== undefined ? ` · ${formatBytes(selectedFile.byteSize)}` : ''}</span></> : null}
          <span className="paw-files-statusbar__root" data-live={roots.length ? true : undefined} title={roots.join('\n') || undefined}>{roots.length ? authorizedRoots(selectedSession).some((root) => roots.some((path) => path === root || path.startsWith(root === '/' ? '/' : `${root}/`))) ? 'Session 工作区快捷入口 · 本机读取' : '本机文件 · 无需 Session' : workspacePending ? '正在打开文件夹' : '输入路径以开始浏览'}</span>
        </footer>
      </section>
    </>
  );
}

function TreeState({ children, error, loading, onRetry }: { children?: ReactNode; error?: string; loading?: boolean; onRetry?: () => void }) {
  return (
    <div className="paw-files-tree__state" data-error={error ? true : undefined} role={error ? 'alert' : loading ? 'status' : undefined}>
      <span className="paw-files-tree__state-line">
        {loading ? <LoaderCircle className="ui-spin" size={14} /> : error ? <TriangleAlert size={14} /> : null}
        <span>{error ?? children}</span>
        {onRetry ? <button onClick={onRetry} type="button">重试</button> : null}
      </span>
      {loading ? <span aria-hidden="true" className="paw-files-skeleton paw-files-skeleton--tree"><i /><i /><i /></span> : null}
    </div>
  );
}

function renderPreview(file: WorkspacePreview): ReactNode {
  const name = pathName(file.path);
  const extension = fileExtension(name);
  if (!file.content && !file.truncated) {
    return (
      <div className="paw-files-preview__state paw-files-preview__state--empty" role="status">
        <i aria-hidden="true" className="paw-files-preview__state-mark"><File size={17} /></i>
        <span>这个文件是空的。</span>
      </div>
    );
  }
  if (isProbablyBinary(file.content)) {
    return (
      <div className="paw-files-preview__state paw-files-preview__state--binary" role="status">
        <i aria-hidden="true" className="paw-files-preview__state-mark"><FileImage size={17} /></i>
        <span>二进制文件不能作为文本预览。</span>
        <small>可从上方复制完整路径，用 Terminal 或 Agent 工具检查原始内容。</small>
      </div>
    );
  }
  if (extension === 'svg' && !file.truncated) return <SvgFilePreview content={file.content} fileName={name} />;
  if (['md', 'mdx', 'markdown'].includes(extension)) return <MarkdownPreview content={file.content} />;
  if (['html', 'htm'].includes(extension)) return <RichHtmlPreview content={file.content} title={name} />;
  if (['diff', 'patch'].includes(extension)) return <DiffPreview content={file.content} fileName={name} />;
  return <CodePreview content={file.content} fileName={name} language={fileLanguage(name)} />;
}

function highlightMatch(name: string, query: string): ReactNode {
  const index = name.toLocaleLowerCase().indexOf(query);
  if (index < 0 || !query) return name;
  return (
    <>
      {name.slice(0, index)}
      <mark>{name.slice(index, index + query.length)}</mark>
      {name.slice(index + query.length)}
    </>
  );
}

// This preference keeps the existing Files route string, not file contents
// or permissions. Anonymous surfaces do not share another backend's intent.
function filesSelectionKey(transport: ControlTransport): string {
  return recoveryScope(transport, `files-selection:${transport.kind}`);
}
function readFilesSelectionRoute(transport: ControlTransport): string {
  const key = filesSelectionKey(transport);
  if (!key) return '';
  try {
    const route = localStorage.getItem(key) ?? '';
    if (!route.startsWith('/files?')) return '';
    const query = new URLSearchParams(route.slice('/files?'.length));
    const rawSession = query.get('session') ?? '';
    const rawPath = query.get('path') ?? '';
    const requested = requestedWorkspaceFile(route);
    return rawSession === requested.sessionId && rawPath === requested.path
      && !rawSession.includes('\0') && !rawPath.includes('\0')
      && (!rawPath || rawPath.startsWith('/')) ? route : '';
  } catch { return ''; }
}
function rememberFilesSelectionRoute(transport: ControlTransport, sessionId: string, path: string): void {
  const key = filesSelectionKey(transport);
  if (!key || sessionId.length > 200 || path.length > 1_000
    || sessionId.includes('\0') || path.includes('\0') || path && !path.startsWith('/')) return;
  try { localStorage.setItem(key, `/files?${new URLSearchParams({ session: sessionId, path })}`); }
  catch { /* Keep the current selection usable if local preference storage fails. */ }
}

/** `/files?session=…&path=…` — 正向证据链把一个具体文件交给这扇窗。 */
function requestedWorkspaceFile(initialRoute: string): { sessionId: string; path: string } {
  const query = new URLSearchParams(initialRoute.split('?', 2)[1] ?? '');
  const path = (query.get('path') ?? '').trim();
  return {
    sessionId: (query.get('session') ?? '').trim().slice(0, 200),
    path: path.slice(0, 1_000),
  };
}

/** Markdown in a Session usually names files relative to its workspace. The
 * workspace route still uses an absolute path internally so directory listing
 * and reads keep one canonical identity. A root-name prefix remains supported
 * for the common `repository/src/file.ts` form. */
function resolveRequestedWorkspacePath(path: string, roots: string[]): string {
  const normalized = path.trim();
  if (!normalized) return '';
  if (normalized.startsWith('/')) return normalized;
  const relative = normalized.replace(/^\.\//u, '');
  if (!relative || relative.split('/').some((segment) => !segment || segment === '..')) return '';
  const namedRoot = roots.find((root) => (
    relative === pathName(root) || relative.startsWith(`${pathName(root)}/`)
  ));
  if (namedRoot) {
    const suffix = relative.slice(pathName(namedRoot).length).replace(/^\//u, '');
    return suffix ? `${namedRoot}/${suffix}` : namedRoot;
  }
  const root = roots[0];
  return root ? `${root}/${relative}` : '';
}

function authorizedRoots(session: SessionSummary | null): string[] {
  return (session?.workspaceRoots ?? []).filter((path, index, roots) => path.startsWith('/') && roots.indexOf(path) === index);
}

// Directory listings arrive in service order; Files presents them the way a
// file browser reads: directories first, then names in natural order.
function sortedEntries(items: WorkspaceEntry[]): WorkspaceEntry[] {
  return [...items].sort((first, second) => {
    const firstRank = first.kind === 'directory' ? 0 : 1;
    const secondRank = second.kind === 'directory' ? 0 : 1;
    if (firstRank !== secondRank) return firstRank - secondRank;
    return first.name.localeCompare(second.name, undefined, { numeric: true, sensitivity: 'base' });
  });
}

function ancestorDirectories(path: string, roots: string[]): string[] {
  const root = roots.find((candidate) => path === candidate || path.startsWith(candidate === '/' ? '/' : `${candidate}/`));
  if (!root) return [];
  const chain = [root];
  const segments = path.slice(root.length).split('/').filter(Boolean);
  segments.pop();
  let current = root;
  for (const segment of segments) {
    current = `${current === '/' ? '' : current}/${segment}`;
    chain.push(current);
  }
  return chain;
}

/** Root-relative crumb chain for the reader header: the workspace root first,
    then every intermediate directory. The file's own name stays in the h2. */
function pathCrumbs(path: string, roots: string[]): PathCrumb[] {
  const root = roots.find((candidate) => path === candidate || path.startsWith(candidate === '/' ? '/' : `${candidate}/`));
  if (!root) return [];
  const crumbs: PathCrumb[] = [{ label: pathName(root), path: root, kind: 'root' }];
  const segments = path.slice(root.length).split('/').filter(Boolean);
  segments.pop();
  let current = root;
  for (const segment of segments) {
    current = `${current === '/' ? '' : current}/${segment}`;
    crumbs.push({ label: segment, path: current, kind: 'directory' });
  }
  return crumbs;
}

function rootRelativeParent(path: string, roots: string[]): string {
  const root = roots.find((candidate) => path.startsWith(candidate === '/' ? '/' : `${candidate}/`));
  const parent = path.split('/').slice(0, -1).join('/');
  if (!root) return parent || '/';
  const relative = parent.slice(root.length).replace(/^\//, '');
  return relative ? `${pathName(root)}/${relative}` : pathName(root);
}

function copyableContent(preview: WorkspacePreview | null, loading: boolean, error: string): boolean {
  if (!preview || loading || error) return false;
  return preview.content !== '' && !isProbablyBinary(preview.content);
}

function copyContentTitle(preview: WorkspacePreview | null, loading: boolean, error: string, copied: boolean): string {
  if (copied) return '已复制文件内容';
  if (loading || error || !preview) return '内容尚未读取';
  if (preview.content === '') return '文件没有文本内容';
  if (isProbablyBinary(preview.content)) return '二进制内容不能复制为文本';
  return preview.truncated ? `复制已加载的前 ${formatBytes(preview.loadedBytes)} 内容` : '复制文件内容';
}

function workspaceListing(value: unknown): WorkspaceListing {
  if (!isRecord(value) || !Array.isArray(value.items)) throw new Error('目录服务返回了无法识别的数据。');
  const items = value.items.flatMap((item): WorkspaceEntry[] => {
    if (!isRecord(item)) return [];
    const path = typeof item.path === 'string' ? item.path.trim() : '';
    const name = typeof item.name === 'string' ? item.name.trim() : '';
    const kind = String(item.kind);
    if (!path.startsWith('/') || !name || !['directory', 'file', 'symlink'].includes(kind)) return [];
    return [{ path, name, kind: kind as WorkspaceEntry['kind'], ...(typeof item.byteSize === 'number' ? { byteSize: item.byteSize } : {}) }];
  });
  return { items: sortedEntries(items), limited: value.truncated === true, ...(typeof value.nextOffset === 'number' && value.nextOffset > 0 ? { nextOffset: value.nextOffset } : {}) };
}

function workspaceFileChunk(value: unknown, path: string, sessionId: string): { canonicalPath: string; content: string; byteSize: number; nextOffset: number; truncated: boolean; resourceRevision?: string; editability?: EditableWorkspacePreview['editability'] } {
  if (!isRecord(value) || typeof value.content !== 'string') throw new Error('文件服务返回了无法识别的数据。');
  const canonicalPath = value.scope === 'local'
    ? localReadPath(value, path) : canonicalWorkspaceReadPath(value, path, sessionId);
  const byteSize = typeof value.byteSize === 'number' ? value.byteSize : 0;
  const offset = typeof value.offset === 'number' ? value.offset : 0;
  const nextOffset = typeof value.nextOffset === 'number'
    ? value.nextOffset
    : offset + new TextEncoder().encode(value.content).length;
  return {
    canonicalPath, content: value.content, byteSize, nextOffset, truncated: value.truncated === true,
    resourceRevision: typeof value.resourceRevision === 'string' ? value.resourceRevision : undefined,
    editability: isRecord(value.editability) ? { editable: value.editability.editable === true, reason: typeof value.editability.reason === 'string' ? value.editability.reason : undefined } : undefined,
  };
}

function localReadPath(value: Record<string, unknown>, requestedPath: string): string {
  if (value.ok !== true || value.requestedPath !== requestedPath || typeof value.path !== 'string' || !value.path.startsWith('/') || value.path.includes('\0')) throw new Error('文件服务返回了无法识别的数据。');
  return value.path;
}

function entryFamily(entry: Pick<WorkspaceEntry, 'kind' | 'name'>): string | undefined {
  if (entry.kind === 'symlink') return 'symlink';
  if (entry.kind === 'directory') return undefined;
  return FILE_FAMILY[fileExtension(entry.name)] ?? 'plain';
}

function fileGlyph(name: string, size = 14): ReactNode {
  switch (FILE_FAMILY[fileExtension(name)]) {
    case 'code':
    case 'diff':
      return <FileCode2 size={size} />;
    case 'doc':
      return <FileText size={size} />;
    case 'data':
      return <FileJson2 size={size} />;
    case 'media':
      return <FileImage size={size} />;
    default:
      return <File size={size} />;
  }
}

function fileLanguage(name: string): string {
  const extension = fileExtension(name);
  return ({ js: 'javascript', jsx: 'jsx', json: 'json', md: 'markdown', py: 'python', sh: 'shellscript', svg: 'xml', ts: 'typescript', tsx: 'tsx', yaml: 'yaml', yml: 'yaml' } as Record<string, string>)[extension] ?? (extension || 'text');
}

/** Honest loaded share for the bounded-read meter; floored so a partial file
    never rounds up to a full bar. */
function loadedShare(preview: WorkspacePreview): number {
  if (preview.byteSize <= 0) return 0;
  return Math.max(2, Math.min(100, Math.floor((preview.loadedBytes / preview.byteSize) * 100)));
}

/** Numeric readout share: plain floored percentage without the visual floor,
    so the printed number never claims more than the bytes on screen. */
function honestShare(preview: WorkspacePreview): number {
  if (preview.byteSize <= 0) return 0;
  return Math.min(100, Math.floor((preview.loadedBytes / preview.byteSize) * 100));
}

/** One gauge tick per 64 KB read request. Ticks disappear when the file is so
    large they would blur into noise (below a 4% pitch). */
function chunkTickShare(preview: WorkspacePreview): number {
  if (preview.byteSize <= 0) return 0;
  const share = (PREVIEW_CHUNK_BYTES / preview.byteSize) * 100;
  return share >= 4 && share < 100 ? share : 0;
}

/** Position of the 512 KB in-App reading cap on the gauge scale; zero when
    the whole file fits inside the cap and no marker is needed. */
function capMarkerShare(preview: WorkspacePreview): number {
  if (preview.byteSize <= PREVIEW_MAX_BYTES) return 0;
  return Math.min(98, (PREVIEW_MAX_BYTES / preview.byteSize) * 100);
}

function meterStyle(preview: WorkspacePreview): CSSProperties {
  const tick = chunkTickShare(preview);
  const cap = capMarkerShare(preview);
  return {
    '--paw-files-loaded': `${loadedShare(preview)}%`,
    ...(tick ? { '--paw-files-tick': `${tick}%` } : {}),
    ...(cap ? { '--paw-files-cap': `${cap}%` } : {}),
  } as CSSProperties;
}

/** Which renderer is actually on screen — the reader names its own lens. */
function rendererLabel(preview: WorkspacePreview): string {
  if (!preview.content && !preview.truncated) return '空文件';
  if (isProbablyBinary(preview.content)) return '二进制';
  const extension = fileExtension(pathName(preview.path));
  if (extension === 'svg' && !preview.truncated) return 'SVG';
  if (['md', 'mdx', 'markdown'].includes(extension)) return 'Markdown';
  if (['html', 'htm'].includes(extension)) return '网页';
  if (['diff', 'patch'].includes(extension)) return 'Diff';
  return FILE_FAMILY[extension] === 'code' ? '代码' : '纯文本';
}

/** File names read base-first: the extension keeps its own quieter span so a
    column of names scans by stem. Dotfiles stay whole. */
function entryLabel(name: string): ReactNode {
  const dot = name.lastIndexOf('.');
  if (dot <= 0 || dot === name.length - 1) return name;
  return <>{name.slice(0, dot)}<i aria-hidden="true">{name.slice(dot)}</i></>;
}

function flattenVisibleTree(roots: string[], entries: Record<string, WorkspaceListing>, expanded: Set<string>): VisibleTreeNode[] {
  const nodes: VisibleTreeNode[] = [];
  const appendChildren = (parentPath: string) => {
    for (const entry of entries[parentPath]?.items ?? []) {
      nodes.push({ path: entry.path, parentPath, kind: entry.kind });
      if (entry.kind === 'directory' && expanded.has(entry.path)) appendChildren(entry.path);
    }
  };
  for (const root of roots) {
    nodes.push({ path: root, parentPath: null, kind: 'root' });
    if (expanded.has(root)) appendChildren(root);
  }
  return nodes;
}

function isProbablyBinary(content: string): boolean {
  if (content.includes('\u0000')) return true;
  const sample = content.slice(0, 4_096);
  if (!sample) return false;
  let controlCharacters = 0;
  for (const character of sample) {
    const code = character.charCodeAt(0);
    if (code < 32 && code !== 9 && code !== 10 && code !== 13) controlCharacters += 1;
  }
  return controlCharacters > Math.max(2, sample.length * 0.02);
}

function fileExtension(value: string): string { return value.split('.').at(-1)?.toLowerCase() ?? ''; }
function pathName(value: string): string { return value.split('/').filter(Boolean).at(-1) ?? value; }
function formatBytes(value: number): string { return value < 1_024 ? `${value} B` : value < 1_048_576 ? `${Math.max(1, Math.round(value / 1_024))} KB` : `${Math.max(1, Math.round(value / 1_048_576))} MB`; }
function omitKey<Value>(record: Record<string, Value>, key: string): Record<string, Value> { const next = { ...record }; delete next[key]; return next; }
function isRecord(value: unknown): value is Record<string, unknown> { return Boolean(value) && typeof value === 'object' && !Array.isArray(value); }
function publicError(error: unknown, fallback: string): string { return error instanceof Error && error.message.trim() ? error.message : fallback; }
