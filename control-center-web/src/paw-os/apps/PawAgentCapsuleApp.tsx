import { BookOpenText, ExternalLink, Keyboard, Languages, Lightbulb, MousePointer2, Notebook, Scan, ShieldCheck } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import type { LucideIcon } from 'lucide-react';
import { openPawOsRoute, usePawOsDesktop } from '@/features/paw-os/surface-context';
import { PawAppIcon } from '../shell/PawAppIcon';
import { screenActions, type ScreenAssistantHost } from '@/features/screen-assistant/screen-assistant-model';
import './paw-agent-capsule.css';

/**
 * The managed PAWOS entry for the desktop Agent Capsule. The actual capture
 * window remains owned by Electron's screen-assistant host; this App owns
 * discovery, affordances and the handoff into that host without creating a
 * second Session or screen-reading implementation.
 */
export function PawAgentCapsuleApp() {
  const desktop = usePawOsDesktop();
  const [busy, setBusy] = useState(false);
  const [captureState, setCaptureState] = useState<'idle' | 'launching' | 'opened' | 'cancelled' | 'error'>('idle');
  const [notice, setNotice] = useState('');
  const [host, setHost] = useState<ScreenAssistantHost | undefined>(() => (
    typeof window === 'undefined' ? undefined : window.pawScreenAssistant
  ));

  useEffect(() => {
    const next = typeof window === 'undefined' ? undefined : window.pawScreenAssistant;
    setHost(next);
    if (!next) setNotice('请在 PAW 桌面宿主中使用框选；浏览器预览不会读取系统屏幕。');
  }, []);

  const capture = useCallback(async () => {
    if (!host || busy) return;
    setBusy(true);
    setCaptureState('launching');
    setNotice('正在打开系统框选…');
    try {
      const started = await host.capture();
      setCaptureState(started ? 'opened' : 'cancelled');
      setNotice(started ? '已打开框选窗口；完成后会进入同一 Session 对话。' : '框选已取消，可以再次尝试。');
    } catch (reason) {
      setCaptureState('error');
      setNotice(reason instanceof Error && reason.message ? reason.message : '框选没有启动，请重试。');
    } finally {
      setBusy(false);
    }
  }, [busy, host]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!event.metaKey && !event.ctrlKey) return;
      if (!event.shiftKey || event.code !== 'Space') return;
      event.preventDefault();
      void capture();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [capture]);

  const actionIcons: Record<(typeof screenActions)[number]['id'], LucideIcon> = {
    translate: Languages,
    explain: Lightbulb,
    note: Notebook,
    act: MousePointer2,
  };

  return (
    <main className="paw-agent-capsule" data-app="agent-capsule" data-capture-state={captureState}>
      <header className="paw-agent-capsule__hero">
        <div className="paw-agent-capsule__identity">
          <PawAppIcon appId="agent-capsule" size={42} title="Agent Capsule" />
          <div>
            <h1>Agent Capsule</h1>
            <p>在任何界面框选内容，马上翻译、解释、做笔记或继续操作。</p>
          </div>
        </div>
        <button aria-describedby={notice ? 'capsule-capture-status' : undefined} aria-keyshortcuts="Meta+Shift+Space Control+Shift+Space" className="paw-agent-capsule__capture" data-loading={busy || undefined} disabled={!host || busy} onClick={() => void capture()} type="button">
          <Scan aria-hidden="true" size={18} />
          {busy ? '正在准备…' : '框选屏幕'}
          <kbd aria-hidden="true">⌘ ⇧ Space</kbd>
        </button>
      </header>
      {notice ? <p className="paw-agent-capsule__notice" data-status={captureState} id="capsule-capture-status" role="status">{notice}</p> : null}

      <section aria-labelledby="capsule-actions" className="paw-agent-capsule__section">
        <div className="paw-agent-capsule__section-heading">
          <div><h2 id="capsule-actions">框选后可以做什么</h2></div>
          <span>同一 Session 可继续追问</span>
        </div>
        <ul className="paw-agent-capsule__actions">
          {screenActions.map((action) => {
            const Icon = actionIcons[action.id];
            return (
              <li key={action.id}>
                <span className="paw-agent-capsule__action-icon"><Icon aria-hidden="true" size={17} /></span>
                <div><strong>{action.label}</strong><p>{action.draft}</p></div>
                <span className="paw-agent-capsule__action-meta">选区</span>
              </li>
            );
          })}
        </ul>
      </section>

      <aside aria-labelledby="capsule-protocol" className="paw-agent-capsule__protocol">
        <div className="paw-agent-capsule__section-heading">
          <div><BookOpenText aria-hidden="true" size={16} /><h2 id="capsule-protocol">一次框选，持续协作</h2></div>
          <span>图片只在本次 Session 中复用</span>
        </div>
        <ol>
          <li><span>01</span><div><strong>框选眼前内容</strong><p>PAW 暂时隐藏自己的窗口，避免把工具栏带进截图。</p></div></li>
          <li><span>02</span><div><strong>在同一对话里追问</strong><p>先用快捷动作，再补充你的目标或操作步骤。</p></div></li>
          <li><span>03</span><div><strong>留下可回看的结果</strong><p>回答可以保存为 Markdown；桌面操作仍会重新读取当前目标。</p></div></li>
        </ol>
      </aside>

      <section aria-label="使用方式" className="paw-agent-capsule__details">
        <div><Keyboard aria-hidden="true" size={16} /><span><b>⌘ ⇧ Space</b> 随时打开框选</span></div>
        <div><ShieldCheck aria-hidden="true" size={16} /><span>系统屏幕权限由 macOS 控制，PAW 只保存本次受管附件</span></div>
        <button onClick={() => openPawOsRoute(desktop, '/agent')} type="button"><ExternalLink aria-hidden="true" size={15} />打开完整 Agent</button>
      </section>

    </main>
  );
}
