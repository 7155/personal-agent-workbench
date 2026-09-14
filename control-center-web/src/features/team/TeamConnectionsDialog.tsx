import {
  CheckCircle2,
  CircleAlert,
  CircleDashed,
  Clock3,
  GitBranch,
  KeyRound,
  Link2,
  LoaderCircle,
  RefreshCw,
  ShieldCheck,
  Unplug,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Field,
  Input,
  TextArea,
} from '@/components/primitives';
import { normalizeGitHubRepository, teamApiErrorMessage } from './team-api';
import { useTeam } from './team-context';
import {
  TEAM_GITHUB_OPERATIONS,
  TEAM_GITHUB_READ_OPERATIONS,
  type TeamConnection,
  type TeamConnectionCreateInput,
  type TeamConnectionGrant,
  type TeamConnectionGrantInput,
  type TeamConnectionList,
  type TeamGitHubOperation,
} from './types';
import './team.css';

type SetupMethod = 'token' | 'oauth';
type Mutation = 'token' | 'oauth' | 'grant' | string;

const OPERATION_LABELS: Record<TeamGitHubOperation, string> = {
  'repo.read': '读取仓库信息',
  'file.read': '读取文件',
  'issues.list': '查看 Issue 列表',
  'issue.read': '读取 Issue',
  'issue.create': '创建 Issue',
  'issue.comment': '评论 Issue',
};

