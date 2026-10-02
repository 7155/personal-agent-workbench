/**
 * Chrome DevTools Protocol bridge over WebSocket.
 *
 * Unit tests inject a pure text transport via createCdpSession / createCdpBridge.
 * Production uses connectCdp(port) against Chrome's /json/version endpoint.
 */

import { makeEgoError } from "./errors.js";
import { readFile } from "node:fs/promises";

const DEFAULT_TIMEOUT_MS = 15_000;

/** A single script connection owns its replies until that connection closes. */
export type CdpRawOwner = {
  signal: AbortSignal;
  onMessage(message: any): void;
};

export type CdpPageTarget = {
  active?: boolean;
  targetId: string;
  tabId?: string;
  title: string;
  url: string;
  type: string;
  webContentsId?: number;
};

export type CdpBridge = {
  send(method: string, params?: object, sessionId?: string): Promise<any>;
  sendRaw(payload: object, owner?: CdpRawOwner): void;
  onEvent(handler: (msg: any) => void): () => void;
  /**
   * Unowned raw caller responses (with original ids) and CDP events.
   * Connection-owned responses go directly to their owner.
   * Internal send() responses stay private to their owning promise.
   */
  onMessage?(handler: (msg: any) => void): () => void;
  close(): Promise<void>;
  listPageTargets(): Promise<CdpPageTarget[]>;
  createTarget(url: string): Promise<string>;
  attach(targetId: string): Promise<string>;
};

export type CdpTransport = {
  send(text: string): void;
  onMessage(cb: (text: string) => void): void;
};

export type CdpSessionOptions = {
  /** Request timeout in ms (default 15_000). Injectable for unit tests. */
  timeoutMs?: number;
  /** Forwarded responses may outlive the internal timeout (PAW run: <=120s). */
  forwardedTimeoutMs?: number;
};

export type CdpSession = {
  send(method: string, params?: object, sessionId?: string): Promise<any>;
  sendRaw(payload: object, owner?: CdpRawOwner): void;
  onEvent(handler: (msg: any) => void): () => void;
  /** Forwarded caller responses and events; never internal send() responses. */
  onMessage(handler: (msg: any) => void): () => void;
  handleIncoming(text: string): void;
  /** Reject all pending requests (e.g. on close / transport drop). */
  dispose(reason?: Error): void;
};

type Pending = {
  resolve: (value: any) => void;
  reject: (err: Error) => void;
  timer: ReturnType<typeof setTimeout>;
  method: string;
};

/**
 * Pure request/response CDP session over an injectable text transport.
 * Registers itself on transport.onMessage at construction.
 */
