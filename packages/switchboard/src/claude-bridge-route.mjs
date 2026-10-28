// Claude models served by the local Claude-for-ChatGPT bridge, routed through
// the Switchboard so that it remains the single entry for every model.
// The bridge keeps Claude sessions, compaction checkpoints and tool calls; the
// Switchboard forwards Claude requests to it unchanged and opens Claude
// checkpoints (sealed with the bridge token) before any other route sees them.
import http from "node:http";
import { readFile } from "node:fs/promises";
import { createHash, randomUUID } from "node:crypto";
import { pathToFileURL } from "node:url";
import { nativeSseProjector } from "./nokiy-native-tool.mjs";

const routeSourceSha256 = createHash("sha256").update(await readFile(new URL(import.meta.url))).digest("hex");

export const CLAUDE_BRIDGE_ROOT = "/Users/nokiy/Library/Application Support/Claude for ChatGPT Desktop";
// Requests the Switchboard sends to the bridge carry this header. The bridge
// relays non-Claude traffic to its upstream (the Switchboard) with the header
// intact, so such a request is never sent back to Claude: no routing loop.
export const HOP_HEADER = "x-switchboard-claude-hop";
const HOP = new Set(["connection", "keep-alive", "proxy-connection", "transfer-encoding", "upgrade", "te",
  "trailer", "proxy-authorization", "proxy-authenticate", "host", "content-length", "content-encoding"]);
const WS_ONLY = new Set(["sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions",
  "sec-websocket-protocol", "sec-websocket-accept"]);
const TERMINAL_EVENTS = new Set(["response.completed", "response.failed", "response.incomplete", "error"]);
const safeLabel = (value) => typeof value === "string" && /^[A-Za-z][A-Za-z0-9_-]{0,79}$/.test(value) ? value : undefined;
const safeTransportCode = (error) => /^(?:E[A-Z0-9_]{1,39}|ABORT_ERR)$/.test(error?.code || "") ? error.code : "NETWORK_ERROR";
const streamFailure = (code) => ({ type: "server_error", code,
  message: "The local Claude bridge response ended before a complete terminal event. No request was replayed." });

export async function loadClaudeBridge({ root = process.env.SWITCHBOARD_CLAUDE_BRIDGE_ROOT || CLAUDE_BRIDGE_ROOT,
  enabled, activationPath = new URL("../runtime/activation-state.json", import.meta.url) } = {}) {
  if (enabled === false || process.env.SWITCHBOARD_CLAUDE_BRIDGE === "0") return null;
  if (enabled !== true) {
    let activation;
    try { activation = JSON.parse(await readFile(activationPath, "utf8")); }
    catch (error) { if (error.code !== "ENOENT") throw error; }
    if (activation?.routing_binding?.owner === "switchboard" && activation.routing_binding.claude_enabled === false) return null;
  }
  let launch, key, token, picker, combined;
  try {
    [launch, key, token, picker, combined] = await Promise.all([
      readFile(root + "/launch.json", "utf8").then(JSON.parse),
      readFile(root + "/gateway-key", "utf8").then((value) => value.trim()),
      readFile(root + "/token", "utf8").then((value) => value.trim()),
      readFile(root + "/integration-models.json", "utf8").then(JSON.parse),
      readFile(root + "/combined-models.json", "utf8").then(JSON.parse),
    ]);
  } catch (error) {
    if (error.code === "ENOENT") return null;
    throw error;
  }
  const port = Number(process.env.SWITCHBOARD_CLAUDE_BRIDGE_PORT || launch.port);
  const slugs = (picker.models || []).map((model) => model?.slug).filter((slug) => typeof slug === "string");
  if (!Number.isInteger(port) || !key || !token || !slugs.length) {
    throw new Error("Claude bridge installation is incomplete");
  }
  const gateway = await import(pathToFileURL(launch.source + "/src/gateway.mjs").href);
  const catalogRows = (combined.models || []).filter((row) => slugs.includes(row?.slug));
  return createClaudeBridge({ port, key, token, slugs, catalogRows, prepare: gateway.prepareForOpenAI });
}