export function TeamConnectionsDialog({ open, onOpenChange, navigate = (url) => window.location.assign(url) }: { open: boolean; onOpenChange(open: boolean): void; navigate?(url: string): void }) {
  const team = useTeam();
  const space = team.activeSpace;
  const spaceId = space?.id ?? '';
  const scopeKey = team.scopeKey ?? '';
  const [connectionList, setConnectionList] = useState<TeamConnectionList | null>(null);
  const [loading, setLoading] = useState(false);
  const [mutation, setMutation] = useState<Mutation | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [setupMethod, setSetupMethod] = useState<SetupMethod>('token');
  const [connectionScope, setConnectionScope] = useState<'personal' | 'project'>('personal');
  const [label, setLabel] = useState('');
  const [repositoriesText, setRepositoriesText] = useState('');
  const [setupOperations, setSetupOperations] = useState<TeamGitHubOperation[]>([...TEAM_GITHUB_READ_OPERATIONS]);
  const [token, setToken] = useState('');
  const [selectedConnectionId, setSelectedConnectionId] = useState('');
  const [grantSessionId, setGrantSessionId] = useState('');
  const [grantRepository, setGrantRepository] = useState('');
  const [grantOperations, setGrantOperations] = useState<TeamGitHubOperation[]>([]);
  const [grantTtlSeconds, setGrantTtlSeconds] = useState('28800');
  const [now, setNow] = useState(Date.now);
  const requestRef = useRef(0);
  const mutationRef = useRef(0);
  const grantConnectionRef = useRef('');
  const callbackResultRef = useRef<{ scopeKey: string; result: 'connected' | 'failed' } | null>(null);
  const viewRef = useRef({ open, spaceId, scopeKey });
  viewRef.current = { open, spaceId, scopeKey };

  const resetForm = useCallback(() => {
    setSetupMethod('token');
    setConnectionScope('personal');
    setLabel('');
    setRepositoriesText('');
    setSetupOperations([...TEAM_GITHUB_READ_OPERATIONS]);
    setToken('');
    setSelectedConnectionId('');
    grantConnectionRef.current = '';
    setGrantSessionId('');
    setGrantRepository('');
    setGrantOperations([]);
    setGrantTtlSeconds('28800');
  }, []);

  const isCurrentView = useCallback((expectedSpaceId: string, expectedScopeKey: string) => (
    viewRef.current.open
    && viewRef.current.spaceId === expectedSpaceId
    && viewRef.current.scopeKey === expectedScopeKey
  ), []);

  const load = useCallback(async () => {
    if (!open || !spaceId || !scopeKey) return;
    const expectedSpaceId = spaceId;
    const expectedScopeKey = scopeKey;
    const requestId = ++requestRef.current;
    setLoading(true);
    setError('');
    try {
      const next = await team.listConnections(expectedSpaceId);
      if (requestId !== requestRef.current || !isCurrentView(expectedSpaceId, expectedScopeKey)) return;
      setConnectionList(next);
      setNotice((current) => current === '读取连接失败，请重试。' ? '' : current);
    } catch (reason) {
      if (requestId !== requestRef.current || !isCurrentView(expectedSpaceId, expectedScopeKey)) return;
      setError(teamApiErrorMessage(reason, '读取外部连接失败，请重试。'));
    } finally {
      if (requestId === requestRef.current && isCurrentView(expectedSpaceId, expectedScopeKey)) setLoading(false);
    }
  }, [isCurrentView, open, scopeKey, spaceId, team.listConnections]);

  useEffect(() => {
    requestRef.current += 1;
    mutationRef.current += 1;
    setConnectionList(null);
    setLoading(false);
    setMutation(null);
    setError('');
    setNotice('');
    resetForm();
    if (open) void load();
    return () => {
      requestRef.current += 1;
      mutationRef.current += 1;
    };
  }, [load, open, resetForm, scopeKey, spaceId]);

  useEffect(() => {
    if (!open) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, [open]);

  useEffect(() => {
    if (!open || !spaceId) {
      callbackResultRef.current = null;
      return;
    }
    const returned = consumeTeamConnectionResult();
    if (returned) callbackResultRef.current = { scopeKey, result: returned };
    const state = callbackResultRef.current?.scopeKey === scopeKey ? callbackResultRef.current.result : null;
    if (state === 'connected') setNotice('已返回 GitHub 授权，请在下方核对连接结果。');
    else if (state === 'failed') setError('GitHub 连接没有完成，请重试。');
  }, [open, scopeKey, spaceId]);

  const activeConnections = useMemo(
    () => connectionList?.items.filter((connection) => connection.status === 'active') ?? [],
    [connectionList],
  );
  const selectedConnection = activeConnections.find((connection) => connection.id === selectedConnectionId) ?? null;
  const canCreateProjectConnection = Boolean(
    connectionList?.canCreateProject
      && space?.kind === 'project'
      && (space.role === 'owner' || space.role === 'maintainer'),
  );
  const ownSessions = useMemo(
    () => connectionList?.sessions.filter((session) => session.ownerUserId === team.user?.id && session.status !== 'archived') ?? [],
    [connectionList, team.user?.id],
  );
  const personalGrantDisclosure = Boolean(selectedConnection?.scope === 'personal' && space?.kind === 'project');

  useEffect(() => {
    if (connectionScope === 'project' && !canCreateProjectConnection) setConnectionScope('personal');
  }, [canCreateProjectConnection, connectionScope]);

  useEffect(() => {
    setSelectedConnectionId((current) => activeConnections.some((connection) => connection.id === current) ? current : activeConnections[0]?.id ?? '');
  }, [activeConnections]);

  useEffect(() => {
    if (!selectedConnection) {
      setGrantRepository('');
      setGrantOperations([]);
      return;
    }
    setGrantRepository((current) => selectedConnection.repositories.includes(current) ? current : selectedConnection.repositories[0] ?? '');
    const changed = grantConnectionRef.current !== selectedConnection.id;
    grantConnectionRef.current = selectedConnection.id;
    setGrantOperations((current) => {
      if (changed) return selectedConnection.operations.filter((operation) => (TEAM_GITHUB_READ_OPERATIONS as readonly TeamGitHubOperation[]).includes(operation));
      const retained = current.filter((operation) => selectedConnection.operations.includes(operation));
      return retained;
    });
    setGrantSessionId((current) => ownSessions.some((session) => session.id === current) ? current : ownSessions[0]?.id ?? '');
  }, [ownSessions, selectedConnection]);

  function buildConnectionInput(): TeamConnectionCreateInput | null {
    const repositories = parseRepositories(repositoriesText);
    if (!label.trim()) {
      setError('请填写连接名称。');
      return null;
    }
    if (!repositories.length) {
      setError('请填写 1–20 个正确的 owner/repo 仓库名称，每行一个。');
      return null;
    }
    if (!setupOperations.length) {
      setError('请至少选择一项操作。');
      return null;
    }
    if (connectionScope === 'project' && !canCreateProjectConnection) {
      setError('当前账号不能创建项目连接。');
      return null;
    }
    return {
      scope: connectionScope,
      label: label.trim(),
      repositories,
      operations: [...setupOperations],
    };
  }

  async function createWithToken(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (mutation || !spaceId) return;
    const input = buildConnectionInput();
    if (!input) return;
    if (!token) {
      setError('请输入 GitHub PAT。');
      return;
    }
    const mutationId = ++mutationRef.current;
    const expectedSpaceId = spaceId;
    const expectedScopeKey = scopeKey;
    requestRef.current += 1;
    setMutation('token');
    setError('');
    setNotice('');
    try {
      const connection = await team.createConnectionWithToken(expectedSpaceId, { ...input, token });
      if (mutationId !== mutationRef.current || !isCurrentView(expectedSpaceId, expectedScopeKey)) return;
      requestRef.current += 1;
      setNotice(`已添加 GitHub 连接「${connection.label}」。`);
      await load();
    } catch (reason) {
      if (mutationId === mutationRef.current && isCurrentView(expectedSpaceId, expectedScopeKey)) {
        requestRef.current += 1;
        setError(teamApiErrorMessage(reason, '添加 GitHub 连接失败，请重试。'));
      }
    } finally {
      if (mutationId === mutationRef.current && isCurrentView(expectedSpaceId, expectedScopeKey)) {
        setToken('');
        setMutation(null);
      }
    }
  }

  async function startOAuth(): Promise<void> {
    if (mutation || !spaceId) return;
    const input = buildConnectionInput();
    if (!input) return;
    const mutationId = ++mutationRef.current;
    const expectedSpaceId = spaceId;
    const expectedScopeKey = scopeKey;
    requestRef.current += 1;
    setMutation('oauth');
    setError('');
    setNotice('');
    try {
      const result = await team.startConnectionOAuth(expectedSpaceId, input);
      if (mutationId !== mutationRef.current || !isCurrentView(expectedSpaceId, expectedScopeKey)) return;
      const safeUrl = validateGitHubAuthorizationUrl(result.authorizationUrl);
      requestRef.current += 1;
      setToken('');
      navigate(safeUrl);
    } catch (reason) {
      if (mutationId === mutationRef.current && isCurrentView(expectedSpaceId, expectedScopeKey)) {
        requestRef.current += 1;
        setError(reason instanceof TypeError ? reason.message : teamApiErrorMessage(reason, '开始 GitHub 授权失败，请重试。'));
      }
    } finally {
      if (mutationId === mutationRef.current && isCurrentView(expectedSpaceId, expectedScopeKey)) setMutation(null);
    }
  }

  async function revokeConnection(connection: TeamConnection): Promise<void> {
    if (mutation || !connection.canManage || !spaceId) return;
    const mutationId = ++mutationRef.current;
    const expectedSpaceId = spaceId;
    const expectedScopeKey = scopeKey;
    requestRef.current += 1;
    setMutation(`revoke-connection:${connection.id}`);
    setError('');
    try {
      await team.revokeConnection(expectedSpaceId, connection.id);
      if (mutationId !== mutationRef.current || !isCurrentView(expectedSpaceId, expectedScopeKey)) return;
      requestRef.current += 1;
      setNotice(`连接「${connection.label}」已撤销。`);
      await load();
    } catch (reason) {
      if (mutationId === mutationRef.current && isCurrentView(expectedSpaceId, expectedScopeKey)) {
        requestRef.current += 1;
        setError(teamApiErrorMessage(reason, '撤销连接失败，请重试。'));
      }
    } finally {
      if (mutationId === mutationRef.current && isCurrentView(expectedSpaceId, expectedScopeKey)) setMutation(null);
    }
  }

  async function createGrant(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (mutation || !spaceId || !selectedConnection) return;
    const ttlSeconds = Number(grantTtlSeconds);
    const operations = grantOperations.filter((operation) => selectedConnection.operations.includes(operation));
    if (!grantSessionId || !ownSessions.some((session) => session.id === grantSessionId)) {
      setError('请选择要授权的自己的任务。');
      return;
    }
    if (!grantRepository || !selectedConnection.repositories.includes(grantRepository)) {
      setError('请选择连接允许的仓库。');
      return;
    }
    if (!operations.length) {
      setError('请至少选择一项任务操作。');
      return;
    }
    if (!Number.isSafeInteger(ttlSeconds) || ttlSeconds < 60 || ttlSeconds > 86_400) {
      setError('授权时长必须在 60 到 86400 秒之间。');
      return;
    }
    const input: TeamConnectionGrantInput = {
      connectionId: selectedConnection.id,
      sessionId: grantSessionId,
      repository: grantRepository,
      operations,
      ttlSeconds,
    };
    const mutationId = ++mutationRef.current;
    const expectedSpaceId = spaceId;
    const expectedScopeKey = scopeKey;
    requestRef.current += 1;
    setMutation('grant');
    setError('');
    setNotice('');
    try {
      const grant = await team.createConnectionGrant(expectedSpaceId, input);
      if (mutationId !== mutationRef.current || !isCurrentView(expectedSpaceId, expectedScopeKey)) return;
      requestRef.current += 1;
      setNotice(`已将「${grant.connectionLabel}」授权给所选任务。`);
      await load();
    } catch (reason) {
      if (mutationId === mutationRef.current && isCurrentView(expectedSpaceId, expectedScopeKey)) {
        requestRef.current += 1;
        setError(teamApiErrorMessage(reason, '授权连接给任务失败，请重试。'));
      }
    } finally {
      if (mutationId === mutationRef.current && isCurrentView(expectedSpaceId, expectedScopeKey)) setMutation(null);
    }
  }

  async function revokeGrant(grant: TeamConnectionGrant): Promise<void> {
    if (mutation || !spaceId || grant.status !== 'active') return;
    const mutationId = ++mutationRef.current;
    const expectedSpaceId = spaceId;
    const expectedScopeKey = scopeKey;
    requestRef.current += 1;
    setMutation(`revoke-grant:${grant.id}`);
    setError('');
    try {
      await team.revokeConnectionGrant(expectedSpaceId, grant.id);
      if (mutationId !== mutationRef.current || !isCurrentView(expectedSpaceId, expectedScopeKey)) return;
      requestRef.current += 1;
      setNotice('任务授权已撤销。');
      await load();
    } catch (reason) {
      if (mutationId === mutationRef.current && isCurrentView(expectedSpaceId, expectedScopeKey)) {
        requestRef.current += 1;
        setError(teamApiErrorMessage(reason, '撤销任务授权失败，请重试。'));
      }
    } finally {
      if (mutationId === mutationRef.current && isCurrentView(expectedSpaceId, expectedScopeKey)) setMutation(null);
    }
  }

  function handleOpenChange(nextOpen: boolean): void {
    if (!nextOpen) {
      requestRef.current += 1;
      mutationRef.current += 1;
      setToken('');
      setError('');
      setNotice('');
      resetForm();
      callbackResultRef.current = null;
    }
    onOpenChange(nextOpen);
  }

  if (!space) return null;

  return (
    <Dialog onOpenChange={handleOpenChange} open={open}>
      <DialogContent className="team-connections-dialog">
        <DialogHeader>
          <DialogTitle>连接 GitHub</DialogTitle>
          <DialogDescription>
            当前空间：{space.name}。连接只开放你明确选择的仓库和操作；授权还需要指定自己的任务和有效期。
          </DialogDescription>
        </DialogHeader>
        <div className="team-connections-dialog__body">
          {error ? <p className="team-connections-dialog__error" role="alert"><CircleAlert aria-hidden="true" size={15} />{error}</p> : null}
          {error && !connectionList ? <Button disabled={loading} onClick={() => void load()} variant="quiet">重试</Button> : null}
          {notice ? <p className="team-connections-dialog__notice" role="status"><CheckCircle2 aria-hidden="true" size={15} />{notice}</p> : null}
          {!connectionList && loading ? <p className="team-connections-dialog__loading" role="status"><LoaderCircle className="ui-spin" size={15} />正在读取连接…</p> : null}
          {connectionList && !connectionList.configured ? (
            <div className="team-connections-dialog__empty team-connections-dialog__empty--service">
              <Unplug aria-hidden="true" size={18} />
              <div><strong>外部连接服务尚未配置</strong><p>当前团队还不能设置 GitHub 连接，请联系管理员配置后再试。</p></div>
            </div>
          ) : null}

          {connectionList?.configured ? (
            <>
              <section aria-labelledby="team-connections-list-title" className="team-connections-dialog__section">
                <div className="team-connections-dialog__section-heading">
                  <div><h2 id="team-connections-list-title">当前连接</h2><p>个人连接归你管理；项目连接归项目管理者管理。</p></div>
                  <Button disabled={Boolean(mutation) || loading} leadingIcon={<RefreshCw size={14} />} onClick={() => void load()} size="small" variant="quiet">刷新</Button>
                </div>
                {connectionList.items.length ? (
                  <div className="team-connections-dialog__connection-list" aria-label="当前 GitHub 连接列表">
                    {connectionList.items.map((connection) => <ConnectionCard key={connection.id} connection={connection} projectName={space.name} onRevoke={() => void revokeConnection(connection)} busy={mutation === `revoke-connection:${connection.id}`} />)}
                  </div>
                ) : (
                  <div className="team-connections-dialog__empty"><CircleDashed aria-hidden="true" size={17} /><span>还没有 GitHub 连接。先创建一个只包含必要仓库和操作的连接。</span></div>
                )}
              </section>

              <section aria-labelledby="team-connections-setup-title" className="team-connections-dialog__section">
                <div className="team-connections-dialog__section-heading"><div><h2 id="team-connections-setup-title">添加连接</h2><p>GitHub 只会收到当前授权方式所需的信息。</p></div></div>
                <div className="team-connections-dialog__method-tabs" role="group" aria-label="连接方式">
                  <button className={setupMethod === 'token' ? 'is-selected' : undefined} onClick={() => setSetupMethod('token')} type="button"><KeyRound aria-hidden="true" size={14} />使用 PAT</button>
                  <button className={setupMethod === 'oauth' ? 'is-selected' : undefined} disabled={!connectionList.oauthAvailable} onClick={() => setSetupMethod('oauth')} type="button"><Link2 aria-hidden="true" size={14} />使用 GitHub 授权</button>
                </div>
                {!connectionList.oauthAvailable ? <p className="team-connections-dialog__field-note">GitHub 授权暂未配置，可以使用 PAT。</p> : null}
                <div className="team-connections-dialog__form-grid">
                  <Field htmlFor="team-connection-label" label="连接名称" required><Input id="team-connection-label" maxLength={160} onChange={(event) => setLabel(event.target.value)} placeholder="例如：官网代码只读" value={label} /></Field>
                  <Field htmlFor="team-connection-scope" label="连接归属" required>
                    <select className="paw-select" id="team-connection-scope" onChange={(event) => setConnectionScope(event.target.value as 'personal' | 'project')} value={connectionScope}>
                      <option value="personal">个人连接（仅你可管理）</option>
                      <option disabled={!canCreateProjectConnection} value="project">项目连接（项目成员可按授权使用）</option>
                    </select>
                  </Field>
                  <Field className="team-connections-dialog__field--wide" description="每行一个，例如 octo-org/website；提交时会统一为小写。" htmlFor="team-connection-repositories" label="允许仓库" required>
                    <TextArea id="team-connection-repositories" onChange={(event) => setRepositoriesText(event.target.value)} placeholder="octo-org/website\nocto-org/docs" rows={3} value={repositoriesText} />
                  </Field>
                </div>
                <OperationChecklist label="连接允许的操作" operations={setupOperations} onChange={setSetupOperations} />
                {setupMethod === 'token' ? (
                  <form className="team-connections-dialog__token-form" onSubmit={(event) => void createWithToken(event)}>
                    <Field description="只用于本次请求，成功或失败后都会清除。" htmlFor="team-github-pat" label="GitHub PAT" required><Input autoComplete="off" id="team-github-pat" name="github-pat" onChange={(event) => setToken(event.target.value)} type="password" value={token} /></Field>
                    <Button disabled={Boolean(mutation)} loading={mutation === 'token'} leadingIcon={<GitBranch size={14} />} type="submit" variant="primary">保存 PAT 连接</Button>
                  </form>
                ) : (
                  <div className="team-connections-dialog__oauth-action">
                    <Button disabled={Boolean(mutation) || !connectionList.oauthAvailable} loading={mutation === 'oauth'} leadingIcon={<GitBranch size={14} />} onClick={() => void startOAuth()} variant="primary">前往 GitHub 授权</Button>
                  </div>
                )}
              </section>

              <section aria-labelledby="team-connections-grant-title" className="team-connections-dialog__section">
                <div className="team-connections-dialog__section-heading"><div><h2 id="team-connections-grant-title">授权给我的任务</h2><p>选择你的任务、一个仓库和允许的操作。</p></div></div>
                {activeConnections.length && ownSessions.length ? (
                  <form className="team-connections-dialog__grant-form" onSubmit={(event) => void createGrant(event)}>
                    <div className="team-connections-dialog__form-grid">
                      <Field htmlFor="team-grant-connection" label="连接" required><select className="paw-select" id="team-grant-connection" onChange={(event) => setSelectedConnectionId(event.target.value)} value={selectedConnectionId}>{activeConnections.map((connection) => <option key={connection.id} value={connection.id}>{connection.label} · {connection.accountLogin}</option>)}</select></Field>
                      <Field htmlFor="team-grant-session" label="我的任务" required><select className="paw-select" id="team-grant-session" onChange={(event) => setGrantSessionId(event.target.value)} value={grantSessionId}>{ownSessions.map((session) => <option key={session.id} value={session.id}>{session.title} · {sessionStatusLabel(session.status)}</option>)}</select></Field>
                      <Field htmlFor="team-grant-repository" label="仓库" required><select className="paw-select" id="team-grant-repository" onChange={(event) => setGrantRepository(event.target.value)} value={grantRepository}>{selectedConnection?.repositories.map((repository) => <option key={repository} value={repository}>{repository}</option>)}</select></Field>
                      <Field htmlFor="team-grant-ttl" label="授权有效期" required><select className="paw-select" id="team-grant-ttl" onChange={(event) => setGrantTtlSeconds(event.target.value)} value={grantTtlSeconds}><option value="900">15 分钟</option><option value="3600">1 小时</option><option value="28800">8 小时</option><option value="86400">24 小时</option></select></Field>
                    </div>
                    <OperationChecklist label="这项任务允许的操作" operations={grantOperations} availableOperations={selectedConnection?.operations ?? []} onChange={setGrantOperations} />
                    {personalGrantDisclosure ? <p className="team-connections-dialog__disclosure" role="note"><ShieldCheck aria-hidden="true" size={15} />这是个人连接；返回结果会进入当前项目会话，项目成员可见。请选择适合在项目中共享的资料。</p> : null}
                    <DialogFooter><Button disabled={Boolean(mutation) || !selectedConnection} loading={mutation === 'grant'} leadingIcon={<ShieldCheck size={14} />} type="submit" variant="primary">授权给我的任务</Button></DialogFooter>
                  </form>
                ) : (
                  <div className="team-connections-dialog__empty"><ShieldCheck aria-hidden="true" size={17} /><span>{activeConnections.length ? '当前账号没有可授权的自己的任务。先创建或打开自己的任务。' : '先创建一个有效连接，再把它授权给自己的任务。'}</span></div>
                )}
              </section>

              <section aria-labelledby="team-connections-grants-title" className="team-connections-dialog__section">
                <div className="team-connections-dialog__section-heading"><div><h2 id="team-connections-grants-title">我的任务授权</h2><p>这里只显示当前账号可见的授权，可以随时撤销。</p></div></div>
                {connectionList.grants.length ? <div className="team-connections-dialog__grant-list" aria-label="我的任务授权列表">{connectionList.grants.map((grant) => <GrantCard grant={grant} now={now} taskTitle={ownSessions.find((task) => task.id === grant.sessionId)?.title ?? grant.sessionId} key={grant.id} onRevoke={() => void revokeGrant(grant)} busy={mutation === `revoke-grant:${grant.id}`} />)}</div> : <div className="team-connections-dialog__empty"><Clock3 aria-hidden="true" size={17} /><span>还没有任务授权。</span></div>}
              </section>
            </>
          ) : null}
        </div>
        <DialogFooter><Button disabled={Boolean(mutation)} onClick={() => handleOpenChange(false)} variant="quiet">完成</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ConnectionCard({ connection, projectName, onRevoke, busy }: { connection: TeamConnection; projectName: string; onRevoke(): void; busy: boolean }) {
  return (
    <article className="team-connections-dialog__connection">
      <div className="team-connections-dialog__connection-heading"><div><strong>{connection.label}</strong><span>{connection.accountLogin} · {connection.scope === 'personal' ? '个人连接 · 你的账号' : `项目连接 · ${projectName}`}</span></div><span className={`team-connections-dialog__status team-connections-dialog__status--${connection.status}`}>{connectionStatusLabel(connection.status)}</span></div>
      <div className="team-connections-dialog__connection-details"><span><b>仓库</b>{connection.repositories.join('、')}</span><span><b>连接范围</b>{connection.operations.map(operationLabel).join('、')}</span></div>
      {connection.canManage && connection.status !== 'revoked' ? <Button disabled={busy} loading={busy} leadingIcon={<Unplug size={13} />} onClick={onRevoke} size="small" variant="danger">撤销连接</Button> : null}
    </article>
  );
}

function GrantCard({ grant, taskTitle, now, onRevoke, busy }: { grant: TeamConnectionGrant; taskTitle: string; now: number; onRevoke(): void; busy: boolean }) {
  const expired = grant.status === 'expired' || (grant.status === 'active' && grant.expiresAtMs <= now);
  const status = expired ? 'expired' : grant.status;
  return (
    <article className="team-connections-dialog__grant">
      <div className="team-connections-dialog__grant-heading"><div><strong>{grant.connectionLabel}</strong><span>{grant.accountLogin} · {grant.repository}</span></div><span className={`team-connections-dialog__status team-connections-dialog__status--${status}`}>{grantStatusLabel(status)}</span></div>
      <div className="team-connections-dialog__grant-details"><span>任务：{taskTitle}</span><span>{grant.operations.map(operationLabel).join('、')}</span><span>到期：{formatDateTime(grant.expiresAtMs)}</span></div>
      {grant.status === 'active' && !expired ? <Button disabled={busy} loading={busy} leadingIcon={<Unplug size={13} />} onClick={onRevoke} size="small" variant="quiet">撤销授权</Button> : null}
    </article>
  );
}

function OperationChecklist({ label, operations, availableOperations = [...TEAM_GITHUB_OPERATIONS], onChange }: { label: string; operations: TeamGitHubOperation[]; availableOperations?: readonly TeamGitHubOperation[]; onChange: (operations: TeamGitHubOperation[]) => void }) {
  return (
    <fieldset className="team-connections-dialog__operations">
      <legend>{label}</legend>
      <div>{availableOperations.map((operation) => <label key={operation}><input checked={operations.includes(operation)} onChange={(event) => onChange(event.target.checked ? [...new Set([...operations, operation])] : operations.filter((item) => item !== operation))} type="checkbox" />{OPERATION_LABELS[operation]}</label>)}</div>
    </fieldset>
  );
}

function parseRepositories(value: string): string[] {
  const pieces = value.split(/[\n,]/u).map((item) => item.trim()).filter(Boolean);
  const normalized = pieces.map(normalizeGitHubRepository);
  return pieces.length > 20 || normalized.some((item) => item === null) ? [] : [...new Set(normalized as string[])];
}

function validateGitHubAuthorizationUrl(value: string): string {
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    throw new TypeError('GitHub 授权地址无效，已阻止打开。');
  }
  if (parsed.protocol !== 'https:' || parsed.hostname !== 'github.com' || parsed.port || parsed.username || parsed.password || parsed.hash || parsed.pathname !== '/login/oauth/authorize') {
    throw new TypeError('GitHub 授权地址不安全，已阻止打开。');
  }
  return parsed.toString();
}

function consumeTeamConnectionResult(): 'connected' | 'failed' | null {
  if (typeof window === 'undefined') return null;
  const url = new URL(window.location.href);
  const state = url.searchParams.get('teamConnection');
  if (state !== 'connected' && state !== 'failed') return null;
  url.searchParams.delete('teamConnection');
  window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}${url.hash}` || '/');
  return state;
}

function operationLabel(operation: TeamGitHubOperation): string {
  return OPERATION_LABELS[operation] ?? operation;
}

function connectionStatusLabel(status: TeamConnection['status']): string {
  if (status === 'active') return '已连接';
  if (status === 'reconnect_required') return '需要重新连接';
  return '已撤销';
}

function grantStatusLabel(status: 'active' | 'revoked' | 'expired'): string {
  if (status === 'active') return '授权中';
  if (status === 'expired') return '已过期';
  return '已撤销';
}

function sessionStatusLabel(status: string): string {
  if (status === 'running') return '运行中';
  if (status === 'idle') return '空闲';
  if (status === 'archived') return '已归档';
  return status;
}

function formatDateTime(timestamp: number): string {
  try {
    return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(timestamp);
  } catch {
    return '未知时间';
  }
}
