import { RefreshCw } from 'lucide-react';
import { useEffect, useState } from 'react';
import type { CapabilityFilter } from './capability-display';

type Exposure = 'direct' | 'codemode' | 'deferred' | 'hidden';
type ServerState = 'starting' | 'disabled' | 'connecting' | 'connected' | 'disconnected' | 'needs-auth' | 'failed' | 'closed';
interface McpServer { name: string; namespace: string; scope: string; state: ServerState; exposure: Exposure; toolCount: number; resourceCount: number; resourceTemplateCount: number }
interface McpTool { name: string; namespace: { name: string }; description: string; exposure: Exposure; active: boolean; routable: boolean; parameters: unknown }
interface NativeCapabilities { sessionId: string; codemodeMode: 'on' | 'only' | 'off' | null; mcp: { available: true; active: boolean; configErrorCount: number; servers: McpServer[] }; tools: McpTool[] }
const states: Record<ServerState, string> = { starting: '正在启动', connecting: '正在连接', connected: '已连接', disconnected: '连接已断开', 'needs-auth': '需要登录', failed: '连接失败', closed: '已关闭', disabled: '已停用' };
const exposures: Record<Exposure, string> = { direct: '直接调用', codemode: '代码编排', deferred: '搜索后调用', hidden: '已隐藏' };
const scopes: Record<string, string> = { global: '全局设置', project: '项目设置', extension: '扩展注册', config: 'Pi 配置' };

export function parseNativeCapabilities(value: unknown, sessionId: string): NativeCapabilities {
  const root = value as { nativeCapabilities?: NativeCapabilities & { schemaVersion?: string } } | null;
  const data = root?.nativeCapabilities;
  if (!data || data.schemaVersion !== 'rag-ime.pi-native-capabilities.v1' || data.sessionId !== sessionId
    || data.mcp?.available !== true || !Array.isArray(data.mcp.servers) || !Array.isArray(data.tools)
    || typeof data.mcp.active !== 'boolean' || !Number.isInteger(data.mcp.configErrorCount) || data.mcp.configErrorCount < 0
    || data.mcp.servers.some(server => !server || !(server.state in states) || !(server.exposure in exposures) || !server.name || !server.namespace
      || [server.toolCount, server.resourceCount, server.resourceTemplateCount].some(count => !Number.isInteger(count) || count < 0))
    || data.tools.some(tool => !tool || !(tool.exposure in exposures) || !tool.name || !tool.namespace?.name || typeof tool.active !== 'boolean' || typeof tool.routable !== 'boolean')) {
    throw new Error('当前 Pi 尚未提供可核对的 MCP 状态。');
  }
  return data;
}

