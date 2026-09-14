import {
  CircleAlert,
  CircleDashed,
  CircleStop,
  ExternalLink,
  GitBranch,
  LoaderCircle,
  RefreshCw,
  ShieldCheck,
} from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { teamApiErrorMessage } from './team-api';
import { useTeam } from './team-context';
import type { TeamProjectDeployment, TeamProjectPreview as TeamProjectPreviewState } from './types';

type PreviewAction = 'start' | 'stop' | null;

export function TeamProjectPreviewCard() {
  const team = useTeam();
  const project = team.activeSpace?.kind === 'project' ? team.activeSpace : null;
  const projectId = project?.id ?? '';
  const userId = team.user?.id ?? '';
  const scopeKey = `${userId}:${projectId}`;
  const [preview, setPreview] = useState<TeamProjectPreviewState | null>(null);
  const [loading, setLoading] = useState(false);
  const [action, setAction] = useState<PreviewAction>(null);
  const [error, setError] = useState('');
  const requestRef = useRef(0);
  const actionRef = useRef<PreviewAction>(null);
  const mutationRef = useRef(0);
  const currentScopeRef = useRef(scopeKey);
  const inFlightRef = useRef<{ scopeKey: string; promise: Promise<void> } | null>(null);
  currentScopeRef.current = scopeKey;

  const load = useCallback(async ({ background = false }: { background?: boolean } = {}) => {
    if (!projectId || !userId) return;
    const existing = inFlightRef.current;
    if (existing?.scopeKey === scopeKey) {
      if (background) return;
      await existing.promise;
      if (currentScopeRef.current !== scopeKey) return;
    }

    const requestId = ++requestRef.current;
    if (!background) {
      setLoading(true);
      setError('');
    }
    const pending = (async () => {
      try {
        const next = await team.getProjectPreview(projectId);
        if (requestId !== requestRef.current || currentScopeRef.current !== scopeKey) return;
        setPreview(next);
        setError('');
      } catch (reason) {
        if (requestId !== requestRef.current || currentScopeRef.current !== scopeKey) return;
        setError(teamApiErrorMessage(reason, '读取共享预览状态失败，请重试。'));
      } finally {
        if (requestId === requestRef.current && !background) setLoading(false);
      }
    })();
    inFlightRef.current = { scopeKey, promise: pending };
    try {
      await pending;
    } finally {
      if (inFlightRef.current?.promise === pending) inFlightRef.current = null;
    }
  }, [projectId, scopeKey, team.getProjectPreview, userId]);

  useEffect(() => {
    currentScopeRef.current = scopeKey;
    requestRef.current += 1;
    mutationRef.current += 1;
    setPreview(null);
    setError('');
    setAction(null);
    actionRef.current = null;
    setLoading(false);
    if (projectId && userId && !document.hidden) void load();
    return () => {
      requestRef.current += 1;
      mutationRef.current += 1;
      if (currentScopeRef.current === scopeKey) currentScopeRef.current = '';
    };
  }, [load, projectId, userId]);

  useEffect(() => {
    if (!projectId || !userId) return;
    const syncWhenVisible = () => {
      if (!document.hidden) void load({ background: true });
    };
    const interval = window.setInterval(syncWhenVisible, 5_000);
    window.addEventListener('focus', syncWhenVisible);
    document.addEventListener('visibilitychange', syncWhenVisible);
    return () => {
      window.clearInterval(interval);
      window.removeEventListener('focus', syncWhenVisible);
      document.removeEventListener('visibilitychange', syncWhenVisible);
    };
  }, [load, projectId, userId]);

  const canManage = Boolean(
    preview?.canManage
      && (project?.role === 'owner' || project?.role === 'maintainer'),
  );
  const active = preview?.active ?? null;
  const latest = preview?.latest ?? null;
  // Only the server-designated active deployment can receive the browser
  // handoff. A retained/latest row is metadata until the server promotes it.
  const openDeployment = active && isOpenable(active) ? active : null;
  const outdated = Boolean(
    openDeployment
      && preview
      && openDeployment.requirementsRevision !== preview.currentRequirementsRevision,
  );
  const commitDrifted = Boolean(
    openDeployment
      && preview?.repositoryHead
      && openDeployment.commit !== preview.repositoryHead,
  );
  const stoppable = latest?.status === 'recovery_required'
    ? latest
    : active && isStoppable(active)
      ? active
      : latest && isStoppable(latest)
        ? latest
        : null;

  const runStart = useCallback(async () => {
    if (!canManage || !projectId || action || actionRef.current) return;
    // A background status read may have started just before this mutation.
    // Its result must not roll the card back after the POST has returned.
    requestRef.current += 1;
    const mutationId = ++mutationRef.current;
    actionRef.current = 'start';
    setAction('start');
    setError('');
    try {
      const next = await team.startProjectPreview(projectId, createClientRequestId());
      if (mutationId === mutationRef.current && currentScopeRef.current === scopeKey) {
        // Invalidate any status read that may have started while the POST was
        // waiting, then apply the server-confirmed mutation result.
        requestRef.current += 1;
        setPreview(next);
      }
    } catch (reason) {
      if (mutationId === mutationRef.current && currentScopeRef.current === scopeKey) {
        requestRef.current += 1;
        setError(teamApiErrorMessage(reason, '生成共享预览失败，请重试。'));
      }
    } finally {
      if (mutationId === mutationRef.current && currentScopeRef.current === scopeKey) {
        actionRef.current = null;
        setAction(null);
      }
    }
  }, [action, canManage, projectId, scopeKey, team.startProjectPreview]);

  const runStop = useCallback(async () => {
    if (!canManage || !projectId || action || actionRef.current) return;
    requestRef.current += 1;
    const mutationId = ++mutationRef.current;
    actionRef.current = 'stop';
    setAction('stop');
    setError('');
    try {
      const next = await team.stopProjectPreview(projectId);
      if (mutationId === mutationRef.current && currentScopeRef.current === scopeKey) {
        requestRef.current += 1;
        setPreview(next);
      }
    } catch (reason) {
      if (mutationId === mutationRef.current && currentScopeRef.current === scopeKey) {
        requestRef.current += 1;
        setError(teamApiErrorMessage(reason, '停止共享预览失败，请重试。'));
      }
    } finally {
      if (mutationId === mutationRef.current && currentScopeRef.current === scopeKey) {
        actionRef.current = null;
        setAction(null);
      }
    }
  }, [action, canManage, projectId, scopeKey, team.stopProjectPreview]);

  const startLabel = latest?.status === 'starting'
    ? '正在生成预览…'
    : latest?.status === 'ready' || latest?.status === 'retained'
      ? '生成新预览'
      : '生成共享预览';

  if (!project) return null;

  return (
    <section aria-labelledby="team-project-preview-title" className="team-project-workbench__preview">
      <header className="team-project-workbench__section-header">
        <div>
          <h2 id="team-project-preview-title">共享预览</h2>
          <p>固定版本供项目成员查看</p>
        </div>
        <div className="team-project-workbench__section-actions">
          {preview ? <span className="team-project-workbench__version">需求 v{preview.currentRequirementsRevision}</span> : null}
          <button
            aria-label="刷新共享预览状态"
            className="team-project-workbench__quiet-action"
            disabled={loading || Boolean(action)}
            onClick={() => void load()}
            type="button"
          >
            <RefreshCw aria-hidden="true" className={loading ? 'ui-spin' : undefined} size={14} />
            刷新
          </button>
        </div>
      </header>

      {error ? (
        <div className="team-project-workbench__preview-error" role="alert">
          <CircleAlert aria-hidden="true" size={15} />
          <span>{error}</span>
        </div>
      ) : null}

      {loading && !preview ? (
        <div aria-label="正在读取共享预览状态" className="team-project-workbench__preview-loading">
          <LoaderCircle aria-hidden="true" className="ui-spin" size={16} />
          <span>正在读取预览状态…</span>
        </div>
      ) : preview ? (
        <>
          <PreviewTruth preview={preview} />
          {openDeployment ? (
            <DeploymentCard
              deployment={openDeployment}
              kind="active"
              outdated={outdated}
              commitDrifted={commitDrifted}
              openPath={preview.openPath}
            />
          ) : null}
          {latest && latest.id !== openDeployment?.id ? (
            <DeploymentCard deployment={latest} kind="latest" outdated={false} commitDrifted={false} />
          ) : null}
          {!active && !latest ? (
            <div className="team-project-workbench__preview-empty">
              <CircleDashed aria-hidden="true" size={16} />
              <span>还没有共享预览。维护者可以生成一个固定版本。</span>
            </div>
          ) : null}
          {canManage ? (
            <div className="team-project-workbench__preview-actions">
              <button
                className="team-project-workbench__quiet-action"
                disabled={Boolean(action) || latest?.status === 'starting' || !preview.configured}
                onClick={() => void runStart()}
                type="button"
              >
                {action === 'start' ? <LoaderCircle aria-hidden="true" className="ui-spin" size={14} /> : <ShieldCheck aria-hidden="true" size={14} />}
                {action === 'start' ? '正在请求…' : startLabel}
              </button>
              {stoppable ? (
                <button
                  className="team-project-workbench__quiet-action team-project-workbench__quiet-action--danger"
                  disabled={Boolean(action)}
                  onClick={() => void runStop()}
                  type="button"
                >
                  {action === 'stop' ? <LoaderCircle aria-hidden="true" className="ui-spin" size={14} /> : <CircleStop aria-hidden="true" size={14} />}
                  {action === 'stop' ? '正在停止…' : stoppable.status === 'recovery_required' ? '重试清理' : '停止预览'}
                </button>
              ) : null}
            </div>
          ) : null}
          <p className="team-project-workbench__preview-note">
            页面可见时每 5 秒同步状态；后台不会发起生成或停止操作。
          </p>
        </>
      ) : null}
    </section>
  );
}

