import cluster from "node:cluster";
import http from "node:http";
import https from "node:https";
import { createHash, randomUUID } from "node:crypto";
import { readFile, stat } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { performance } from "node:perf_hooks";
import { BlockList } from "node:net";

import WebSocket, { WebSocketServer } from "ws";

const runningSourceSha256 = createHash("sha256").update(await readFile(fileURLToPath(import.meta.url))).digest("hex");

import {
  ADMISSION_PATH,
  CATALOG_PATH,
  RUNTIME_PATH,
} from "./catalog.mjs";
import {
  EphemeralProviderState,
  createCanonicalCompaction,
  normalizeCompactions,
} from "./history-neutral.mjs";
import {
  DEFAULT_HOST,
  DEFAULT_PORT,
  MAX_REQUEST_BYTES,
  OFFICIAL_BASE_PATH,
  OFFICIAL_ORIGIN,
  SwitchboardError,
  assertExternalRequestAllowed,
  encodeJsonBody,
  endpointFromPath,
  errorPayload,
  externalRequestBody,
  externalThreadSessionKey,
  newRequestId,
  officialErrorObservation,
  officialRequestHeaders,
  officialResponseHeaders,
  officialResponseObservation,
  officialRequestCorrelation,
  parseJsonBody,
  readRawBody,
  responseIdFromEvents,
  responseOutputItems,
  safeLog,
  sendJson,
  sendTypedError,
  sseFrame,
} from "./shared.mjs";
import { IsolatedWorkerClient } from "./worker-client.mjs";
import { createInferenceAttemptEvidence } from "./inference-attempt-evidence.mjs";
import { requireExternalAdmission, requireNokiyRouteAdmission } from "./external-admission.mjs";
import { HOP_HEADER as CLAUDE_HOP_HEADER, loadClaudeBridge } from "./claude-bridge-route.mjs";
import { routeWithCurrentOfficialModels } from "./official-catalog-refresh.mjs";
import { projectImageModel } from "./image-model-route.mjs";
import {
  createExternalEventBridge,
  externalToolShape,
  flattenTools,
  normalizeExternalItemsForOfficial,
} from "./tool-bridge.mjs";
import { rememberSpawnPayloads } from "./spawn-payload-cache.mjs";
import { elideNativeStaleAppshots, projectNokiyDirect, trimNativePrediction } from "./nokiy-direct-route.mjs";
import { admittedHostSnapshot, nativeToolItem, nativeEventProjector, nativeSseProjector, verifyNativeContract, verifyNativeInterpreter, continueNativeObservation, userMessageTexts } from "./nokiy-native-tool.mjs";
import { bindPendingResultWait, resultContinuationCall, prepareResultDeliveryDiagnostic } from "./nokiy-result-delivery.mjs";
import { latestInputIsOperatorTurn, nativeRunAfterResponse, nativeRunReminder } from "./nokiy-workflow-profiles.mjs";
import { coordinatorProjection } from "./nokiy-coordinator-policy.mjs";
import { startStdoutLogRotation } from "./log-rotation.mjs";
import { itemHashes, itemKinds, rememberChain, replayDivergence, templateDiff } from "./replay-diagnostic.mjs";
import { sharedReplayRestorer } from "./nokiy-replay-restore.mjs";

// Cost hints only: never use these bindings for execution permission or projection.
export function nativeRunControlBindings(body, previous = null) {
  const catalogKnown = previous ? previous.catalogKnown !== false : !body.previous_response_id;
  try {
    const input = (Array.isArray(body.input) ? body.input : []).filter((item) =>
      ["additional_tools", "tool_search_output"].includes(item?.type));
    const tools = new Map((previous?.tools ?? []).map((item) => [JSON.stringify(item), item]));
    for (const item of flattenTools(body.tools, input).bridge.values()) {
      if (item.adapter) continue;
      const identity = { type: item.responseType, name: item.responseName, namespace: item.namespace ?? null };
      tools.set(JSON.stringify(identity), identity);
    }
    const projected = coordinatorProjection({ tools: body.tools, input },
      { type: "custom", name: "nokiy_execute" }, previous?.bindings);
    const bindings = projected.bindings.filter((item) => item.name !== "nokiy_execute").map((item) =>
      Object.freeze({ ...item, unqualifiedUnique: catalogKnown && [...tools.values()].filter((tool) =>
        tool.type === item.type && tool.name === item.name).length === 1 }));
    return Object.freeze({ bindings: Object.freeze(bindings), tools: Object.freeze([...tools.values()]), catalogKnown });
  } catch {
    // Unknown history stays unknown for this bound chain, not a fresh empty catalog.
    return Object.freeze({ bindings: Object.freeze([]), tools: Object.freeze([]), catalogKnown: false });
  }
}

const TERMINAL_EVENTS = new Set(["response.completed", "response.failed", "response.incomplete", "error"]);
const TERMINAL_FAILURE_EVENTS = new Set(["response.failed", "error"]);
// Optional capped generating replays of the accepted prefix during long tool waits.
// Off by default: on 10-18 the log showed >=100K-token prefixes still 93% cached after
// 300-600 s idle and 91% after 600 s without refreshes, and refreshed chains hit at the
// same ~90% while 317 refreshes re-read 59M cached tokens. A positive MAX re-enables them.
const CACHE_KEEPALIVE_MS = Number(process.env.SWITCHBOARD_CACHE_KEEPALIVE_MS ?? 50_000);
const CACHE_KEEPALIVE_MAX = Number(process.env.SWITCHBOARD_CACHE_KEEPALIVE_MAX ?? 0);
const CACHE_KEEPALIVE_OUTPUT_TOKENS = Number(process.env.SWITCHBOARD_CACHE_KEEPALIVE_OUTPUT_TOKENS ?? 16);
const CACHE_KEEPALIVE_DONE = new Set(["response.completed", "response.incomplete"]);
const CACHE_KEEPALIVE_ITEMS = new Set(["function_call", "custom_tool_call", "local_shell_call"]);
// The official Responses WebSocket ends a connection 60 minutes after it opens and aborts any
// response then in progress (websocket_connection_limit_reached, observed mid-stream after
// 10-100 s of output). An aged connection is rotated before a request starts instead. The
// rotated full request is served from the shared prompt cache, which keeps a chained
// conversation's recent tail only briefly (a rotation after 10.5 idle minutes hit 79.7%), so
// rotate while the chain is warm, and unconditionally only close to the cutoff.
const UPSTREAM_ROTATE_MS = Number(process.env.SWITCHBOARD_UPSTREAM_ROTATE_MS ?? 55 * 60_000);
const UPSTREAM_ROTATE_WARM_MS = Number(process.env.SWITCHBOARD_UPSTREAM_ROTATE_WARM_MS ?? 40 * 60_000);
const UPSTREAM_WARM_IDLE_MS = Number(process.env.SWITCHBOARD_UPSTREAM_WARM_IDLE_MS ?? 120_000);
// Official generate:false is reported as full uncached input yet neither reads nor writes
// the prompt cache, so the next request is uncached anyway. Answer it locally through the
// normal event path; its successor carries the full chain input instead. "0" forwards it.
const syntheticOfficialPrewarm = () => process.env.SWITCHBOARD_SYNTHETIC_OFFICIAL_PREWARM !== "0";
// With store:false only reasoning items may carry ids (they replay via encrypted_content);
// other output ids are not persisted and make a full replay invalid. Ids are not rendered.
function replayableOutputItem(item) {
  if (!item || typeof item !== "object" || item.type === "reasoning" || !Object.hasOwn(item, "id")) return item;
  const { id: _id, ...rest } = item;
  return rest;
}
const LOOPBACK_NETWORKS = new BlockList();
LOOPBACK_NETWORKS.addSubnet("127.0.0.0", 8, "ipv4");
LOOPBACK_NETWORKS.addAddress("::1", "ipv6");

export function webSocketCompressionEnabled(peer) {
  const host = String(peer || "").replace(/^\[|\]$/g, "").toLowerCase();
  return host !== "localhost" && !LOOPBACK_NETWORKS.check(host, host.includes(":") ? "ipv6" : "ipv4");
}

function externalTransport(route, compact = false) {
  if (route === "web") return compact ? "chatgpt-web-compact" : "chatgpt-web-http";
  if (route === "xai") return compact ? "xai-compact" : "xai-ws";
  if (route === "openrouter") return compact ? "openrouter-compact" : "openrouter-responses";
  return "agy-cli";
}

function logXaiToolShape({ requestId, body, transport }) {
  if (!["grok-4.6", "grok-4.7"].includes(body?.model)) return;
  safeLog({
    request_id: requestId,
    model: body.model,
    route: "xai",
    endpoint: "/responses",
    transport,
    diagnostic: "tool_shape",
    ...externalToolShape(body),
  });
}

function externalInputEnvelopeShape(input) {
  return (Array.isArray(input) ? input : [])
    .slice(-16)
    .map((item) => {
      const type = typeof item?.type === "string" ? item.type : typeof item;
      const role = typeof item?.role === "string" ? item.role : "-";
      const content = Array.isArray(item?.content) ? "array" : typeof item?.content;
      const keys = item && typeof item === "object"
        ? Object.keys(item).sort().slice(0, 12).join("+")
        : "-";
      return `${type}:${role}:${content}:${keys}`;
    })
    .join("|")
    .slice(0, 900);
}

function upstreamErrorShape(payload) {
  const error = payload?.error || payload?.detail?.error || payload?.detail;
  return [error?.type, error?.code, error?.param]
    .filter((value) => typeof value === "string" && value)
    .join(":") || undefined;
}

function headersForWebSocket(headers) {
  const blocked = new Set([
    "host",
    "connection",
    "upgrade",
    "content-length",
    "sec-websocket-key",
    "sec-websocket-version",
    "sec-websocket-extensions",
    "sec-websocket-protocol",
  ]);
  return Object.fromEntries(
    Object.entries(headers).filter(([name, value]) => value !== undefined && !blocked.has(name.toLowerCase())),
  );
}

function protocolsFromHeaders(headers) {
  const value = headers["sec-websocket-protocol"];
  if (!value) return [];
  return String(value).split(",").map((part) => part.trim()).filter(Boolean);
}

// Codex retries every error status except 400 (InvalidRequest), and once the retry budget is
// spent it disables WebSockets for the rest of the session. A switchboard validation or conflict
// error is deterministic, so report it as 400: the turn ends with the message instead of
// replaying the same request and pinning the session to HTTP full replays. Logs keep the status.
// 409 stays retryable: a response-state conflict is resolved by the client's full-input retry.
const DETERMINISTIC_CLIENT_STATUSES = new Set([413, 415, 422]);
export function clientWireStatus(error) {
  if (!(error instanceof SwitchboardError)) return 502;
  return DETERMINISTIC_CLIENT_STATUSES.has(error.status) ? 400 : error.status;
}

function wrappedWebSocketError(error, requestId) {
  const payload = errorPayload(error, requestId);
  return {
    type: "error",
    status: clientWireStatus(error),
    error: payload.error,
  };
}

// Codex treats an SSE EOF without response.completed as
// "stream closed before response.completed", and it ignores a bare error
// event. response.failed is stored and reported when the body ends.
function externalResponseFailure(error, requestId, responseId) {
  const requestToken = String(requestId || "unknown").replace(/[^A-Za-z0-9_-]/g, "").slice(0, 80);
  const { error: failure } = errorPayload(error, requestId);
  return {
    type: "response.failed",
    response: {
      id: responseId || `resp_switchboard_${requestToken}`,
      status: "failed",
      error: {
        code: failure.code === "EXTERNAL_MANDATORY_CONTEXT_TOO_LARGE"
          ? "context_length_exceeded" : "server_error",
        message: `${failure.code}: ${failure.message}`,
      },
    },
  };
}

// The admitted catalog is read-only and about 1.4 MB; parse it once per file
// identity instead of on every Nokiy request (about 5 ms of event-loop time each).
let catalogMemo = null;
function deepFreeze(value) {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    Object.freeze(value);
    for (const entry of Object.values(value)) deepFreeze(entry);
  }
  return value;
}
async function readCatalog() {
  const info = await stat(CATALOG_PATH);
  const key = `${info.dev}:${info.ino}:${info.size}:${info.mtimeMs}`;
  if (catalogMemo?.key !== key) catalogMemo = { key, value: deepFreeze(JSON.parse(await readFile(CATALOG_PATH, "utf8"))) };
  return catalogMemo.value;
}

class AdmissionGate {
  constructor({ admission, admissionPath = ADMISSION_PATH, skipFileGate = false }) {
    this.admission = admission;
    this.admissionPath = admissionPath;
    this.skipFileGate = skipFileGate;
  }

  async current() {
    if (this.skipFileGate) return this.admission;
    try {
      return JSON.parse(await readFile(this.admissionPath, "utf8"));
    } catch {
      throw new SwitchboardError("CAPABILITY_MANIFEST_UNREADABLE", "The external model batch has no readable admission manifest.", 503);
    }
  }

  async requireExternal() {
    return requireExternalAdmission(await this.current());
  }
}

function normalizeForExternal(body, route, providerState) {
  const clean = route === "web" ? structuredClone(body) : externalRequestBody(body);
  const normalized = normalizeCompactions(clean.input, route, providerState.asMap());
  clean.input = normalized.input;
  return clean;
}

function normalizeOfficialRaw(raw, body, encoding, providerState) {
  if (!Array.isArray(body?.input)) return raw;
  const compacted = normalizeCompactions(body.input, "openai", providerState.asMap());
  const external = normalizeExternalItemsForOfficial(compacted.input);
  if (!compacted.changed && !external.changed) return raw;
  return encodeJsonBody({ ...body, input: external.input }, encoding);
}

