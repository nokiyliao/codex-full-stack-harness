import http from "node:http";
import https from "node:https";
import { randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join, resolve } from "node:path";

import { SseDecoder, SwitchboardError } from "./shared.mjs";

const DEFAULT_RESPONSES_ENDPOINT = "http://127.0.0.1:17841/v1/responses";
const TERMINAL_EVENTS = new Set(["response.completed", "response.failed", "response.incomplete", "error"]);
const WEB_COMPACTION_PREFIX = "ocx1:";

async function loadLocalControlConfig() {
  const configured = process.env.CODEX_CHATGPT_WEB_HOME?.trim();
  const home = configured === "~" ? homedir()
    : configured?.startsWith("~/") ? join(homedir(), configured.slice(2))
      : configured || join(homedir(), ".codex-chatgpt-web");
  return JSON.parse(await readFile(join(resolve(home), "config.json"), "utf8"));
}
const WEB_SUMMARY_PREFIX = "Another language model started to solve this problem and produced a summary of its thinking process. You also have access to the state of the tools that were used by that language model. Use this to build on the work that has already been done and avoid duplicating work. Here is the summary produced by the other language model, use the information in this summary to assist with your own analysis:";

function safeError(code, message, status = 502) {
  return { code, message, status };
}

function contentType(headers) {
  if (typeof headers?.get === "function") return String(headers.get("content-type") || "");
  return String(headers?.["content-type"] || "");
}

async function readBody(body) {
  if (Buffer.isBuffer(body)) return body;
  if (body instanceof Uint8Array) return Buffer.from(body);
  if (typeof body === "string") return Buffer.from(body);
  const chunks = [];
  for await (const chunk of body || []) chunks.push(Buffer.from(chunk));
  return Buffer.concat(chunks);
}

function downstreamError(raw) {
  try {
    const parsed = JSON.parse(raw.toString("utf8"));
    const error = parsed?.error;
    const message = typeof error?.message === "string" && error.message.trim()
      ? error.message.trim().slice(0, 1_024)
      : null;
    const code = typeof error?.code === "string" && /^[A-Za-z0-9_.-]{1,128}$/.test(error.code)
      ? error.code
      : null;
    return { message, code };
  } catch {
    return { message: null, code: null };
  }
}

async function* bodyChunks(body) {
  if (Buffer.isBuffer(body) || body instanceof Uint8Array || typeof body === "string") {
    yield Buffer.from(body);
    return;
  }
  for await (const chunk of body || []) yield Buffer.from(chunk);
}

function compactEndpoint(responsesEndpoint) {
  const url = new URL(responsesEndpoint);
  url.pathname = url.pathname.replace(/\/responses\/?$/, "/responses/compact");
  return url.toString();
}

export function postJsonTransport({ endpoint, payload, headers, markSubmitted, signal }) {
  return new Promise((resolve, reject) => {
    let url;
    try {
      url = new URL(endpoint);
    } catch {
      reject(new SwitchboardError("WEB_PROVIDER_UNAVAILABLE", "CHATGPT_WEB_ENDPOINT is not a valid URL."));
      return;
    }
    const client = url.protocol === "https:" ? https : url.protocol === "http:" ? http : null;
    if (!client) {
      reject(new SwitchboardError("WEB_PROVIDER_UNAVAILABLE", "CHATGPT_WEB_ENDPOINT must use HTTP or HTTPS."));
      return;
    }
    const request = client.request(url, { method: "POST", headers }, (response) => {
      resolve({ status: response.statusCode || 502, headers: response.headers, body: response });
    });
    const abort = () => request.destroy(Object.assign(new Error("ChatGPT Web request cancelled."), { code: "ABORT_ERR" }));
    signal?.addEventListener("abort", abort, { once: true });
    request.once("close", () => signal?.removeEventListener("abort", abort));
    request.once("error", reject);
    markSubmitted();
    request.end(payload);
  });
}

export class ChatGptWebWorker {
  constructor({
    endpoint = process.env.CHATGPT_WEB_ENDPOINT || DEFAULT_RESPONSES_ENDPOINT,
    transport = postJsonTransport,
    loadControlConfig = loadLocalControlConfig,
  } = {}) {
    this.endpoint = endpoint;
    this.transport = transport;
    this.loadControlConfig = loadControlConfig;
  }

