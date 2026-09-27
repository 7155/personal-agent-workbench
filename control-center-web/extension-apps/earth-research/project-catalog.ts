import { bindLayerFeatures, type ProjectLayer } from './layer-catalog';
import { mapBounded, workspaceFilePath } from './workspace-io';

type CatalogLayer = Omit<ProjectLayer, 'features'>;
export async function loadProjectCatalog({ root, records, previous, read }: {
  root: string; records: CatalogLayer[]; previous: ProjectLayer[];
  read: (path: string) => Promise<string>;
}): Promise<{ layers: ProjectLayer[]; warnings: string[] }> {
  const warnings: string[] = [];
  const layers = await mapBounded(records, 4, async record => {
    try {
      const path = workspaceFilePath(root, record.path);
      const value = JSON.parse(await read(path));
      if (!value || value.type !== 'FeatureCollection' || !Array.isArray(value.features)
        || value.features.some((feature: GeoJSON.Feature) => feature?.type !== 'Feature' || !feature.geometry)
        || value.features.length !== record.featureCount) {
        throw new Error('图层结构或要素数量与目录不一致，请重新读取。');
      }
      return bindLayerFeatures({ ...record, loadError: undefined, features: value.features });
    } catch (reason) {
      const error = reason instanceof Error ? reason.message : String(reason);
      warnings.push(`${record.name}：${error}`);
      // Never attach a new revision to old geometry. That could authorize an
      // edit against bytes the user has not actually seen.
      const cached = previous.find(layer => layer.id === record.id);
      return cached ? { ...cached, loadError: error } : { ...record, features: [], loadError: error };
    }
  });
  return { layers, warnings };
}