async function proxyOfficialHttp({
  req,
  res,
  raw,
  endpoint,
  search,
  requestId,
  model,
  providerState,
  parsedBody,
  officialOrigin,
  officialBasePath,
  requestedAlias,
  nativeTool,
  resultDeliveryDiagnostic,
  receiptProjection,
  attempt,
}) {
  const started = Date.now();
  const encoding = req.headers["content-encoding"] || "identity";
  const outgoing = parsedBody ? normalizeOfficialRaw(raw, parsedBody, encoding, providerState) : raw;
  const rawSha256 = createHash("sha256").update(raw).digest("hex");
  const outgoingSha256 = outgoing === raw ? rawSha256 : createHash("sha256").update(outgoing).digest("hex");
  const headers = officialRequestHeaders(req.headers, outgoing.length);
  const requestCorrelation = officialRequestCorrelation(parsedBody, req.headers);
  if (nativeTool) headers["accept-encoding"] = "identity";
  let responseBytes = 0;
  let upstreamErrorCode;
  let upstreamResponse;
  let phase = "before_headers";
  let terminalCause;
  let downstreamCloseObserved = false;
  let forwardedTerminalEvent = null;
  let sseObservation = "not_sse";
  let providerObservation = null;
  let upstreamErrorObservation = null;
  let terminalParsed = false;
  let firstParsedTerminalEvent = null;
  let terminalWriteSucceeded = false;
  let failureWritePending = false;
  let upstreamEofObserved = false;
  let upstreamTransportWhileTerminalPending = null;
  let resultProjector = null;
  const diagnostic = () => ({
    phase,
    terminal_cause: terminalCause,
    downstream_close_observed: downstreamCloseObserved,
    downstream_destroyed: res.destroyed,
    downstream_ended: res.writableEnded,
    headers_sent: res.headersSent,
    upstream_response_received: Boolean(upstreamResponse),
    upstream_response_complete: Boolean(upstreamResponse?.complete),
    sse_terminal_forwarded: forwardedTerminalEvent,
    sse_observation: sseObservation,
    terminal_parsed: terminalParsed,
    terminal_write_succeeded: terminalWriteSucceeded,
    upstream_eof_observed: upstreamEofObserved,
    upstream_transport_while_terminal_pending: upstreamTransportWhileTerminalPending,
    upstream_error: upstreamErrorObservation,
    request_raw_sha256: rawSha256,
    request_outgoing_sha256: outgoingSha256,
    request_bytes: outgoing.length,
    response_bytes: responseBytes,
  });
  await new Promise((resolve, reject) => {
    let upstream;
    let onData;
    let onEnd;
    const settle = (cause, error) => {
      if (terminalCause) return;
      // Latch before destruction: its queued reset/close cannot replace the
      // observed cause, and cancellation never waits for an upstream event.
      terminalCause = cause;
      attempt?.finish({
        outcome: cause === "downstream_close" || cause === "downstream_error" ? "cancel"
          : error || (upstreamResponse?.statusCode || 0) >= 400
            || (firstParsedTerminalEvent && firstParsedTerminalEvent !== "response.completed") ? "failure"
            : providerObservation?.terminal_event === "response.completed" ? "success" : "unknown",
        terminalEvent: providerObservation?.terminal_event || firstParsedTerminalEvent,
        observation: providerObservation,
        errorCode: error?.code || upstreamErrorCode,
      });
      if (error) {
        error.officialHttpLifecycle = diagnostic();
        error.providerObservation = providerObservation;
        error.requestCorrelation = requestCorrelation;
      }
      res.off("close", onDownstreamClose);
      res.off("error", onDownstreamError);
      res.off("drain", onDrain);
      upstream?.off("response", onResponse);
      if (upstreamResponse) {
        upstreamResponse.unpipe(res);
        if (onData) upstreamResponse.off("data", onData);
        if (onEnd) upstreamResponse.off("end", onEnd);
      }
      // Keep only error/close guards until owned streams close, so errors
      // already queued by destroy are consumed without retaining listeners.
      if (cause !== "completed") {
        upstreamResponse?.destroy();
        upstream?.destroy();
      }
      if (error) reject(error);
      else resolve();
    };
    const deferPendingTerminalTransport = (event) => {
      if (!failureWritePending || terminalCause) return false;
      upstreamTransportWhileTerminalPending ||= event;
      return true;
    };
    const onUpstreamError = (error) => {
      if (!deferPendingTerminalTransport("upstream_error")) settle("upstream_error", error);
    };
    const onResponseError = (error) => {
      if (!deferPendingTerminalTransport("response_error")) settle("upstream_response_error", error);
    };
    const onUpstreamClose = () => {
      upstream.off("error", onUpstreamError);
      upstream.off("response", onResponse);
      if (!terminalCause && !upstreamResponse) {
        settle("upstream_close", new SwitchboardError("OFFICIAL_HTTP_INCOMPLETE", "Official upstream closed before response headers."));
      }
    };
    const onResponseClose = () => {
      upstreamResponse.off("error", onResponseError);
      // A complete failure is awaiting delivery, not an early upstream EOF.
      if (deferPendingTerminalTransport("response_close")) return;
      if (!terminalCause) {
        settle("upstream_response_close", new SwitchboardError("OFFICIAL_HTTP_INCOMPLETE", "Official upstream closed before response completion."));
      }
    };
    const onDownstreamClose = () => {
      downstreamCloseObserved = true;
      if (!res.writableEnded) {
        settle(forwardedTerminalEvent === "response.completed" ? "client_closed_after_completed"
          : forwardedTerminalEvent ? "client_closed_after_terminal" : "downstream_close");
      }
    };
    const onDownstreamError = () => {
      settle("downstream_error");
      res.destroy();
    };
    const onDrain = () => { if (!terminalCause) upstreamResponse?.resume(); };
    const writeChunk = (chunk, terminalEvent) => {
      if (!chunk.length) return;
      const terminalFailure = TERMINAL_FAILURE_EVENTS.has(terminalEvent) && terminalEvent === firstParsedTerminalEvent;
      if (terminalFailure) failureWritePending = true;
      const ready = res.write(chunk, (error) => {
        // A parsed frame is not delivery evidence until its local write succeeds.
        if (!error && terminalEvent && !forwardedTerminalEvent) {
          forwardedTerminalEvent = terminalEvent;
          terminalWriteSucceeded = true;
          if (terminalEvent !== "response.completed") {
            upstreamErrorCode = `OFFICIAL_${terminalEvent.replaceAll(".", "_").toUpperCase()}`;
          }
        }
        if (terminalFailure && !terminalCause) {
          failureWritePending = false;
          if (error) {
            settle("downstream_error");
            res.destroy();
          } else {
            // The whole terminal frame is locally forwarded before releasing
            // a held-open upstream; late transport errors cannot replace it.
            phase = "terminal_forwarded";
            settle("upstream_terminal_failure");
            res.end();
          }
        }
      });
      if (!ready && !terminalCause) upstreamResponse.pause();
    };
    const onResponse = (response) => {
      upstreamResponse = response;
      upstreamResponse.on("error", onResponseError);
      upstreamResponse.once("close", onResponseClose);
      if (terminalCause) { upstreamResponse.destroy(); return; }
      phase = "response_body";
      try {
        const errorChunks = [];
        const projected = nativeTool && (upstreamResponse.statusCode || 0) < 400;
        const contentType = String(upstreamResponse.headers["content-type"] || "");
        // Some official responses omit Content-Type. The explicit streaming
        // request still defines the wire format; compact remains a JSON response.
        const isSse = contentType.includes("text/event-stream")
          || (!contentType && endpoint === "/responses" && parsedBody?.stream === true);
        const identityEncoding = !upstreamResponse.headers["content-encoding"]
          || upstreamResponse.headers["content-encoding"] === "identity";
        sseObservation = isSse ? identityEncoding ? "observing" : "unsupported_content_encoding" : "not_sse";
        const outputHeaders = officialResponseHeaders(upstreamResponse.headers);
        if (projected) { delete outputHeaders["content-length"]; delete outputHeaders["content-encoding"]; }
        if (projected && isSse && !contentType) outputHeaders["content-type"] = "text/event-stream";
        res.writeHead(upstreamResponse.statusCode || 502, outputHeaders);
        const project = projected ? nativeEventProjector(nativeTool, { onProjected: sharedReplayRestorer().record }) : null;
        resultProjector = project;
        let chunkTerminalEvent = null;
        const observe = (event) => {
          for (const item of [event].flat()) {
            if (TERMINAL_EVENTS.has(item?.type)) {
              terminalParsed = true;
              firstParsedTerminalEvent ||= item.type;
              chunkTerminalEvent ||= item.type;
            }
          }
          return event;
        };
        const observeProvider = (event) => {
          attempt?.observeOutput?.(event);
          providerObservation ||= officialResponseObservation(event);
          upstreamErrorObservation ||= officialErrorObservation(event);
          return event;
        };
        const sse = projected && isSse ? nativeSseProjector((event) => observe(project(observeProvider(event)))) : null;
        // Observe pass-through SSE without rewriting its bytes or making parser
        // limitations a new transport failure. Compressed responses stay opaque.
        let observer = !projected && isSse && identityEncoding
          ? nativeSseProjector((event) => { observeProvider(event); observe(event); return null; }) : null;
        const jsonChunks = [];
        let jsonBytes = 0;
        onData = (chunk) => {
          if (terminalCause) return;
          chunkTerminalEvent = null;
          responseBytes += chunk.length;
          if ((upstreamResponse.statusCode || 0) >= 400
            && errorChunks.reduce((total, part) => total + part.length, 0) < 8_192) {
            errorChunks.push(chunk);
          }
          if (projected) {
            try {
              if (sse) writeChunk(sse.push(chunk), chunkTerminalEvent);
              else {
                jsonBytes += chunk.length;
                if (jsonBytes > MAX_REQUEST_BYTES) throw new SwitchboardError("NOKIY_RESPONSE_TOO_LARGE", "Official response exceeded the adapter bound.");
                jsonChunks.push(chunk);
              }
            } catch (error) { settle("projection_error", error); }
          } else {
            if (observer) {
              try { observer.push(chunk); }
              catch {
                observer = null;
                chunkTerminalEvent = null;
                sseObservation = "unparseable";
              }
            }
            writeChunk(chunk, chunkTerminalEvent);
          }
        };
        onEnd = () => {
          if (terminalCause) return;
          upstreamEofObserved = true;
          if (failureWritePending) return;
          phase = "response_end";
          try {
            if (projected) {
              if (sse) res.end(sse.end());
              else {
                const response = JSON.parse(Buffer.concat(jsonChunks).toString("utf8"));
                observeProvider({ type: `response.${response.status}`, response });
                res.end(JSON.stringify(project({ response }).response));
              }
            } else res.end();
          } catch (error) { settle(projected ? "projection_error" : "response_error", error); return; }
          try {
            const parsed = JSON.parse(Buffer.concat(errorChunks).toString("utf8"));
            upstreamErrorCode = upstreamErrorShape(parsed) || upstreamErrorCode;
          } catch { /* SSE terminal failures may already have an error code. */ }
          settle("completed");
        };
        upstreamResponse.on("data", onData);
        upstreamResponse.once("end", onEnd);
      } catch (error) { settle("response_error", error); }
    };
    res.once("close", onDownstreamClose);
    res.once("error", onDownstreamError);
    res.on("drain", onDrain);
    if (res.destroyed) { settle("downstream_close"); return; }
    try {
      const target = new URL(`${officialOrigin}${officialBasePath}${endpoint}${search}`);
      const requestImpl = target.protocol === "http:" ? http.request : https.request;
      attempt?.dispatch(model, { wire: outgoing, encoding, projection: receiptProjection });
      upstream = requestImpl(target, { method: req.method, headers });
      upstream.once("response", onResponse);
      upstream.on("error", onUpstreamError);
      upstream.once("close", onUpstreamClose);
      if (outgoing.length) upstream.write(outgoing);
      upstream.end();
    } catch (error) { settle("upstream_error", error); }
  });
  const downstreamClosed = terminalCause === "downstream_close" || terminalCause === "downstream_error";
  safeLog({
    request_id: requestId,
    model,
    requested_alias: requestedAlias,
    provider_response: providerObservation,
    result_delivery: resultProjector ? { ...resultProjector.resultDeliveryObservation(), ...resultDeliveryDiagnostic } : undefined,
    request_correlation: requestCorrelation,
    route: "openai",
    endpoint,
    transport: "http",
    status: downstreamClosed ? 499 : res.statusCode,
    latency_ms: Date.now() - started,
    request_bytes: outgoing.length,
    response_bytes: responseBytes,
    error_code: downstreamClosed ? "OFFICIAL_HTTP_DOWNSTREAM_CLOSED" : upstreamErrorCode,
    diagnostic: { official_http: diagnostic() },
  });
}

// What the worker reported about its transport and compaction, for the log.
function workerObservation(result) {
  return {
    worker_transport: typeof result?.transport === "string" ? result.transport : undefined,
    compacted: typeof result?.compacted === "boolean" ? result.compacted : undefined,
    compaction_scope: typeof result?.compaction_scope === "string" ? result.compaction_scope : undefined,
  };
}

// Grok and AGY keep one worker session per caller thread, as the Claude bridge
// does. Other routes, and callers that name no thread, stay per owner.
function externalSessionKey(route, threadKey, ownerKey) {
  return (route === "xai" || route === "agy") && threadKey ? threadKey : ownerKey;
}

