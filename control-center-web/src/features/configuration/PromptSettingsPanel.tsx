import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useRef, useState } from 'react';
import { Button } from '@/components/primitives';
import { InlineNotice, ManagementSection, asRecord, publicErrorText } from '@/features/overview/management-ui';
import type { ControlTransport } from '@/platform/transport';
import './prompt-settings.css';
import { useConfigurationSectionAnchor } from './useConfigurationSectionAnchor';

type PromptValues = { systemInstructions: string; compactionInstructions: string };
type PromptPolicy = { maxCharacters: number; defaults: PromptValues; builtInSystemPrompt: string };
type Snapshot = { revision: number; values: PromptValues; policy: PromptPolicy };
type Draft = { revision: number; base: PromptValues; values: PromptValues };
const fields = ['systemInstructions', 'compactionInstructions'] as const;
const queryKey = ['configuration', 'prompt-settings'] as const;

export function PromptSettingsPanel({ routeIds, transport, highlighted = false }: {
  routeIds: readonly string[];
  transport: ControlTransport;
  highlighted?: boolean;
}) {
  const client = useQueryClient();
  const panel = useRef<HTMLDivElement>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [saved, setSaved] = useState(false);
  const readable = routeIds.includes('agent.configuration.get');
  const writable = routeIds.includes('agent.configuration.update');
  const query = useQuery({
    queryKey,
    queryFn: async ({ signal }) => readSnapshot(await transport.request({ pathId: 'agent.configuration.get', signal })),
    enabled: readable,
    retry: false,
    refetchOnWindowFocus: false,
  });
  useConfigurationSectionAnchor(panel, highlighted);
  const mutation = useMutation({
    mutationFn: async ({ changes, revision }: { changes: Record<string, string>; revision: number }) => {
      if (!writable) throw new Error('当前版本不支持保存提示词');
      return readSnapshot(await transport.request({
        pathId: 'agent.configuration.update',
        body: { expectedRevision: revision, changes, updatedBy: 'settings-ui' },
      }), query.data?.policy);
    },
    onSuccess: (snapshot) => {
      client.setQueryData(queryKey, snapshot);
      void client.invalidateQueries({ queryKey: ['configuration', 'scenario-skill-routing', 'configuration'] });
      setDraft(null);
      setSaved(true);
    },
  });
  const snapshot = query.data;
  const values = draft?.values ?? snapshot?.values;
  const changes = Object.fromEntries(fields.flatMap((name) => draft && draft.values[name] !== draft.base[name]
    ? [[`prompts.${name}`, draft.values[name]]]
    : []));
  const dirty = Object.keys(changes).length > 0;
  const update = (name: keyof PromptValues, value: string) => {
    if (!snapshot) return;
    mutation.reset();
    setSaved(false);
    setDraft((current) => ({
      revision: current?.revision ?? snapshot.revision,
      base: current?.base ?? snapshot.values,
      values: { ...(current?.values ?? snapshot.values), [name]: value },
    }));
  };

  return <div id="configuration-prompts" ref={panel} className="configuration-prompts">
    <ManagementSection title="系统提示词与压缩" description="调整对话的补充规则，以及长对话压缩时需要保留的信息。项目规则继续来自 AGENTS.md 和按需读取的 docs。">
      {!readable ? <p>当前版本尚未提供提示词设置</p>
        : query.isPending ? <p role="status">正在读取提示词…</p>
          : !snapshot || !values ? <InlineNotice title="提示词暂不可用" tone="warning">
            <p>{publicErrorText(query.error, '当前版本尚未提供提示词设置')}</p>
            <Button onClick={() => void query.refetch()} size="small">重新读取提示词</Button>
          </InlineNotice> : <>
            <p className="configuration-prompts__scope">保存后对之后首次启动的会话生效。已有会话保留原设置，包括重连后的继续工作。</p>
            <div className="configuration-prompts__fields">
              <div>
                <label htmlFor="prompt-system">系统补充指令</label>
                <p id="prompt-system-help">填写表达习惯和工作偏好。留空使用内置规则；不会把整份项目文档固定放进每次请求。</p>
                <textarea id="prompt-system" aria-describedby="prompt-system-help" rows={7}
                  maxLength={snapshot.policy.maxCharacters} value={values.systemInstructions}
                  onChange={(event) => update('systemInstructions', event.target.value)} disabled={mutation.isPending || !writable}
                  placeholder="例如：默认使用中文，先给结论，再提供必要证据。" />
              </div>
              <div>
                <label htmlFor="prompt-compaction">压缩补充指令</label>
                <p id="prompt-compaction-help">手动与自动压缩共用。摘要保留文档引用、尚未落盘的变化和下一步；留空使用 Pi 原生摘要。</p>
                <textarea id="prompt-compaction" aria-describedby="prompt-compaction-help" rows={10}
                  maxLength={snapshot.policy.maxCharacters} value={values.compactionInstructions}
                  onChange={(event) => update('compactionInstructions', event.target.value)} disabled={mutation.isPending || !writable} />
              </div>
            </div>
            <details className="configuration-prompts__built-in">
              <summary>查看内置系统规则</summary>
              <p>这里展示 PAW 的基础规则。实际会话还会按职责、工具和项目上下文补充内容。</p>
              <pre>{snapshot.policy.builtInSystemPrompt}</pre>
            </details>
            {mutation.error ? <InlineNotice title="提示词未保存" tone="danger">
              <p>保存未成功，草稿已保留。</p>
              <p>{publicErrorText(mutation.error, '请重新读取后核对当前设置。')}</p>
              <Button onClick={() => void query.refetch()} size="small">重新读取当前版本</Button>
              {draft && snapshot.revision !== draft.revision ? <>
                <details className="configuration-prompts__built-in">
                  <summary>比较当前已保存内容</summary>
                  <p>其他位置已更新设置。请先比较，再决定是否用这份草稿覆盖修改过的字段。</p>
                  <p>系统补充指令</p><pre>{snapshot.values.systemInstructions || '（空）'}</pre>
                  <p>压缩补充指令</p><pre>{snapshot.values.compactionInstructions || '（空）'}</pre>
                </details>
                <Button size="small" disabled={!dirty} onClick={() => mutation.mutate({ changes, revision: snapshot.revision })}>按最新版本保存草稿</Button>
              </> : null}
            </InlineNotice> : null}
            {query.error ? <p role="status">当前配置未能刷新，已保留草稿和上次读取的版本。</p> : null}
            <div className="configuration-prompts__actions">
              <Button disabled={!dirty || !writable} loading={mutation.isPending}
                onClick={() => mutation.mutate({ changes, revision: draft?.revision ?? snapshot.revision })}>保存提示词</Button>
              <Button disabled={mutation.isPending || !writable} size="small" onClick={() => {
                mutation.reset(); setSaved(false);
                setDraft({ revision: snapshot.revision, base: snapshot.values, values: snapshot.policy.defaults });
              }}>恢复默认草稿</Button>
              {saved ? <span role="status">提示词已保存</span> : null}
              {dirty ? <span>有未保存的修改</span> : null}
            </div>
          </>}
    </ManagementSection>
  </div>;
}