/** Read the existing Pi command/catalog owner; actions never execute a model prompt. */
export function NativeMcpPanel({ sessionId, query, filter, locked, load, invoke }: {
  sessionId: string; query: string; filter: CapabilityFilter; locked: boolean;
  load: (sessionId: string) => Promise<unknown>;
  invoke: (sessionId: string, command: string) => Promise<unknown>;
}) {
  const [state, setState] = useState<{ owner: string; data?: NativeCapabilities; error?: string }>({ owner: sessionId });
  const [refresh, setRefresh] = useState(0);
  const [pending, setPending] = useState('');
  const [actionError, setActionError] = useState('');
  useEffect(() => {
    let active = true; let timer: ReturnType<typeof setTimeout> | undefined;
    setState({ owner: sessionId }); setActionError(''); setPending('');
    async function read() {
      try {
        const data = parseNativeCapabilities(await load(sessionId), sessionId);
        if (!active) return;
        setState({ owner: sessionId, data });
        if (data.mcp.servers.some(server => server.state === 'starting' || server.state === 'connecting')) timer = setTimeout(() => void read(), 3_000);
      } catch {
        if (active) setState({ owner: sessionId, error: 'MCP 状态暂不可用，请刷新重试。当前连接仍由 Pi 管理。' });
      }
    }
    void read();
    return () => { active = false; clearTimeout(timer); };
  }, [sessionId, load, refresh]);
  const data = state.owner === sessionId ? state.data : undefined;
  const error = state.owner === sessionId ? state.error : undefined;
  async function command(server: McpServer, action: 'login' | 'reconnect') {
    if (locked || pending || !/^[A-Za-z0-9_-]+$/.test(server.name)) return;
    setPending(`${action}:${server.name}`); setActionError('');
    try {
      await invoke(sessionId, `/mcp ${action} ${server.name}`);
      setRefresh(value => value + 1);
    } catch {
      setActionError('请求未确认，请刷新查看连接状态后再决定是否重试。');
    } finally { setPending(''); }
  }
  const term = query.trim().toLocaleLowerCase();
  const visible = data?.mcp.servers.filter(server => {
    const tools = data.tools.filter(tool => tool.namespace.name === server.namespace);
    const reachable = server.state === 'connected' && tools.some(tool => tool.routable && tool.exposure !== 'hidden' && (tool.exposure !== 'codemode' || data.codemodeMode !== 'off'));
    return (filter === 'all' || (filter === 'usable' ? reachable : !reachable)) && (!term || [server.name, server.namespace, ...tools.flatMap(tool => [tool.name, tool.description])].some(text => text.toLocaleLowerCase().includes(term)));
  }) ?? [];
  return <section className="pi-mcp" aria-label="Pi MCP 连接与工具">
    <div className="pi-mcp__toolbar"><span>Pi 原生 MCP · {data ? `${data.mcp.servers.length} 个服务器` : '读取当前对话'}</span><button type="button" aria-label="刷新 MCP 状态" disabled={Boolean(pending)} onClick={() => setRefresh(value => value + 1)}><RefreshCw size={14} aria-hidden />刷新</button></div>
    {actionError ? <p role="alert">{actionError}</p> : null}
    {!data ? <div className="pi-capabilities__empty" role="status"><strong>{error ? '无法读取 MCP 状态' : '正在读取 MCP 状态'}</strong><p>{error || '只读取当前 Pi 连接，不启动模型响应。'}</p></div> : <>
      {data.mcp.configErrorCount > 0 ? <p role="alert">{data.mcp.configErrorCount} 项 MCP 配置无法加载。请检查 Pi 的 mcp.json，再刷新连接。</p> : null}
      {data.codemodeMode === 'off' && data.mcp.servers.some(server => server.exposure === 'codemode') ? <p className="pi-mcp__notice">代码执行已关闭，代码编排模式的工具当前不可通过 codemode 调用。可在上方调整调用方式。</p> : null}
      {!visible.length ? <div className="pi-capabilities__empty" role="status"><strong>{data.mcp.servers.length ? '没有匹配的 MCP 服务器' : '尚未配置 MCP 服务器'}</strong><p>{data.mcp.servers.length ? '调整名称或状态筛选。' : 'Pi 读取全局 mcp.json 和受信任项目的 .pi/mcp.json，也接受扩展注册的服务器。配置后重新打开当前对话。'}</p></div> : null}
      {visible.map(server => <details className="pi-mcp__server" key={server.namespace}>
        <summary><strong>{server.name}</strong><span data-state={server.state === 'connected' ? 'usable' : 'unavailable'}>{states[server.state]}</span><small>{exposures[server.exposure]} · {server.toolCount} 个工具</small></summary>
        <div className="pi-mcp__server-body"><p>{scopes[server.scope] || 'Pi 配置'} · <code>{server.namespace}</code> · {server.resourceCount} 个资源 / {server.resourceTemplateCount} 个模板</p>
          {server.state === 'needs-auth' ? <p>服务器需要登录。登录由 Pi 打开浏览器并处理授权。</p> : server.state === 'failed' || server.state === 'disconnected' ? <p>连接未就绪，可重连；失败不表示工具已经执行。</p> : null}
          <div className="pi-mcp__actions">{server.state === 'needs-auth' ? <button type="button" disabled={locked || Boolean(pending)} onClick={() => void command(server, 'login')}>登录 {server.name}</button> : null}
            {!['disabled', 'starting', 'connecting', 'closed'].includes(server.state) ? <button type="button" disabled={locked || Boolean(pending)} onClick={() => void command(server, 'reconnect')}>{pending === `reconnect:${server.name}` ? '正在重连…' : `重连 ${server.name}`}</button> : null}</div>
          {data.tools.filter(tool => tool.namespace.name === server.namespace).map(tool => <details className="pi-mcp__tool" key={tool.name}><summary><code>{tool.name}</code><small>{exposures[tool.exposure]} · {tool.active ? '已披露给模型' : '未直接披露'}</small></summary><p>{tool.description || '未提供用途说明'}</p><p>{tool.routable ? 'Pi 允许路由；实际调用仍经过本对话工具执行规则。' : '当前不可路由。'}{tool.exposure === 'codemode' ? ' 通过 searchTools 查找后在代码中调用。' : tool.exposure === 'deferred' ? ' 通过 tool_search 加载后调用。' : ''}</p><details><summary>参数摘要</summary><pre tabIndex={0}>{JSON.stringify(tool.parameters, null, 2)}</pre></details></details>)}
          {server.state === 'connected' && !data.tools.some(tool => tool.namespace.name === server.namespace) ? <p>当前工具目录未附带该服务器的工具定义。</p> : null}
        </div>
      </details>)}
    </>}
    <p className="pi-capabilities__footnote">连接状态与披露方式来自当前 Pi；连接成功不代表本轮已经调用工具。</p>
  </section>;
}
