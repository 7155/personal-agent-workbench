import type { WorkspaceFileSummary } from './layer-catalog';

export type WorkspaceFileCategory = 'directory' | 'shapefile' | 'geopackage' | 'database' | 'vector' | 'raster' | 'report' | 'table' | 'image' | 'archive' | 'sidecar' | 'file' | 'symlink';

export type WorkspaceFileNode = {
  id: string;
  name: string;
  path: string;
  kind: 'directory' | 'shapefile' | 'file' | 'symlink';
  category: WorkspaceFileCategory;
  entry?: WorkspaceFileSummary;
  children: WorkspaceFileNode[];
  shapefile?: { missingRequired: Array<'shx' | 'dbf'>; hasProjection: boolean };
  partOfShapefile?: string;
};

export const WORKSPACE_FILE_LABELS: Record<WorkspaceFileCategory, string> = {
  directory: '文件夹', shapefile: 'Shapefile', geopackage: 'GeoPackage', database: '空间数据库',
  vector: '矢量', raster: '栅格', report: '报告', table: '表格', image: '图像',
  archive: '压缩包', sidecar: 'SHP 配套', file: '文件', symlink: '符号链接',
};

const compareNames = new Intl.Collator('zh-CN', { numeric: true, sensitivity: 'base' });
const SHAPEFILE_EXTENSIONS = ['shp', 'shx', 'dbf', 'prj', 'cpg', 'qix', 'sbn', 'sbx', 'shp.xml'];

function pathParts(path: string): { directory: string; name: string } {
  const slash = path.lastIndexOf('/');
  return { directory: slash < 0 ? '' : path.slice(0, slash), name: path.slice(slash + 1) };
}

function shapefilePart(name: string): { stem: string; extension: string } | undefined {
  const match = /^(.*)\.(shp\.xml|shp|shx|dbf|prj|cpg|qix|sbn|sbx)$/iu.exec(name);
  return match ? { stem: match[1], extension: match[2].toLowerCase() } : undefined;
}

export function workspaceFileCategory(entry: WorkspaceFileSummary): WorkspaceFileCategory {
  if (entry.kind !== 'file') return entry.kind;
  const name = entry.name.toLowerCase();
  if (/\.shp$/u.test(name)) return 'shapefile';
  if (/\.gpkg$/u.test(name)) return 'geopackage';
  if (/\.(sqlite|sqlite3|db)$/u.test(name)) return 'database';
  if (/\.(geojson|jsonl|kml|kmz|gml|fgb|gpx)$/u.test(name)) return 'vector';
  if (/\.(tiff?|img|vrt|asc|nc|hdf|h5)$/u.test(name)) return 'raster';
  if (/\.(html?|md|pdf|txt|docx?)$/u.test(name)) return 'report';
  if (/\.(csv|tsv|xlsx?|parquet)$/u.test(name)) return 'table';
  if (/\.(png|jpe?g|gif|webp|svg)$/u.test(name)) return 'image';
  if (/\.(zip|7z|tar|gz)$/u.test(name)) return 'archive';
  if (shapefilePart(entry.name)) return 'sidecar';
  return 'file';
}

function fileNode(entry: WorkspaceFileSummary): WorkspaceFileNode {
  return { id: `${entry.kind}:${entry.path}`, name: entry.name, path: entry.path, kind: entry.kind, category: workspaceFileCategory(entry), entry, children: [] };
}

