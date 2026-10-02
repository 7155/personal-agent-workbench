/**
 * Daemon-side implementations of globalThis.ego methods.
 *
 * Enforces Task Space isolation (listTabs / createTab) and user-control
 * blocks on snapshot / page-domain CDP.
 */

import type { CdpBridge, CdpRawOwner } from "./cdp-bridge.js";
import { makeEgoError } from "./errors.js";
import type { RpcEvent } from "./rpc.js";
import { snapshotPage, type SnapshotOptions } from "./snapshot-engine.js";
import type { SpaceManager } from "./space-manager.js";

/** Browser-level CDP domains that remain allowed under user control. */
function isBrowserLevelMethod(method: string): boolean {
  return method.startsWith("Target.") || method.startsWith("Browser.");
}

export type EgoRuntimeDeps = {
  spaceManager: SpaceManager;
  getCdp: () => CdpBridge;
  ensureSession: () => Promise<string>;
  createVisibleTab?: (url: string) => Promise<string>;
  activateVisibleTarget?: (targetId: string) => Promise<void>;
  /** PAW Electron keeps Agent navigation in the selected visible guest. */
  reuseSelectedTarget?: boolean;
  /** Package version reported by ping when routed through runtime (optional). */
  version?: string;
};

export type EgoRequestContext = {
  cdpOwner: CdpRawOwner;
  onEvent(event: RpcEvent): void;
};

export type EgoRuntime = {
  handle(method: string, params?: any, context?: EgoRequestContext): Promise<any>;
  /** Subscribe to runtime-pushed events (cdp.message, cdp.sendError). */
  onEvent(handler: (ev: RpcEvent) => void): () => void;
  /**
   * Forward all CDP messages from the current bridge to event subscribers.
   * Call after connect / reconnect. Returns unsubscribe.
   */
  attachCdpForwarding(): () => void;
};

function normalizeMethod(method: string): string {
  if (method.startsWith("ego.")) return method.slice(4);
  return method;
}

function publicSpace(space: {
  taskId: string;
  id: number;
  name: string;
  createdBy: string;
  ownership: string;
  recentTabTitles?: string[];
}) {
  return {
    taskId: space.taskId,
    id: space.id,
    name: space.name,
    createdBy: space.createdBy,
    ownership: space.ownership,
    ...(space.recentTabTitles
      ? { recentTabTitles: [...space.recentTabTitles] }
      : {}),
  };
}

/**
 * Create the ego method dispatcher used by the host daemon.
 */
