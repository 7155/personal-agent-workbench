import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { ShieldCheck } from 'lucide-react';
import { Button, Field, Switch, Tabs, TabsContent, TabsList, TabsTrigger, TextArea } from '@/components/primitives';
import { InlineNotice, ManagementSection, StatusBadge, asRecord, arrayRecords, publicErrorText, stringValue } from '@/features/overview/management-ui';
import type { ControlTransport } from '@/platform/transport';
import './configuration.css';

const scenarioIds = ['ordinary', 'room', 'trace', 'agentLab'] as const;
type ScenarioId = (typeof scenarioIds)[number];
type ScenarioPolicy = {
  id: ScenarioId;
  label: string;
  description: string;
  appIds: string[];
  appLabels: string[];
  variantModes: string[];
  builtInToolIds: string[];
  toolAllowlist: string[];
  promptInstructions: string;
  systemPrompt: string;
};
type ScenarioPolicyCatalog = { revision: number; policyRevision: string; scenarios: ScenarioPolicy[] };

const toolLabels: Record<string, string> = {
  overview: '概览', input: '输入', voice: '语音', planning: '规划', agent_schedule: '调度',
  memory: '记忆', agent_role_book: '角色书', knowledge: '知识', models: '模型', runtime: 'Runtime',
  configuration: '配置', agents: 'Agent 委派', session_search: 'Session 搜索', trace_diagnostics: 'Trace 诊断',
  room_partner: 'Room 伙伴', browser: '浏览器', plugins: '插件', work_documents: '工作文档', lab_project: 'Lab 项目',
  lab_research: 'Lab 研究', desktop_semantic: '桌面语义', workspace_list: '工作区清单', workspace_read: '工作区读取',
  workspace_search: '工作区搜索', workspace_lsp: '代码智能', workspace_patch: '工作区补丁', workspace_edit: '工作区编辑',
  workspace_write: '工作区写入', workspace_job: '工作区任务', workspace_shell: '工作区 Shell',
};

export function ScenarioAgentPolicySettings({ routeIds, transport, active = true }: {
  routeIds: readonly string[];
  transport: ControlTransport;
  active?: boolean;
}) {
  const queryClient = useQueryClient();
  const [activeScenario, setActiveScenario] = useState<ScenarioId>('ordinary');
  const [drafts, setDrafts] = useState<Partial<Record<ScenarioId, Pick<ScenarioPolicy, 'promptInstructions' | 'toolAllowlist'>>>>({});
  const [saved, setSaved] = useState<ScenarioId | null>(null);
  const supported = routeIds.includes('agent.configuration.get');
  const canUpdate = routeIds.includes('agent.configuration.update');
  const query = useQuery({
    queryKey: ['configuration', 'scenario-agent-policy'],
    queryFn: async ({ signal }) => parseCatalog(await transport.request({ pathId: 'agent.configuration.get', signal })),
    enabled: active && supported,
    retry: false,
    staleTime: 5_000,
    refetchOnReconnect: 'always',
  });
  const mutation = useMutation({
    mutationFn: async (input: { scenario: ScenarioPolicy; draft: Pick<ScenarioPolicy, 'promptInstructions' | 'toolAllowlist'> }) => {
      if (!canUpdate) throw new Error('当前 Runtime 没有公布场景策略保存能力，本次修改未发送。');
      const response = await transport.request({
        pathId: 'agent.configuration.update',
        body: {
          expectedRevision: query.data!.revision,
          changes: { [`scenarioPolicies.${input.scenario.id}`]: input.draft },
          updatedBy: 'scenario-policy-settings-ui',
        },
      });
      return parseCatalog(response);
    },
    onSuccess: (catalog) => {
      queryClient.setQueryData(['configuration', 'scenario-agent-policy'], catalog);
      setDrafts({});
      setSaved(activeScenario);
    },
  });

  const current = query.data?.scenarios.find((scenario) => scenario.id === activeScenario);
  const toolGroups = useMemo(() => {
    if (!current) return [];
    return [...current.builtInToolIds].sort().map((id) => ({ id, label: toolLabels[id] ?? id }));
  }, [current]);

  return <ManagementSection
    title="场景 Agent 策略"
    description="在一个地方管理每个 App/场景的系统提示词补充、Tool 披露和 Skill 路由。Runtime 按 Session 身份选择策略；已有 Session 保持首次打开时的冻结快照。"
  >
    {!supported ? <InlineNotice title="场景策略暂不可用" tone="warning"><p>当前 Runtime 没有公布配置读取能力，因此不会猜测或修改策略。</p></InlineNotice>
      : query.isPending ? <p className="configuration-agent-policy__state" role="status">正在读取 App/场景策略…</p>
        : query.error ? <InlineNotice title="场景策略读取失败" tone="danger"><p>{publicErrorText(query.error, '无法读取场景策略；现有配置没有改变。')}</p><Button size="small" variant="secondary" onClick={() => void query.refetch()}>重试</Button></InlineNotice>
          : query.data ? <div className="configuration-agent-policy">
            {!canUpdate ? <InlineNotice title="当前只能查看" tone="warning"><p>Runtime 没有公布场景策略更新能力；开关和提示词已锁定。</p></InlineNotice> : null}
            <div className="configuration-agent-policy__identity"><ShieldCheck aria-hidden="true" size={16} /><span>策略版本 {query.data.policyRevision}</span><StatusBadge label={`${query.data.scenarios.length} 个 App/场景`} tone="neutral" /></div>
            <Tabs value={activeScenario} onValueChange={(value) => { setActiveScenario(value as ScenarioId); setSaved(null); mutation.reset(); }}>
              <TabsList aria-label="App 和场景策略" className="configuration-agent-policy__tabs">
                {query.data.scenarios.map((scenario) => <TabsTrigger disabled={mutation.isPending} key={scenario.id} value={scenario.id}><strong>{scenario.label}</strong><small>{scenario.description}</small></TabsTrigger>)}
              </TabsList>
              {query.data.scenarios.map((scenario) => {
                const selected = drafts[scenario.id]?.toolAllowlist ?? scenario.toolAllowlist;
                const prompt = drafts[scenario.id]?.promptInstructions ?? scenario.systemPrompt;
                const dirty = prompt !== scenario.systemPrompt || !sameArray(selected, scenario.toolAllowlist);
                return <TabsContent className="configuration-agent-policy__panel" key={scenario.id} value={scenario.id}>
                  <p className="configuration-agent-policy__variants">App：{(scenario.appLabels.length ? scenario.appLabels : scenario.appIds).join('、')} · 命中变体：{scenario.variantModes.join('、')}</p>
                  <Field htmlFor={`scenario-agent-prompt-${scenario.id}`} label="App/场景系统提示词补充" description="这是附加层；Runtime 的安全边界、证据要求和执行授权仍由系统固定控制。">
                    <TextArea disabled={!canUpdate} id={`scenario-agent-prompt-${scenario.id}`} maxLength={8_000} rows={5} value={prompt} placeholder="例如：输出必须带来源、实验编号和验证状态。" onChange={(event) => { setSaved(null); setDrafts((currentDrafts) => ({ ...currentDrafts, [scenario.id]: { promptInstructions: event.target.value, toolAllowlist: selected } })); }} />
                  </Field>
                  <div className="configuration-agent-policy__tools"><div className="configuration-agent-policy__tools-heading"><strong>允许披露的 Tool</strong><span>关闭后新 Session 不会收到该 Tool，也不能通过直接调用绕过。</span></div>
                    {toolGroups.map((tool) => <Switch checked={selected.includes(tool.id)} disabled={!canUpdate} id={`scenario-agent-tool-${scenario.id}-${tool.id}`} key={tool.id} label={tool.label} description={tool.id} onCheckedChange={(checked) => { setSaved(null); const next = checked ? [...new Set([...selected, tool.id])].sort() : selected.filter((id) => id !== tool.id); setDrafts((currentDrafts) => ({ ...currentDrafts, [scenario.id]: { promptInstructions: prompt, toolAllowlist: next } })); }} />)}
                  </div>
                  <footer className="configuration-agent-policy__actions"><p>保存后只影响新打开的 Session；当前 Session 的提示词和 Tool 快照不会被热替换。</p><Button disabled={!dirty || !canUpdate} loading={mutation.isPending && mutation.variables?.scenario.id === scenario.id} size="small" variant="primary" onClick={() => { mutation.reset(); mutation.mutate({ scenario, draft: { promptInstructions: prompt, toolAllowlist: selected } }); }}>保存 {scenario.label}</Button></footer>
                  {mutation.error && mutation.variables?.scenario.id === scenario.id ? <InlineNotice title="场景策略未保存" tone="danger"><p>{publicErrorText(mutation.error, '设置尚未保存，请重新读取后重试。')}</p></InlineNotice> : null}
                  {saved === scenario.id ? <InlineNotice title="场景策略已保存" tone="success"><p>新 Session 将使用这份 App/场景策略；已有 Session 继续使用原快照。</p></InlineNotice> : null}
                </TabsContent>;
              })}
            </Tabs>
          </div> : null}
  </ManagementSection>;
}

