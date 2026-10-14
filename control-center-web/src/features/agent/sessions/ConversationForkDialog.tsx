import { CornerDownRight, GitBranch, LoaderCircle } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import * as RadioGroup from '@radix-ui/react-radio-group';
import { useControlTransport } from '@/app/control-transport';
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/primitives';
import { publicAgentErrorText } from '../public-error';
import type { SessionSummary } from '../types';

interface ForkCandidate {
  entryId: string;
  text: string;
  role: 'user' | 'assistant';
  createdAtMs: number;
}

interface PathNode extends ConversationNode {
  key: string;
  branchEntryId?: string;
  jumpEntryId?: string;
}

export interface ConversationNode {
  entryId: string;
  role: 'user' | 'assistant';
  text: string;
  createdAtMs: number;
}

export function resolveConversationEntryId(
  response: unknown,
  nodes: ConversationNode[],
  projectedEntryId: string,
): string {
  const node = mergePathNodes(nodes, forkCandidates(response))
    .find((item) => item.key === projectedEntryId);
  return node?.branchEntryId ?? '';
}

export function ConversationForkDialog({
  assistantName = 'Agent',
  open,
  sessionId,
  sessionTitle,
  nodes,
  initialEntryId,
  branchAvailable,
  branchBlocked,
  branchUnavailableReason,
  onOpenChange,
  onJump,
  onCreated,
}: {
  assistantName?: string;
  open: boolean;
  sessionId: string;
  sessionTitle: string;
  nodes: ConversationNode[];
  initialEntryId?: string;
  branchAvailable: boolean;
  branchBlocked: boolean;
  branchUnavailableReason?: string;
  onOpenChange: (open: boolean) => void;
  onJump: (entryId: string) => void;
  onCreated: (session: SessionSummary, selectedText: string) => void;
}) {
  const transport = useControlTransport();
  const [items, setItems] = useState<ForkCandidate[]>([]);
  const [selectedId, setSelectedId] = useState('');
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState('');
  const [readError, setReadError] = useState(false);
  const [readAttempt, setReadAttempt] = useState(0);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open || !sessionId) return;
    const preferred = nodes.some((item) => item.entryId === initialEntryId)
      ? initialEntryId ?? ''
      : nodes.at(-1)?.entryId ?? '';
    setSelectedId(preferred);
    setReadError(false);
  }, [initialEntryId, nodes, open, sessionId, transport]);

  useEffect(() => {
    if (!open || !sessionId) return;
    if (!branchAvailable || branchBlocked) {
      setItems([]);
      setLoading(false);
      setError('');
      setReadError(false);
      return;
    }
    let active = true;
    const abort = new AbortController();
    setItems([]);
    setLoading(true);
    setError('');
    void transport.request<Record<string, unknown>>({
      pathId: 'agent.session.forks.list',
      params: { sessionId },
      signal: abort.signal,
      timeoutMs: 45_000,
    }).then((response) => {
      if (!active) return;
      const candidates = forkCandidates(response);
      setItems(candidates);
      setReadError(false);
    }).catch((requestError) => {
      if (!active) return;
      setItems([]);
      setReadError(true);
      setError(publicAgentErrorText(requestError, '历史分支点暂时无法读取。'));
    }).finally(() => {
      if (active) setLoading(false);
    });
    return () => { active = false; abort.abort(); };
  }, [branchAvailable, branchBlocked, initialEntryId, nodes, open, readAttempt, sessionId, transport]);

  const pathNodes = useMemo(() => mergePathNodes(nodes, items), [items, nodes]);
  const selectedNode = useMemo(() => pathNodes.find((item) => item.key === selectedId), [pathNodes, selectedId]);
  const selectedCandidate = useMemo(
    () => items.find((item) => item.entryId === selectedNode?.branchEntryId),
    [items, selectedNode?.branchEntryId],
  );

  async function createFork(): Promise<void> {
    if (!selectedCandidate || creating || branchBlocked) return;
    setCreating(true);
    setError('');
    try {
      const response = await transport.request<Record<string, unknown>>({
        pathId: 'agent.session.forks.create',
        params: { sessionId },
        body: { entryId: selectedCandidate.entryId, title: `${sessionTitle} · 分支` },
      });
      const session = record(response.session) as unknown as SessionSummary;
      if (!session.id) throw new Error('后端没有返回新分支会话。');
      const selectedText = typeof response.selectedText === 'string'
        ? response.selectedText
        : selectedCandidate.role === 'user' ? selectedCandidate.text : '';
      onCreated(session, selectedText);
      onOpenChange(false);
    } catch (requestError) {
      setError(publicAgentErrorText(requestError, '创建对话分支失败。'));
    } finally {
      setCreating(false);
    }
  }

  function jumpToNode(): void {
    if (!selectedNode?.jumpEntryId) return;
    onJump(selectedNode.jumpEntryId);
    onOpenChange(false);
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !creating && onOpenChange(next)}>
      <DialogContent className="agent-fork-dialog">
        <DialogHeader>
          <DialogTitle><GitBranch size={18} />对话路径</DialogTitle>
          <DialogDescription>
            {branchAvailable
              ? '所有公开消息都可跳转和创建分支；用户输入会回到草稿，助手回答会作为新分支的已有上下文。'
              : '公开消息仍可在当前路径中跳转；这段对话不允许创建普通对话分支。'}
          </DialogDescription>
        </DialogHeader>
        {pathNodes.length ? (
          <RadioGroup.Root ref={listRef} className="agent-fork-dialog__list" aria-label="对话分支点" value={selectedId} onValueChange={setSelectedId}>
            {pathNodes.map((item, index) => {
              const branchable = Boolean(item.branchEntryId);
              return (
              <RadioGroup.Item
                key={item.key}
                value={item.key}
                data-selected={item.key === selectedId}
              >
                <span className="agent-fork-dialog__index">{index + 1}</span>
                <span className="agent-fork-dialog__node-copy">
                  <small>{item.role === 'user' ? '你' : assistantName} · {formatNodeTime(item.createdAtMs)}</small>
                  <strong>{item.text}</strong>
                </span>
                <em data-branchable={branchable || undefined}>
                  {branchable ? '可跳转 · 可分支' : loading ? '核对中' : '可跳转'}
                </em>
              </RadioGroup.Item>
              );
            })}
          </RadioGroup.Root>
        ) : (
          <div className="agent-fork-dialog__state">当前对话还没有可回溯的消息。</div>
        )}
        {loading ? <p className="agent-fork-dialog__status"><LoaderCircle size={14} />正在核对历史节点</p> : null}
        {branchBlocked ? <p className="agent-fork-dialog__status">当前回合结束后可创建分支，历史节点仍可直接跳转。</p> : null}
        {!branchAvailable ? (
          <p className="agent-fork-dialog__status">
            {branchUnavailableReason ?? '当前运行时未提供分支能力，历史节点仍可直接跳转。'}
          </p>
        ) : null}
        {error ? <p className="agent-fork-dialog__error" role="alert">{error} 历史节点仍可直接跳转。</p> : null}
        {readError ? <p className="agent-fork-dialog__status"><Button variant="quiet" size="small" loading={loading} disabled={creating || branchBlocked} onClick={() => {
          setReadAttempt(attempt => attempt + 1);
          listRef.current?.querySelector<HTMLElement>('[role="radio"][aria-checked="true"]')?.focus({ preventScroll: true });
        }}>重试历史节点</Button></p> : null}
        <footer>
          <Button variant="quiet" onClick={() => onOpenChange(false)} disabled={creating}>取消</Button>
          <Button variant="quiet" leadingIcon={<CornerDownRight size={15} />} onClick={jumpToNode} disabled={!selectedNode?.jumpEntryId || creating}>跳到节点</Button>
          <Button data-loading={creating || undefined} onClick={() => void createFork()} disabled={!selectedCandidate || loading || creating || branchBlocked}>
            {creating ? <><LoaderCircle size={15} />正在创建</> : <><GitBranch size={15} />创建分支</>}
          </Button>
        </footer>
      </DialogContent>
    </Dialog>
  );
}