  async interruptTurn(body) {
    let metadata = body?.client_metadata?.["x-codex-turn-metadata"];
    try { if (typeof metadata === "string") metadata = JSON.parse(metadata); } catch { metadata = null; }
    const threadId = typeof metadata?.thread_id === "string" ? metadata.thread_id.trim() : "";
    const turnId = typeof metadata?.turn_id === "string" ? metadata.turn_id.trim() : "";
    if (!/^[A-Za-z0-9_-]{6,128}$/.test(threadId) || !/^[A-Za-z0-9_-]{6,128}$/.test(turnId)) {
      throw new SwitchboardError("WEB_INTERRUPT_IDENTITY_UNAVAILABLE", "Explicit Web interruption requires canonical native thread and turn metadata.", 409);
    }
    let endpoint;
    let config;
    try {
      endpoint = new URL(this.endpoint);
      config = await this.loadControlConfig();
      if (endpoint.protocol !== "http:" || endpoint.hostname !== "127.0.0.1"
          || config.host !== endpoint.hostname || Number(endpoint.port || 80) !== config.port
          || typeof config.controlToken !== "string" || !config.controlToken) throw new Error("control identity mismatch");
      endpoint.pathname = "/admin/interrupt-turn";
      endpoint.search = "";
      endpoint.hash = "";
      if (endpoint.username || endpoint.password) throw new Error("unexpected URL credentials");
    } catch {
      throw new SwitchboardError("WEB_INTERRUPT_CONTROL_UNAVAILABLE", "The existing local Web control endpoint could not be verified.", 409);
    }
    const payload = Buffer.from(JSON.stringify({ threadId, turnId }));
    let response;
    let result;
    try {
      response = await this.transport({
        endpoint: endpoint.toString(), payload,
        headers: { "content-type": "application/json", "content-length": String(payload.length), authorization: `Bearer ${config.controlToken}` },
        markSubmitted: () => {}, signal: AbortSignal.timeout(2_000),
      });
      result = JSON.parse((await readBody(response.body)).toString("utf8"));
    } catch {
      throw new SwitchboardError("WEB_INTERRUPT_DELIVERY_UNSETTLED", "Explicit Web interruption was not acknowledged; it was not retried.", 409);
    }
    if (response.status !== 200 || result?.status !== "ok") {
      throw new SwitchboardError("WEB_INTERRUPT_REJECTED", "The local Web control endpoint did not accept interruption.", 409);
    }
    return result;
  }

