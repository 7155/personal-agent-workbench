export const CHAT_SURFACE_API_MAJOR = 1;
const V1_FEATURES = ['embedded', 'composer-context', 'draft-intent', 'composer-header'] as const;
export type ChatSurfaceRequirement = { major: number; features?: readonly string[] };

/** UI API compatibility, not a tool grant or an independently pinned renderer.
 * A release currently ships one V1 implementation; unsupported majors never
 * silently fall back to another contract. Kept pure so the OS loader does not
 * load the chat implementation just to check a module's requirements. */
export function chatSurfaceCompatibility(requirement: ChatSurfaceRequirement): { supported: boolean; reason?: string } {
  if (!requirement || !Number.isInteger(requirement.major) || requirement.major < 1
    || requirement.features !== undefined && (!Array.isArray(requirement.features) || requirement.features.some(feature => typeof feature !== 'string'))) {
    return { supported: false, reason: '此 App 的聊天接口要求不完整，请更新 App 后重试。' };
  }
  if (requirement.major !== CHAT_SURFACE_API_MAJOR) {
    return { supported: false, reason: `此 App 需要聊天接口 v${requirement.major}，当前工作台支持 v${CHAT_SURFACE_API_MAJOR}。` };
  }
  const missing = requirement.features?.filter(feature => !(V1_FEATURES as readonly string[]).includes(feature)) ?? [];
  return missing.length ? { supported: false, reason: `此 App 所需的聊天功能暂不可用：${missing.join('、')}。` } : { supported: true };
}
