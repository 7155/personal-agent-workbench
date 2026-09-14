import { LoaderCircle, RefreshCw, ShieldCheck } from 'lucide-react';
import { useState, type FormEvent, type ReactNode } from 'react';
import { Button, Field, Input } from '@/components/primitives';
import { TeamScopedProviders, TeamProvider, useTeam } from './team-context';
import './team.css';

export function TeamGateway({ children }: { children: (scopeKey: string) => ReactNode }) {
  return (
    <TeamProvider>
      <TeamGatewayState>{children}</TeamGatewayState>
    </TeamProvider>
  );
}

function TeamGatewayState({ children }: { children: (scopeKey: string) => ReactNode }) {
  const team = useTeam();

  if (team.phase === 'checking') {
    return team.busy === 'logout'
      ? <TeamStatusCard label="正在退出登录" detail="正在结束本次浏览器登录。" />
      : <TeamStatusCard label="正在检查团队服务" detail="确认当前部署和登录状态。" />;
  }
  if (team.phase === 'error') {
    return (
      <TeamStatusCard
        action={<Button leadingIcon={<RefreshCw size={15} />} onClick={() => void team.retry()} variant="primary">重试连接</Button>}
        detail={team.error ?? '团队服务暂时不可用，请稍后重试。'}
        label="团队服务暂时不可用"
      />
    );
  }
  if (team.phase === 'anonymous') return <TeamLoginScreen />;
  if (!team.user || !team.activeSpace || !team.scopeKey) {
    return <TeamStatusCard detail="当前账号没有可用的个人或项目空间。" label="没有可用工作空间" />;
  }

  return <TeamScopedProviders key={team.scopeKey}>{children(team.scopeKey)}</TeamScopedProviders>;
}

export function TeamLoginScreen() {
  const team = useTeam();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [validationError, setValidationError] = useState<string | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const normalizedUsername = username.trim();
    if (!normalizedUsername || password.length < 8) {
      setValidationError('请输入账号和至少 8 位密码。');
      return;
    }
    setValidationError(null);
    try {
      await team.login(normalizedUsername, password);
    } catch {
      // The context keeps the actionable server error visible in the form.
    }
  }

  return (
    <main className="team-auth" data-testid="team-login-screen">
      <section className="team-auth__card" aria-labelledby="team-login-title">
        <div className="team-auth__eyebrow"><ShieldCheck size={16} aria-hidden="true" /> PAW TEAM</div>
        <h1 id="team-login-title">进入共享工作台</h1>
        <p className="team-auth__lede">登录后选择个人空间或项目空间，继续已有 Session 和 Room。</p>
        <form className="team-auth__form" onSubmit={(event) => void submit(event)}>
          <Field htmlFor="team-username" label="账号" required>
            <Input
              autoComplete="username"
              autoFocus
              id="team-username"
              onChange={(event) => setUsername(event.target.value)}
              placeholder="输入账号"
              value={username}
            />
          </Field>
          <Field htmlFor="team-password" label="密码" required>
            <Input
              autoComplete="current-password"
              id="team-password"
              onChange={(event) => setPassword(event.target.value)}
              placeholder="输入密码"
              type="password"
              value={password}
            />
          </Field>
          {validationError || team.error ? (
            <p className="team-auth__error" role="alert">{validationError ?? team.error}</p>
          ) : null}
          <Button loading={team.busy === 'login'} size="large" type="submit" variant="primary">
            登录 PAW
          </Button>
        </form>
        <p className="team-auth__note">个人空间仅自己可见；项目空间与成员共享。</p>
      </section>
    </main>
  );
}

function TeamStatusCard({ action, detail, label }: { action?: ReactNode; detail: string; label: string }) {
  return (
    <main className="team-auth" data-testid="team-status-card">
      <section className="team-auth__card team-auth__card--status" aria-live="polite">
        <LoaderCircle className="team-auth__spinner" size={22} aria-hidden="true" />
        <h1>{label}</h1>
        <p className="team-auth__lede">{detail}</p>
        {action ? <div className="team-auth__actions">{action}</div> : null}
      </section>
    </main>
  );
}
