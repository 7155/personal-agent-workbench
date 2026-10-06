import { useQuery } from '@tanstack/react-query';
import {
  Bot,
  Braces,
  GitFork,
  Network,
  ShieldCheck,
  Wrench,
} from 'lucide-react';
import { useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { useControlTransport } from '@/app/control-transport';
import { Button } from '@/components/primitives';
import type { AgentTemplateV1 } from '@/contracts/generated/agent-template.v1';
import { ManagementSection, StatusBadge } from '@/features/overview/management-ui';
import { usePawOsAppActive } from '@/features/paw-os/surface-context';
import { useConfigurationSectionAnchor } from './useConfigurationSectionAnchor';

export function SubagentSettingsPanel({ highlighted = false }: { highlighted?: boolean }) {
  const navigate = useNavigate();
  const transport = useControlTransport();
  const active = usePawOsAppActive() ?? true;
  const panelRef = useRef<HTMLDivElement>(null);

  useConfigurationSectionAnchor(panelRef, highlighted);
  const templatesQuery = useQuery({
    enabled: active,
    queryKey: ['configuration', 'subagents', 'templates'],
    queryFn: ({ signal }) => transport.request({
      pathId: 'agent.subagents.templates',
      signal,
    }),
    staleTime: 60_000,
    retry: false,
  });
  const envelope = record(templatesQuery.data);
  const templates = Array.isArray(envelope.items)
    ? envelope.items.filter(isTemplate)
    : [];
  const maxParallel = Number(envelope.maxParallel) || 2;
  const maxDepth = Number(envelope.maxDepth) || 2;

  return (
    <div
      aria-label="子 Agent 设置"
      className="configuration-subagents"
      data-highlighted={highlighted || undefined}
      id="configuration-subagents"
      ref={panelRef}
    >
      <ManagementSection
        title="子 Agent"
        description="对话可以临时分出小帮手，各自领一块任务、做完汇报回来。这里是它们的模板目录和运行边界；启动与监督都在对话的工作台里进行。"
      >
        <div className="configuration-subagents__summary">
          <div>
            <span><Network size={18} /></span>
            <p><strong>同时能有多少帮手</strong><small>最多同时 {maxParallel} 个 · 嵌套不超过 {maxDepth} 层</small></p>
          </div>
          <StatusBadge label={templatesQuery.error ? '目录读取失败' : templatesQuery.isPending ? '正在读取' : `${templates.length} 个模板可用`} tone={templatesQuery.error ? 'danger' : templatesQuery.isPending ? 'neutral' : 'success'} />
          <Button leadingIcon={<Network size={15} />} onClick={() => navigate('/agent?subagents=open')} size="small">打开子 Agent 工作台</Button>
        </div>

        <div className="configuration-subagents__rules" aria-label="子 Agent 运行边界">
          <article><GitFork size={17} /><span><strong>从零开始，或接着看</strong><small>帮手可以拿全新的上下文，也可以从当前对话的指定位置接着看。</small></span></article>
          <article><Wrench size={17} /><span><strong>工具只减不增</strong><small>帮手能用的工具只会比对话本身更少，不会更多。</small></span></article>
          <article><ShieldCheck size={17} /><span><strong>看得到，改不了</strong><small>只读帮手能看到同样的目录；写文件、执行命令这类操作会被拦下。</small></span></article>
          <article><Braces size={17} /><span><strong>结果要交得清楚</strong><small>汇报必须符合约定格式；不合格只标记这个帮手，不影响其他工作。</small></span></article>
        </div>

        {templatesQuery.error ? <p className="configuration-subagents__error" role="alert">模板目录暂时不可用；不会用空目录覆盖当前配置。</p> : null}
        {templates.length ? <div className="configuration-subagents__templates" aria-label="子 Agent 模板">
          {templates.map((template) => <article key={`${template.templateId}:${template.version}`}>
            <span><Bot size={16} /></span>
            <p><strong>{template.displayName}</strong><small>{template.summary}</small></p>
            <em>{template.defaultAccess === 'write' ? '可申请写入' : '只读'}</em>
          </article>)}
        </div> : null}
      </ManagementSection>
    </div>
  );
}

function isTemplate(value: unknown): value is AgentTemplateV1 {
  const item = record(value);
  return item.schemaVersion === 'rag-ime.agent-template.v1'
    && typeof item.templateId === 'string'
    && typeof item.displayName === 'string'
    && typeof item.summary === 'string';
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}