async function streamExternalHttp({ res, worker, body, route, endpoint, requestId, requestBytes, threadKey = null }) {
  if (res.destroyed) return;
  const started = Date.now();
  let responseBytes = 0;
  let startedStream = false;
  let workerResult;
  const ownerKey = `http-${requestId}`;
  const sessionKey = externalSessionKey(route, threadKey, ownerKey);
  let release;
  // Closing the owner cancels this request's job; a thread session stays for
  // the thread's next request and ends on the worker's idle timeout.
  const releaseSession = () => (release ||= worker.closeSession(ownerKey).catch(() => {}));
  const onClose = () => {
    if (!res.writableEnded) void releaseSession();
  };
  res.once("close", onClose);
  const eventBridge = createExternalEventBridge(route);
  let responseId = eventBridge.publicResponseId;
  let terminalEvent = null;
  const onEvent = (providerEvent) => {
    if (res.destroyed || terminalEvent) return;
    let event = eventBridge.transform(providerEvent);
    if (!event) return;
    responseId ||= event.response?.id || event.response_id;
    if (event.type === "error") {
      event = externalResponseFailure(new SwitchboardError(
        event.error?.code || "EXTERNAL_WORKER_ERROR",
        event.error?.message || "External worker returned an error event.",
        event.status || 502,
      ), requestId, responseId);
    }
    if (TERMINAL_EVENTS.has(event.type)) terminalEvent = event.type;
    rememberSpawnPayloads(event);
    if (!startedStream) {
      res.writeHead(200, {
        "content-type": "text/event-stream",
        "cache-control": "no-cache, no-store",
        connection: "keep-alive",
      });
      startedStream = true;
    }
    const frame = sseFrame(event);
    responseBytes += Buffer.byteLength(frame);
    res.write(frame);
  };
  try {
    workerResult = await worker.runResponses(body, { sessionKey, ownerKey, onEvent });
    if (!startedStream) throw new SwitchboardError("INVALID_EXTERNAL_EVENT", "External worker completed without Responses events.");
    if (!terminalEvent) throw new SwitchboardError("EXTERNAL_STREAM_INCOMPLETE", "External worker ended without a terminal Responses event.");
    res.end();
    safeLog({
      request_id: requestId,
      model: body.model,
      route,
      endpoint,
      transport: externalTransport(route),
      status: 200,
      latency_ms: Date.now() - started,
      request_bytes: requestBytes,
      response_bytes: responseBytes,
      ...workerResult?.tool_projection,
      ...workerObservation(workerResult),
      session_scope: sessionKey === ownerKey ? "request" : "thread",
    });
  } catch (error) {
    if (!startedStream) throw error;
    if (!res.destroyed && !terminalEvent) {
      const frame = sseFrame(externalResponseFailure(error, requestId, responseId));
      responseBytes += Buffer.byteLength(frame);
      terminalEvent = "response.failed";
      res.end(frame);
    } else if (!res.destroyed) {
      res.end();
    }
    safeLog({
      request_id: requestId,
      model: body.model,
      route,
      endpoint,
      transport: externalTransport(route),
      status: error.status || 502,
      latency_ms: Date.now() - started,
      request_bytes: requestBytes,
      response_bytes: responseBytes,
      error_code: error.code || "EXTERNAL_WORKER_ERROR",
    });
  } finally {
    res.off("close", onClose);
    await releaseSession();
  }
}

function boundedHistorySet(history, id, value, limit = 64) {
  if (!id) return;
  history.delete(id);
  history.set(id, value);
  while (history.size > limit) history.delete(history.keys().next().value);
}

function agyDeltaInput(input, prior) {
  if (!Array.isArray(input) || input.length === 0 || !prior) return false;
  const canonical = [
    ...(Array.isArray(prior.input) ? prior.input : []),
    ...(Array.isArray(prior.output) ? prior.output : []),
  ];
  if (canonical.length === 0) return true;
  return JSON.stringify(input[0]) !== JSON.stringify(canonical[0]);
}

function fullExternalInput(body, history) {
  if (!body.previous_response_id) return Array.isArray(body.input) ? body.input : [];
  const prior = history.get(body.previous_response_id);
  if (!prior) {
    if (Array.isArray(body.input) && body.input.length) return body.input;
    throw new SwitchboardError(
      "CROSS_ROUTE_RESPONSE_STATE_REJECTED",
      "The external previous_response_id is unavailable; retry with full canonical history.",
      409,
    );
  }
  return [...prior.input, ...prior.output, ...(Array.isArray(body.input) ? body.input : [])];
}

function syntheticPrewarmEvents(route) {
  const id = `resp_${route}_prewarm_${randomUUID()}`;
  return [
    { type: "response.created", response: { id } },
    {
      type: "response.completed",
      response: {
        id,
        output: [],
        // Explicit zero details: no input was processed, so accounting sums stay known.
        usage: { input_tokens: 0, input_tokens_details: { cached_tokens: 0 }, output_tokens: 0,
          output_tokens_details: { reasoning_tokens: 0 }, total_tokens: 0 },
      },
    },
  ];
}

// Where an untyped exception was thrown: its name and first stack frames only.
// Messages can quote request text, so they are not logged.
export function untypedErrorDiagnostic(error) {
  if (error instanceof SwitchboardError) return {};
  return {
    name: typeof error?.name === "string" ? error.name : typeof error,
    site: String(error?.stack || "").split("\n").slice(1, 3)
      .map((line) => line.trim()).filter((line) => line.startsWith("at ")),
  };
}

function projectionRejectionDiagnostic(error) {
  if (!(error instanceof SwitchboardError)) return {};
  if (["EXTERNAL_MANDATORY_CONTEXT_TOO_LARGE", "AGY_PROMPT_BYTE_LIMIT"].includes(error.code)) {
    const budget = Object.fromEntries([
      "max_bytes", "mandatory_bytes", "prompt_bytes", "validated_prefix_items", "new_items",
    ].filter((key) => Number.isSafeInteger(error.details?.[key]) && error.details[key] >= 0)
      .map((key) => [key, error.details[key]]));
    if (/^[a-f0-9]{64}$/.test(error.details?.current_request_sha256 || ""))
      budget.current_request_sha256 = error.details.current_request_sha256;
    return { external_budget: budget };
  }
  if (error.code === "NOKIY_COORDINATOR_TOOL_FORBIDDEN") {
    return { coordinator_rejection: error.details?.coordinator_rejection };
  }
  if (error.code === "NOKIY_DIRECT_NATIVE_TOOL_REQUIRED") {
    return { native_tool_missing: error.details?.native_tool_missing };
  }
  if (error.code === "NOKIY_NATIVE_OBSERVATION_INVALID") {
    return { native_observation: error.details?.native_observation };
  }
  return {};
}

function isRemoteCompactionV2(body) {
  return Array.isArray(body?.input)
    && body.input.some((item) => item?.type === "compaction_trigger");
}

// Log-only: Codex labels internal and spawned sessions (guardian, collab_spawn, ...).
function subagentLabel(body, headers) {
  const value = body?.client_metadata?.["x-openai-subagent"] ?? headers?.["x-openai-subagent"];
  return typeof value === "string" && /^[A-Za-z0-9_.:-]{1,64}$/.test(value) ? value : null;
}

// Log-only: content fingerprints of the leading prefix items without their per-thread ids,
// so a forked thread's prefix can be compared with its parent's. Never logs content.
function prefixFingerprints(input) {
  if (!Array.isArray(input)) return null;
  return input.slice(0, 4).map((item) => {
    const { id: _id, ...rest } = item && typeof item === "object" ? item : { value: item };
    return `${item?.type ?? typeof item}:${item?.role ?? ""}:${createHash("sha256").update(JSON.stringify(rest)).digest("hex").slice(0, 12)}${item?.id ? "+id" : ""}`;
  });
}

function normalizedUsage(usage = {}) {
  const input = Number(usage.input_tokens || 0);
  const output = Number(usage.output_tokens || 0);
  return {
    input_tokens: input,
    input_tokens_details: usage.input_tokens_details || null,
    output_tokens: output,
    output_tokens_details: usage.output_tokens_details || null,
    total_tokens: Number(usage.total_tokens || input + output),
  };
}

async function externalCompactionEvents({ worker, body, route, providerState, sessionKey }) {
  const result = await worker.compact(body, { sessionKey });
  const canonical = createCanonicalCompaction({ model: body.model, route, summary: result.summary });
  if (result.provider_item) providerState.set(canonical.stateKey, { item: result.provider_item });
  const responseId = `resp_compaction_switchboard_${randomUUID()}`;
  return [
    { type: "response.created", response: { id: responseId } },
    {
      type: "response.output_item.done",
      output_index: 0,
      item: canonical.item,
    },
    {
      type: "response.completed",
      response: {
        id: responseId,
        output: [canonical.item],
        usage: normalizedUsage(result.usage),
      },
    },
  ];
}

async function streamExternalCompactionHttp({
  res,
  worker,
  body,
  route,
  providerState,
  requestId,
  requestBytes,
}) {
  const started = Date.now();
  const sessionKey = `http-compact-v2-${requestId}`;
  let responseBytes = 0;
  try {
    const events = await externalCompactionEvents({ worker, body, route, providerState, sessionKey });
    res.writeHead(200, {
      "content-type": "text/event-stream",
      "cache-control": "no-cache, no-store",
      connection: "keep-alive",
    });
    for (const event of events) {
      const frame = sseFrame(event);
      responseBytes += Buffer.byteLength(frame);
      res.write(frame);
    }
    res.end();
    safeLog({
      request_id: requestId,
      model: body.model,
      route,
      endpoint: "/responses",
      transport: `${route}-compact-v2`,
      status: 200,
      latency_ms: Date.now() - started,
      request_bytes: requestBytes,
      response_bytes: responseBytes,
    });
  } finally {
    await worker.closeSession(sessionKey).catch(() => {});
  }
}

