import { fork } from "node:child_process";
import { randomUUID } from "node:crypto";

import { SwitchboardError } from "./shared.mjs";

const DEFAULT_TIMEOUT_MS = 360_000;

function workerEnvironment(extra = {}) {
  return {
    HOME: "/Users/nokiy",
    PATH: process.env.PATH || "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
    TMPDIR: process.env.TMPDIR || "/tmp",
    LANG: process.env.LANG || "en_US.UTF-8",
    LC_ALL: process.env.LC_ALL || "en_US.UTF-8",
    ...extra,
  };
}

export class IsolatedWorkerClient {
  #script;
  #label;
  #child = null;
  #pending = new Map();
  #extraEnv;

  constructor({ script, label, env = {} }) {
    this.#script = script;
    this.#label = label;
    this.#extraEnv = env;
  }

  #ensureChild() {
    if (this.#child?.connected) return this.#child;
    const child = fork(this.#script, [], {
      stdio: ["ignore", "ignore", "ignore", "ipc"],
      execArgv: [],
      env: workerEnvironment(this.#extraEnv),
      serialization: "advanced",
    });
    this.#child = child;
    child.on("message", (message) => this.#handleMessage(message));
    child.on("exit", () => {
      if (this.#child === child) this.#child = null;
      for (const [id, pending] of this.#pending) {
        clearTimeout(pending.timer);
        pending.reject(new SwitchboardError(
          "EXTERNAL_WORKER_CRASHED",
          `${this.#label} worker exited before completing the request.`,
        ));
        this.#pending.delete(id);
      }
    });
    child.on("error", () => {
      // Exit handling above provides the typed request failures.
    });
    return child;
  }

  #handleMessage(message) {
    const pending = this.#pending.get(message?.id);
    if (!pending) return;
    if (message.kind === "event") {
      pending.events.push(message.event);
      pending.onEvent?.(message.event);
      return;
    }
    clearTimeout(pending.timer);
    this.#pending.delete(message.id);
    if (message.kind === "done") {
      pending.resolve({ ...message.result, events: pending.events });
      return;
    }
    pending.reject(new SwitchboardError(
      message.error?.code || "EXTERNAL_WORKER_ERROR",
      message.error?.message || `${this.#label} worker failed.`,
      message.error?.status || 502,
      message.error?.details,
    ));
  }

  // sessionKey names the worker session; ownerKey names the connection or
  // request whose close cancels this job. A thread session outlives its owners.
  run(op, body, { sessionKey = randomUUID(), ownerKey = null, onEvent = null, timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
    const child = this.#ensureChild();
    const id = randomUUID();
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.#pending.delete(id);
        const cancelId = randomUUID();
        const cancelGuard = setTimeout(() => {
          this.#pending.delete(cancelId);
          if (this.#child === child) child.kill("SIGKILL");
        }, 2_000);
        cancelGuard.unref?.();
        this.#pending.set(cancelId, {
          resolve: () => clearTimeout(cancelGuard),
          reject: () => clearTimeout(cancelGuard),
          onEvent: null,
          events: [],
          timer: cancelGuard,
        });
        child.send({ id: cancelId, op: "close_session", body: null, session_key: sessionKey }, (error) => {
          if (!error) return;
          clearTimeout(cancelGuard);
          this.#pending.delete(cancelId);
          if (this.#child === child) child.kill("SIGKILL");
        });
        reject(new SwitchboardError("EXTERNAL_WORKER_TIMEOUT", `${this.#label} worker timed out.`));
      }, timeoutMs);
      this.#pending.set(id, { resolve, reject, onEvent, events: [], timer });
      child.send({ id, op, body, session_key: sessionKey, ...(ownerKey ? { owner_key: ownerKey } : {}) }, (error) => {
        if (!error) return;
        clearTimeout(timer);
        this.#pending.delete(id);
        reject(new SwitchboardError("EXTERNAL_WORKER_IPC_FAILED", `${this.#label} worker IPC failed.`));
      });
    });
  }

  runResponses(body, options = {}) {
    return this.run("responses", body, options);
  }

  compact(body, options = {}) {
    return this.run("compact", body, options);
  }

  async closeSession(sessionKey) {
    if (!this.#child?.connected) return;
    await this.run("close_session", null, { sessionKey, timeoutMs: 10_000 });
  }

  async interruptTurn(sessionKey, body) {
    if (!this.#child?.connected) {
      throw new SwitchboardError("WEB_INTERRUPT_OWNER_UNAVAILABLE", "The active Web worker is unavailable.", 409);
    }
    return this.run("interrupt_turn", body, { sessionKey, timeoutMs: 10_000 });
  }

  async testCrash() {
    if (!this.#child?.connected) this.#ensureChild();
    return this.run("crash", null, { timeoutMs: 10_000 });
  }

  close() {
    this.#child?.kill("SIGTERM");
    this.#child = null;
  }
}