export function createCdpSession(
  transport: CdpTransport,
  options: CdpSessionOptions = {},
): CdpSession {
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  let nextId = 1;
  const pending = new Map<number, Pending>();
  const forwarded = new Map<number, {
    callerId: number; timer: ReturnType<typeof setTimeout>; owner?: CdpRawOwner;
  }>();
  const owners = new Map<CdpRawOwner, { ids: Set<number>; abort: () => void }>();
  const eventHandlers = new Set<(msg: any) => void>();
  const messageHandlers = new Set<(msg: any) => void>();
  let disposed = false;

  function removeForwarded(id: number): void {
    const entry = forwarded.get(id);
    if (!entry) return;
    clearTimeout(entry.timer);
    forwarded.delete(id);
    if (entry.owner) {
      const owned = owners.get(entry.owner);
      owned?.ids.delete(id);
      if (owned && owned.ids.size === 0) {
        entry.owner.signal.removeEventListener("abort", owned.abort);
        owners.delete(entry.owner);
      }
    }
  }

  function handleIncoming(text: string): void {
    let msg: any;
    try {
      msg = JSON.parse(text);
    } catch {
      return;
    }

    if (msg && msg.id != null && pending.has(msg.id)) {
      const entry = pending.get(msg.id)!;
      pending.delete(msg.id);
      clearTimeout(entry.timer);
      if (msg.error) {
        const detail =
          typeof msg.error === "object" && msg.error?.message
            ? String(msg.error.message)
            : JSON.stringify(msg.error);
        entry.reject(
          makeEgoError(
            "EGO_CDP_SEND_FAILED",
            `CDP error for ${entry.method}: ${detail}`,
          ),
        );
      } else {
        entry.resolve(msg.result);
      }
      return;
    }

    if (msg && msg.id != null) {
      const entry = forwarded.get(msg.id);
      // A late or duplicate response cannot satisfy a later script that reused
      // its caller id. All ids on this browser socket are allocated here.
      if (!entry) return;
      removeForwarded(msg.id);
      msg = { ...msg, id: entry.callerId };
      if (entry.owner) {
        if (!entry.owner.signal.aborted) {
          try { entry.owner.onMessage(msg); } catch { /* isolated listener */ }
        }
        return;
      }
    }
    for (const handler of messageHandlers) {
      try {
        handler(msg);
      } catch {
        // Listener errors must not break the session
      }
    }

    // Events: messages with a method and no correlated pending id
    if (msg && typeof msg.method === "string") {
      for (const handler of eventHandlers) {
        try {
          handler(msg);
        } catch {
          // Listener errors must not break the session
        }
      }
    }
  }

  transport.onMessage(handleIncoming);

  function send(
    method: string,
    params?: object,
    sessionId?: string,
  ): Promise<any> {
    if (disposed) {
      return Promise.reject(
        makeEgoError(
          "EGO_CDP_CHANNEL_UNAVAILABLE",
          "CDP session is closed",
        ),
      );
    }
    const id = nextId++;
    const payload: Record<string, unknown> = { id, method };
    if (params !== undefined) payload.params = params;
    if (sessionId !== undefined) payload.sessionId = sessionId;

    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(
          makeEgoError(
            "EGO_CDP_SEND_FAILED",
            `CDP timeout after ${timeoutMs}ms: ${method}`,
          ),
        );
      }, timeoutMs);

      pending.set(id, { resolve, reject, timer, method });

      try {
        transport.send(JSON.stringify(payload));
      } catch (err) {
        pending.delete(id);
        clearTimeout(timer);
        reject(
          makeEgoError(
            "EGO_CDP_SEND_FAILED",
            `CDP send failed for ${method}: ${err instanceof Error ? err.message : String(err)}`,
          ),
        );
      }
    });
  }

  function sendRaw(payload: object, owner?: CdpRawOwner): void {
    if (disposed || owner?.signal.aborted) {
      throw makeEgoError(
        "EGO_CDP_CHANNEL_UNAVAILABLE",
        "CDP session is closed",
      );
    }
    const raw = payload as Record<string, unknown>;
    if (raw.id != null && (typeof raw.id !== "number" || !Number.isSafeInteger(raw.id))) {
      throw makeEgoError("EGO_INVALID_ARGUMENT", "CDP request id must be a safe integer");
    }
    const wireId = raw.id == null ? undefined : nextId++;
    if (wireId !== undefined) {
      const timer = setTimeout(() => removeForwarded(wireId),
        options.forwardedTimeoutMs ?? Math.max(timeoutMs, 150_000));
      timer.unref();
      forwarded.set(wireId, { callerId: raw.id as number, timer, owner });
      if (owner) {
        let owned = owners.get(owner);
        if (!owned) {
          const ids = new Set<number>();
          const abort = () => { for (const id of ids) removeForwarded(id); };
          owned = { ids, abort };
          owners.set(owner, owned);
          owner.signal.addEventListener("abort", abort, { once: true });
        }
        owned.ids.add(wireId);
      }
    }
    try {
      transport.send(JSON.stringify(wireId === undefined ? raw : { ...raw, id: wireId }));
    } catch (err) {
      if (wireId !== undefined) {
        removeForwarded(wireId);
      }
      throw makeEgoError(
        "EGO_CDP_SEND_FAILED",
        `CDP sendRaw failed: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
  }

  function onEvent(handler: (msg: any) => void): () => void {
    eventHandlers.add(handler);
    return () => {
      eventHandlers.delete(handler);
    };
  }

  function onMessage(handler: (msg: any) => void): () => void {
    messageHandlers.add(handler);
    return () => {
      messageHandlers.delete(handler);
    };
  }

  function dispose(reason?: Error): void {
    if (disposed) return;
    disposed = true;
    const err =
      reason ??
      makeEgoError("EGO_CDP_CHANNEL_UNAVAILABLE", "CDP session disposed");
    for (const [, entry] of pending) {
      clearTimeout(entry.timer);
      entry.reject(err);
    }
    pending.clear();
    for (const id of forwarded.keys()) removeForwarded(id);
    eventHandlers.clear();
    messageHandlers.clear();
  }

  return { send, sendRaw, onEvent, onMessage, handleIncoming, dispose };
}

export type CdpBridgeOptions = CdpSessionOptions & {
  /** Called by bridge.close(); defaults to session.dispose only. */
  onClose?: () => void | Promise<void>;
};

/**
 * Build a full CdpBridge on top of an injectable transport (for unit tests).
 */
export function createCdpBridge(
  transport: CdpTransport,
  options: CdpBridgeOptions = {},
): CdpBridge {
  const session = createCdpSession(transport, options);
  return wrapSessionAsBridge(session, options.onClose);
}

function wrapSessionAsBridge(
  session: CdpSession,
  onClose?: () => void | Promise<void>,
): CdpBridge {
  return {
    send: (method, params, sessionId) =>
      session.send(method, params, sessionId),
    sendRaw: (payload, owner) => session.sendRaw(payload, owner),
    onEvent: (handler) => session.onEvent(handler),
    onMessage: (handler) => session.onMessage(handler),
    async close() {
      session.dispose(
        makeEgoError("EGO_CDP_CHANNEL_UNAVAILABLE", "CDP bridge closed"),
      );
      if (onClose) await onClose();
    },
    async listPageTargets() {
      const result = await session.send("Target.getTargets");
      const infos = (result?.targetInfos ?? []) as Array<{
        targetId?: string;
        title?: string;
        url?: string;
        type?: string;
      }>;
      const targets: CdpPageTarget[] = infos
        .filter((t) =>
          (t.type === "page" || t.type === "webview")
          && !String(t.url ?? "").includes("pawHost=electron")
        )
        .map((t) => ({
          active: false,
          targetId: String(t.targetId ?? ""),
          tabId: String(t.targetId ?? ""),
          title: String(t.title ?? ""),
          url: String(t.url ?? ""),
          type: String(t.type ?? "page"),
        }));
      const activeTargetFile = process.env.EGO_PAW_ACTIVE_TARGET_FILE;
      if (!activeTargetFile) return targets;
      try {
        const active = JSON.parse(await readFile(activeTargetFile, "utf8"));
        const activeTargetId = String(active?.targetId ?? "");
        const index = targets.findIndex((target) => target.targetId === activeTargetId);
        if (index >= 0) {
          targets[index].active = true;
          targets[index].webContentsId = Number(active?.webContentsId || 0) || undefined;
          targets.push(...targets.splice(index, 1));
        }
      } catch {
        // The renderer refreshes this optional selection hint as tabs change.
      }
      return targets;
    },
    async createTarget(url: string) {
      const result = await session.send("Target.createTarget", { url });
      if (!result?.targetId) {
        throw makeEgoError(
          "EGO_CDP_SEND_FAILED",
          "Target.createTarget returned no targetId",
        );
      }
      return String(result.targetId);
    },
    async attach(targetId: string) {
      const result = await session.send("Target.attachToTarget", {
        targetId,
        flatten: true,
      });
      if (!result?.sessionId) {
        throw makeEgoError(
          "EGO_CDP_SEND_FAILED",
          "Target.attachToTarget returned no sessionId",
        );
      }
      return String(result.sessionId);
    },
  };
}

/**
 * Connect to Chrome CDP on loopback `port`.
 * GET /json/version → webSocketDebuggerUrl → WebSocket (Node 22 global).
 */
export async function connectCdp(port: number): Promise<CdpBridge> {
  let version: { webSocketDebuggerUrl?: string };
  try {
    const res = await fetch(`http://127.0.0.1:${port}/json/version`, {
      signal: AbortSignal.timeout(5000),
    });
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
    version = (await res.json()) as { webSocketDebuggerUrl?: string };
  } catch (err) {
    throw makeEgoError(
      "EGO_CDP_CHANNEL_UNAVAILABLE",
      `CDP HTTP endpoint unavailable on 127.0.0.1:${port}: ${err instanceof Error ? err.message : String(err)}`,
    );
  }

  const wsUrl = version.webSocketDebuggerUrl;
  if (!wsUrl || typeof wsUrl !== "string") {
    throw makeEgoError(
      "EGO_CDP_CHANNEL_UNAVAILABLE",
      `CDP /json/version missing webSocketDebuggerUrl on port ${port}`,
    );
  }

  const ws = new WebSocket(wsUrl);

  await new Promise<void>((resolve, reject) => {
    const onOpen = () => {
      cleanup();
      resolve();
    };
    const onError = () => {
      cleanup();
      reject(
        makeEgoError(
          "EGO_CDP_CHANNEL_UNAVAILABLE",
          `WebSocket connection failed to ${wsUrl}`,
        ),
      );
    };
    const cleanup = () => {
      ws.removeEventListener("open", onOpen);
      ws.removeEventListener("error", onError);
    };
    ws.addEventListener("open", onOpen);
    ws.addEventListener("error", onError);
  });

  const messageHandlers = new Set<(text: string) => void>();
  ws.addEventListener("message", (ev) => {
    const data = ev.data;
    const text =
      typeof data === "string"
        ? data
        : data instanceof ArrayBuffer
          ? Buffer.from(data).toString("utf8")
          : Buffer.from(data as Buffer).toString("utf8");
    for (const cb of messageHandlers) cb(text);
  });

  const transport: CdpTransport = {
    send(text: string) {
      if (ws.readyState !== WebSocket.OPEN) {
        throw new Error("WebSocket is not open");
      }
      ws.send(text);
    },
    onMessage(cb: (text: string) => void) {
      messageHandlers.add(cb);
    },
  };

  const session = createCdpSession(transport);

  ws.addEventListener("close", () => {
    session.dispose(
      makeEgoError(
        "EGO_CDP_CHANNEL_UNAVAILABLE",
        "CDP WebSocket closed",
      ),
    );
  });

  return wrapSessionAsBridge(session, async () => {
    messageHandlers.clear();
    if (
      ws.readyState === WebSocket.OPEN ||
      ws.readyState === WebSocket.CONNECTING
    ) {
      await new Promise<void>((resolve) => {
        const done = () => resolve();
        ws.addEventListener("close", done, { once: true });
        try {
          ws.close();
        } catch {
          resolve();
          return;
        }
        // Fallback if close never fires
        setTimeout(resolve, 1000);
      });
    }
  });
}