export async function createSwitchboardServer({
  runtime,
  admission,
  xaiWorker = null,
  agyWorker = null,
  openrouterWorker = null,
  webWorker = null,
  modelCatalog = null,
  workflowProfiles = null,
  skipFileGate = false,
  officialOrigin = OFFICIAL_ORIGIN,
  officialBasePath = OFFICIAL_BASE_PATH,
  claudeBridge = null,
} = {}) {
  const claude = claudeBridge;
  if (!runtime?.installation_capability) throw new Error("switchboard runtime capability is missing");
  if (!Array.isArray(admission?.official_models)) throw new Error("switchboard admission manifest is missing official models");
  const officialModels = new Set(admission.official_models);
  const gate = new AdmissionGate({ admission, skipFileGate });
  // Publish the admitted host snapshot at start, before any later edit of the working copy.
  if (admission.local_nokiy_direct?.admitted === true) {
    admittedHostSnapshot(admission.local_nokiy_direct.host).catch((error) => safeLog({ route: "nokiy", status: 503,
      error_code: error.code || "NOKIY_HOST_SNAPSHOT_UNAVAILABLE", diagnostic: { event: "host_snapshot_publication" } }));
  }
  const inferenceEvidence = createInferenceAttemptEvidence();
  const projectDirect = async (body, endpoint = "/responses", continuationTarget = null, continuationCall = null, continuationCoordinatorBindings = null, continuationPendingDelivery = null, continuationObservation = null, operatorTexts = null, runReminder = null, continuationFallback = null) => {
    if (typeof body?.model !== "string" || !/^nokiy(?:$|[-/])/.test(body.model)) return null;
    const current = requireNokiyRouteAdmission(await gate.current());
    const admitted = current.local_nokiy_direct?.admitted === true ? current.local_nokiy_direct.host : null;
    if (!admitted) throw new SwitchboardError("NOKIY_DIRECT_NOT_ADMITTED", "Nokiy Direct has not completed deployment admission.", 503);
    // Requests run the host from its admitted immutable snapshot, so working-copy edits never reach them.
    const host = await admittedHostSnapshot(admitted);
    const actual = createHash("sha256").update(await readFile(host.script)).digest("hex");
    if (actual !== host.sha256) throw new SwitchboardError("NOKIY_NATIVE_HOST_CHANGED", "Nokiy native host bytes changed after admission.", 503);
    verifyNativeContract(host);
    const catalog = modelCatalog || await readCatalog();
    const profiles = workflowProfiles ?? current.local_nokiy_direct.profiles;
    const projected = projectNokiyDirect(body, { endpoint, host, catalog, continuationTarget, continuationCall, continuationCoordinatorBindings, continuationPendingDelivery, continuationFallback, operatorTexts, profiles, runReminder,
      replayRestore: sharedReplayRestorer().restore, recordedNativeCall: sharedReplayRestorer().originalCall });
    const insertion = projected?.nativeTool?.deliveryInsertion;
    if (insertion?.anchor) sharedReplayRestorer().recordInsertion(insertion.anchor, insertion.items);
    // Compact/prewarm requests do not launch a worker and must not depend on its interpreter.
    if (projected?.dispatch === "native-tool") verifyNativeInterpreter(host);
    if (projected?.nativeTool) projected.nativeTool = continueNativeObservation(projected.nativeTool, continuationObservation);
    if (projected?.nativeTool) {
      // Ineligible calls retain the restored original pair; eligible calls have
      // an appended delivery note, so use the unchanged execution input instead.
      const sourceBody = projected.nativeTool.resultDelivery || projected.nativeTool.runReminder
        ? projected.nativeTool.observationBody : projected.nativeTool.body;
      projected.resultDeliveryDiagnostic = prepareResultDeliveryDiagnostic(sourceBody, continuationCall,
        (item) => nativeToolItem(item, projected.nativeTool));
    }
    return projected;
  };
  const providerState = new EphemeralProviderState();
  const ownedWorkers = [];
  if (!xaiWorker) {
    xaiWorker = new IsolatedWorkerClient({
      script: fileURLToPath(new URL("./xai-worker.mjs", import.meta.url)),
      label: "xAI",
    });
    ownedWorkers.push(xaiWorker);
  }
  if (!agyWorker) {
    agyWorker = new IsolatedWorkerClient({
      script: fileURLToPath(new URL("./agy-worker.mjs", import.meta.url)),
      label: "AGY",
    });
    ownedWorkers.push(agyWorker);
  }
  if (!openrouterWorker) {
    openrouterWorker = new IsolatedWorkerClient({
      script: fileURLToPath(new URL("./openrouter-worker.mjs", import.meta.url)),
      label: "OpenRouter",
    });
    ownedWorkers.push(openrouterWorker);
  }
  if (!webWorker) {
    webWorker = new IsolatedWorkerClient({
      script: fileURLToPath(new URL("./chatgpt-web-worker.mjs", import.meta.url)),
      label: "ChatGPT Web",
    });
    ownedWorkers.push(webWorker);
  }
  const workerForRoute = (route) => {
    if (route === "web") return webWorker;
    if (route === "xai") return xaiWorker;
    if (route === "openrouter") return openrouterWorker;
    return agyWorker;
  };

  const activeHttp = new Set();
  const socketTurns = new Map();
  const activeRelays = new Set();
  let draining = false;
  const activity = () => ({
    active_http_requests: activeHttp.size,
    active_ws_turns: [...socketTurns.values()].filter((isActive) => isActive()).length,
    websocket_connections: socketTurns.size,
    active_relay_connections: activeRelays.size,
  });

  const server = http.createServer(async (req, res) => {
    const requestId = newRequestId();
    const started = Date.now();
    let requestModel = null;
    let requestEndpoint = null;
    let requestAttempt = null;
    if (req.method === "POST") {
      activeHttp.add(requestId);
      const finished = () => activeHttp.delete(requestId);
      res.once("finish", finished);
      res.once("close", finished);
    }
    try {
      const url = new URL(req.url, `http://${req.headers.host || "127.0.0.1"}`);
      if (req.method === "GET" && url.pathname === "/health") {
        return sendJson(res, 200, { status: "ok", activity: activity() });
      }
      if (req.method === "GET" && url.pathname === `/${runtime.installation_capability}/ready`) {
        const current = await gate.current();
        const dependency = claude?.dependencyReadiness
          ? await claude.dependencyReadiness()
          : { status: claude ? "UNKNOWN" : "DISABLED" };
        return sendJson(res, current.status === "ADMITTED" ? 200 : 503, {
          status: current.status,
          catalog_sha256: current.catalog_sha256,
          cli_sha256: current.cli_sha256,
          adapter_source_sha256: runningSourceSha256,
          worker_pid: process.pid,
          supervisor_pid: cluster.isWorker ? process.ppid : null,
          workflow_release: current.local_nokiy_direct?.release || null,
          claude_route_source_sha256: claude?.route_source_sha256 || null,
          dependencies: { claude_bridge: dependency },
          attempt_lifecycle_checkpoint: inferenceEvidence.checkpoint(),
          activity: activity(),
        });
      }
      const endpoint = endpointFromPath(url.pathname, runtime.installation_capability);
      requestEndpoint = endpoint;
      const validMethod = (endpoint === "/models" && req.method === "GET")
        || (endpoint !== "/models" && req.method === "POST");
      if (!validMethod) throw new SwitchboardError("METHOD_NOT_ALLOWED", "Method is not allowed for this endpoint.", 405);
      if (endpoint === "/models") {
        await gate.requireExternal();
        const catalog = modelCatalog || await readCatalog();
        return sendJson(res, 200, claude ? claude.withCatalogRows(catalog) : catalog);
      }
      let raw = await readRawBody(req);
      const inboundWire = raw;
      const parsedBody = raw.length ? parseJsonBody(raw, req.headers["content-encoding"]) : null;
      rememberSpawnPayloads(parsedBody);
      const model = parsedBody?.model || null;
      requestModel = model;
      if (claude && !req.headers[CLAUDE_HOP_HEADER] && claude.models.has(model)) {
        if (endpoint !== "/responses") {
          throw new SwitchboardError("CLAUDE_ENDPOINT_UNAVAILABLE", `Endpoint ${endpoint} is not served for Claude models.`, 400, { endpoint });
        }
        const outcome = await claude.proxyHttp({ req, res, raw, endpoint, search: url.search });
        safeLog({
          request_id: requestId, model, route: "claude", endpoint, transport: "claude-bridge-http",
          status: outcome.status, latency_ms: Date.now() - started, request_bytes: raw.length,
          error_code: outcome.error_code,
          diagnostic: { terminal_cause: outcome.terminal_cause, upstream_http_status: outcome.upstream_http_status,
            upstream_terminal_event: outcome.upstream_terminal_event, transport_error: outcome.transport_error },
        });
        return;
      }
      if (claude && Array.isArray(parsedBody?.input)) {
        const prepared = claude.sanitize(parsedBody.input);
        if (prepared) {
          parsedBody.input = prepared.input;
          raw = encodeJsonBody(parsedBody, req.headers["content-encoding"]);
          safeLog({ request_id: requestId, model, route: "claude-history", endpoint,
            diagnostic: { claude_checkpoints_opened: prepared.checkpoints, claude_ids_removed: prepared.ids } });
        }
      }
      const nativePrediction = endpoint === "/responses" ? trimNativePrediction(parsedBody) : null;
      if (nativePrediction) {
        parsedBody.input = nativePrediction.input;
        raw = encodeJsonBody(parsedBody, req.headers["content-encoding"]);
        safeLog({ request_id: requestId, model, route: "prediction-trim", endpoint, transport: "http",
          diagnostic: { prediction_trim: nativePrediction.stats } });
      }
      const nativeAppshots = endpoint === "/responses"
        ? elideNativeStaleAppshots(parsedBody, (name) => officialModels.has(name)) : null;
      if (nativeAppshots) {
        parsedBody.input = nativeAppshots.input;
        raw = encodeJsonBody(parsedBody, req.headers["content-encoding"]);
        safeLog({ request_id: requestId, model, route: "stale-appshots", endpoint, transport: "http",
          diagnostic: { stale_appshots: nativeAppshots.stats } });
      }
      const direct = await projectDirect(parsedBody, endpoint);
      if (direct?.fullRequest) safeLog({ request_id: requestId, model: direct.body?.model ?? model, requested_alias: direct.requestedAlias ?? null,
        route: "replay-restore", transport: "http", diagnostic: { restored_calls: direct.replayRestored ?? 0,
          inserted_items: direct.replayInserted ?? 0,
          ...(direct.predictionTrim ? { prediction_trim: direct.predictionTrim } : {}),
          ...(direct.staleAppshots ? { stale_appshots: direct.staleAppshots } : {}),
          input_items: Array.isArray(parsedBody?.input) ? parsedBody.input.length : null, alignment: direct.cacheAlignment ?? null,
          subagent: subagentLabel(parsedBody, req.headers), prefix: prefixFingerprints(direct.body?.input) } });
      if (direct?.resultCompaction) safeLog({ request_id: requestId, model: direct.body?.model ?? model,
        route: "nokiy-result-compaction", transport: "http",
        diagnostic: { ...direct.resultCompaction, full_request: direct.fullRequest } });
      if (direct?.nativeToolMissing) safeLog({ request_id: requestId, model: direct.body?.model ?? model,
        requested_alias: direct.requestedAlias ?? null, route: "nokiy-native-tool-missing", transport: "http",
        diagnostic: { native_tool_missing: direct.nativeToolMissing, dispatch: direct.dispatch } });
      const imageRoute = projectImageModel(parsedBody, endpoint);
      const route = direct || imageRoute ? "openai" : model ? await routeWithCurrentOfficialModels(model, officialModels, () => gate.current()) : "openai";
      if (route === "openai") {
        // Scope starts only after route selection, not for arbitrary HTTP traffic.
        if (endpoint === "/responses") requestAttempt = inferenceEvidence.start({
          requestId, modelRequested: model, transport: "http",
          request: { wire: inboundWire, encoding: req.headers["content-encoding"] },
          correlation: officialRequestCorrelation(parsedBody, req.headers),
        });
        await proxyOfficialHttp({
          req,
          res,
          raw: direct || imageRoute ? encodeJsonBody(direct?.body || imageRoute.body, req.headers["content-encoding"]) : raw,
          endpoint,
          search: url.search,
          requestId,
          model: direct?.body.model || imageRoute?.body.model || model,
          requestedAlias: direct?.requestedAlias || imageRoute?.requestedModel,
          nativeTool: direct?.nativeTool,
          resultDeliveryDiagnostic: direct?.resultDeliveryDiagnostic,
          receiptProjection: direct,
          attempt: requestAttempt,
          providerState,
          parsedBody: direct?.body || parsedBody,
          officialOrigin,
          officialBasePath,
        });
        return;
      }
      await gate.requireExternal();
      assertExternalRequestAllowed(parsedBody, endpoint);
      const body = normalizeForExternal(parsedBody, route, providerState);
      const worker = workerForRoute(route);
      if (endpoint === "/responses") logXaiToolShape({ requestId, body, transport: "http" });
      if (endpoint === "/responses/compact") {
        const result = await worker.compact(body, { sessionKey: `compact-${requestId}` });
        const canonical = createCanonicalCompaction({ model, route, summary: result.summary });
        if (result.provider_item) providerState.set(canonical.stateKey, { item: result.provider_item });
        const payload = {
          id: `resp_compaction_switchboard_${randomUUID()}`,
          object: "response.compaction",
          created_at: Math.floor(Date.now() / 1000),
          model,
          output: route === "web" && Array.isArray(result.replacement_history)
            ? result.replacement_history
            : [canonical.item],
          usage: result.usage || {},
        };
        sendJson(res, 200, payload);
        safeLog({
          request_id: requestId,
          model,
          route,
          endpoint,
          transport: externalTransport(route, true),
          status: 200,
          latency_ms: Date.now() - started,
          request_bytes: raw.length,
          response_bytes: Buffer.byteLength(JSON.stringify(payload)),
        });
        return;
      }
      if (endpoint === "/responses" && isRemoteCompactionV2(body)) {
        await streamExternalCompactionHttp({
          res,
          worker,
          body,
          route,
          providerState,
          requestId,
          requestBytes: raw.length,
        });
        return;
      }
      await streamExternalHttp({
        res,
        worker,
        body,
        route,
        endpoint,
        requestId,
        requestBytes: raw.length,
        threadKey: externalThreadSessionKey(parsedBody, req.headers),
      });
    } catch (error) {
      requestAttempt?.finish({ outcome: "failure", errorCode: error.code,
        observation: error.providerObservation });
      if (!res.destroyed) {
        if (!res.headersSent) sendJson(res, clientWireStatus(error), errorPayload(error, requestId));
        // A local projection denial is an application terminal, not a broken
        // network body. Keep the rejection intact without replaying its tool.
        else if (!res.writableEnded && error instanceof SwitchboardError
            && error.officialHttpLifecycle?.terminal_cause === "projection_error"
            && error.officialHttpLifecycle?.sse_observation === "observing") {
          // Codex ignores a bare error event and retries the EOF; response.failed with
          // invalid_prompt is a terminal it does not retry. The error frame stays for other readers.
          const { error: failure } = errorPayload(error, requestId);
          const failed = { type: "response.failed", response: { status: "failed",
            error: { code: "invalid_prompt", message: `${failure.code}: ${failure.message}` } } };
          res.end(`data: ${JSON.stringify(failed)}\n\ndata: ${JSON.stringify({ type: "error", ...errorPayload(error, requestId) })}\n\n`);
        }
        else if (!res.writableEnded) res.destroy();
      }
      safeLog({
        request_id: requestId,
        route: "switchboard",
        status: error.status || 502,
        latency_ms: Date.now() - started,
        error_code: error.code || "SWITCHBOARD_INTERNAL_ERROR",
        provider_response: error.providerObservation,
        request_correlation: error.requestCorrelation,
        model: requestModel,
        endpoint: requestEndpoint,
        diagnostic: { ...(error.officialHttpLifecycle
          ? { official_http: error.officialHttpLifecycle }
          : { name: error.name, response: error.responseShape,
            site: String(error.stack || "").split("\n").slice(1, 3)
              .map((line) => line.trim()).filter((line) => line.startsWith("at ")) }),
          ...projectionRejectionDiagnostic(error) },
      });
    }
  });

  const responseWss = new WebSocketServer({
    noServer: true,
    perMessageDeflate: true,
    maxPayload: MAX_REQUEST_BYTES,
  });
  const relayWss = new WebSocketServer({ noServer: true, perMessageDeflate: true, maxPayload: MAX_REQUEST_BYTES });

  responseWss.on("connection", (downstream, req, url) => {
    const sessionKey = `ws-${randomUUID()}`;
    const history = new Map();
    let inFlight = false;
    let officialUpstream = null;
    let officialConnectionRetired = false;
    let officialTurn = null;
    let pendingOfficialAttempt = null;
    let externalTurn = null;
    let claudeTurn = null;
    let directContinuation = null;
    // Operator message texts seen on this connection; previous_response_id frames carry only deltas.
    let operatorTexts = [];
    // Native coordinator steps since the last worker dispatch, for the advisory run reminder.
    let nativeRun = null;
    // Exact official chain (full input plus raw output) for prompt-cache keep-alive.
    let cacheChain = null;
    let cacheKeepalive = null;
    const stopCacheKeepalive = () => {
      if (!cacheKeepalive) return;
      clearTimeout(cacheKeepalive.timer);
      cacheKeepalive.stopped = true;
      cacheKeepalive = null;
    };
    const sendCacheKeepalive = (state) => new Promise((resolve) => {
      const { chain } = state;
      const requestId = newRequestId();
      const started = Date.now();
      const attempt = inferenceEvidence.start({ requestId, modelRequested: chain.requestedAlias ?? chain.model, transport: "ws",
        correlation: chain.correlation });
      let socket = null;
      let settled = false;
      const finish = (status, event = null, errorCode = null) => {
        if (settled) return;
        settled = true;
        clearTimeout(timeout);
        const observation = event ? officialResponseObservation(event) : null;
        attempt.finish({ outcome: CACHE_KEEPALIVE_DONE.has(event?.type) ? "success"
          : errorCode === "CACHE_KEEPALIVE_SUPERSEDED" ? "cancel" : "failure",
        terminalEvent: event?.type ?? null, observation, errorCode });
        safeLog({ request_id: requestId, model: chain.model, requested_alias: chain.requestedAlias ?? undefined,
          route: "openai", endpoint: "/responses", transport: "ws",
          status, latency_ms: Date.now() - started, request_correlation: chain.correlation,
          provider_response: observation ?? undefined, error_code: errorCode ?? undefined,
          diagnostic: { cache_keepalive: { index: state.count, max: CACHE_KEEPALIVE_MAX,
            chain_response_id: chain.responseId, input_items: chain.replayInput.length,
            ...(event?.error?.message ? { error_message: String(event.error.message).slice(0, 300) } : {}) } } });
        try { socket?.close(1000, "cache keepalive complete"); } catch {}
        // Continue only while refreshes actually hit; a cold refresh is a full-price read.
        const input = observation?.input_tokens, cached = observation?.cached_input_tokens;
        resolve(CACHE_KEEPALIVE_DONE.has(event?.type) && Number.isFinite(input) && input > 0
          && Number.isFinite(cached) && cached / input >= 0.5);
      };
      const timeout = setTimeout(() => finish(504, null, "CACHE_KEEPALIVE_TIMEOUT"), 30_000);
      try {
        const protocols = protocolsFromHeaders(req.headers);
        socket = new WebSocket(`${officialOrigin.replace(/^http/, "ws")}${officialBasePath}/responses${url.search}`,
          protocols.length ? protocols : undefined, {
            headers: headersForWebSocket(req.headers),
            perMessageDeflate: webSocketCompressionEnabled(new URL(officialOrigin).hostname),
            handshakeTimeout: 15_000,
            maxPayload: MAX_REQUEST_BYTES,
          });
      } catch {
        finish(502, null, "CACHE_KEEPALIVE_CONNECT_FAILED");
        return;
      }
      socket.once("open", () => {
        if (state.stopped) return finish(499, null, "CACHE_KEEPALIVE_SUPERSEDED");
        const wire = JSON.stringify({ ...chain.template, input: chain.replayInput,
          max_output_tokens: CACHE_KEEPALIVE_OUTPUT_TOKENS });
        attempt.dispatch(chain.model, { wire });
        socket.send(wire);
      });
      socket.on("message", (data) => {
        let event;
        try { event = JSON.parse(data.toString()); } catch { return; }
        if (!TERMINAL_EVENTS.has(event?.type)) return;
        finish(CACHE_KEEPALIVE_DONE.has(event.type) ? 200 : event.status || 502, event,
          CACHE_KEEPALIVE_DONE.has(event.type) ? null : upstreamErrorShape(event));
      });
      socket.once("unexpected-response", (_request, response) => {
        response.resume();
        finish(response.statusCode || 502, null, "CACHE_KEEPALIVE_HANDSHAKE_REJECTED");
      });
      socket.once("error", () => finish(502, null, "CACHE_KEEPALIVE_CONNECT_FAILED"));
      socket.once("close", () => finish(502, null, "CACHE_KEEPALIVE_CLOSED"));
    });
    const scheduleCacheKeepalive = (chain) => {
      stopCacheKeepalive();
      if (!(CACHE_KEEPALIVE_MAX > 0) || !(CACHE_KEEPALIVE_MS > 0)) return;
      const state = { chain, count: 0, timer: null, stopped: false };
      const tick = async () => {
        if (state.stopped || cacheChain !== chain || inFlight) return;
        state.count += 1;
        const warmed = await sendCacheKeepalive(state);
        // Refreshes land by session affinity, not on this pinned upstream; continue there.
        if (warmed && cacheChain === chain) chain.warmed = true;
        if (!warmed || state.stopped || state.count >= CACHE_KEEPALIVE_MAX) {
          if (cacheKeepalive === state) cacheKeepalive = null;
          return;
        }
        state.timer = setTimeout(tick, CACHE_KEEPALIVE_MS);
        state.timer.unref?.();
      };
      state.timer = setTimeout(tick, CACHE_KEEPALIVE_MS);
      state.timer.unref?.();
      cacheKeepalive = state;
    };
    socketTurns.set(downstream, () => inFlight);

    const sendDownstream = (value) => {
      if (downstream.readyState === WebSocket.OPEN) downstream.send(value);
    };

    const ensureOfficial = async () => {
      if (officialUpstream?.readyState === WebSocket.OPEN) return officialUpstream;
      officialUpstream = await new Promise((resolve, reject) => {
        const protocols = protocolsFromHeaders(req.headers);
        const upstream = new WebSocket(
          `${officialOrigin.replace(/^http/, "ws")}${officialBasePath}/responses${url.search}`,
          protocols.length ? protocols : undefined,
          {
            headers: headersForWebSocket(req.headers),
            perMessageDeflate: webSocketCompressionEnabled(new URL(officialOrigin).hostname),
            handshakeTimeout: 35_000,
            maxPayload: MAX_REQUEST_BYTES,
          },
        );
        // The downstream owns the handshake too, not just an already-sent turn.
        officialUpstream = upstream;
        upstream.once("open", () => { upstream.openedAt = upstream.lastActivityAt = Date.now(); resolve(upstream); });
        upstream.once("error", () => reject(new SwitchboardError("OFFICIAL_WS_CONNECT_FAILED", "Official Responses WebSocket connection failed.")));
        upstream.once("unexpected-response", (_request, response) => {
          reject(new SwitchboardError(
            "OFFICIAL_WS_HANDSHAKE_REJECTED",
            `Official Responses WebSocket handshake returned ${response.statusCode}.`,
            response.statusCode || 502,
          ));
          response.resume();
          upstream.terminate();
        });
        upstream.on("message", (data, isBinary) => {
          if (isBinary) {
            sendDownstream(JSON.stringify(wrappedWebSocketError(
              new SwitchboardError("OFFICIAL_WS_INVALID_FRAME", "Official Responses WebSocket returned a binary frame."),
              officialTurn?.requestId || newRequestId(),
            )));
            return;
          }
          try {
            const event = JSON.parse(data.toString());
            officialTurn?.attempt?.observeOutput?.(event);
            if (officialTurn && officialTurn.firstOutputEventMs === null && [
              "response.output_text.delta", "response.function_call_arguments.delta",
              "response.output_item.added", "response.content_part.added",
            ].includes(event.type)) {
              officialTurn.firstOutputEventMs = Math.max(0, Math.floor(performance.now() - officialTurn.timingStarted));
            }
            // An incomplete terminal can carry an unfinished call in response.output.
            // Keep the terminal, but never project that partial call as executable output.
            const projectionEvent = event.type === "response.incomplete" && officialTurn?.nativeToolTarget
              && Array.isArray(event.response?.output)
              ? { ...event, response: { ...event.response, output: event.response.output.filter((item) =>
                !["custom_tool_call", "function_call"].includes(item?.type) || item.status === "completed") } }
              : event;
            const partialToolDone = officialTurn?.nativeToolTarget && event.type === "response.output_item.done"
              && ["custom_tool_call", "function_call"].includes(event.item?.type)
              && ["in_progress", "incomplete"].includes(event.item?.status);
            const projected = partialToolDone ? null : officialTurn?.project ? officialTurn.project(projectionEvent) : event;
            const projectedEvents = [projected].flat();
            for (const item of projectedEvents) {
              if (item) sendDownstream(item === event ? data.toString() : JSON.stringify(item));
            }
            rememberSpawnPayloads(event);
            if (event.type === "response.output_item.done" && !partialToolDone && event.item && officialTurn?.cacheCandidate) {
              officialTurn.cacheOutput.push(event.item);
            }
            if (TERMINAL_EVENTS.has(event.type) && officialTurn) {
              upstream.lastActivityAt = Date.now();
              officialTurn.attempt?.finish({
                outcome: event.type === "response.completed" ? "success"
                  : officialTurn.cancelRequested ? "cancel" : "failure",
                terminalEvent: event.type, observation: officialResponseObservation(event),
                errorCode: event.type === "error" ? upstreamErrorShape(event) : null,
                syntheticRequest: officialTurn.syntheticRequest,
              });
              const terminalMs = Math.max(0, Math.floor(performance.now() - officialTurn.timingStarted));
              const output = projectedEvents.find((item) => item?.type === "response.completed")?.response?.output
                ?.filter((item) => item?.type !== "reasoning");
              // The raw output still names nokiy_execute; the projected one shows the native cell.
              if (event.type === "response.completed") {
                nativeRun = nativeRunAfterResponse(nativeRun, officialTurn.requestedAlias, event.response?.output,
                  event.response?.usage, officialTurn.nativeRunControls?.bindings ?? officialTurn.coordinatorBindings);
              }
              const resultCall = officialTurn.resultDeliveryEnabled ? resultContinuationCall(output) : null;
              // Capability belongs to this exact response chain, even when generation stops
              // incomplete. Only completed output above can bind a call or result delivery.
              directContinuation = ["response.completed", "response.incomplete"].includes(event.type)
                && event.response?.id && (officialTurn.nativeToolTarget || officialTurn.coordinatorFallback)
                ? { model: officialTurn.requestedAlias, target: officialTurn.nativeToolTarget,
                  fallback: officialTurn.coordinatorFallback,
                  toolsIdentity: officialTurn.nativeToolsIdentity, responseId: event.response.id, terminalEvent: event.type,
                  profileIdentity: officialTurn.profileIdentity, resultCall,
                  coordinatorBindings: officialTurn.coordinatorBindings,
                  nativeRunControls: officialTurn.nativeRunControls,
                  nativeObservation: officialTurn.nativeObservationState
                    ? { state: officialTurn.nativeObservationState, call: resultContinuationCall(output) } : null,
                  pendingDelivery: bindPendingResultWait(officialTurn.pendingResultDelivery, resultCall) }
                : null;
              try {
                if (event.type === "response.completed" && event.response?.id && officialTurn.cacheCandidate) {
                  const rawOutput = Array.isArray(event.response.output) && event.response.output.length
                    ? event.response.output : officialTurn.cacheOutput;
                  // A reasoning item without encrypted_content cannot be replayed statelessly.
                  const replayable = rawOutput.every((item) => item?.type !== "reasoning" || item.encrypted_content);
                  // A full request cannot end in an unanswered tool call, so a refresh replays the
                  // already-accepted input; the pending call stays on the chain for continuation.
                  cacheChain = { responseId: event.response.id, template: officialTurn.cacheCandidate.template,
                    input: officialTurn.cacheCandidate.input.concat(rawOutput.map(replayableOutputItem)),
                    replayInput: officialTurn.cacheCandidate.input,
                    model: officialTurn.model, requestedAlias: officialTurn.requestedAlias,
                    correlation: officialTurn.requestCorrelation };
                  if (!replayable) cacheChain = null;
                  else if (rawOutput.some((item) => CACHE_KEEPALIVE_ITEMS.has(item?.type))) scheduleCacheKeepalive(cacheChain);
                  if (cacheChain && officialTurn.cacheCandidate.hashes && officialTurn.cacheCandidate.kinds) {
                    try {
                      const outputs = rawOutput.map(replayableOutputItem);
                      cacheChain.hashes = officialTurn.cacheCandidate.hashes.concat(itemHashes(outputs));
                      cacheChain.kinds = officialTurn.cacheCandidate.kinds.concat(itemKinds(outputs));
                      rememberChain(cacheChain.correlation?.client_thread_id, cacheChain.template, cacheChain.hashes, cacheChain.kinds);
                    } catch {}
                  }
                } else if (event.type === "response.completed" || event.type === "response.incomplete") {
                  cacheChain = null;
                }
              } catch {
                cacheChain = null;
              }
              safeLog({
                request_id: officialTurn.requestId,
                model: officialTurn.model,
                requested_alias: officialTurn.requestedAlias,
                provider_response: officialResponseObservation(event),
                provider_transport_timing: {
                  schema_version: "provider_transport_timing_v1",
                  source: "local_monotonic_clock",
                  scope: "request_dispatch_to_terminal_event",
                  transport: "ws",
                  response_headers_ms: null,
                  first_output_event_ms: officialTurn.firstOutputEventMs,
                  completed_ms: event.type === "response.completed" ? terminalMs : null,
                  terminal_ms: terminalMs,
                  terminal_event: event.type,
                  pre_dispatch_ms: officialTurn.preDispatchMs,
                },
                result_delivery: officialTurn.project ? { ...officialTurn.project.resultDeliveryObservation(),
                  ...officialTurn.resultDeliveryDiagnostic } : undefined,
                request_correlation: officialTurn.requestCorrelation,
                route: "openai",
                endpoint: "/responses",
                transport: "ws",
                status: event.type === "error" ? event.status || 502 : 200,
                latency_ms: Date.now() - officialTurn.started,
                request_bytes: officialTurn.requestBytes,
                response_bytes: officialTurn.responseBytes,
                error_code: event.type === "error" ? upstreamErrorShape(event) : undefined,
                ...(officialTurn.cacheRerouted || officialTurn.syntheticPrewarm || officialTurn.upstreamRotated
                  || officialTurn.upstreamRotationDeferred ? { diagnostic: {
                  ...(officialTurn.cacheRerouted ? { cache_reroute: true } : {}),
                  ...(officialTurn.upstreamRotated ? { upstream_rotation: officialTurn.upstreamRotated } : {}),
                  // An aged upstream kept a chained turn: no exact chain could be carried.
                  ...(officialTurn.upstreamRotationDeferred
                    ? { upstream_rotation_deferred: officialTurn.upstreamRotationDeferred } : {}),
                  ...(officialTurn.syntheticPrewarm ? { synthetic_prewarm: true } : {}) } } : {}),
              });
              officialTurn = null;
              inFlight = false;
              if (event.type === "error" && event.error?.message === "Invalid `previous_response_id`.") {
                // Forward the rejection once, then retire the unusable connection without replay.
                officialConnectionRetired = true;
                downstream.close(1012, "official continuation rejected");
              }
            } else if (officialTurn) {
              officialTurn.responseBytes += data.length;
            }
          } catch (error) {
            officialTurn?.attempt?.finish({ outcome: "failure", errorCode: error.code || "OFFICIAL_WS_INVALID_FRAME" });
            if (error instanceof SwitchboardError && officialTurn) {
              safeLog({ request_id: officialTurn.requestId, model: officialTurn.requestedAlias,
                route: "switchboard", endpoint: "/responses", transport: "ws", status: error.status,
                latency_ms: Date.now() - officialTurn.started, error_code: error.code,
                diagnostic: projectionRejectionDiagnostic(error) });
            }
            sendDownstream(JSON.stringify(wrappedWebSocketError(error instanceof SwitchboardError ? error :
              new SwitchboardError("OFFICIAL_WS_INVALID_FRAME", "Official response could not be projected."),
              officialTurn?.requestId || newRequestId())));
            officialTurn = null;
            inFlight = false;
            officialConnectionRetired = true;
            upstream.close();
          }
        });
        upstream.on("close", () => {
          // A deliberate prompt-cache reroute already carries the full chain to a new upstream.
          if (upstream.cacheRerouted) {
            if (officialUpstream === upstream) officialUpstream = null;
            return;
          }
          if (officialUpstream === upstream) officialUpstream = null;
          officialConnectionRetired = true;
          if (officialTurn) {
            officialTurn.attempt?.finish({ outcome: "failure", errorCode: "OFFICIAL_WS_EARLY_EOF" });
            sendDownstream(JSON.stringify(wrappedWebSocketError(
              new SwitchboardError("OFFICIAL_WS_EARLY_EOF", "Official Responses WebSocket closed before completion."),
              officialTurn.requestId,
            )));
            officialTurn = null;
            inFlight = false;
          }
          // Do not hide an upstream connection change from the native continuation owner.
          // Leave an already-running external turn alone; retire before its next official send.
          if (!inFlight && downstream.readyState === WebSocket.OPEN) {
            downstream.close(1012, "official upstream connection retired");
          }
        });
      });
      return officialUpstream;
    };

    downstream.on("message", async (data, isBinary) => {
      const requestId = newRequestId();
      const started = Date.now();
      const receivedAt = performance.now();
      // A draining process starts no new turn; the client reconnects to its successor.
      if (draining && !inFlight) {
        let type = null;
        try { type = JSON.parse(data.toString())?.type; } catch { /* handled below when not draining */ }
        if (type === "response.create") {
          downstream.close(1012, "service restart");
          return;
        }
      }
      if (isBinary) {
        sendDownstream(JSON.stringify(wrappedWebSocketError(
          new SwitchboardError("INVALID_WEBSOCKET_FRAME", "Responses WebSocket accepts text JSON frames only.", 400),
          requestId,
        )));
        return;
      }
      let body;
      let requestAttempt = null;
      let ownsTurn = false;
      try {
        const inboundWire = data;
        body = JSON.parse(data.toString());
        stopCacheKeepalive();
        rememberSpawnPayloads(body);
        if (body?.type !== "response.create") {
          if ((body?.type === "response.cancel" || body?.type === "response.interrupt") && claudeTurn) {
            claudeTurn.abort();
            return;
          }
          if (body?.type === "response.cancel" && externalTurn) {
            const cancelledTurn = externalTurn;
            if (cancelledTurn.route === "web") {
              cancelledTurn.interruption ||= cancelledTurn.worker.interruptTurn(sessionKey, cancelledTurn.body);
              await cancelledTurn.interruption;
            } else await cancelledTurn.worker.closeSession(sessionKey);
          } else if (officialUpstream?.readyState === WebSocket.OPEN) {
            if (body?.type === "response.cancel" && officialTurn) officialTurn.cancelRequested = true;
            officialUpstream.send(data.toString());
          }
          else throw new SwitchboardError("UNSUPPORTED_WEBSOCKET_EVENT", "No active route can accept this WebSocket event.", 400);
          return;
        }
        if (inFlight) throw new SwitchboardError("WEBSOCKET_TURN_IN_FLIGHT", "A response is already in flight on this connection.", 409);
        // Reserve before async routing/handshake; rejected messages cannot
        // release another message's turn in the catch handler below.
        inFlight = true;
        ownsTurn = true;
        if (claude && claude.models.has(body.model)) {
          const turn = claude.runWsTurn({
            message: body,
            upgradeHeaders: req.headers,
            history,
            send: (value) => sendDownstream(typeof value === "string" ? value : JSON.stringify(value)),
          });
          claudeTurn = turn;
          try {
            const result = await turn.done;
            safeLog({ request_id: requestId, model: body.model, route: "claude", endpoint: "/responses",
              transport: "claude-bridge-ws", status: result?.status ?? (result?.aborted ? 499 : 502), latency_ms: Date.now() - started,
              request_bytes: data.length, error_code: result?.error_code,
              diagnostic: { terminal_cause: result?.terminal_cause, upstream_http_status: result?.upstream_http_status,
                upstream_terminal_event: result?.upstream_terminal_event, transport_error: result?.transport_error } });
          } finally {
            if (claudeTurn === turn) claudeTurn = null;
            inFlight = false;
          }
          return;
        }
        if (claude && history.get(body.previous_response_id)?.route === "claude") {
          body = { ...body, input: fullExternalInput(body, history) };
          delete body.previous_response_id;
          data = Buffer.from(JSON.stringify(body));
        }
        if (claude && Array.isArray(body.input)) {
          const prepared = claude.sanitize(body.input);
          if (prepared) {
            body = { ...body, input: prepared.input };
            data = Buffer.from(JSON.stringify(body));
            safeLog({ request_id: requestId, model: body.model, route: "claude-history", endpoint: "/responses",
              transport: "ws", diagnostic: { claude_checkpoints_opened: prepared.checkpoints, claude_ids_removed: prepared.ids } });
          }
        }
        // Deferred tool definitions can live in the input prefix omitted by a
        // native WS delta. Reuse only the confirmed same-connection capability.
        const nativeToolsIdentity = JSON.stringify(body.tools ?? null);
        const continuationCondition = !directContinuation ? "connection_state_unavailable"
          : directContinuation.model !== body.model ? "model_mismatch"
          : directContinuation.responseId !== body.previous_response_id ? "response_mismatch"
          : directContinuation.toolsIdentity !== nativeToolsIdentity
            && body.tools !== undefined ? "tools_mismatch" : "bound";
        const continuation = continuationCondition === "bound" ? directContinuation : null;
        operatorTexts = body.previous_response_id
          ? [...operatorTexts, ...userMessageTexts(body)].slice(-16) : userMessageTexts(body);
        const nativePrediction = trimNativePrediction(body);
        if (nativePrediction) {
          body = { ...body, input: nativePrediction.input };
          data = Buffer.from(JSON.stringify(body));
          safeLog({ request_id: requestId, model: body.model, route: "prediction-trim", endpoint: "/responses",
            transport: "ws", diagnostic: { prediction_trim: nativePrediction.stats } });
        }
        const nativeAppshots = elideNativeStaleAppshots(body, (name) => officialModels.has(name));
        if (nativeAppshots) {
          body = { ...body, input: nativeAppshots.input };
          data = Buffer.from(JSON.stringify(body));
          safeLog({ request_id: requestId, model: body.model, route: "stale-appshots", endpoint: "/responses",
            transport: "ws", diagnostic: { stale_appshots: nativeAppshots.stats } });
        }
        // An empty local prewarm has no upstream context to inherit. Classify its
        // exact unbound successor before Nokiy projects the full Lite catalog.
        const emptySyntheticSuccessor = cacheChain?.synthetic === true && cacheChain.input.length === 0
          && cacheChain.responseId === body.previous_response_id && cacheChain.requestedAlias === body.model
          && continuation === null && Array.isArray(body.input) && body.generate !== false && !isRemoteCompactionV2(body);
        let projectionBody = body;
        if (emptySyntheticSuccessor) {
          const { previous_response_id: _syntheticResponseId, ...full } = body;
          projectionBody = full;
        }
        const runReminder = nativeRunReminder(nativeRun, projectionBody.model, latestInputIsOperatorTurn(projectionBody.input));
        nativeRun = runReminder.state;
        if (runReminder.text) safeLog({ request_id: requestId, model: projectionBody.model, requested_alias: projectionBody.model,
          route: "native-run-reminder", transport: "ws", diagnostic: runReminder.diagnostic,
          request_correlation: officialRequestCorrelation(projectionBody, req.headers) });
        const direct = await projectDirect(projectionBody, "/responses", continuation?.target ?? null, continuation?.resultCall ?? null,
          continuation?.coordinatorBindings ?? null, continuation?.pendingDelivery ?? null,
          continuation?.nativeObservation ?? { condition: continuationCondition === "bound" ? "observation_state_unavailable" : continuationCondition },
          operatorTexts, runReminder.text, continuation?.fallback ?? null);
        if (direct?.fullRequest) safeLog({ request_id: requestId, model: direct.body?.model ?? body.model, requested_alias: direct.requestedAlias ?? null,
          route: "replay-restore", transport: "ws", diagnostic: { restored_calls: direct.replayRestored ?? 0,
            inserted_items: direct.replayInserted ?? 0,
            ...(direct.predictionTrim ? { prediction_trim: direct.predictionTrim } : {}),
            ...(direct.staleAppshots ? { stale_appshots: direct.staleAppshots } : {}),
            input_items: Array.isArray(body?.input) ? body.input.length : null, alignment: direct.cacheAlignment ?? null,
            subagent: subagentLabel(body, req.headers), prefix: prefixFingerprints(direct.body?.input) } });
        if (direct?.resultCompaction) safeLog({ request_id: requestId, model: direct.body?.model ?? body.model,
          route: "nokiy-result-compaction", transport: "ws",
          diagnostic: { ...direct.resultCompaction, full_request: direct.fullRequest } });
        if (direct?.nativeToolMissing) safeLog({ request_id: requestId, model: direct.body?.model ?? body.model,
          requested_alias: direct.requestedAlias ?? null, route: "nokiy-native-tool-missing", transport: "ws",
          request_correlation: officialRequestCorrelation(body, req.headers),
          diagnostic: { native_tool_missing: { ...direct.nativeToolMissing,
            continuation_condition: continuationCondition,
            continuation_terminal_event: directContinuation?.terminalEvent ?? null }, dispatch: direct.dispatch } });
        const profileIdentity = JSON.stringify(direct?.profile ?? null);
        if (direct && directContinuation?.model === body.model
            && directContinuation.responseId === body.previous_response_id
            && directContinuation.profileIdentity !== profileIdentity) {
          throw new SwitchboardError("NOKIY_WORKFLOW_CHANGED_DURING_CONTINUATION",
            "The selected workflow changed during this response chain; no request was replayed or worker dispatched.", 409);
        }
        directContinuation = null;
        const route = direct ? "openai" : await routeWithCurrentOfficialModels(body.model, officialModels, () => gate.current());
        if (route === "openai") {
          requestAttempt = inferenceEvidence.start({ requestId, modelRequested: body.model,
            request: { wire: inboundWire },
            transport: "ws", correlation: officialRequestCorrelation(body, req.headers) });
          if (officialConnectionRetired) {
            requestAttempt.finish({ outcome: "failure", errorCode: "OFFICIAL_WS_CONNECTION_RETIRED" });
            inFlight = false;
            downstream.close(1012, "official upstream connection retired");
            return;
          }
          pendingOfficialAttempt = requestAttempt;
          if (downstream.readyState !== WebSocket.OPEN) {
            requestAttempt.finish({ outcome: "cancel", errorCode: "OFFICIAL_WS_DOWNSTREAM_CLOSED" });
            pendingOfficialAttempt = null;
            inFlight = false;
            return;
          }
          const outgoing = direct?.body || body;
          // After a warmed idle wait the pinned upstream has usually lost the prefix, while
          // refreshes kept it warm on the session-affinity route: continue on a fresh upstream.
          // A synthetic prewarm id is unknown upstream, so its successor always carries the chain.
          // An aged upstream is rotated the same way: a chained request carries this connection's
          // exact chain to a fresh upstream; a full request needs nothing from the old one.
          const upstreamAge = officialUpstream?.readyState === WebSocket.OPEN && Number.isFinite(officialUpstream.openedAt)
            ? Date.now() - officialUpstream.openedAt : 0;
          const upstreamWarm = Number.isFinite(officialUpstream?.lastActivityAt)
            && Date.now() - officialUpstream.lastActivityAt <= UPSTREAM_WARM_IDLE_MS;
          const rotationReason = UPSTREAM_ROTATE_MS > 0 && upstreamAge >= UPSTREAM_ROTATE_MS ? "age"
            : UPSTREAM_ROTATE_WARM_MS > 0 && upstreamWarm && upstreamAge >= UPSTREAM_ROTATE_WARM_MS ? "warm_age" : null;
          const aged = rotationReason !== null;
          // Only an exact chain can move; without one a chained turn stays on its upstream.
          const rerouteInput = cacheChain && ((CACHE_KEEPALIVE_MAX > 0 && cacheChain.warmed) || cacheChain.synthetic || aged)
            && Array.isArray(outgoing.input)
            && outgoing.previous_response_id && outgoing.previous_response_id === cacheChain.responseId
            && (cacheChain.synthetic || officialUpstream?.readyState === WebSocket.OPEN) ? cacheChain.input : null;
          const syntheticSuccessor = emptySyntheticSuccessor || Boolean(rerouteInput && cacheChain.synthetic);
          if (syntheticSuccessor) cacheChain.synthetic = false;
          else if (rerouteInput) cacheChain.warmed = false;
          const rotateAged = aged && (Boolean(rerouteInput) || !outgoing.previous_response_id);
          if ((rerouteInput && !syntheticSuccessor) || rotateAged) {
            const stale = officialUpstream;
            if (stale) {
              stale.cacheRerouted = true;
              officialUpstream = null;
              try { stale.close(1000, rotateAged ? "connection age rotation" : "prompt-cache reroute"); } catch {}
            }
          }
          const upstream = await ensureOfficial();
          if (downstream.readyState !== WebSocket.OPEN) {
            requestAttempt.finish({ outcome: "cancel", errorCode: "OFFICIAL_WS_DOWNSTREAM_CLOSED" });
            pendingOfficialAttempt = null;
            upstream.close(1000, "downstream closed");
            inFlight = false;
            return;
          }
          const compacted = normalizeCompactions(outgoing.input, "openai", providerState.asMap());
          const external = normalizeExternalItemsForOfficial(compacted.input);
          const frame = direct || compacted.changed || external.changed
            ? JSON.stringify({ ...outgoing, ...(Array.isArray(outgoing.input) ? { input: external.input } : {}) })
            : data.toString();
          let wire = frame;
          if (rerouteInput) {
            try {
              const { previous_response_id: _previous, ...rest } = JSON.parse(frame);
              wire = JSON.stringify({ ...rest, input: rerouteInput.concat(rest.input) });
            } catch {
              wire = frame;
            }
          }
          let cacheCandidate = null;
          let prewarm = false;
          if (CACHE_KEEPALIVE_MAX > 0 || syntheticOfficialPrewarm()) {
            try {
              const { input: sentInput, previous_response_id: previous, generate, ...template } = JSON.parse(wire);
              prewarm = generate === false;
              const chainInput = !Array.isArray(sentInput) ? null : !previous ? sentInput
                : cacheChain?.responseId === previous ? cacheChain.input.concat(sentInput) : null;
              cacheCandidate = chainInput ? { template, input: chainInput } : null;
              if (cacheCandidate) {
                // Log-only: locate where a full-input replay leaves this thread's last upstream chain.
                try {
                  const hashes = itemHashes(sentInput), kinds = itemKinds(sentInput);
                  cacheCandidate.hashes = !previous ? hashes : cacheChain?.hashes ? cacheChain.hashes.concat(hashes) : null;
                  cacheCandidate.kinds = !previous ? kinds : cacheChain?.kinds ? cacheChain.kinds.concat(kinds) : null;
                  const divergence = !previous && !prewarm
                    ? replayDivergence(officialRequestCorrelation(body, req.headers).client_thread_id, template, sentInput, hashes) : null;
                  if (divergence) safeLog({ request_id: requestId, model: outgoing.model, requested_alias: direct?.requestedAlias ?? null,
                    route: "replay-diagnostic", diagnostic: { replay_divergence: divergence } });
                } catch {}
              }
              // Log-only: a chained request reuses the stored prefix only if its prompt-shaping
              // fields match the previous turn on this connection; compactions are always logged.
              try {
                const compaction = Array.isArray(sentInput) && sentInput.some((item) => item?.type === "compaction_trigger");
                const comparable = Boolean(previous) && cacheChain?.responseId === previous && cacheChain.template;
                const changed = comparable ? templateDiff(cacheChain.template, template) : null;
                if (compaction || changed?.length) safeLog({ request_id: requestId, model: outgoing.model,
                  requested_alias: direct?.requestedAlias ?? null, route: "template-diagnostic",
                  diagnostic: { template_diff: { chained: Boolean(previous), comparable: Boolean(comparable), changed,
                    compaction, alignment: direct?.cacheAlignment ?? null } } });
              } catch {}
            } catch {
              cacheCandidate = null;
            }
          }
          officialTurn = {
            attempt: requestAttempt,
            timingStarted: performance.now(),
            preDispatchMs: Math.max(0, Math.floor(performance.now() - receivedAt)),
            firstOutputEventMs: null,
            requestId,
            model: outgoing.model,
            requestedAlias: direct?.requestedAlias,
            requestCorrelation: officialRequestCorrelation(outgoing, req.headers),
            resultDeliveryDiagnostic: direct?.resultDeliveryDiagnostic,
            project: direct?.nativeTool ? nativeEventProjector(direct.nativeTool, { onProjected: sharedReplayRestorer().record }) : null,
            nativeToolTarget: direct?.nativeTool?.target || null,
            coordinatorFallback: direct?.coordinatorFallback
              ? { wireEffort: direct.wireEffort, nativeToolMissing: direct.nativeToolMissing,
                answerOnly: direct.fallbackAnswerOnly } : null,
            resultDeliveryEnabled: direct?.nativeTool?.resultDeliveryEnabled === true,
            coordinatorBindings: direct?.nativeTool?.coordinatorBindings ?? null,
            nativeRunControls: direct?.nativeTool?.adaptiveNative
              ? nativeRunControlBindings(projectionBody, continuation?.nativeRunControls) : null,
            pendingResultDelivery: direct?.nativeTool?.pendingResultDelivery ?? null,
            nativeObservationState: direct?.nativeTool?.nativeObservationState ?? null,
            nativeToolsIdentity: continuation && body.tools === undefined
              ? continuation.toolsIdentity : nativeToolsIdentity,
            profileIdentity,
            started,
            requestBytes: Buffer.byteLength(wire),
            cacheRerouted: emptySyntheticSuccessor || Boolean(rerouteInput),
            upstreamRotated: rotateAged ? rotationReason : false,
            upstreamRotationDeferred: aged && !rotateAged ? rotationReason : false,
            responseBytes: 0,
            cacheCandidate,
            cacheOutput: [],
          };
          pendingOfficialAttempt = null;
          if (prewarm && cacheCandidate && syntheticOfficialPrewarm()) {
            // Replayed through the upstream listener so attempt evidence (no dispatch), logs,
            // continuation and the cache chain follow the normal completion path.
            officialTurn.syntheticPrewarm = true;
            officialTurn.syntheticRequest = { wire, projection: direct };
            const events = syntheticPrewarmEvents("openai");
            for (const event of events) upstream.emit("message", Buffer.from(JSON.stringify(event)), false);
            if (cacheChain?.responseId === responseIdFromEvents(events)) cacheChain.synthetic = true;
            return;
          }
          requestAttempt.dispatch(outgoing.model, { wire, projection: direct });
          upstream.send(wire);
          return;
        }
        await gate.requireExternal();
        assertExternalRequestAllowed(body, "/responses");
        const worker = workerForRoute(route);
        logXaiToolShape({ requestId, body, transport: "ws" });
        // This connection owns the turn (cancel and close abort it); the
        // worker session follows the caller's thread across reconnects.
        const workerSessionKey = externalSessionKey(route, externalThreadSessionKey(body, req.headers), sessionKey);
        let clean = normalizeForExternal(body, route, providerState);
        let fullInput = fullExternalInput(clean, history);
        const preparedHistory = claude?.sanitize(fullInput);
        if (preparedHistory) {
          fullInput = normalizeForExternal({ ...clean, input: preparedHistory.input }, route, providerState).input;
          safeLog({ request_id: requestId, model: clean.model, route: "claude-history", endpoint: "/responses",
            transport: "ws", diagnostic: { claude_checkpoints_opened: preparedHistory.checkpoints, claude_ids_removed: preparedHistory.ids } });
        }
        const prior = clean.previous_response_id ? history.get(clean.previous_response_id) : null;
        if (route === "agy" && clean.generate === false) {
          const events = syntheticPrewarmEvents(route);
          for (const event of events) sendDownstream(JSON.stringify(event));
          boundedHistorySet(history, responseIdFromEvents(events), { input: fullInput, output: [], model: clean.model });
          inFlight = false;
          return;
        }
        const latestHistoryId = [...history.keys()].at(-1);
        if (clean.previous_response_id
            && (route === "xai" || route === "web")
            && prior?.route === route
            && prior.model === clean.model
            && prior.provider_response_id) {
          clean = { ...clean, previous_response_id: prior.provider_response_id };
        } else if (
          route === "agy"
          && clean.previous_response_id
          && prior?.route === "agy"
          && prior.model === clean.model
          && latestHistoryId === clean.previous_response_id
          && agyDeltaInput(clean.input, prior)
        ) {
          clean = {
            ...clean,
            input: clean.input,
            agy_linear_extension: true,
          };
          delete clean.previous_response_id;
        } else if (clean.previous_response_id) {
          clean = { ...clean, input: fullInput };
          delete clean.previous_response_id;
        }
        if (isRemoteCompactionV2(clean)) {
          externalTurn = { requestId, worker, route, body: clean };
          const events = await externalCompactionEvents({
            worker,
            body: clean,
            route,
            providerState,
            sessionKey,
          });
          for (const event of events) sendDownstream(JSON.stringify(event));
          const responseBytes = events.reduce(
            (total, event) => total + Buffer.byteLength(JSON.stringify(event)),
            0,
          );
          safeLog({
            request_id: requestId,
            model: clean.model,
            route,
            endpoint: "/responses",
            transport: `${route}-compact-v2`,
            status: 200,
            latency_ms: Date.now() - started,
            request_bytes: data.length,
            response_bytes: responseBytes,
          });
          externalTurn = null;
          inFlight = false;
          return;
        }
        const emitted = [];
        let eventBridge = createExternalEventBridge(route);
        const responseBoundary = {
          requestId, worker, route, body: clean,
          responseId: eventBridge.publicResponseId, terminalEvent: null,
        };
        externalTurn = responseBoundary;
        const onEvent = (providerEvent) => {
          if (responseBoundary.terminalEvent) return;
          let event = eventBridge.transform(providerEvent);
          if (!event) return;
          responseBoundary.responseId ||= event.response?.id || event.response_id;
          if (event.type === "error" && event.status !== 499) {
            event = externalResponseFailure(new SwitchboardError(
              event.error?.code || "EXTERNAL_WORKER_ERROR",
              event.error?.message || "External worker returned an error event.",
              event.status || 502,
            ), requestId, responseBoundary.responseId);
          }
          if (TERMINAL_EVENTS.has(event.type)) responseBoundary.terminalEvent = event.type;
          rememberSpawnPayloads(event);
          emitted.push(event);
          sendDownstream(JSON.stringify(event));
        };
        let workerResult;
        const workerOptions = { sessionKey: workerSessionKey, ownerKey: sessionKey, onEvent };
        try {
          workerResult = await worker.runResponses(clean, workerOptions);
        } catch (error) {
          if (route !== "xai" || error.code !== "previous_response_not_found") throw error;
          emitted.length = 0;
          eventBridge = createExternalEventBridge(route);
          responseBoundary.responseId = eventBridge.publicResponseId;
          clean = { ...clean, input: fullInput };
          delete clean.previous_response_id;
          workerResult = await worker.runResponses(clean, workerOptions);
        }
        const responseId = responseIdFromEvents(emitted);
        if (!responseId) throw new SwitchboardError("INVALID_EXTERNAL_EVENT", "External response completed without a response id.");
        if (!responseBoundary.terminalEvent)
          throw new SwitchboardError("EXTERNAL_STREAM_INCOMPLETE", "External worker ended without a terminal Responses event.");
        boundedHistorySet(history, responseId, {
          input: fullInput,
          output: responseOutputItems(emitted),
          model: clean.model,
          route,
          provider_response_id: eventBridge.providerResponseId,
        });
        const responseBytes = emitted.reduce((total, event) => total + Buffer.byteLength(JSON.stringify(event)), 0);
        safeLog({
          request_id: requestId,
          model: clean.model,
          route,
          endpoint: "/responses",
          transport: externalTransport(route),
          status: 200,
          latency_ms: Date.now() - started,
          request_bytes: data.length,
          response_bytes: responseBytes,
          ...workerResult?.tool_projection,
          ...workerObservation(workerResult),
          session_scope: workerSessionKey === sessionKey ? "connection" : "thread",
        });
        if (externalTurn?.requestId === requestId) externalTurn = null;
        inFlight = false;
      } catch (error) {
        requestAttempt?.finish({ outcome: "failure", errorCode: error.code });
        if (pendingOfficialAttempt === requestAttempt) pendingOfficialAttempt = null;
        const failedTurn = externalTurn?.requestId === requestId ? externalTurn : null;
        if (!failedTurn?.terminalEvent) {
          // Explicit cancellation retains its existing 499 control contract.
          sendDownstream(JSON.stringify(failedTurn?.responseId && error.status !== 499
            ? externalResponseFailure(error, requestId, failedTurn.responseId)
            : wrappedWebSocketError(error, requestId)));
        }
        if (externalTurn?.requestId === requestId) externalTurn = null;
        if (ownsTurn) inFlight = false;
        safeLog({
          request_id: requestId,
          model: body?.model,
          route: failedTurn?.route || "switchboard",
          endpoint: "/responses",
          transport: "ws",
          status: error.status || 502,
          latency_ms: Date.now() - started,
          request_bytes: data.length,
          error_code: error.code || "SWITCHBOARD_INTERNAL_ERROR",
          diagnostic: error.code === "EXTERNAL_CURRENT_REQUEST_MISSING"
            ? externalInputEnvelopeShape(body?.input)
            : { ...untypedErrorDiagnostic(error), ...projectionRejectionDiagnostic(error) },
        });
      }
    });

    downstream.on("close", (code, reason) => {
      // Transport evidence: who closed this Codex connection and whether a turn was in flight.
      safeLog({ route: "switchboard", transport: "ws", diagnostic: { event: "downstream_closed", code,
        reason: String(reason || "").slice(0, 120), turn_in_flight: inFlight,
        thread: officialRequestCorrelation({}, req.headers).client_thread_id, connection: sessionKey } });
      stopCacheKeepalive();
      cacheChain = null;
      pendingOfficialAttempt?.finish({ outcome: "cancel", errorCode: "OFFICIAL_WS_DOWNSTREAM_CLOSED" });
      pendingOfficialAttempt = null;
      officialTurn?.attempt?.finish({ outcome: "cancel", errorCode: "OFFICIAL_WS_DOWNSTREAM_CLOSED" });
      officialTurn = null;
      inFlight = false;
      socketTurns.delete(downstream);
      claudeTurn?.abort();
      officialUpstream?.close(1000, "downstream closed");
      xaiWorker.closeSession(sessionKey).catch(() => {});
      agyWorker.closeSession(sessionKey).catch(() => {});
      openrouterWorker.closeSession(sessionKey).catch(() => {});
      webWorker.closeSession(sessionKey).catch(() => {});
    });
  });

  relayWss.on("connection", (downstream, _req, upstream) => {
    activeRelays.add(downstream);
    downstream.once("close", () => activeRelays.delete(downstream));
    downstream.on("message", (data, isBinary) => {
      if (upstream.readyState === WebSocket.OPEN) upstream.send(data, { binary: isBinary });
    });
    upstream.on("message", (data, isBinary) => {
      if (downstream.readyState === WebSocket.OPEN) downstream.send(data, { binary: isBinary });
    });
    downstream.on("ping", (data) => upstream.ping(data));
    upstream.on("ping", (data) => downstream.ping(data));
    downstream.on("close", (code, reason) => upstream.close(code, reason));
    upstream.on("close", (code, reason) => downstream.close(code, reason));
    downstream.on("error", () => upstream.terminate());
    upstream.on("error", () => downstream.terminate());
  });

  server.on("upgrade", async (req, socket, head) => {
    try {
      const url = new URL(req.url, `http://${req.headers.host || "127.0.0.1"}`);
      const prefix = `/${runtime.installation_capability}/backend-api/codex/v1`;
      // Refuse local zlib negotiation without changing the remote-hop policy.
      if (!webSocketCompressionEnabled(socket.remoteAddress)) delete req.headers["sec-websocket-extensions"];
      if (url.pathname === `${prefix}/responses`) {
        responseWss.handleUpgrade(req, socket, head, (ws) => responseWss.emit("connection", ws, req, url));
        return;
      }
      if (url.pathname === `${prefix}/realtime` || url.pathname === `${prefix}/live`) {
        const suffix = url.pathname.endsWith("/live") ? "/live" : "/realtime";
        const protocols = protocolsFromHeaders(req.headers);
        const upstream = new WebSocket(
          `wss://api.openai.com/v1${suffix}${url.search}`,
          protocols.length ? protocols : undefined,
          {
            headers: headersForWebSocket(req.headers),
            perMessageDeflate: true,
            handshakeTimeout: 35_000,
            maxPayload: MAX_REQUEST_BYTES,
          },
        );
        upstream.once("open", () => {
          relayWss.handleUpgrade(req, socket, head, (ws) => relayWss.emit("connection", ws, req, upstream));
        });
        upstream.once("error", () => socket.destroy());
        upstream.once("unexpected-response", () => socket.destroy());
        return;
      }
      socket.write("HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n");
      socket.destroy();
    } catch {
      socket.destroy();
    }
  });

  return {
    server,
    async listen({ host = runtime.host || DEFAULT_HOST, port = runtime.port || DEFAULT_PORT } = {}) {
      await new Promise((resolve, reject) => {
        server.once("error", reject);
        server.listen(port, host, () => {
          server.off("error", reject);
          resolve();
        });
      });
      return server.address();
    },
    // Codex counts every refused WebSocket reconnect against its stream retry budget (five attempts,
    // about 6 s of backoff) and then disables WebSockets for the rest of that session, so a restart
    // must not keep the listener closed while lingering connections finish. They get a short grace,
    // then end; Codex retries a cut turn on the successor process.
    async close({ graceMs = Number(process.env.SWITCHBOARD_CLOSE_GRACE_MS ?? 500) } = {}) {
      for (const worker of ownedWorkers) worker.close();
      for (const client of responseWss.clients) client.close(1001, "server shutdown");
      for (const client of relayWss.clients) client.close(1001, "server shutdown");
      responseWss.close();
      relayWss.close();
      if (!server.listening) return;
      const closed = new Promise((resolve) => server.close(resolve));
      server.closeIdleConnections?.();
      const cut = setTimeout(() => {
        for (const client of responseWss.clients) client.terminate();
        for (const client of relayWss.clients) client.terminate();
        server.closeAllConnections?.();
      }, graceMs);
      await closed;
      clearTimeout(cut);
    },
    // Graceful restart: accept nothing new, close each Responses connection once it has no
    // turn in flight (a new turn on it is refused, so the client reconnects), let HTTP
    // requests finish, then report whether everything drained before the deadline.
    async drain({ timeoutMs = 300_000, sweepMs = 200 } = {}) {
      draining = true;
      if (server.listening) server.close();
      const deadline = Date.now() + timeoutMs;
      for (;;) {
        for (const client of responseWss.clients) {
          const active = socketTurns.get(client);
          if (!active || !active()) client.close(1012, "service restart");
        }
        server.closeIdleConnections?.();
        if (!responseWss.clients.size && !activeHttp.size) return true;
        if (Date.now() >= deadline) return false;
        await new Promise((resolve) => setTimeout(resolve, sweepMs));
      }
    },
    // Restart point that keeps serving: resolve once no POST or WebSocket turn is in flight (the
    // idleness workflow-update restarts in), or false after the bound. Nothing is refused meanwhile.
    async waitForIdle({ timeoutMs = 300_000, pollMs = 100 } = {}) {
      const deadline = Date.now() + timeoutMs;
      for (;;) {
        const now = activity();
        if (now.active_http_requests === 0 && now.active_ws_turns === 0) return true;
        if (Date.now() >= deadline) return false;
        await new Promise((resolve) => setTimeout(resolve, pollMs));
      }
    },
  };
}

