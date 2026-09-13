import type { LabProjectSummary, LabProjectWorkState, LabProjectNextAction, LabProjectLatestRecord } from './types';

type ProjectHomeProjection = { workState: LabProjectWorkState; nextAction: LabProjectNextAction; latestRecord: LabProjectLatestRecord | null; rerunReadiness: { status: 'ready' | 'not_ready'; reason: string; missing: string[] } };

export function projectHomeProjection(item: LabProjectSummary): ProjectHomeProjection {
  if (item.workState && item.nextAction && item.rerunReadiness) {
    return { workState: item.workState, nextAction: item.nextAction, rerunReadiness: item.rerunReadiness, latestRecord: item.latestRecord ?? null };
  }
  const historical = Boolean(item.historyOrigin);
  const hasMaterials = item.materialCount > 0 || Boolean(item.knowledgeResources?.documentCount);
  const missing = !hasMaterials ? ['当前材料快照'] : ['执行绑定'];
  const workState: LabProjectWorkState = historical && !hasMaterials
    ? { status: 'history_only', label: '历史结果', reason: '这是迁移的历史记录，可以查看，但当前材料尚未确认可复跑。' }
    : !hasMaterials
      ? { status: 'draft', label: '待接入材料', reason: '项目还没有可用于验证的当前材料。' }
      : { status: 'needs_binding', label: '待连接执行', reason: '材料已保存，但执行绑定尚未从项目摘要中确认。' };
  const nextAction: LabProjectNextAction = historical
    ? { kind: 'prepare_rerun', label: '准备复跑', reason: '先绑定当前数据与执行环境。' }
    : !hasMaterials
      ? { kind: 'add_materials', label: '添加材料', reason: '上传、粘贴或连接执行器上的材料。' }
      : { kind: 'bind_execution', label: '连接执行', reason: '选择已登记的评测或运行适配器。' };
  const latestRecord = item.latestRecord ?? (historical ? { kind: 'history', status: 'historical', title: '历史实验快照', updatedAtMs: item.updatedAtMs, artifactId: item.historyOrigin?.snapshotArtifactId ?? '' } : item.artifactCount > 0 ? { kind: 'artifact', status: 'available', title: '已有成果', updatedAtMs: item.updatedAtMs, artifactId: '' } : null);
  return { workState, nextAction, latestRecord, rerunReadiness: { status: missing.length ? 'not_ready' : 'ready', reason: missing.join('；'), missing } };
}


export const needsProjectAttention = (item: LabProjectSummary) => ['blocked', 'draft', 'needs_binding', 'history_only'].includes(projectHomeProjection(item).workState.status);