  async #request(body, { deliveryId, signal, endpoint = this.endpoint }) {
    if (!endpoint) {
      throw new SwitchboardError(
        "WEB_PROVIDER_UNAVAILABLE",
        "CHATGPT_WEB_ENDPOINT is not configured; no Web turn was submitted.",
      );
    }
    const outboundIdentity = `chatgpt-web:${deliveryId}`;
    const payload = Buffer.from(JSON.stringify(body));
    let submitted = false;
    try {
      const response = await this.transport({
        endpoint,
        payload,
        headers: {
          accept: "application/json, text/event-stream",
          "content-type": "application/json",
          "content-length": String(payload.length),
          "idempotency-key": outboundIdentity,
        },
        markSubmitted: () => { submitted = true; },
        signal,
      });
      if (response.status < 200 || response.status >= 300) {
        const rawError = await readBody(response.body).catch(() => Buffer.alloc(0));
        const downstream = downstreamError(rawError);
        // A completed HTTP response gives us a settled delivery result. Preserve
        // deterministic client errors and map server errors to a non-retryable
        // conflict so Codex never replays an exactly-once browser turn.
        const status = response.status >= 400 && response.status < 500
          ? response.status
          : 409;
        const error = new SwitchboardError(
          response.status >= 400 && response.status < 500
            ? "WEB_REQUEST_REJECTED"
            : "WEB_UPSTREAM_REJECTED",
          downstream.message
            ? `ChatGPT Web rejected the request: ${downstream.message}`
            : `ChatGPT Web downstream returned HTTP ${response.status}.`,
          status,
          {
            outbound_identity: outboundIdentity,
            downstream_status: response.status,
            ...(downstream.code ? { downstream_code: downstream.code } : {}),
            retry_allowed: false,
            fallback_allowed: false,
          },
        );
        error.deliveryKnown = true;
        throw error;
      }
      return { response, outboundIdentity };
    } catch (error) {
      if (signal?.aborted || error?.code === "ABORT_ERR") {
        throw new SwitchboardError("WEB_REQUEST_CANCELLED", "ChatGPT Web request was cancelled.", 499);
      }
      if (submitted && error?.deliveryKnown !== true) {
        throw new SwitchboardError(
          "WEB_DELIVERY_UNCERTAIN",
          "ChatGPT Web delivery may have been submitted but no terminal response was observed; it was not retried or substituted.",
          502,
          { outbound_identity: outboundIdentity, retry_allowed: false, fallback_allowed: false },
        );
      }
      if (error instanceof SwitchboardError) throw error;
      throw new SwitchboardError("WEB_PROVIDER_UNAVAILABLE", "ChatGPT Web failed before submission.");
    }
  }

  async runResponses(body, {
    deliveryId = randomUUID(),
    onEvent = null,
    signal = null,
  } = {}) {
    const directBody = structuredClone(body);
    directBody.stream = true;
    let outboundIdentity = `chatgpt-web:${deliveryId}`;
    let terminalObserved = false;
    let providerResponseId = null;
    try {
      const requested = await this.#request(directBody, { deliveryId, signal });
      outboundIdentity = requested.outboundIdentity;
      if (!requested.response.body || !contentType(requested.response.headers).includes("text/event-stream")) {
        await readBody(requested.response.body).catch(() => Buffer.alloc(0));
        throw new Error("ChatGPT Web downstream returned a non-streaming response.");
      }
      const decoder = new SseDecoder();
      const consume = (event) => {
        const responseId = event?.response?.id || event?.response_id;
        if (typeof responseId === "string" && responseId) providerResponseId ||= responseId;
        onEvent?.(event);
        if (TERMINAL_EVENTS.has(event?.type)) terminalObserved = true;
      };
      for await (const chunk of bodyChunks(requested.response.body)) {
        if (signal?.aborted) throw Object.assign(new Error("ChatGPT Web request cancelled."), { code: "ABORT_ERR" });
        for (const event of decoder.push(chunk)) consume(event);
      }
      for (const event of decoder.finish()) consume(event);
      if (!terminalObserved) throw new Error("ChatGPT Web stream ended without a terminal event.");
      return {
        status: 200,
        outbound_identity: outboundIdentity,
        provider_response_id: providerResponseId,
      };
    } catch (error) {
      if (signal?.aborted || error?.code === "ABORT_ERR") {
        throw new SwitchboardError("WEB_REQUEST_CANCELLED", "ChatGPT Web request was cancelled.", 499);
      }
      if (!terminalObserved
          && error?.code !== "WEB_PROVIDER_UNAVAILABLE"
          && error?.code !== "WEB_REQUEST_REJECTED"
          && error?.code !== "WEB_UPSTREAM_REJECTED") {
        throw new SwitchboardError(
          "WEB_DELIVERY_UNCERTAIN",
          "ChatGPT Web delivery may have been submitted but no terminal response was observed; it was not retried or substituted.",
          502,
          { outbound_identity: outboundIdentity, retry_allowed: false, fallback_allowed: false },
        );
      }
      throw error;
    }
  }

  async compact(body, { deliveryId = randomUUID(), signal = null } = {}) {
    const compactBody = structuredClone(body);
    compactBody.stream = false;
    compactBody.store = false;
    const requested = await this.#request(compactBody, {
      deliveryId,
      signal,
      endpoint: compactEndpoint(this.endpoint),
    });
    try {
      const parsed = JSON.parse((await readBody(requested.response.body)).toString("utf8"));
      const items = Array.isArray(parsed.output) ? parsed.output : [];
      // Unary /responses/compact returns v1 replacement history, not a v2 stream item.
      if (!items.some(item => item?.type === "compaction")) {
        const last = items.at(-1);
        const validHistory = items.length > 0 && items.every(item =>
          item?.type === "message" && item.role === "user" && Array.isArray(item.content));
        const block = last?.content?.[0];
        const prefix = `${WEB_SUMMARY_PREFIX}\n`;
        if (validHistory && last.content.length === 1 && block?.type === "input_text"
            && typeof block.text === "string" && block.text.startsWith(prefix)) {
          const summary = block.text.slice(prefix.length).trim();
          if (summary && !parsed.error && (parsed.status === undefined || parsed.status === "completed")) {
            // Preserve the producer's exact checkpoint when native v2 replays the canonical wrapper.
            return { summary, provider_item: last, replacement_history: items, usage: parsed.usage || {} };
          }
        }
        throw new Error("ChatGPT Web compaction returned invalid v1 replacement history.");
      }
      const providerItem = items.find((item) => item?.type === "compaction");
      if (!providerItem || typeof providerItem.encrypted_content !== "string"
          || !providerItem.encrypted_content.startsWith(WEB_COMPACTION_PREFIX)) {
        throw new Error("ChatGPT Web compaction returned no decodable compaction item.");
      }
      const summary = Buffer.from(
        providerItem.encrypted_content.slice(WEB_COMPACTION_PREFIX.length),
        "base64",
      ).toString("utf8").trim();
      if (!summary) throw new Error("ChatGPT Web compaction returned an empty summary.");
      return { summary, provider_item: providerItem, usage: parsed.usage || {} };
    } catch (error) {
      if (error instanceof SwitchboardError) throw error;
      throw new SwitchboardError(
        "WEB_COMPACTION_INVALID",
        "ChatGPT Web compaction returned an invalid terminal payload; it was not retried.",
      );
    }
  }
}