/** Named alias for callers that treat the file component as the project preview surface. */
export const TeamProjectPreview = TeamProjectPreviewCard;

function PreviewTruth({ preview }: { preview: TeamProjectPreviewState }) {
  if (!preview.configured) {
    return (
      <div className="team-project-workbench__preview-truth is-warning">
        <CircleAlert aria-hidden="true" size={16} />
        <div><strong>执行服务尚未配置</strong><small>团队还不能生成共享预览，请联系管理员配置执行服务。</small></div>
      </div>
    );
  }
  if (preview.latest?.status === 'starting') {
    return (
      <div className="team-project-workbench__preview-truth is-progress">
        <LoaderCircle aria-hidden="true" className="ui-spin" size={16} />
        <div><strong>正在准备固定预览</strong><small>当前可用版本会继续保留，新的结果确认就绪后才会替换它。</small></div>
      </div>
    );
  }
  if (preview.latest?.status === 'failed') {
    return (
      <div className="team-project-workbench__preview-truth is-danger">
        <CircleAlert aria-hidden="true" size={16} />
        <div><strong>最新预览生成失败</strong><small>{preview.latest.error || '服务没有返回具体原因，请刷新状态或联系管理员。'}{preview.active ? ' 当前可用版本仍保留。' : ''}</small></div>
      </div>
    );
  }
  if (preview.latest?.status === 'recovery_required') {
    return (
      <div className="team-project-workbench__preview-truth is-danger">
        <CircleAlert aria-hidden="true" size={16} />
        <div><strong>最新预览需要恢复</strong><small>这个请求不能视为可用，请联系管理员处理恢复。{preview.active ? ' 当前可用版本仍保留。' : ' 当前没有可用预览。'}</small></div>
      </div>
    );
  }
  return null;
}

