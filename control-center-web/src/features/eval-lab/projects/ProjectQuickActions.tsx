import { ExternalLink, FileTerminal, LoaderCircle, Play, ShieldCheck } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useOptionalControlTransport } from '@/app/control-transport';
import { Button } from '@/components/primitives';
import type { AgentBackgroundJobV1 } from '@/contracts/generated/agent-background-job.v1';
import { usePawOsDesktop } from '@/features/paw-os/surface-context';
import { usePageVisibility } from '@/platform/use-page-visibility';
import { backgroundJobWindowRequest } from '@/paw-os/runtime/runtime-tool-window';
import type { LabProject } from './types';
import './project-quick-actions.css';

export type ProjectQuickAction = 'preview' | 'checks';

export type ProjectQuickActionContext = {
  projectId: string;
  title: string;
  sessionId: string;
  cwd: string;
  previewUrl?: string;
};

type BackgroundJobListResponse = {
  ok: boolean;
  sessionId: string;
  items: AgentBackgroundJobV1[];
};

type QuickActionResponse = {
  ok: boolean;
  job?: AgentBackgroundJobV1;
  quickAction?: { action?: string; script?: string; previewUrl?: string };
  summary?: string;
};

const ACTIVE_STATUSES = new Set<AgentBackgroundJobV1['status']>(['queued', 'running', 'cancelling']);

export function projectQuickActionCommand(action: ProjectQuickAction): string {
  return action === 'preview' ? 'npm run start' : 'npm run test';
}

export function projectQuickActionLabel(action: ProjectQuickAction): string {
  return action === 'preview' ? '项目预览' : '项目检核';
}

export function isActiveProjectQuickAction(job: AgentBackgroundJobV1, action: ProjectQuickAction, cwd?: string): boolean {
  if (!ACTIVE_STATUSES.has(job.status) || cwd && job.cwd !== cwd) return false;
  if (job.command === projectQuickActionCommand(action)) return true;
  // A Room partner can use explicit port flags on the declared start script.
  // Shell chains and filtered test commands are separate jobs, not full actions.
  return action === 'preview' && !/[\r\n]/u.test(job.command)
    && /^[ \t]*npm[ \t]+run[ \t]+start(?:[ \t]+--(?:[ \t]+[A-Za-z0-9_.:/=-]+)+)?[ \t]*$/u.test(job.command);
}

export function projectPreviewUrl(value: unknown): string {
  if (typeof value !== 'string' || !value.trim()) return '';
  try {
    const url = new URL(value);
    if (!['http:', 'https:'].includes(url.protocol)) return '';
    if (!['127.0.0.1', 'localhost', '[::1]', '::1'].includes(url.hostname)) return '';
    if (!url.port || !url.pathname.startsWith('/') || url.pathname.includes('..') || url.search || url.hash || url.username || url.password) return '';
    return url.toString();
  } catch {
    return '';
  }
}