function parseCatalog(value: unknown): ScenarioPolicyCatalog {
  const envelope = asRecord(value);
  const snapshot = asRecord(envelope.configuration);
  const configuration = asRecord(snapshot.configuration);
  const supplied = asRecord(envelope.scenarioPolicyCatalog);
  const rows = arrayRecords(supplied.scenarios);
  const revision = Number(snapshot.revision);
  if (!Number.isInteger(revision) || revision < 1 || rows.length !== scenarioIds.length) throw new Error('Runtime 返回的 App/场景策略不完整；不会猜测或修改现有设置。');
  const scenarios = rows.map((row) => {
    const id = stringValue(row.id) as ScenarioId;
    const builtIn = row.builtInToolIds;
    const selected = row.toolAllowlist;
    if (!scenarioIds.includes(id) || !Array.isArray(builtIn) || !Array.isArray(selected) || builtIn.some((v) => typeof v !== 'string') || selected.some((v) => typeof v !== 'string')) throw new Error('Runtime 返回的 Tool 披露策略格式无效。');
    const modes = Array.isArray(row.variantModes) ? row.variantModes.filter((v): v is string => typeof v === 'string') : [];
    const appIds = Array.isArray(row.appIds) ? row.appIds.filter((v): v is string => typeof v === 'string') : [];
    const appLabels = Array.isArray(row.appLabels) ? row.appLabels.filter((v): v is string => typeof v === 'string') : [];
    const prompt = stringValue(row.systemPrompt, stringValue(row.promptInstructions));
    return { id, label: stringValue(row.label, id), description: stringValue(row.description), appIds, appLabels, variantModes: modes, builtInToolIds: builtIn.map(String), toolAllowlist: selected.map(String), promptInstructions: prompt, systemPrompt: prompt };
  });
  if (!rows.length) {
    const policies = asRecord(configuration.scenarioPolicies);
    if (!policies) throw new Error('Runtime 未返回场景策略目录。');
  }
  return { revision, policyRevision: stringValue(supplied.policyRevision, 'rag-ime.agent-scenario-policy.v1'), scenarios };
}

function sameArray(left: readonly string[], right: readonly string[]) { return left.length === right.length && left.every((value, index) => value === right[index]); }