// Supervised hot reload. The launchd job runs this file as a cluster primary that owns the
// listening socket for its whole life, so a reload never refuses a connection. SIGHUP (or SIGUSR2)
// forks a successor from the source on disk; once it reports ready, the predecessor drains: it
// stops receiving connections, closes each Responses socket when its turn ends (Codex reconnects to
// the successor) and exits after its last in-flight turn, so a reload cuts no turn and needs no idle
// moment. A successor that fails to start leaves the predecessor serving.
export function runSupervisor({ clusterApi = cluster, exec = fileURLToPath(import.meta.url),
  readyTimeoutMs = Number(process.env.SWITCHBOARD_RELOAD_READY_TIMEOUT_MS ?? 30_000),
  drainTimeoutMs = Number(process.env.SWITCHBOARD_RELOAD_DRAIN_TIMEOUT_MS ?? 3_600_000),
  signals = process, exit = (code) => process.exit(code), now = Date.now,
  log = (diagnostic) => safeLog({ route: "switchboard", status: 200, diagnostic }) } = {}) {
  clusterApi.setupPrimary({ exec });
  let current = null;
  let reloading = null;
  let stopping = false;
  const draining = new WeakSet();
  const starts = [];
  const live = () => Object.values(clusterApi.workers ?? {}).filter((worker) => worker && !worker.isDead?.());
  const start = () => {
    const at = now();
    while (starts.length && at - starts[0] > 60_000) starts.shift();
    starts.push(at);
    // Leave a crash loop to launchd throttling rather than spinning here.
    if (starts.length > 5) { log({ event: "supervisor_crash_loop" }); exit(1); return null; }
    return clusterApi.fork();
  };
  const awaitReady = (worker) => new Promise((resolve) => {
    let timer = null;
    const done = (result) => {
      clearTimeout(timer);
      worker.off("message", onMessage);
      worker.off("exit", onExit);
      resolve(result);
    };
    const onMessage = (message) => { if (message?.type === "switchboard-ready") done({ ok: true, message }); };
    const onExit = (code, signal) => done({ ok: false, failure: `exit:${code ?? signal}` });
    timer = setTimeout(() => done({ ok: false, failure: "ready_timeout" }), readyTimeoutMs);
    worker.on("message", onMessage);
    worker.once("exit", onExit);
  });
  clusterApi.on("exit", (worker, code, signal) => {
    if (stopping) {
      if (!live().length) exit(0);
      return;
    }
    // A drained predecessor or a failed successor exiting is expected.
    if (worker !== current) return;
    log({ event: "worker_exit_unexpected", worker_pid: worker.process?.pid ?? null, code, signal });
    current = start();
  });
  current = start();
  const reload = (reason) => {
    if (stopping) return Promise.resolve(false);
    if (reloading) return reloading;
    reloading = (async () => {
      const successor = clusterApi.fork();
      const ready = await awaitReady(successor);
      if (!ready.ok || stopping) {
        log({ event: "reload_failed", reason, failure: ready.failure ?? "stopping" });
        if (!successor.isDead?.()) successor.process.kill("SIGTERM");
        return false;
      }
      const previous = current;
      current = successor;
      for (const worker of live()) {
        if (worker === successor || draining.has(worker)) continue;
        draining.add(worker);
        worker.send({ type: "switchboard-drain", timeoutMs: drainTimeoutMs });
      }
      log({ event: "reload_ready", reason, previous_pid: previous?.process?.pid ?? null,
        worker_pid: successor.process?.pid ?? null,
        adapter_source_sha256: ready.message.adapter_source_sha256 ?? null });
      return true;
    })().finally(() => { reloading = null; });
    return reloading;
  };
  signals.on("SIGHUP", () => { reload("SIGHUP"); });
  signals.on("SIGUSR2", () => { reload("SIGUSR2"); });
  const stop = () => {
    stopping = true;
    const workers = live();
    if (!workers.length) { exit(0); return; }
    for (const worker of workers) worker.process.kill("SIGTERM");
    setTimeout(() => exit(0), 10_000).unref?.();
  };
  signals.once("SIGTERM", stop);
  signals.once("SIGINT", stop);
  return { reload, current: () => current };
}