function mergePathNodes(nodes: ConversationNode[], candidates: ForkCandidate[]): PathNode[] {
  const remaining = new Map(candidates.map((candidate) => [candidate.entryId, candidate]));
  const path = nodes.map((node): PathNode => {
    const candidate = remaining.get(node.entryId);
    if (candidate) remaining.delete(candidate.entryId);
    return {
      ...node,
      key: node.entryId,
      jumpEntryId: node.entryId,
      branchEntryId: candidate?.entryId,
    };
  });

  // Pi entry ids and projected message ids can differ. Prefer the stable
  // public role/timestamp pair, then fall back to role-scoped text occurrence.
  // Ambiguous partial catalogs fail closed instead of branching from a hidden
  // or repeated message with a different role.
  for (const node of path) {
    if (node.branchEntryId || node.createdAtMs <= 0) continue;
    const matches = [...remaining.values()].filter((candidate) => (
      candidate.role === node.role
      && candidate.createdAtMs > 0
      && candidate.createdAtMs === node.createdAtMs
    ));
    if (matches.length !== 1) continue;
    const [candidate] = matches;
    if (!candidate) continue;
    node.branchEntryId = candidate.entryId;
    remaining.delete(candidate.entryId);
  }

  const nodesByText = new Map<string, PathNode[]>();
  for (const node of path) {
    if (node.branchEntryId) continue;
    const normalized = `${node.role}\0${normalizeText(node.text)}`;
    nodesByText.set(normalized, [...(nodesByText.get(normalized) ?? []), node]);
  }
  const candidatesByText = new Map<string, ForkCandidate[]>();
  for (const candidate of remaining.values()) {
    const normalized = `${candidate.role}\0${normalizeText(candidate.text)}`;
    candidatesByText.set(normalized, [...(candidatesByText.get(normalized) ?? []), candidate]);
  }
  for (const [normalized, publicNodes] of nodesByText) {
    const matching = candidatesByText.get(normalized) ?? [];
    if (matching.length !== publicNodes.length) continue;
    publicNodes.forEach((node, index) => { node.branchEntryId = matching[index]?.entryId; });
  }
  return path;
}

function normalizeText(value: string): string {
  return value.replace(/\s+/gu, ' ').trim();
}

function formatNodeTime(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return '历史消息';
  return new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' }).format(value);
}

function forkCandidates(value: unknown): ForkCandidate[] {
  const items = Array.isArray(record(value).items) ? record(value).items as unknown[] : [];
  return items.map((item) => record(item)).filter((item) => (
    typeof item.entryId === 'string' && item.entryId.length > 0
      && typeof item.text === 'string' && item.text.trim().length > 0
      && (item.role === 'user' || item.role === 'assistant')
      && !item.text.includes('<rag-ime-deep-search-context')
      && !item.text.includes('<rag-ime-user-query>')
      && !item.text.includes('<agent-deep-search-context')
      && !item.text.includes('<agent-user-query>')
  )).map((item) => ({
    entryId: text(item.entryId),
    text: text(item.text).trim(),
    role: item.role as ForkCandidate['role'],
    createdAtMs: Math.max(0, Number(item.createdAtMs) || 0),
  })).slice(0, 500);
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : '';
}
