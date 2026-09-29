import type { ToolManifest } from '../../src/features/agent/types';
import type { CapabilityCatalog, CapabilityCatalogItem, CapabilityPreference } from '../../src/features/plugins/capability-policy';
import type { GalleryImage } from '../../src/features/conversation-ui/media/image-gallery-model';
import { PI_DEMO_ASSETS } from './pi-content-v5-assets';

/** Explicit synthetic inputs; none of these tools, resources or states are installed by this fixture. */
export function demoTools(): ToolManifest[] {
  const definitions: Array<[string, string, string, ToolManifest['riskLevel'], ToolManifest['availability'], ToolManifest['resultPresentation']]> = [
    ['read', '读取文件', '读取工作区中的文档、代码或图片。', 'R0', 'online', 'media'],
    ['bash', '终端命令', '在当前授权工作区内运行命令，返回执行记录。', 'R2', 'online', 'terminal'],
    ['workspace_write', '写入文件', '保存修改后的文档与代码文件。', 'R2', 'online', 'diff'],
    ['memory', '记忆召回', '召回当前问题相关的背景与偏好。', 'R0', 'online', 'citation'],
    ['knowledge', '知识库检索', '检索已连接的项目知识库。', 'R0', 'unconfigured', 'citation'],
    ['browser', '浏览网页', '查看页面内容与截图，保留公开来源。', 'R0', 'online', 'media'],
  ];
  return definitions.map(([id, displayName, description, riskLevel, availability, resultPresentation]) => ({
    schemaVersion: 'rag-ime.control-tool-manifest.v1', id, domain: 'fixture', displayName, description,
    riskLevel, availability, resultPresentation, category: 'workspace', sessionModes: ['assistant', 'coordinator'],
    operations: ['example'], effectiveOperations: ['example'], version: 'fixture-1',
  }));
}
export function demoCatalog(sessionId = 'fixture-earth', preferences: Record<string, CapabilityPreference> = {}): CapabilityCatalog {
  const values: CapabilityCatalogItem[] = demoTools().map(tool => ({
    id: tool.id, canonicalId: `tool:${tool.id}`, kind: 'tool', displayName: tool.displayName, description: tool.description,
    source: { kind: 'fixture', label: ['read', 'bash'].includes(tool.id) ? 'Pi 工具 · 示例目录' : 'PAW 适配器 · 示例目录' },
    status: tool.availability, risk: tool.riskLevel, requiredPermissions: tool.id === 'workspace_write' ? ['示例工作区写入权限'] : [],
    authorization: { state: tool.id === 'workspace_write' ? 'denied' : 'authorized', reason: tool.id === 'workspace_write' ? '此伙伴在示例中只有读取权限；开启披露不会提升写入权限。' : '' },
    disclosure: { preference: 'inherit', effective: tool.id === 'memory' ? 'disabled' : 'enabled', state: tool.id === 'memory' ? 'hidden' : 'disclosed', reason: tool.id === 'memory' ? '此对话沿用示例默认设置：记忆关闭。' : '' },
    effectiveScope: 'built_in_default', reasons: tool.availability === 'unconfigured' ? ['示例知识库尚未连接，需要先在原设置入口完成配置。'] : [],
    revision: 'fixture-catalog-1', effectiveAtMs: 0,
  }));
  values.push(...([
    ['image-review', '截图审阅', '逐项核对图片中的信息层级、可读性与交互状态。', 'skill', 'installed'],
    ['source-check', '源码核对', '沿实际入口查找下游组件与数据来源。', 'skill', 'installed'],
    ['image-utilities', '图像处理扩展', '示例扩展：输出受控图片与文件，由工作台的内容组件展示。', 'extension', 'installed'],
    ['legacy-terminal', '终端专用扩展', '示例扩展：仅在 Pi 终端提供自定义交互，本页不执行其终端 UI。', 'extension', 'available'],
  ] as const).map(([id, displayName, description, kind, status]): CapabilityCatalogItem => ({
    id, canonicalId: `${kind}:${id}`, kind, displayName, description, source: { kind: 'fixture', label: '本地示例包' },
    status, risk: 'R0', requiredPermissions: [], authorization: { state: 'not_applicable', reason: '' },
    disclosure: { preference: 'inherit', effective: 'enabled', state: 'disclosed', reason: '' },
    effectiveScope: 'project_default', reasons: status === 'available' ? ['仅有示例资源记录；未安装到任何真实 Pi Session。'] : [], revision: 'fixture-catalog-1', effectiveAtMs: 0,
  })));
  if (sessionId === 'fixture-mars') {
    const shell = values.find(item => item.id === 'bash')!;
    shell.authorization = { state: 'denied', reason: 'Mars 的合成场景只允许读取，不允许运行命令。' };
  }
  for (const item of values) {
    const pref = preferences[item.canonicalId]; if (!pref || pref === 'inherit') continue;
    item.disclosure = { preference: pref, effective: pref, state: pref === 'enabled' ? 'disclosed' : 'hidden', reason: '' };
    item.effectiveScope = 'session';
  }
  return { schemaVersion: 'rag-ime.capability-catalog.v1', ok: true, revision: 'fixture-catalog-1', effectiveAtMs: 0,
    projectScope: { supported: false, identityKind: 'fixture', reason: 'synthetic_preview' },
    sessionPolicy: { sessionId, policyRevision: 1, effectiveAtMs: 0, disclosurePreferences: { globalDefault: {}, projectDefault: {}, session: preferences,
      effective: Object.fromEntries(values.map(item => [item.canonicalId, item.disclosure.effective])) } }, items: values };
}
export function demoImages(): GalleryImage[] {
  return PI_DEMO_ASSETS.map((asset, index) => ({ ...asset, id: `fixture-image-${index + 1}`, source: asset.dataUrl,
    alt: asset.name, origin: index === 0 ? 'user_upload' : 'tool_result', originTool: index === 0 ? '' : 'read',
    caption: '来自前序设计包的界面截图，仅用于本地图片阅读样例。', receipt: undefined }));
}