export function loopbackPreviewUrlFromLogs(value: unknown): string {
  if (typeof value !== 'string') return '';
  const candidates = value.match(/https?:\/\/(?:127\.0\.0\.1|localhost|\[::1\]):\d+\/[^\s"'<>]*/gu) ?? [];
  for (const candidate of candidates) {
    const parsed = projectPreviewUrl(candidate.replace(/[),.;]+$/u, ''));
    if (parsed) return parsed;
  }
  return '';
}

export function ProjectQuickActions({ project, context, compact = false, active = true }: {
  project?: LabProject;
  context?: Partial<ProjectQuickActionContext>;
  compact?: boolean;
  active?: boolean;
}) {
  const transport = useOptionalControlTransport();
  const desktop = usePawOsDesktop();
  const pageVisible = usePageVisibility();
  const [busy, setBusy] = useState<ProjectQuickAction | ''>('');
  const [notice, setNotice] = useState('');
  const [discoveredPreviewUrl, setDiscoveredPreviewUrl] = useState('');
  const [items, setItems] = useState<AgentBackgroundJobV1[]>([]);
  const discoveryGeneration = useRef(0);
  const discoveryAbort = useRef<AbortController | null>(null);
  const idempotencyKeys = useRef(new Map<string, string>());
  const projectId = context?.projectId ?? project?.projectId ?? '';
  const title = context?.title ?? project?.title ?? '项目';
  const sessionId = context?.sessionId ?? project?.guideSessionId ?? '';
  const cwd = context?.cwd ?? project?.directory?.path ?? '';
  const configuredPreviewUrl = projectPreviewUrl(context?.previewUrl ?? project?.directory?.previewUrl);
  const previewUrl = configuredPreviewUrl || discoveredPreviewUrl;

  const refreshJobs = useCallback(async (signal?: AbortSignal) => {
    if (!transport || !sessionId) {
      setItems([]);
      return [];
    }
    if (!active || !pageVisible) return [];
    try {
      const response = await transport.request<BackgroundJobListResponse>({
        pathId: 'agent.session.backgroundJobs.list',
        params: { sessionId },
        query: { limit: 50 },
        signal,
        timeoutMs: 10000,
      });
      if (signal?.aborted) return [];
      const nextItems = Array.isArray(response.items) ? response.items : [];
      setItems(nextItems);
      return nextItems;
    } catch {
      // The buttons remain usable when the optional status projection is unavailable.
      return [];
    }
  }, [active, pageVisible, sessionId, transport]);

  useEffect(() => {
    discoveryGeneration.current += 1;
    discoveryAbort.current?.abort();
    discoveryAbort.current = null;
    setDiscoveredPreviewUrl('');
    const controller = new AbortController();
    let disposed = false;
    let timer: number | undefined;
    let loading = false;
    if (!active || !pageVisible) return () => {
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
    const poll = async () => {
      if (disposed || loading) return;
      loading = true;
      try {
        const jobs = await refreshJobs(controller.signal);
        if (!disposed) timer = window.setTimeout(() => void poll(), jobs.some(job => ACTIVE_STATUSES.has(job.status)) ? 1000 : 5000);
      } finally {
        loading = false;
      }
    };
    void poll();
    return () => {
      disposed = true;
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [active, pageVisible, refreshJobs]);

  useEffect(() => () => {
    discoveryGeneration.current += 1;
    discoveryAbort.current?.abort();
  }, [sessionId]);

  const openJob = (job: AgentBackgroundJobV1) => {
    desktop?.openWindow(backgroundJobWindowRequest(job));
  };

  const openPreview = (requestedUrl = previewUrl) => {
    if (!requestedUrl) return;
    desktop?.openWindow({
      appId: 'browser',
      target: {
        kind: 'browser-target',
        id: requestedUrl,
        title: `${title} · 打开预览页面`,
        sessionId,
        toolCallId: `project-quick-action:${projectId}`,
        targetId: '',
        provisional: true,
        url: requestedUrl,
      },
    });
  };

  const discoverPreviewUrl = async (job: AgentBackgroundJobV1) => {
    if (!transport || job.sessionId !== sessionId) return;
    discoveryAbort.current?.abort();
    const controller = new AbortController();
    discoveryAbort.current = controller;
    const generation = discoveryGeneration.current;
    for (let attempt = 0; attempt < 12; attempt += 1) {
      if (controller.signal.aborted || generation !== discoveryGeneration.current) return;
      try {
        const logs = await transport.request<{ text?: string }>({
          pathId: 'agent.session.backgroundJob.logs',
          params: { sessionId, jobId: job.jobId },
          query: { cursor: 0, limitBytes: 131_072 },
          signal: controller.signal,
        });
        const discovered = loopbackPreviewUrlFromLogs(logs.text);
        if (discovered && !controller.signal.aborted && generation === discoveryGeneration.current) {
          setDiscoveredPreviewUrl(discovered);
          openPreview(discovered);
          return;
        }
      } catch {
        // The terminal remains the failure surface; stdout discovery is best effort.
      }
      await new Promise<void>((resolve) => {
        const timer = window.setTimeout(resolve, 500);
        controller.signal.addEventListener('abort', () => {
          window.clearTimeout(timer);
          resolve();
        }, { once: true });
      });
    }
  };

  const start = async (action: ProjectQuickAction) => {
    if (busy) return;
    const active = items.find((job) => isActiveProjectQuickAction(job, action, cwd));
    if (active) {
      openJob(active);
      if (action === 'preview') {
        if (previewUrl) openPreview();
        else void discoverPreviewUrl(active);
      }
      return;
    }
    if (!cwd) {
      setNotice('项目文件夹尚未可用，不能启动项目动作。');
      return;
    }
    if (!transport) {
      setNotice('控制运行时尚未连接，不能启动项目动作。');
      return;
    }
    const requestGeneration = discoveryGeneration.current;
    setBusy(action);
    setNotice('');
    try {
      const keyId = `${sessionId}:${projectId}:${action}`;
      const idempotencyKey = idempotencyKeys.current.get(keyId) ?? quickActionIdempotencyKey(projectId, action);
      idempotencyKeys.current.set(keyId, idempotencyKey);
      const response = await transport.request<QuickActionResponse>({
        pathId: 'agent.session.backgroundJob.start',
        params: { sessionId },
        timeoutMs: 30000,
        body: {
          action,
          projectId,
          cwd,
          label: projectQuickActionLabel(action),
          idempotencyKey,
          ...(previewUrl && action === 'preview' ? { previewUrl } : {}),
        },
      });
      if (response.ok !== true || !response.job) throw new Error(response.summary || '项目动作没有返回后台任务');
      idempotencyKeys.current.delete(keyId);
      if (requestGeneration !== discoveryGeneration.current) return;
      openJob(response.job);
      if (action === 'preview') {
        const receiptPreview = projectPreviewUrl(response.quickAction?.previewUrl);
        if (receiptPreview) {
          setDiscoveredPreviewUrl(receiptPreview);
          openPreview(receiptPreview);
        } else {
          void discoverPreviewUrl(response.job);
        }
      }
      void refreshJobs();
    } catch (error) {
      setNotice(error instanceof Error && error.message ? error.message : '项目动作启动失败，请查看项目日志。');
    } finally {
      setBusy('');
    }
  };

  const projectItems = items.filter(job => job.cwd === cwd);
  const latest = projectItems.find(job => job.command === projectQuickActionCommand('preview') || job.command === projectQuickActionCommand('checks'));
  const hasActive = projectItems.some(job => isActiveProjectQuickAction(job, 'preview', cwd) || isActiveProjectQuickAction(job, 'checks', cwd));

  return (
    <section aria-label="项目快速动作" className={`project-quick-actions${compact ? ' project-quick-actions--compact' : ''}`}>
      {!compact ? <div className="project-quick-actions__heading">
        <div>
          <small>QUICK ACTIONS</small>
          <strong>直接运行项目</strong>
        </div>
        <span aria-live="polite" data-active={hasActive || undefined}>{hasActive ? '后台任务运行中' : latest ? `最近：${latest.label}` : '使用成果声明的项目脚本'}</span>
      </div> : null}
      <div className="project-quick-actions__buttons">
        <Button aria-label="运行预览" disabled={!cwd || Boolean(busy)} loading={busy === 'preview'} onClick={() => void start('preview')} size="small" variant={compact ? 'secondary' : 'primary'}><Play size={14} />运行预览</Button>
        <Button aria-label="运行检核" disabled={!cwd || Boolean(busy)} loading={busy === 'checks'} onClick={() => void start('checks')} size="small"><ShieldCheck size={14} />运行检核</Button>
        <Button aria-label="日志" disabled={!latest} onClick={() => latest && openJob(latest)} size="small" variant="quiet"><FileTerminal size={14} />日志</Button>
        <Button aria-label="打开预览页面" disabled={!previewUrl} onClick={() => openPreview()} size="small" variant="quiet"><ExternalLink size={14} />打开预览页面</Button>
      </div>
      {busy ? <span className="project-quick-actions__status" role="status"><LoaderCircle className="ui-spin" size={13} />正在提交{busy === 'preview' ? '预览' : '检核'}任务…</span> : null}
      {notice ? <p className="project-quick-actions__notice" role="alert">{notice}</p> : null}
      {!compact && !previewUrl ? <p className="project-quick-actions__hint">项目尚未声明可验证的本机预览地址；运行日志仍可打开查看和停止。</p> : null}
    </section>
  );
}

function quickActionIdempotencyKey(projectId: string, action: ProjectQuickAction): string {
  const suffix = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `pawos-project:${projectId}:${action}:${suffix}`;
}
