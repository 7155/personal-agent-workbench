import type { JevTask } from '../../src/features/semantic-workspace/jev-execution';
import type { JevDeliveryFile } from '../../src/paw-os/apps/jev-delivery-desk-model';

export type StudioScene = 'review' | 'delivered' | 'offline' | 'empty' | 'many';
/** Every item is synthetic. File references are never dereferenced by this fixture. */
export function studioFiles(scene: StudioScene): JevDeliveryFile[] {
  if (scene === 'empty') return [];
  const task = (id: string, ownerId: string, objective: string, result: string, state: string): JevTask => ({
    id, ownerId, objective, result, state, revision: 1, parentId: 'fixture-root', acceptedTurnId: `fixture-${id}`,
    expectedOutput: '交付可继续修改的源码与可打开的设计预览。',
    acceptance: ['保留当前对话与任务入口', '窄屏可以阅读和操作', '说明哪些结果来自合成预览'], artifacts: [], evidence: [],
  });
  const tasks = [
    task('copy', 'earth', '整理交付说明与原始依据', '交付说明按改动、使用入口与未验证项整理。源文件与参考截图分别保留，不把演示状态写成实测结论。', 'done'),
    task('media', 'mars', '优化图片阅读与多图浏览', '保留每张图片的完整比例。多图可以切换总览与聚焦，放大查看时可选择画布背景。', 'done'),
    task('capability', 'mars', '说明当前伙伴的功能边界', '工具、技能与扩展沿用同一个 Session 目录。可用范围与实际调用分别显示，未获得的权限没有被界面开关替代。', 'done'),
    task('review', 'venus', '核对窄屏阅读与交付入口', scene === 'delivered' ? '样例复核已结束。此状态仅用于展示 UI，不是工程联调结果。' : '已经收到截图与源码。当前仍在核对窄屏详情返回时的阅读位置。', scene === 'delivered' ? 'done' : 'review'),
  ];
  const source: Array<[string, number]> = [['交付说明.md',0],['图片阅读.png',1],['capability-display.ts',2],['状态覆盖.csv',3],['studio-preview.html',0],['视觉审查.md',3]];
  const rows = source.map(([name, index]) => {
    const work = tasks[index]!; const path = `delivery/${name}`;
    return { key: JSON.stringify([work.id, work.revision, path]), name, path, ref: path, task: work,
      ownerName: work.ownerId === 'earth' ? 'Earth' : work.ownerId === 'mars' ? 'Mars' : 'Venus',
      sessionId: name === '状态覆盖.csv' ? '' : `fixture-session-${work.ownerId}` };
  });
  if (scene === 'many') return Array.from({ length: 85 }, (_, index) => {
    const base = rows[index % rows.length]!; const path = `delivery/batch-${index}/用于检查窄窗口与超长名称的文件-${base.name}`;
    return { ...base, key: JSON.stringify([base.task.id, 1, path]), name: path.split('/').at(-1)!, path, ref: path };
  });
  return rows;
}
export const studioStageLabel = (file: JevDeliveryFile) => file.task.state === 'done' ? '已验收' : '待复核';
export const studioInputFiles = [{ mediaId: 'fixture-reference', fileName: '原始界面截图.png' }, { mediaId: 'fixture-brief', fileName: '界面改造要求.md' }];
