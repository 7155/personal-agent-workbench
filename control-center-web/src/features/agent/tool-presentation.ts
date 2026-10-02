const toolNames: Record<string, string> = {
  overview: '当前状态',
  input: '输入法与词库',
  voice: '语音输入',
  planning: '任务与安排',
  memory: '我的记忆',
  agent_role_book: '伙伴记忆',
  knowledge: '知识库',
  models: '模型与连接',
  runtime: '运行检查',
  configuration: '设置与记录',
  agents: '委派协作',
  browser: '浏览网页',
  plugins: '扩展能力',
  todo: 'Todo',
  agent_schedule: '定时提醒',
  desktop_semantic: '操作当前应用',
  room_state: '查看协作状态',
  room_post: '发送协作消息',
  room_partner: '协作发言',
  room_commit: '提交工作结果',
  room_collaborate: '邀请伙伴协作',
  agent_goal: '长期目标',
  work_documents: '工作文档',
  workspace_job: '后台任务',
  subagent: '子 Agent',
  codemode: '代码执行',
  skill_load: '读取技能说明',
  tool_load: '读取工具说明',
  workspace_list: '浏览项目文件',
  workspace_read: '读取项目文件',
  workspace_search: '搜索项目内容',
  workspace_patch: '修改项目文件',
  workspace_shell: '运行项目命令',
  workspace_lsp: '代码智能',
  earth_workspace: '准备云端计算环境',
  earth_gis_search: '查找地理分析方法',
  earth_gis_inspect: '检查地理数据',
  earth_read_docs: '查阅 Google 官方资料',
  earth_run_script: '执行 GEE 云端计算',
  earth_remote_prepare: '生成遥感分析计划',
  earth_remote_run: '执行本地遥感分析',
  earth_view: '更新地图视图',
  earth_map_state: '读取地图范围',
  earth_task_status: '检查云端任务进度',
  earth_gis_bundle: '整理地理分析成果',
  read: '读取文件',
  read_file: '读取文件',
  write: '写入文件',
  write_file: '写入文件',
  workspace_write_file: '写入文件',
  edit: '编辑文件',
  edit_file: '编辑文件',
  workspace_edit_file: '编辑文件',
  bash: '运行命令',
  shell: '运行命令',
  grep: '搜索文本',
  find: '查找文件',
  ls: '浏览目录',
  add_location: '创建地点',
  add_new_entitlement: '创建服务权益',
  add_new_group_member: '添加组成员',
  add_new_user: '创建用户',
  add_new_user_group: '创建用户组',
  create_contact: '创建联系人',
  create_knowledge: '创建知识文章',
  create_new_case: '创建客户工单',
  enlist_new_contract: '创建服务合同',
  find_account: '查找客户账户',
  find_contact_by_portal_user: '查找门户联系人',
  find_contacts: '查找联系人',
  find_entitlements: '查找服务权益',
  find_product: '查找产品',
  find_sla_definitions: '查找 SLA',
  find_user: '查找用户',
  find_user_group: '查找用户组',
  link_case_knowledge: '关联工单知识',
  link_new_case_sla: '关联工单 SLA',
  list_users: '列出用户',
  register_new_installed_product: '登记已安装产品',
  retrieve_installed_products: '查找已安装产品',
  send_notification: '发送通知',
  update_case: '更新客户工单',
  update_contact: '更新联系人',
  update_entitlement: '更新服务权益',
};

const toolIntents: Record<string, string> = {
  overview: '帮我看看当前状态',
  input: '帮我调整输入法或词库',
  voice: '帮我检查语音输入',
  planning: '帮我整理任务与安排',
  memory: '帮我从记忆里找找',
  agent_role_book: '帮我查看伙伴记忆',
  knowledge: '帮我从知识库里查找',
  models: '帮我检查模型与连接',
  runtime: '帮我检查运行状态',
  configuration: '帮我查看设置或变更记录',
  agents: '请伙伴和我一起完成',
  browser: '帮我查看当前网页',
  todo: '帮我维护 Todo',
  workspace_list: '帮我看看项目里有哪些文件',
  workspace_read: '帮我读取这个项目文件',
  workspace_search: '帮我在项目里搜索',
  workspace_patch: '帮我修改这个项目文件',
  workspace_shell: '帮我运行这条项目命令',
};

export function publicToolName(toolId: string, fallback = ''): string {
  const normalizedId = toolId.trim().toLowerCase();
  const mapped = toolNames[normalizedId];
  if (mapped) return mapped;
  const readableFallback = fallback.trim();
  return isReadableToolName(readableFallback) ? readableFallback : '工具操作';
}

export function toolIntentPrompt(toolId: string, fallback = ''): string {
  const normalizedId = toolId.trim().toLowerCase();
  return toolIntents[normalizedId] ?? `帮我用${publicToolName(normalizedId, fallback)}处理`;
}

function isReadableToolName(value: string): boolean {
  return Boolean(value)
    && value.length <= 64
    && !/^[a-z0-9]+(?:[._-][a-z0-9]+)+$/i.test(value)
    && !/pathId|schema|receipt|operation|policy|profile|\/api\/|https?:\/\//i.test(value);
}

/** Visual grouping for compact per-row glyphs. Families are presentation-only:
 * they never change which payload fields are projected, only which icon the
 * conversation shows next to the accessible tool name. */
export type AgentToolFamily =
  | 'browser'
  | 'collaboration'
  | 'docs'
  | 'file'
  | 'goal'
  | 'job'
  | 'knowledge'
  | 'memory'
  | 'plan'
  | 'runtime'
  | 'search'
  | 'terminal'
  | 'generic';

const toolFamilies: Record<string, AgentToolFamily> = {
  codemode: 'terminal',
  bash: 'terminal',
  shell: 'terminal',
  workspace_shell: 'terminal',
  workspace_job: 'job',
  read: 'file',
  read_file: 'file',
  workspace_read: 'file',
  write: 'file',
  write_file: 'file',
  workspace_write: 'file',
  workspace_write_file: 'file',
  edit: 'file',
  edit_file: 'file',
  workspace_edit_file: 'file',
  workspace_patch: 'file',
  grep: 'search',
  find: 'search',
  ls: 'search',
  workspace_list: 'search',
  workspace_search: 'search',
  workspace_lsp: 'search',
  agents: 'collaboration',
  subagent: 'collaboration',
  room_state: 'collaboration',
  room_post: 'collaboration',
  room_partner: 'collaboration',
  room_commit: 'collaboration',
  room_collaborate: 'collaboration',
  agent_goal: 'goal',
  work_documents: 'docs',
  skill_load: 'docs',
  tool_load: 'docs',
  memory: 'memory',
  agent_role_book: 'memory',
  knowledge: 'knowledge',
  browser: 'browser',
  desktop_semantic: 'browser',
  planning: 'plan',
  todo: 'plan',
  agent_schedule: 'plan',
  overview: 'runtime',
  runtime: 'runtime',
  models: 'runtime',
  configuration: 'runtime',
  plugins: 'runtime',
  input: 'runtime',
  voice: 'runtime',
};

export function publicToolFamily(toolId: string): AgentToolFamily {
  return toolFamilies[toolId.trim().toLowerCase()] ?? 'generic';
}
