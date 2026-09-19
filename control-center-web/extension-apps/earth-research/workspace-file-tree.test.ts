import { expect, it } from 'vitest';
import type { WorkspaceFileSummary } from './layer-catalog';
import { buildWorkspaceFileTree, filterWorkspaceFileTree, workspaceFileCategory, workspaceNodeByteSize, type WorkspaceFileNode } from './workspace-file-tree';

const root = '/work/project';
const file = (path: string, byteSize?: number): WorkspaceFileSummary => ({ path: `${root}/${path}`, name: path.split('/').at(-1)!, kind: 'file', ...(byteSize === undefined ? {} : { byteSize }) });
const allNodes = (nodes: WorkspaceFileNode[]): WorkspaceFileNode[] => nodes.flatMap(node => [node, ...allNodes(node.children)]);

it('builds real path hierarchies, retains empty directories, and groups only matching Shapefile stems in the same directory', () => {
  const entries: WorkspaceFileSummary[] = [
    ...['SHP', 'shx', 'dbf', 'prj', 'cpg'].map(extension => file(`data/source/parcels.${extension}`, 1024)),
    file('data/source/parcels-copy.dbf'), file('data/other/parcels.shp'), file('data/other/parcels.dbf'),
    { path: `${root}/data/empty`, name: 'empty', kind: 'directory' }, file('report.html'),
  ];
  const tree = buildWorkspaceFileTree(entries, root);
  expect(tree.map(node => node.name)).toEqual(['data', 'report.html']);
  const data = tree[0];
  expect(data.children.map(node => node.name)).toEqual(['empty', 'other', 'source']);
  expect(data.children[0]).toMatchObject({ kind: 'directory', path: `${root}/data/empty`, children: [] });
  const groups = allNodes(tree).filter(node => node.kind === 'shapefile');
  const source = groups.find(node => node.path.includes('/source/'))!;
  expect(source.children.map(node => node.name)).toEqual(['parcels.SHP', 'parcels.shx', 'parcels.dbf', 'parcels.prj', 'parcels.cpg']);
  expect(source.shapefile).toEqual({ missingRequired: [], hasProjection: true });
  expect(source.entry).toEqual(file('data/source/parcels.SHP', 1024));
  expect(workspaceNodeByteSize(source)).toBe(5120);
  expect(groups.find(node => node.path.includes('/other/'))?.shapefile).toEqual({ missingRequired: ['shx'], hasProjection: false });
  expect(allNodes(tree).find(node => node.name === 'parcels-copy.dbf')?.partOfShapefile).toBeUndefined();
});

it('distinguishes missing required components from an unrecorded CRS and never borrows a similarly named sidecar', () => {
  const tree = buildWorkspaceFileTree([file('roads.shp'), file('Roads.shx'), file('roads.dbf'), file('unprojected.shp'), file('unprojected.shx'), file('unprojected.dbf')], root);
  expect(tree.find(node => node.name === 'roads.shp')?.shapefile).toEqual({ missingRequired: ['shx'], hasProjection: false });
  expect(tree.find(node => node.name === 'Roads.shx')).toMatchObject({ kind: 'file', category: 'sidecar' });
  expect(tree.find(node => node.name === 'unprojected.shp')?.shapefile).toEqual({ missingRequired: [], hasProjection: false });
});

it('keeps dataset context and all companion files when searching a nested sidecar', () => {
  const entries = ['shp', 'shx', 'dbf', 'prj', 'cpg'].map(extension => file(`data/source/parcels.${extension}`));
  const tree = buildWorkspaceFileTree([...entries, file('data/other/roads.gpkg'), file('report.html')], root);
  const filtered = filterWorkspaceFileTree(tree, 'parcels.dbf');
  expect(filtered.map(node => node.name)).toEqual(['data']);
  expect(filtered[0].children.map(node => node.name)).toEqual(['source']);
  expect(filtered[0].children[0].children[0].children).toHaveLength(5);
  expect(filterWorkspaceFileTree(tree, 'GeoPackage')[0].children[0].name).toBe('other');
  expect(filterWorkspaceFileTree(tree, 'absent')).toEqual([]);
  expect(filterWorkspaceFileTree(tree, ' ')).toBe(tree);
});

it('classifies known file types and does not infer GeoJSON content from a generic JSON suffix', () => {
  expect(['roads.gpkg', 'height.tif', 'height.tiff', 'report.html', 'report.pdf', 'roads.geojson', 'plan.json', 'records.csv', 'package.zip'].map(name => workspaceFileCategory(file(name)))).toEqual(['geopackage', 'raster', 'raster', 'report', 'report', 'vector', 'file', 'table', 'archive']);
  const symlink = { ...file('linked.shp'), kind: 'symlink' as const };
  expect(buildWorkspaceFileTree([symlink], root)[0]).toMatchObject({ kind: 'symlink', category: 'symlink', children: [] });
});

it('only totals measured sizes and deduplicates repeated listing records without losing known metadata', () => {
  const tree = buildWorkspaceFileTree([file('complete.shp', 100), file('complete.shx', 0), file('complete.dbf', 20), file('complete.shp'), file('partial.shp', 100), file('partial.dbf')], root);
  const complete = tree.find(node => node.name === 'complete.shp')!;
  expect(complete.children).toHaveLength(3);
  expect(workspaceNodeByteSize(complete)).toBe(120);
  expect(workspaceNodeByteSize(tree.find(node => node.name === 'partial.shp')!)).toBeUndefined();
  const folder = buildWorkspaceFileTree([{ path: `${root}/data`, name: 'data', kind: 'directory', byteSize: 4096 }], root)[0];
  expect(workspaceNodeByteSize(folder)).toBeUndefined();
  expect(workspaceNodeByteSize(buildWorkspaceFileTree([file('bad.tif', Number.NaN)], root)[0])).toBeUndefined();
});