function send(message) {
  if (process.send) process.send(message);
}

const activeSessions = new Map();
const activeWork = new Set();
let terminating = false;

function registerSession(sessionKey, controller) {
  const controllers = activeSessions.get(sessionKey) || new Set();
  controllers.add(controller);
  activeSessions.set(sessionKey, controllers);
}

function unregisterSession(sessionKey, controller) {
  const controllers = activeSessions.get(sessionKey);
  if (!controllers) return;
  controllers.delete(controller);
  if (controllers.size === 0) activeSessions.delete(sessionKey);
}

if (typeof process.send === "function") process.on("message", (job) => {
  if (!job || typeof job.id !== "string") return;
  if (job.op === "close_session") {
    for (const controller of activeSessions.get(job.session_key) || []) controller.abort();
    send({ id: job.id, kind: "done", result: { status: 200 } });
    return;
  }
  if (job.op === "interrupt_turn") {
    // Snapshot controllers before awaiting the exact control call so a later session reuse is untouched.
    const controllers = [...activeSessions.get(job.session_key) || []];
    void worker.interruptTurn(job.body).then(
      result => send({ id: job.id, kind: "done", result }),
      error => send({ id: job.id, kind: "error", error: safeError(error.code, error.message, error.status) }),
    ).finally(() => { for (const controller of controllers) controller.abort(); });
    return;
  }
  if (job.op !== "responses" && job.op !== "compact") {
    send({
      id: job.id,
      kind: "error",
      error: safeError("WEB_OPERATION_UNSUPPORTED", `ChatGPT Web does not support ${job.op}.`, 400),
    });
    return;
  }
  const sessionKey = job.session_key || job.id;
  const controller = new AbortController();
  registerSession(sessionKey, controller);
  const work = job.op === "compact"
    ? worker.compact(job.body, { deliveryId: job.id, signal: controller.signal })
    : worker.runResponses(job.body, {
        deliveryId: job.id,
        signal: controller.signal,
        onEvent: (event) => send({ id: job.id, kind: "event", event }),
      });
  const completion = Promise.resolve(work).then(
    (result) => send({ id: job.id, kind: "done", result }),
    (error) => send({
      id: job.id,
      kind: "error",
      error: safeError(error.code || "WEB_WORKER_ERROR", error.message || "ChatGPT Web worker failed.", error.status || 502),
    }),
  ).finally(() => unregisterSession(sessionKey, controller));
  activeWork.add(completion);
  completion.finally(() => activeWork.delete(completion));
});

const worker = new ChatGptWebWorker();

process.on("SIGTERM", () => {
  if (terminating) return;
  terminating = true;
  for (const controllers of activeSessions.values()) {
    for (const controller of controllers) controller.abort();
  }
  const guard = setTimeout(() => process.exit(1), 5_000);
  guard.unref?.();
  Promise.allSettled([...activeWork]).then(() => {
    clearTimeout(guard);
    process.exit(0);
  });
});