function DeploymentCard({
  deployment,
  kind,
  outdated,
  commitDrifted,
  openPath,
}: {
  deployment: TeamProjectDeployment;
  kind: 'active' | 'latest';
  outdated: boolean;
  commitDrifted: boolean;
  openPath?: string | null;
}) {
  const statusCopy = deploymentStatusCopy(deployment.status);
  return (
    <article className={`team-project-workbench__preview-deployment team-project-workbench__preview-deployment--${kind}`}>
      <div className="team-project-workbench__preview-deployment-heading">
        <div>
          <h3>{kind === 'active' ? '当前可用版本' : '最新请求'}</h3>
          <p data-status={deployment.status} className="team-project-workbench__preview-status">{statusCopy}</p>
        </div>
        {kind === 'active' && openPath && isOpenable(deployment) ? (
          <a
            className="team-project-workbench__preview-open"
            href={openPath}
            rel="noopener noreferrer"
            target="_blank"
          >
            <ExternalLink aria-hidden="true" size={14} />
            打开预览
          </a>
        ) : null}
      </div>
      <dl className="team-project-workbench__preview-facts">
        <div><dt>分支</dt><dd><GitBranch aria-hidden="true" size={12} />{deployment.branch}</dd></div>
        <div><dt>固定提交</dt><dd><code title={deployment.commit}>{shortCommit(deployment.commit)}</code></dd></div>
        <div><dt>需求基线</dt><dd>v{deployment.requirementsRevision}</dd></div>
        <div><dt>发起人</dt><dd>{deployment.requestedByDisplayName}</dd></div>
      </dl>
      {outdated ? <p className="team-project-workbench__preview-outdated"><CircleAlert aria-hidden="true" size={13} />需求已更新，当前预览仍基于 v{deployment.requirementsRevision}，未证明满足最新版本。</p> : null}
      {commitDrifted ? <p className="team-project-workbench__preview-drift"><CircleAlert aria-hidden="true" size={13} />固定提交与当前稳定分支 HEAD 不同。</p> : null}
      {deployment.status === 'stopped' ? <p className="team-project-workbench__preview-muted">此版本已停止，不能作为当前可用预览。</p> : null}
    </article>
  );
}

function deploymentStatusCopy(status: TeamProjectDeployment['status']): string {
  switch (status) {
    case 'starting': return '生成中';
    case 'ready': return '已就绪';
    case 'retained': return '已保留';
    case 'failed': return '生成失败';
    case 'stopped': return '已停止';
    case 'recovery_required': return '需要恢复';
  }
}

function isOpenable(deployment: TeamProjectDeployment): boolean {
  return deployment.status === 'ready' || deployment.status === 'retained';
}

function isStoppable(deployment: TeamProjectDeployment): boolean {
  return deployment.status === 'starting'
    || deployment.status === 'ready'
    || deployment.status === 'retained'
    || deployment.status === 'recovery_required';
}

function shortCommit(commit: string): string {
  return commit.length > 12 ? `${commit.slice(0, 8)}…${commit.slice(-4)}` : commit;
}

function createClientRequestId(): string {
  const randomUUID = globalThis.crypto?.randomUUID;
  if (randomUUID) return randomUUID.call(globalThis.crypto);
  return `preview-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}