export function createEgoRuntime(deps: EgoRuntimeDeps): EgoRuntime {
  const eventHandlers = new Set<(ev: RpcEvent) => void>();
  let detachCdp: (() => void) | undefined;

  function emit(ev: RpcEvent): void {
    for (const h of eventHandlers) {
      try {
        h(ev);
      } catch {
        // subscriber errors must not break the runtime
      }
    }
  }

  function onEvent(handler: (ev: RpcEvent) => void): () => void {
    eventHandlers.add(handler);
    return () => {
      eventHandlers.delete(handler);
    };
  }

  function attachCdpForwarding(): () => void {
    if (detachCdp) {
      detachCdp();
      detachCdp = undefined;
    }
    const cdp = deps.getCdp();
    const handler = (msg: any) => {
      emit({ event: "cdp.message", params: { payload: JSON.stringify(msg) } });
    };
    if (typeof cdp.onMessage === "function") {
      detachCdp = cdp.onMessage(handler);
    } else {
      // Fallback: events only (responses with id may be missed)
      detachCdp = cdp.onEvent(handler);
    }
    return () => {
      if (detachCdp) {
        detachCdp();
        detachCdp = undefined;
      }
    };
  }

  function emitSendError(message: string, error_code?: string, context?: EgoRequestContext): void {
    (context?.onEvent ?? emit)({
      event: "cdp.sendError",
      params: {
        message,
        ...(error_code ? { error_code } : {}),
      },
    });
  }

  async function listTabs(): Promise<{ tabs: any[] }> {
    const allowed = new Set(deps.spaceManager.targetsForSelected());
    const all = await deps.getCdp().listPageTargets();
    const filtered = all.filter((t) => allowed.has(t.targetId));
    const tabs = filtered.map((t, index) => ({
      tabId: t.targetId,
      targetId: t.targetId,
      webContentsId: t.webContentsId,
      title: t.title,
      url: t.url,
      active: t.active ?? index === filtered.length - 1,
      index,
    }));
    return { tabs };
  }

  async function createTab(params: { url?: string } = {}): Promise<{
    targetId: string;
  }> {
    const selected = deps.spaceManager.selected();
    if (!selected) {
      throw makeEgoError(
        "EGO_TASK_SPACE_NOT_SELECTED",
        "task space not selected",
      );
    }
    const url =
      typeof params?.url === "string" && params.url !== ""
        ? params.url
        : "about:blank";
    if (deps.createVisibleTab) {
      const targetId = await deps.createVisibleTab(url);
      deps.spaceManager.assignTarget(targetId);
      return { targetId };
    }
    const selectedTargets = deps.spaceManager.targetsForSelected();
    if (deps.reuseSelectedTarget && selectedTargets.length) {
      const targetId = selectedTargets[selectedTargets.length - 1];
      const sessionId = await deps.getCdp().attach(targetId);
      await deps.getCdp().send("Page.navigate", { url }, sessionId);
      return { targetId };
    }
    const targetId = await deps.getCdp().createTarget(url);
    deps.spaceManager.assignTarget(targetId);
    return { targetId };
  }

  async function snapshot(params: SnapshotOptions = {}): Promise<{
    content: string;
    refs: any[];
  }> {
    if (deps.spaceManager.isPageControlBlocked()) {
      throw makeEgoError(
        "EGO_TASK_SPACE_USER_IN_CONTROL",
        "task space is under user control; claim or takeOver before page ops",
      );
    }
    const sessionId = await deps.ensureSession();
    return snapshotPage(deps.getCdp(), sessionId, params);
  }

  async function sendCDPMessage(params: {
    payload?: string;
  }, context?: EgoRequestContext): Promise<{ ok: true }> {
    const raw = params?.payload;
    if (typeof raw !== "string" || raw === "") {
      throw makeEgoError(
        "EGO_INVALID_ARGUMENT",
        "sendCDPMessage requires { payload: string }",
      );
    }

    let msg: any;
    try {
      msg = JSON.parse(raw);
    } catch (err) {
      emitSendError(
        `invalid CDP payload JSON: ${err instanceof Error ? err.message : String(err)}`,
        "EGO_INVALID_ARGUMENT",
        context,
      );
      return { ok: true };
    }

    const method = typeof msg?.method === "string" ? msg.method : "";
    const pageDomain = method ? !isBrowserLevelMethod(method) : true;

    if (pageDomain && deps.spaceManager.isPageControlBlocked()) {
      emitSendError(
        "task space is under user control; claim or takeOver before page ops",
        "EGO_TASK_SPACE_USER_IN_CONTROL",
        context,
      );
      return { ok: true };
    }

    try {
      deps.getCdp().sendRaw(msg, context?.cdpOwner);
      if (method === "Target.activateTarget" && deps.activateVisibleTarget) {
        await deps.activateVisibleTarget(String(msg?.params?.targetId || ""));
      }
    } catch (err) {
      const code =
        err &&
        typeof err === "object" &&
        typeof (err as { error_code?: string }).error_code === "string"
          ? (err as { error_code: string }).error_code
          : "EGO_CDP_SEND_FAILED";
      const message =
        err instanceof Error
          ? err.message
          : typeof err === "string"
            ? err
            : String(err);
      emitSendError(message, code, context);
    }
    return { ok: true };
  }

  async function listTaskSpaces() {
    return { taskSpaces: deps.spaceManager.listPublic() };
  }

  async function createTaskSpace(params: { name?: string } = {}) {
    const name =
      typeof params?.name === "string" && params.name !== ""
        ? params.name
        : "untitled";
    const space = deps.spaceManager.createAgentSpace(name);
    if (deps.reuseSelectedTarget) {
      const pages = await deps.getCdp().listPageTargets();
      const visible = pages[pages.length - 1];
      if (visible) deps.spaceManager.assignTarget(visible.targetId, space.id);
    }
    return publicSpace(space);
  }

  async function useTaskSpace(params: { id?: number } = {}) {
    const id = Number(params?.id);
    if (!Number.isFinite(id)) {
      return {
        error: "useTaskSpace requires { id: number }",
        error_code: "EGO_INVALID_ARGUMENT",
      };
    }
    const result = deps.spaceManager.use(id);
    if (result.ok === false) {
      return { error: result.error, error_code: result.error_code };
    }
    return publicSpace(result.space);
  }

  async function claimTaskSpace(params: { id?: number; name?: string } = {}) {
    const id = Number(params?.id);
    if (!Number.isFinite(id)) {
      throw makeEgoError(
        "EGO_INVALID_ARGUMENT",
        "claimTaskSpace requires { id: number }",
      );
    }
    try {
      const space = deps.spaceManager.claim(
        id,
        typeof params?.name === "string" ? params.name : undefined,
      );
      return publicSpace(space);
    } catch (err) {
      if (
        err &&
        typeof err === "object" &&
        (err as { error_code?: string }).error_code
      ) {
        throw err;
      }
      throw makeEgoError(
        "EGO_OPERATION_FAILED",
        err instanceof Error ? err.message : String(err),
      );
    }
  }

  async function completeTaskSpace() {
    if (!deps.spaceManager.selected()) {
      throw makeEgoError(
        "EGO_TASK_SPACE_NOT_SELECTED",
        "task space not selected",
      );
    }
    if (deps.spaceManager.targetsForSelected().length) {
      await setAgentTaskState({ label: "" });
    }
    deps.spaceManager.completeKeep();
    return { ok: true };
  }

  async function closeTaskSpace() {
    if (!deps.spaceManager.selected()) {
      throw makeEgoError(
        "EGO_TASK_SPACE_NOT_SELECTED",
        "task space not selected",
      );
    }
    const targetIds = deps.spaceManager.closeSelected();
    // Best-effort close page targets in Chrome
    const cdp = deps.getCdp();
    for (const targetId of targetIds) {
      try {
        await cdp.send("Target.closeTarget", { targetId });
      } catch {
        // ignore close failures
      }
    }
    return { ok: true };
  }

  async function handOffTaskSpace() {
    if (!deps.spaceManager.selected()) {
      throw makeEgoError(
        "EGO_TASK_SPACE_NOT_SELECTED",
        "task space not selected",
      );
    }
    if (deps.spaceManager.targetsForSelected().length) {
      await setAgentTaskState({ label: "" });
    }
    deps.spaceManager.handOff();
    return { ok: true };
  }

  async function takeOverTaskSpace() {
    if (!deps.spaceManager.selected()) {
      throw makeEgoError(
        "EGO_TASK_SPACE_NOT_SELECTED",
        "task space not selected",
      );
    }
    deps.spaceManager.takeOver();
    return { ok: true };
  }

  async function animationHighlightMouseToPosition(
    params: { x?: number; y?: number } = {},
  ) {
    if (deps.spaceManager.isPageControlBlocked()) {
      throw makeEgoError(
        "EGO_TASK_SPACE_USER_IN_CONTROL",
        "task space is under user control; claim or takeOver before page ops",
      );
    }
    const x = Number(params.x);
    const y = Number(params.y);
    if (!Number.isFinite(x) || !Number.isFinite(y)) {
      throw makeEgoError(
        "EGO_INVALID_ARGUMENT",
        "animationHighlightMouseToPosition requires finite x and y",
      );
    }
    const sessionId = await deps.ensureSession();
    await deps.getCdp().send(
      "Runtime.evaluate",
      {
        expression: `(() => {
          const id = '__paw_ego_pointer__';
          let pointer = document.getElementById(id);
          if (!pointer) {
            pointer = document.createElement('div');
            pointer.id = id;
            Object.assign(pointer.style, {
              position: 'fixed', width: '22px', height: '22px',
              border: '2px solid #0d9488', borderRadius: '999px',
              boxShadow: '0 0 0 5px rgba(13,148,136,.18)',
              background: 'rgba(255,255,255,.9)', pointerEvents: 'none',
              zIndex: '2147483647', transform: 'translate(-50%,-50%)',
              transition: 'left 140ms cubic-bezier(.2,.8,.2,1), top 140ms cubic-bezier(.2,.8,.2,1)',
            });
            document.documentElement.appendChild(pointer);
          }
          pointer.style.left = ${JSON.stringify(x)} + 'px';
          pointer.style.top = ${JSON.stringify(y)} + 'px';
          pointer.animate(
            [{ opacity: 0, scale: .62 }, { opacity: 1, scale: 1 }, { opacity: .92, scale: .86 }],
            { duration: 520, easing: 'cubic-bezier(.2,.8,.2,1)', fill: 'forwards' },
          );
          clearTimeout(window.__pawEgoPointerTimer);
          window.__pawEgoPointerTimer = setTimeout(() => pointer?.remove(), 1100);
          return true;
        })()`,
        returnByValue: true,
        awaitPromise: false,
      },
      sessionId,
    );
    return { ok: true };
  }

  async function setAgentTaskState(params: { label?: string } = {}) {
    if (deps.spaceManager.isPageControlBlocked()) return { ok: true };
    const label = String(params.label ?? "").trim().slice(0, 160);
    const sessionId = await deps.ensureSession();
    await deps.getCdp().send(
      "Runtime.evaluate",
      {
        expression: `(() => {
          const id = '__paw_ego_state__';
          let badge = document.getElementById(id);
          if (!${JSON.stringify(Boolean(label))}) { badge?.remove(); return true; }
          if (!badge) {
            badge = document.createElement('div');
            badge.id = id;
            Object.assign(badge.style, {
              position: 'fixed', right: '18px', top: '18px',
              maxWidth: 'min(360px, calc(100vw - 36px))', padding: '8px 11px',
              color: '#0f172a', background: 'rgba(255,255,255,.92)',
              border: '1px solid rgba(13,148,136,.42)', borderRadius: '8px',
              boxShadow: '0 10px 30px rgba(15,23,42,.16)',
              font: '600 12px/1.35 system-ui, sans-serif', pointerEvents: 'none',
              zIndex: '2147483646', backdropFilter: 'blur(12px)',
            });
            document.documentElement.appendChild(badge);
          }
          badge.textContent = 'Agent · ' + ${JSON.stringify(label)};
          badge.animate(
            [{ opacity: 0, transform: 'translateY(-5px)' }, { opacity: 1, transform: 'translateY(0)' }],
            { duration: 180, easing: 'ease-out' },
          );
          return true;
        })()`,
        returnByValue: true,
        awaitPromise: false,
      },
      sessionId,
    );
    return { ok: true };
  }

  async function handle(method: string, params: any = {}, context?: EgoRequestContext): Promise<any> {
    const name = normalizeMethod(method);
    switch (name) {
      case "listTaskSpaces":
        return listTaskSpaces();
      case "createTaskSpace":
        return createTaskSpace(params);
      case "useTaskSpace":
        return useTaskSpace(params);
      case "claimTaskSpace":
        return claimTaskSpace(params);
      case "completeTaskSpace":
        return completeTaskSpace();
      case "closeTaskSpace":
        return closeTaskSpace();
      case "handOffTaskSpace":
        return handOffTaskSpace();
      case "takeOverTaskSpace":
        return takeOverTaskSpace();
      case "animationHighlightMouseToPosition":
        return animationHighlightMouseToPosition(params);
      case "setAgentTaskState":
        return setAgentTaskState(params);
      case "listTabs":
        return listTabs();
      case "createTab":
        return createTab(params);
      case "snapshot":
        return snapshot(params);
      case "sendCDPMessage":
        return sendCDPMessage(params, context);
      default:
        throw makeEgoError(
          "EGO_INVALID_ARGUMENT",
          `unknown ego method: ${method}`,
        );
    }
  }

  return { handle, onEvent, attachCdpForwarding };
}
