import {
  ChevronRight,
  File,
  FileCode2,
  FileDiff,
  FileImage,
  FileJson2,
  FileText,
  Folder,
  FolderOpen,
  LoaderCircle,
  PanelRightClose,
  RefreshCw,
  Settings2,
  TriangleAlert,
} from 'lucide-react';
import {
  forwardRef,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from 'react';
import { useControlTransport } from '@/app/control-transport';
import { IconButton } from '@/components/primitives';
import { WorkspaceFilePreviewDialog } from './WorkspaceFilePreviewDialog';

interface AgentFilesPanelProps {
  sessionId: string;
  workspaceRoots: string[];
  open: boolean;
  modal?: boolean;
  /** TeamGateway owns the mounted workspace; local directory picking is not
   * available in that deployment. */
  allowWorkspaceRootManagement?: boolean;
  onClose: () => void;
  onManageRoots: () => void;
}

interface WorkspaceEntry {
  path: string;
  name: string;
  kind: 'directory' | 'file' | 'symlink';
  byteSize?: number;
}

interface VisibleTreeNode {
  path: string;
  parentPath: string | null;
  kind: 'root' | WorkspaceEntry['kind'];
}

export const AgentFilesPanel = forwardRef<HTMLElement, AgentFilesPanelProps>(function AgentFilesPanel({
  sessionId,
  workspaceRoots,
  open,
  modal = false,
  allowWorkspaceRootManagement = true,
  onClose,
  onManageRoots,
}, ref) {
  const transport = useControlTransport();
  const cacheRef = useRef(new Map<string, WorkspaceEntry[]>());
  const loadingRef = useRef(new Set<string>());
  const requestControllersRef = useRef(new Map<string, AbortController>());
  const errorsRef = useRef(new Map<string, string>());
  const generationRef = useRef(0);
  const treeItemRefs = useRef(new Map<string, HTMLButtonElement>());
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [selectedFile, setSelectedFile] = useState<WorkspaceEntry>();
  const [treeFocusPath, setTreeFocusPath] = useState('');
  const [revision, setRevision] = useState(0);
  const roots = useMemo(() => workspaceRoots
    .map((path) => path.trim())
    .filter((path, index, values) => path.startsWith('/') && values.indexOf(path) === index), [workspaceRoots]);
  const rootsKey = roots.join('\u0000');
  const visibleTreeNodes = useMemo(
    () => flattenVisibleTree(roots, cacheRef.current, expanded),
    [expanded, revision, roots],
  );

  const redraw = useCallback(() => setRevision((value) => value + 1), []);
  const loadChildren = useCallback(async (path: string, force = false) => {
    if (!sessionId) return;
    const previousController = requestControllersRef.current.get(path);
    if (previousController) {
      if (!force) return;
      previousController.abort();
    }
    if (!force && cacheRef.current.has(path)) return;
    const generation = generationRef.current;
    const controller = new AbortController();
    requestControllersRef.current.set(path, controller);
    loadingRef.current.add(path);
    errorsRef.current.delete(path);
    redraw();
    try {
      const response = await transport.request({
        pathId: 'agent.session.workspace.list',
        params: { sessionId },
        query: { path, depth: 1, limit: 240 },
        signal: controller.signal,
      });
      if (generation !== generationRef.current
        || controller.signal.aborted
        || requestControllersRef.current.get(path) !== controller) return;
      cacheRef.current.set(path, workspaceEntries(response));
    } catch (error) {
      if (generation !== generationRef.current
        || controller.signal.aborted
        || requestControllersRef.current.get(path) !== controller) return;
      errorsRef.current.set(path, publicError(error));
    } finally {
      if (requestControllersRef.current.get(path) !== controller) return;
      requestControllersRef.current.delete(path);
      if (generation === generationRef.current) {
        loadingRef.current.delete(path);
        redraw();
      }
    }
  }, [redraw, sessionId, transport]);

  useEffect(() => {
    generationRef.current += 1;
    for (const controller of requestControllersRef.current.values()) controller.abort();
    requestControllersRef.current.clear();
    cacheRef.current.clear();
    loadingRef.current.clear();
    errorsRef.current.clear();
    setExpanded(new Set(roots));
    setSelectedFile(undefined);
    setTreeFocusPath(roots[0] ?? '');
    setRevision((value) => value + 1);
    return () => {
      for (const controller of requestControllersRef.current.values()) controller.abort();
      requestControllersRef.current.clear();
    };
  }, [rootsKey, sessionId]);

  useEffect(() => {
    setTreeFocusPath((current) => (
      visibleTreeNodes.some((node) => node.path === current)
        ? current
        : visibleTreeNodes[0]?.path ?? ''
    ));
  }, [visibleTreeNodes]);

  useEffect(() => {
    if (!open) return;
    for (const root of roots) void loadChildren(root);
  }, [loadChildren, open, rootsKey]);

  function toggleDirectory(path: string): void {
    const willExpand = !expanded.has(path);
    setExpanded((current) => {
      const next = new Set(current);
      if (willExpand) next.add(path);
      else next.delete(path);
      return next;
    });
    if (willExpand) void loadChildren(path);
  }

  function refresh(): void {
    cacheRef.current.clear();
    errorsRef.current.clear();
    const paths = new Set([...roots, ...expanded]);
    for (const path of paths) void loadChildren(path, true);
    redraw();
  }

  function focusTreeItem(path: string): void {
    setTreeFocusPath(path);
    treeItemRefs.current.get(path)?.focus();
  }

  function onTreeKeyDown(event: ReactKeyboardEvent<HTMLButtonElement>, path: string): void {
    const index = visibleTreeNodes.findIndex((node) => node.path === path);
    const node = visibleTreeNodes[index];
    if (!node) return;

    let nextPath = '';
    if (event.key === 'ArrowDown') nextPath = visibleTreeNodes[Math.min(index + 1, visibleTreeNodes.length - 1)]?.path ?? '';
    if (event.key === 'ArrowUp') nextPath = visibleTreeNodes[Math.max(index - 1, 0)]?.path ?? '';
    if (event.key === 'Home') nextPath = visibleTreeNodes[0]?.path ?? '';
    if (event.key === 'End') nextPath = visibleTreeNodes.at(-1)?.path ?? '';
    if (event.key === 'ArrowRight' && (node.kind === 'root' || node.kind === 'directory')) {
      if (!expanded.has(path)) {
        toggleDirectory(path);
      } else {
        nextPath = visibleTreeNodes.find((candidate) => candidate.parentPath === path)?.path ?? '';
      }
    }
    if (event.key === 'ArrowLeft') {
      if ((node.kind === 'root' || node.kind === 'directory') && expanded.has(path)) {
        toggleDirectory(path);
      } else {
        nextPath = node.parentPath ?? '';
      }
    }
    if (!nextPath && !['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    if (!nextPath && !((node.kind === 'root' || node.kind === 'directory') && ['ArrowLeft', 'ArrowRight'].includes(event.key))) return;
    event.preventDefault();
    if (nextPath) focusTreeItem(nextPath);
  }

  function renderChildren(parentPath: string, depth: number): ReactNode {
    const isLoading = loadingRef.current.has(parentPath);
    const error = errorsRef.current.get(parentPath);
    const items = cacheRef.current.get(parentPath);
    if (isLoading && !items) {
      return <div className="agent-files-panel__state" role="status"><LoaderCircle size={14} />正在读取目录…</div>;
    }
    if (error && !items) {
      return (
        <div className="agent-files-panel__state" role="alert">
          <TriangleAlert size={14} />
          <span>{error}</span>
          <button type="button" onClick={() => void loadChildren(parentPath, true)}>重试</button>
        </div>
      );
    }
    if (!items?.length) return <div className="agent-files-panel__state">空目录</div>;
    return (
      <ul role="group">
        {items.map((item, index) => {
          const directory = item.kind === 'directory';
          const isExpanded = directory && expanded.has(item.path);
          const style = { '--agent-file-depth': depth } as CSSProperties;
          return (
            <li key={item.path} role="none">
              {directory ? (
                <button
                  aria-expanded={isExpanded}
                  aria-level={depth + 1}
                  aria-posinset={index + 1}
                  aria-setsize={items.length}
                  className="agent-files-panel__row"
                  style={style}
                  type="button"
                  title={item.path}
                  aria-label={`${isExpanded ? '收起' : '展开'}目录 ${item.name}`}
                  onFocus={() => setTreeFocusPath(item.path)}
                  onClick={() => toggleDirectory(item.path)}
                  onKeyDown={(event) => onTreeKeyDown(event, item.path)}
                  ref={(node) => {
                    if (node) treeItemRefs.current.set(item.path, node);
                    else treeItemRefs.current.delete(item.path);
                  }}
                  role="treeitem"
                  tabIndex={treeFocusPath === item.path ? 0 : -1}
                >
                  <ChevronRight className="agent-files-panel__chevron" size={13} data-open={isExpanded || undefined} />
                  {isExpanded ? <FolderOpen size={15} /> : <Folder size={15} />}
                  <span>{item.name}</span>
                </button>
              ) : (
                <button
                  aria-level={depth + 1}
                  aria-posinset={index + 1}
                  aria-selected={item.path === selectedFile?.path || undefined}
                  aria-setsize={items.length}
                  className="agent-files-panel__row"
                  style={style}
                  type="button"
                  title={`预览 ${item.path}`}
                  aria-label={`预览文件 ${item.name}`}
                  onFocus={() => setTreeFocusPath(item.path)}
                  onClick={() => setSelectedFile(item)}
                  onKeyDown={(event) => onTreeKeyDown(event, item.path)}
                  ref={(node) => {
                    if (node) treeItemRefs.current.set(item.path, node);
                    else treeItemRefs.current.delete(item.path);
                  }}
                  role="treeitem"
                  tabIndex={treeFocusPath === item.path ? 0 : -1}
                >
                  <span className="agent-files-panel__file-indent" />
                  {fileIcon(item.name)}
                  <span>{item.name}</span>
                  {typeof item.byteSize === 'number' ? <small>{formatBytes(item.byteSize)}</small> : null}
                </button>
              )}
              {directory && isExpanded ? renderChildren(item.path, depth + 1) : null}
            </li>
          );
        })}
      </ul>
    );
  }

  return (
    <>
      <aside
        ref={ref}
        id="agent-files-panel"
        className="agent-files-panel"
        data-open={open}
        aria-label="当前对话文件目录"
        aria-hidden={!open || undefined}
        inert={!open ? true : undefined}
        role={modal ? 'dialog' : 'complementary'}
        aria-modal={modal || undefined}
      >
        <header>
          <span><strong>文件目录</strong><small>{roots.length ? `${roots.length} 个工作区` : '未选择工作区'}</small></span>
          <div className="agent-files-panel__actions">
            <IconButton label="刷新文件目录" icon={<RefreshCw size={16} />} onClick={refresh} disabled={!roots.length} tooltip />
            {allowWorkspaceRootManagement ? <IconButton label="管理工作区目录" icon={<Settings2 size={16} />} onClick={onManageRoots} tooltip /> : null}
            <IconButton data-drawer-autofocus label="收起文件目录" icon={<PanelRightClose size={17} />} onClick={onClose} tooltip />
          </div>
        </header>
        <div className="agent-files-panel__body" data-revision={revision}>
          {roots.length ? (
            <nav aria-label="工作区文件">
              <ul aria-label="工作区文件" role="tree">
                {roots.map((root, index) => {
                  const isExpanded = expanded.has(root);
                  return (
                    <li className="agent-files-panel__root" key={root} role="none">
                      <button
                        aria-level={1}
                        aria-posinset={index + 1}
                        aria-setsize={roots.length}
                        className="agent-files-panel__root-toggle"
                        type="button"
                        title={root}
                        aria-expanded={isExpanded}
                        aria-label={`${isExpanded ? '收起' : '展开'}工作区 ${pathName(root)}`}
                        onFocus={() => setTreeFocusPath(root)}
                        onClick={() => toggleDirectory(root)}
                        onKeyDown={(event) => onTreeKeyDown(event, root)}
                        ref={(node) => {
                          if (node) treeItemRefs.current.set(root, node);
                          else treeItemRefs.current.delete(root);
                        }}
                        role="treeitem"
                        tabIndex={treeFocusPath === root ? 0 : -1}
                      >
                        <ChevronRight className="agent-files-panel__chevron" size={14} data-open={isExpanded || undefined} />
                        {isExpanded ? <FolderOpen size={16} /> : <Folder size={16} />}
                        <span><strong>{pathName(root)}</strong><small>{root}</small></span>
                      </button>
                      {isExpanded ? renderChildren(root, 1) : null}
                    </li>
                  );
                })}
              </ul>
            </nav>
          ) : (
            <p className="agent-files-panel__empty" role="status">
              <span>{allowWorkspaceRootManagement ? '当前没有文件；选择工作区目录后即可浏览。' : '当前团队工作区由服务管理，等待服务器返回文件范围。'}</span>
              {allowWorkspaceRootManagement ? <button type="button" onClick={onManageRoots}>选择目录</button> : null}
            </p>
          )}
        </div>
      </aside>
      <WorkspaceFilePreviewDialog
        sessionId={sessionId}
        path={selectedFile?.path ?? ''}
        byteSize={selectedFile?.byteSize}
        open={Boolean(selectedFile)}
        onOpenChange={(next) => { if (!next) setSelectedFile(undefined); }}
      />
    </>
  );
});

function workspaceEntries(value: unknown): WorkspaceEntry[] {
  if (!isRecord(value) || !Array.isArray(value.items)) throw new Error('目录服务返回了无法识别的数据。');
  return value.items.flatMap((item) => {
    if (!isRecord(item)) return [];
    const path = typeof item.path === 'string' ? item.path.trim() : '';
    const name = typeof item.name === 'string' ? item.name.trim() : '';
    const kind = item.kind;
    if (!path.startsWith('/') || !name || !['directory', 'file', 'symlink'].includes(String(kind))) return [];
    return [{
      path,
      name,
      kind: kind as WorkspaceEntry['kind'],
      ...(typeof item.byteSize === 'number' && Number.isFinite(item.byteSize) ? { byteSize: item.byteSize } : {}),
    }];
  });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function pathName(path: string): string {
  return path.split('/').filter(Boolean).at(-1) ?? path;
}

function formatBytes(value: number): string {
  if (value < 1_024) return `${value} B`;
  if (value < 1_048_576) return `${Math.max(1, Math.round(value / 1_024))} KB`;
  return `${Math.max(1, Math.round(value / 1_048_576))} MB`;
}

function flattenVisibleTree(
  roots: string[],
  entries: Map<string, WorkspaceEntry[]>,
  expanded: Set<string>,
): VisibleTreeNode[] {
  const nodes: VisibleTreeNode[] = [];
  const appendChildren = (parentPath: string) => {
    for (const entry of entries.get(parentPath) ?? []) {
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

function fileIcon(name: string): ReactNode {
  const extension = name.split('.').at(-1)?.toLowerCase() ?? '';
  if (['diff', 'patch'].includes(extension)) return <FileDiff aria-hidden="true" size={15} />;
  if (['gif', 'heic', 'jpeg', 'jpg', 'png', 'svg', 'webp'].includes(extension)) return <FileImage aria-hidden="true" size={15} />;
  if (['json', 'jsonl'].includes(extension)) return <FileJson2 aria-hidden="true" size={15} />;
  if (['md', 'mdx', 'markdown', 'txt'].includes(extension)) return <FileText aria-hidden="true" size={15} />;
  if (['c', 'cc', 'cpp', 'css', 'go', 'h', 'hpp', 'html', 'java', 'js', 'jsx', 'kt', 'py', 'rs', 'sh', 'sql', 'swift', 'toml', 'ts', 'tsx', 'vue', 'xml', 'yaml', 'yml', 'zsh'].includes(extension)) {
    return <FileCode2 aria-hidden="true" size={15} />;
  }
  return <File aria-hidden="true" size={15} />;
}

function publicError(error: unknown): string {
  return error instanceof Error && error.message.trim() ? error.message : '目录读取失败。';
}