function readValues(value: unknown): PromptValues {
  const record = asRecord(value);
  if (typeof record.systemInstructions !== 'string' || typeof record.compactionInstructions !== 'string') {
    throw new Error('当前版本尚未提供提示词设置');
  }
  return { systemInstructions: record.systemInstructions, compactionInstructions: record.compactionInstructions };
}

function readSnapshot(value: unknown, existingPolicy?: PromptPolicy): Snapshot {
  const root = asRecord(value);
  if (root.ok === false) throw new Error('提示词请求未成功');
  const snapshot = asRecord(root.configuration);
  const configuration = asRecord(snapshot.configuration);
  const rawPolicy = asRecord(root.promptPolicy);
  const policy = rawPolicy.schemaVersion === 'rag-ime.agent-prompt-policy.v1'
    && typeof rawPolicy.builtInSystemPrompt === 'string'
    && Number.isInteger(rawPolicy.maxCharacters) && Number(rawPolicy.maxCharacters) > 0
    ? { maxCharacters: Number(rawPolicy.maxCharacters), defaults: readValues(rawPolicy.defaults), builtInSystemPrompt: rawPolicy.builtInSystemPrompt }
    : existingPolicy;
  if (!policy) throw new Error('当前版本尚未提供提示词设置');
  if (!Number.isInteger(snapshot.revision) || Number(snapshot.revision) < 1) throw new Error('提示词设置版本无效');
  return { revision: Number(snapshot.revision), values: readValues(configuration.prompts), policy };
}