/** Build a display tree from the listing; never read or mutate the filesystem. */
export function buildWorkspaceFileTree(files: WorkspaceFileSummary[], workspaceRoot: string): WorkspaceFileNode[] {
  const root = workspaceRoot.replace(/\/+$/u, '');
  const entries = new Map<string, WorkspaceFileSummary>();
  for (const entry of files) {
    if (!entry.path || entry.path === root) continue;
    const previous = entries.get(entry.path);
    entries.set(entry.path, { ...previous, ...entry, byteSize: entry.byteSize ?? previous?.byteSize });
  }
  const roots: WorkspaceFileNode[] = [];
  const directories = new Map<string, WorkspaceFileNode>();
  const ensureDirectory = (path: string): WorkspaceFileNode | undefined => {
    if (!path || path === root) return undefined;
    const existing = directories.get(path);
    if (existing) return existing;
    const { directory, name } = pathParts(path);
    const node: WorkspaceFileNode = { id: `directory:${path}`, path, name, kind: 'directory', category: 'directory', children: [] };
    directories.set(path, node);
    const parent = directory !== path ? ensureDirectory(directory) : undefined;
    (parent?.children ?? roots).push(node);
    return node;
  };
  for (const entry of entries.values()) {
    const { directory } = pathParts(entry.path);
    if (entry.kind === 'directory') {
      const node = ensureDirectory(entry.path);
      if (node) { node.entry = entry; node.name = entry.name; }
    } else {
      (ensureDirectory(directory)?.children ?? roots).push(fileNode(entry));
    }
  }
  const organize = (nodes: WorkspaceFileNode[]): WorkspaceFileNode[] => {
    const grouped = new Set<string>();
    const shapefiles: WorkspaceFileNode[] = [];
    // Compare stems exactly and extensions case-insensitively. Do not combine
    // similarly named datasets from another directory or case-sensitive path.
    for (const node of nodes) {
      if (grouped.has(node.id)) continue;
      const part = node.kind === 'file' ? shapefilePart(node.name) : undefined;
      if (!part || part.extension !== 'shp') continue;
      const components = nodes.filter(candidate => {
        const other = candidate.kind === 'file' ? shapefilePart(candidate.name) : undefined;
        return other?.stem === part.stem;
      }).sort((a, b) => SHAPEFILE_EXTENSIONS.indexOf(shapefilePart(a.name)!.extension) - SHAPEFILE_EXTENSIONS.indexOf(shapefilePart(b.name)!.extension));
      components.forEach(component => grouped.add(component.id));
      const extensions = new Set(components.map(component => shapefilePart(component.name)!.extension));
      shapefiles.push({ ...node, id: `shapefile:${node.path}`, kind: 'shapefile', children: components.map(component => ({ ...component, partOfShapefile: node.path })), shapefile: { missingRequired: (['shx', 'dbf'] as const).filter(extension => !extensions.has(extension)), hasProjection: extensions.has('prj') } });
    }
    return [...nodes.filter(node => !grouped.has(node.id)).map(node => node.kind === 'directory' ? { ...node, children: organize(node.children) } : node), ...shapefiles]
      .sort((a, b) => Number(b.kind === 'directory') - Number(a.kind === 'directory') || compareNames.compare(a.name, b.name) || a.path.localeCompare(b.path));
  };
  return organize(roots);
}

/** Keep matching descendants in their real directory and dataset context. */
export function filterWorkspaceFileTree(nodes: WorkspaceFileNode[], query: string): WorkspaceFileNode[] {
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) return nodes;
  return nodes.flatMap(node => {
    if (`${node.name} ${node.path} ${WORKSPACE_FILE_LABELS[node.category]}`.toLocaleLowerCase().includes(needle)) return [node];
    const children = filterWorkspaceFileTree(node.children, query);
    return children.length ? [node.kind === 'shapefile' ? node : { ...node, children }] : [];
  });
}

export function workspaceNodeByteSize(node: WorkspaceFileNode): number | undefined {
  const entries = node.kind === 'shapefile' ? node.children.map(child => child.entry) : node.kind === 'directory' ? [] : [node.entry];
  if (!entries.length || entries.some(entry => typeof entry?.byteSize !== 'number' || !Number.isFinite(entry.byteSize) || entry.byteSize < 0)) return undefined;
  return entries.reduce((total, entry) => total + entry!.byteSize!, 0);
}