export function createClaudeBridge({ host = "127.0.0.1", port, key, token, slugs, catalogRows = [], prepare }) {
  const models = new Set(slugs);
  const basePath = "/g/" + key + "/backend-api/codex";

  // The served model list carries the Claude rows after the admitted ones.
  const withCatalogRows = (catalog) => {
    if (!Array.isArray(catalog?.models) || !catalogRows.length) return catalog;
    const present = new Set(catalog.models.map((row) => row?.slug));
    const missing = catalogRows.filter((row) => !present.has(row.slug));
    return missing.length ? { ...catalog, models: [...catalog.models, ...missing] } : catalog;
  };

  const forwardHeaders = (source, extra = {}) => {
    const headers = {};
    for (const [name, value] of Object.entries(source || {})) {
      const lower = name.toLowerCase();
      if (!HOP.has(lower) && !WS_ONLY.has(lower) && lower !== HOP_HEADER) headers[lower] = value;
    }
    return { ...headers, ...extra, [HOP_HEADER]: "1" };
  };

  // Opens Claude checkpoints and drops bridge-made ids in input bound for any
  // other route. Returns null when the input has nothing of Claude's.
  const sanitize = (input) => {
    if (!Array.isArray(input)) return null;
    return prepare(input, token);
  };

  const unavailable = (error) => ({
    error: {
      type: "server_error",
      code: "CLAUDE_BRIDGE_UNAVAILABLE",
      message: "The Switchboard could not reach the local Claude bridge (" + safeTransportCode(error) + ").",
    },
  });

  // This is local dependency readiness, not a Claude login or inference check.
  // One bounded authenticated health request observes each call; no retry/cache.
  const dependencyReadiness = ({ timeoutMs = 1000 } = {}) => new Promise((resolve) => {
    const started = Date.now();
    let settled = false, responseStatus = null, request;
    const finish = (error_code, transport_error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve({ status: error_code ? "UNREADY" : "READY", provider_readiness: "UNKNOWN",
        http_status: responseStatus, observed_at: new Date().toISOString(), latency_ms: Date.now() - started,
        ...(error_code ? { error_code } : {}), ...(transport_error ? { transport_error } : {}) });
    };
    const limit = Number.isFinite(timeoutMs) ? Math.max(1, Math.min(timeoutMs, 5000)) : 1000;
    const timer = setTimeout(() => {
      finish("CLAUDE_BRIDGE_HEALTH_TIMEOUT");
      request.destroy();
    }, limit);
    request = http.get({ host, port, path: "/health", headers: { "x-claude-bridge-key": token, accept: "application/json" } }, (response) => {
      responseStatus = response.statusCode;
      if (response.statusCode !== 200) {
        finish("CLAUDE_BRIDGE_HEALTH_HTTP_ERROR");
        response.destroy();
        return;
      }
      let bytes = 0;
      const chunks = [];
      response.on("data", (chunk) => {
        bytes += chunk.length;
        if (bytes > 16_384) { finish("CLAUDE_BRIDGE_HEALTH_TOO_LARGE"); response.destroy(); }
        else chunks.push(chunk);
      });
      response.on("end", () => {
        let health;
        try { health = JSON.parse(Buffer.concat(chunks).toString()); }
        catch { finish("CLAUDE_BRIDGE_HEALTH_INVALID"); return; }
        if (health?.status !== "ok") finish("CLAUDE_BRIDGE_HEALTH_INVALID");
        else if (health.picker !== true) finish("CLAUDE_BRIDGE_PICKER_UNAVAILABLE");
        else if (!Array.isArray(health.models) || [...models].some((model) => !health.models.includes(model))) {
          finish("CLAUDE_BRIDGE_MODEL_CATALOG_MISMATCH");
        } else finish();
      });
      response.on("aborted", () => finish("CLAUDE_BRIDGE_HEALTH_INTERRUPTED"));
      response.on("error", () => finish("CLAUDE_BRIDGE_HEALTH_INTERRUPTED"));
    });
    request.on("error", (error) => finish("CLAUDE_BRIDGE_UNAVAILABLE", safeTransportCode(error)));
  });

  // HTTP: the body is relayed unchanged and the SSE stream is piped back.
  const proxyHttp = ({ req, res, raw, endpoint, search = "" }) => new Promise((resolve) => {
    const headers = forwardHeaders(req.headers, { "content-length": String(raw.length) });
    if (req.headers["content-encoding"]) headers["content-encoding"] = req.headers["content-encoding"];
    let clientClosed = false, settled = false, responseStatus = null, terminalEvent = null, sseObservation = "not_sse";
    let upstreamErrorCode;
    const finish = (status, terminal_cause, error_code, transport_error) => {
      if (settled) return;
      settled = true;
      resolve({ status, terminal_cause, upstream_http_status: responseStatus, upstream_terminal_event: terminalEvent,
        sse_observation: sseObservation, error_code, transport_error, upstream_error_code: upstreamErrorCode });
    };
    const upstream = http.request({ host, port, method: "POST", path: basePath + endpoint + search, headers }, (response) => {
      responseStatus = response.statusCode;
      const out = {};
      for (const [name, value] of Object.entries(response.headers)) {
        if (!HOP.has(name) || name === "content-encoding") out[name] = value;
      }
      const isSse = String(response.headers["content-type"] || "").includes("text/event-stream");
      const identity = !response.headers["content-encoding"] || response.headers["content-encoding"] === "identity";
      sseObservation = isSse ? identity ? "observing" : "unsupported_content_encoding" : "not_sse";
      let observer = isSse && identity ? nativeSseProjector((event) => {
        if (!terminalEvent && TERMINAL_EVENTS.has(event?.type)) {
          terminalEvent = event.type;
          upstreamErrorCode = safeLabel(event.type === "error" ? event.error?.code : event.response?.error?.code);
        }
        return null;
      }) : null;
      response.on("data", (chunk) => {
        if (!observer) return;
        try { observer.push(chunk); }
        catch { observer = null; sseObservation = "unparseable"; }
      });
      res.writeHead(response.statusCode, out);
      response.pipe(res);
      response.on("end", () => {
        if (observer) try { observer.end(); } catch { sseObservation = "unparseable"; }
        if (responseStatus >= 400) finish(responseStatus, "upstream_http_error", "CLAUDE_BRIDGE_UPSTREAM_HTTP_ERROR");
        else if (["response.failed", "error"].includes(terminalEvent)) {
          finish(502, "upstream_terminal_failure", "CLAUDE_BRIDGE_UPSTREAM_RESPONSE_FAILED");
        } else if (isSse && identity && !terminalEvent) {
          finish(502, "upstream_eof_without_terminal", "CLAUDE_BRIDGE_STREAM_INCOMPLETE");
        } else finish(responseStatus, terminalEvent ? "upstream_terminal_event" : "upstream_end");
      });
      const interrupted = (error) => {
        finish(clientClosed ? 499 : 502, clientClosed ? "downstream_close" : "upstream_stream_error",
          clientClosed ? undefined : "CLAUDE_BRIDGE_STREAM_INTERRUPTED", error ? safeTransportCode(error) : undefined);
        if (!res.writableFinished) res.destroy();
      };
      response.on("error", interrupted);
      response.on("aborted", () => interrupted());
    });
    upstream.on("error", (error) => {
      if (settled) return;
      if (!clientClosed && !res.headersSent) {
        res.writeHead(502, { "content-type": "application/json" });
        res.end(JSON.stringify(unavailable(error)));
      } else if (!res.writableFinished) res.destroy();
      finish(clientClosed ? 499 : 502, clientClosed ? "downstream_close" : "upstream_request_error",
        clientClosed ? undefined : "CLAUDE_BRIDGE_UNAVAILABLE", safeTransportCode(error));
    });
    res.on("close", () => {
      if (!res.writableFinished) {
        clientClosed = true;
        finish(499, "downstream_close");
        upstream.destroy();
      }
    });
    upstream.end(raw);
  });

  // WebSocket turn: the app's response.create becomes one streamed HTTP
  // request to the bridge, with the full input (previous_response_id is
  // expanded from this connection's Claude history), and each SSE event
  // becomes one WebSocket message, as the bridge's own channel does.
  const runWsTurn = ({ message, upgradeHeaders, history, send }) => {
    const { type, previous_response_id: previous, generate, client_metadata: metadata = {}, ...body } = message;
    let input = Array.isArray(body.input) ? body.input : [];
    if (previous) {
      const prior = history.get(previous);
      if (prior?.route !== "claude") {
        send({ type: "error", status: 400, error: { type: "invalid_request_error", code: "previous_response_not_found",
          message: "The previous Claude response is not on this connection." } });
        return { done: Promise.resolve({ status: 400, terminal: true, aborted: false,
          error_code: "previous_response_not_found", terminal_cause: "request_rejected" }), abort() {} };
      }
      input = [...prior.input, ...prior.output, ...input];
    }
    if (generate === false) {
      const id = "resp_switchboard" + randomUUID().replaceAll("-", "");
      const response = { id, object: "response", created_at: Math.floor(Date.now() / 1000), model: body.model, status: "completed", output: [] };
      send({ type: "response.created", response: { ...response, status: "in_progress" } });
      send({ type: "response.completed", response: { ...response, usage: { input_tokens: 0, output_tokens: 0, total_tokens: 0 } } });
      history.set(id, { route: "claude", input, output: [], model: body.model });
      return { done: Promise.resolve({ responseId: id, status: 200, terminal: true, aborted: false,
        terminal_cause: "local_prewarm" }), abort() {} };
    }
    const extra = {};
    if (metadata["x-codex-turn-metadata"]) extra["x-codex-turn-metadata"] = metadata["x-codex-turn-metadata"];
    if (metadata.thread_id) extra["thread-id"] = metadata.thread_id;
    const payload = Buffer.from(JSON.stringify({ ...body, input, stream: true }));
    const headers = forwardHeaders(upgradeHeaders, { ...extra, "content-type": "application/json",
      accept: "text/event-stream", "content-length": String(payload.length) });
    let responseId = null, output = [], terminal = false, aborted = false, finished = false, request = null;
    let status = 200, terminalCause = null, errorCode, upstreamStatus = null, terminalEvent = null;
    const done = new Promise((resolve) => {
      const finish = (cause = "upstream_end", failureCode = "CLAUDE_BRIDGE_STREAM_INCOMPLETE") => {
        if (finished) return;
        finished = true;
        terminalCause ||= aborted ? "downstream_cancel" : cause;
        if (aborted) status = 499;
        if (!terminal) {
          if (aborted && responseId) {
            send({ type: "response.incomplete", response: { id: responseId, object: "response", status: "incomplete",
              output: [], incomplete_details: { reason: "interrupted" } } });
          } else if (!aborted) {
            status = 502;
            errorCode = failureCode;
            send({ type: "error", status, error: streamFailure(failureCode) });
            terminal = true;
          }
        }
        if (responseId) history.set(responseId, { route: "claude", input, output, model: body.model });
        resolve({ responseId, terminal, aborted, status, error_code: errorCode, terminal_cause: terminalCause,
          upstream_http_status: upstreamStatus, upstream_terminal_event: terminalEvent });
      };
      request = http.request({ host, port, method: "POST", path: basePath + "/responses", headers }, (response) => {
        upstreamStatus = response.statusCode;
        response.on("error", () => finish("upstream_stream_error", "CLAUDE_BRIDGE_STREAM_INTERRUPTED"));
        response.on("aborted", () => finish("upstream_stream_error", "CLAUDE_BRIDGE_STREAM_INTERRUPTED"));
        if (response.statusCode >= 400) {
          const chunks = [];
          let bytes = 0;
          response.on("data", (chunk) => {
            bytes += chunk.length;
            if (bytes <= 65_536) chunks.push(chunk);
          });
          response.on("end", () => {
            if (finished) return;
            const text = Buffer.concat(chunks).toString();
            let error;
            try { error = JSON.parse(text).error; } catch {}
            status = response.statusCode;
            errorCode = safeLabel(error?.code) || "CLAUDE_BRIDGE_UPSTREAM_HTTP_ERROR";
            terminalCause = "upstream_http_error";
            const type = safeLabel(error?.type) || (status >= 500 ? "server_error" : status === 429 ? "rate_limit_error" : "invalid_request_error");
            const message = typeof error?.message === "string" ? error.message
              : "The local Claude bridge returned HTTP " + status + ".";
            send({ type: "error", status, error: { type, code: errorCode, message } });
            terminal = true;
            finish();
          });
          return;
        }
        const sse = nativeSseProjector((event) => {
          if (event.type === "response.created") responseId = event.response?.id ?? responseId;
          if (event.type === "response.output_item.done" && event.item) output.push(event.item);
          if (event.type === "response.completed" && Array.isArray(event.response?.output)) output = event.response.output;
          if (TERMINAL_EVENTS.has(event.type) && !terminal) {
            terminal = true;
            terminalEvent = event.type;
            terminalCause = ["response.failed", "error"].includes(event.type) ? "upstream_terminal_failure" : "upstream_terminal_event";
            if (terminalCause === "upstream_terminal_failure") {
              status = 502;
              errorCode = safeLabel(event.type === "error" ? event.error?.code : event.response?.error?.code)
                || "CLAUDE_BRIDGE_UPSTREAM_RESPONSE_FAILED";
            }
          }
          send(event);
          return null;
        });
        response.on("data", (chunk) => {
          if (finished) return;
          try { sse.push(chunk); }
          catch {
            finish("upstream_invalid_sse", "CLAUDE_BRIDGE_INVALID_SSE");
            response.destroy();
          }
        });
        response.on("end", () => {
          if (finished) return;
          try { sse.end(); }
          catch { finish("upstream_invalid_sse", "CLAUDE_BRIDGE_INVALID_SSE"); return; }
          finish(terminal ? "upstream_terminal_event" : "upstream_eof_without_terminal");
        });
      });
      request.on("error", (error) => {
        if (finished) return;
        if (!aborted) {
          send({ type: "error", status: 502, error: unavailable(error).error });
          terminal = true;
          status = 502;
          errorCode = "CLAUDE_BRIDGE_UNAVAILABLE";
          terminalCause = "upstream_request_error";
        }
        finish();
      });
      request.end(payload);
    });
    return { done, abort() { aborted = true; request?.destroy(); } };
  };

  return { models, sanitize, proxyHttp, runWsTurn, withCatalogRows, dependencyReadiness, port, hopHeader: HOP_HEADER,
    route_source_sha256: routeSourceSha256 };
}