async function main() {
  if (cluster.isPrimary && process.env.SWITCHBOARD_SUPERVISOR !== "0") {
    runSupervisor();
    return;
  }
  const [runtime, storedAdmission] = await Promise.all([
    readFile(process.env.SWITCHBOARD_RUNTIME_PATH || RUNTIME_PATH, "utf8").then(JSON.parse),
    readFile(process.env.SWITCHBOARD_ADMISSION_PATH || ADMISSION_PATH, "utf8").then(JSON.parse),
  ]);
  const staging = process.env.SWITCHBOARD_STAGING_ALLOW_UNADMITTED === "1";
  const admission = staging
    ? { ...storedAdmission, status: "ADMITTED", external_batch_admitted: true }
    : storedAdmission;
  const claudeBridge = await loadClaudeBridge().catch((error) => {
    safeLog({ route: "claude", status: 503, error_code: "CLAUDE_BRIDGE_LOAD_FAILED", diagnostic: { message: error.message } });
    return null;
  });
  const instance = await createSwitchboardServer({ runtime, admission, skipFileGate: staging, claudeBridge });
  const port = Number(process.env.SWITCHBOARD_PORT || runtime.port || DEFAULT_PORT);
  await instance.listen({ host: runtime.host || DEFAULT_HOST, port });
  startStdoutLogRotation(fileURLToPath(new URL("../runtime/switchboard.stdout.log", import.meta.url)));
  safeLog({ route: "switchboard", status: 200, diagnostic: { event: "listening", port,
    adapter_source_sha256: runningSourceSha256, claude_route_source_sha256: claudeBridge?.route_source_sha256 || null,
    process_uptime_ms: Math.round(process.uptime() * 1000) } });
  const stop = async () => {
    await instance.close();
    process.exit(0);
  };
  process.once("SIGTERM", stop);
  process.once("SIGINT", stop);
  if (cluster.isWorker) {
    // Supervised: the primary sends drain once a successor is ready.
    let drainStarted = false;
    process.on("message", async (message) => {
      if (message?.type !== "switchboard-drain" || drainStarted) return;
      drainStarted = true;
      const started = Date.now();
      safeLog({ route: "switchboard", status: 200, diagnostic: { event: "drain_started" } });
      const drained = await instance.drain({ timeoutMs: Number(message.timeoutMs) || 3_600_000 });
      safeLog({ route: "switchboard", status: 200, diagnostic: {
        event: drained ? "drain_complete" : "drain_timeout", drain_ms: Date.now() - started } });
      await instance.close();
      process.exit(0);
    });
    process.send({ type: "switchboard-ready", adapter_source_sha256: runningSourceSha256 });
    return;
  }
  // SIGUSR2 restarts without cutting turns or refusing connections: keep serving until no turn is
  // in flight, then exit for launchd KeepAlive to start the successor. Without an idle moment within
  // the bound it cuts instead of draining: a listener-closing drain refused every reconnect for as
  // long as the longest turn ran (10.5 s and 105 s observed), and Codex then left WebSockets for HTTP
  // in every session that tried to reconnect meanwhile (019ff1d0 and 01a1511f after 2026-10-26 01:47Z).
  process.once("SIGUSR2", async () => {
    const started = Date.now();
    safeLog({ route: "switchboard", status: 200, diagnostic: { event: "restart_waiting_for_idle" } });
    const idle = await instance.waitForIdle({
      timeoutMs: Number(process.env.SWITCHBOARD_IDLE_RESTART_TIMEOUT_MS ?? 300_000) });
    if (idle) {
      safeLog({ route: "switchboard", status: 200, diagnostic: { event: "restart_idle",
        idle_wait_ms: Date.now() - started } });
    } else {
      safeLog({ route: "switchboard", status: 200, diagnostic: { event: "restart_forced",
        idle_wait_ms: Date.now() - started } });
    }
    await instance.close();
    process.exit(0);
  });
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main().catch((error) => {
    safeLog({ route: "switchboard", status: 1, error_code: error.code || "SWITCHBOARD_START_FAILED" });
    process.exitCode = 1;
  });
}
