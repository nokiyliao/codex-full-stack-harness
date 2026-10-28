import { createHash, randomUUID } from "node:crypto";
import {
  brotliDecompressSync,
  brotliCompressSync,
  deflateSync,
  gzipSync,
  gunzipSync,
  inflateSync,
  zstdCompressSync,
  zstdDecompressSync,
} from "node:zlib";

export const DEFAULT_HOST = "127.0.0.1";
export const DEFAULT_PORT = 43130;
export const OFFICIAL_ORIGIN = "https://chatgpt.com";
export const OFFICIAL_BASE_PATH = "/backend-api/codex";
export const XAI_MODEL = "grok-4.7";
export const AGY_MODELS = Object.freeze([
  "gemini-3.8-flash-high",
  "gemini-4-argon-high",
  "claude-opus-4-6-thinking",
]);
export const LEGACY_AGY_MODEL_ALIASES = Object.freeze({
  "grok-agy-gemini-3.8-flash-high": "gemini-3.8-flash-high",
  "grok-agy-gemini-4-argon-high": "gemini-4-argon-high",
  "grok-agy-claude-opus-4-6-thinking": "claude-opus-4-6-thinking",
});
export const WEB_MODELS = Object.freeze([
  "chatgpt-web/light",
  "chatgpt-web/medium",
  "chatgpt-web/high",
  "chatgpt-web/extra-high",
  "chatgpt-web/pro",
]);
export const MAX_REQUEST_BYTES = 96 * 1024 * 1024;

export const EXTERNAL_MODELS = Object.freeze([
  ...WEB_MODELS,
  XAI_MODEL,
  ...AGY_MODELS,
]);
export const EXTERNAL_MULTI_AGENT_MODELS = AGY_MODELS;

export function canonicalAgyModel(model) {
  return typeof model === "string" && Object.hasOwn(LEGACY_AGY_MODEL_ALIASES, model)
    ? LEGACY_AGY_MODEL_ALIASES[model]
    : model;
}

export const EXTERNAL_HOSTED_TOOL_TYPES = new Set([
  "web_search",
  "web_search_preview",
  "image_generation",
  "computer_use_preview",
  "file_search",
]);

export const HOP_BY_HOP_HEADERS = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);

export class SwitchboardError extends Error {
  constructor(code, message, status = 502, details = undefined) {
    super(message);
    this.name = "SwitchboardError";
    this.code = code;
    this.status = status;
    this.details = details;
  }
}

export function errorPayload(error, requestId) {
  const typed = error instanceof SwitchboardError
    ? error
    : new SwitchboardError("SWITCHBOARD_INTERNAL_ERROR", "The local inference switchboard failed.");
  return {
    error: {
      message: typed.message,
      type: "codex_switchboard_error",
      code: typed.code,
      request_id: requestId,
      ...(typed.details === undefined ? {} : { details: typed.details }),
    },
  };
}

export function sendJson(res, status, value, extraHeaders = {}) {
  const body = Buffer.from(JSON.stringify(value));
  res.writeHead(status, {
    "content-type": "application/json",
    "content-length": String(body.length),
    "cache-control": "no-store",
    ...extraHeaders,
  });
  res.end(body);
}

export function sendTypedError(res, error, requestId) {
  const status = error instanceof SwitchboardError ? error.status : 502;
  sendJson(res, status, errorPayload(error, requestId));
}

export async function readRawBody(req, limit = MAX_REQUEST_BYTES) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > limit) {
      throw new SwitchboardError("REQUEST_TOO_LARGE", `Request exceeds ${limit} bytes.`, 413);
    }
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

export function decodeBody(raw, contentEncoding = "identity") {
  const encoding = String(contentEncoding || "identity").trim().toLowerCase();
  if (!raw.length || encoding === "identity") return raw;
  if (encoding === "zstd") return zstdDecompressSync(raw);
  if (encoding === "gzip") return gunzipSync(raw);
  if (encoding === "deflate") return inflateSync(raw);
  if (encoding === "br") return brotliDecompressSync(raw);
  throw new SwitchboardError(
    "UNSUPPORTED_REQUEST_ENCODING",
    `Unsupported request content-encoding: ${encoding}`,
    415,
  );
}

export function parseJsonBody(raw, contentEncoding) {
  try {
    return JSON.parse(decodeBody(raw, contentEncoding).toString("utf8"));
  } catch (error) {
    if (error instanceof SwitchboardError) throw error;
    throw new SwitchboardError("INVALID_REQUEST_JSON", "Request body is not valid JSON.", 400);
  }
}

export function encodeJsonBody(value, contentEncoding = "identity") {
  const raw = Buffer.from(JSON.stringify(value));
  const encoding = String(contentEncoding || "identity").trim().toLowerCase();
  if (encoding === "identity") return raw;
  if (encoding === "zstd") return zstdCompressSync(raw);
  if (encoding === "gzip") return gzipSync(raw);
  if (encoding === "deflate") return deflateSync(raw);
  if (encoding === "br") return brotliCompressSync(raw);
  throw new SwitchboardError(
    "UNSUPPORTED_REQUEST_ENCODING",
    `Unsupported request content-encoding: ${encoding}`,
    415,
  );
}

export function officialRequestHeaders(headers, rawLength) {
  const result = {};
  const connectionTokens = new Set(
    String(headers.connection || "")
      .split(",")
      .map((value) => value.trim().toLowerCase())
      .filter(Boolean),
  );
  for (const [rawName, rawValue] of Object.entries(headers)) {
    const name = rawName.toLowerCase();
    if (rawValue === undefined) continue;
    if (name === "host" || name === "content-length") continue;
    if (HOP_BY_HOP_HEADERS.has(name) || connectionTokens.has(name)) continue;
    result[name] = rawValue;
  }
  result["content-length"] = String(rawLength);
  return result;
}

export function officialResponseHeaders(headers) {
  const result = {};
  const connectionTokens = new Set(
    String(headers.connection || "")
      .split(",")
      .map((value) => value.trim().toLowerCase())
      .filter(Boolean),
  );
  for (const [rawName, rawValue] of Object.entries(headers)) {
    const name = rawName.toLowerCase();
    if (rawValue === undefined) continue;
    if (HOP_BY_HOP_HEADERS.has(name) || connectionTokens.has(name)) continue;
    result[name] = rawValue;
  }
  return result;
}

export function routeForModel(model, officialModels) {
  if (WEB_MODELS.includes(model)) return "web";
  if (officialModels.has(model)) return "openai";
  if (model === XAI_MODEL || model === "grok-4.6") return "xai";
  if (typeof model === "string" && AGY_MODELS.includes(canonicalAgyModel(model))) return "agy";
  throw new SwitchboardError(
    "MODEL_ROUTE_UNAVAILABLE",
    `No admitted route exists for model ${JSON.stringify(model)}.`,
    400,
  );
}

export function assertExternalRequestAllowed(body, endpoint) {
  if (endpoint !== "/responses" && endpoint !== "/responses/compact") {
    throw new SwitchboardError(
      "EXTERNAL_HOSTED_CAPABILITY_UNAVAILABLE",
      `Endpoint ${endpoint} is only available to official OpenAI models.`,
      400,
      { endpoint },
    );
  }
}

export function externalRequestBody(body) {
  const clean = structuredClone(body);
  clean.tools = (Array.isArray(clean.tools) ? clean.tools : [])
    .filter((tool) => !EXTERNAL_HOSTED_TOOL_TYPES.has(tool?.type));
  delete clean.service_tier;
  delete clean.prompt_cache_key;
  delete clean.safety_identifier;
  delete clean.client_metadata;
  return clean;
}

export function endpointFromPath(pathname, capability) {
  // Codex selects the ChatGPT realtime JSON contract when base_url contains
  // "/backend-api". Keep that compatibility marker in the local URL.
  const prefix = `/${capability}/backend-api/codex/v1`;
  if (!pathname.startsWith(`${prefix}/`)) {
    throw new SwitchboardError("SWITCHBOARD_NOT_READY", "Invalid installation capability.", 404);
  }
  const endpoint = pathname.slice(prefix.length);
  const allowed = new Set([
    "/responses",
    "/responses/compact",
    "/alpha/search",
    "/memories/trace_summarize",
    "/realtime/calls",
    "/images/generations",
    "/images/edits",
    "/models",
  ]);
  if (!allowed.has(endpoint)) {
    throw new SwitchboardError("ENDPOINT_NOT_SUPPORTED", `Unsupported endpoint ${endpoint}.`, 404);
  }
  return endpoint;
}

export function newRequestId() {
  return randomUUID();
}

export function officialErrorObservation(event) {
  if (!["response.failed", "response.incomplete", "error"].includes(event?.type)) return null;
  const error = event.type === "error" ? event.error : event.response?.error;
  if (!error || typeof error !== "object" || Array.isArray(error)) return null;
  const label = value => typeof value === "string" && /^[A-Za-z][A-Za-z0-9_-]{0,79}$/.test(value) ? value : null;
  const message = typeof error.message === "string" ? error.message : null;
  const messageBytes = message === null ? null : Buffer.byteLength(message);
  const boundedMessage = messageBytes !== null && messageBytes <= 65_536;
  const headers = error.headers && typeof error.headers === "object" && !Array.isArray(error.headers) ? error.headers : {};
  return {
    code: label(error.code),
    type: label(error.type),
    message_sha256: boundedMessage ? createHash("sha256").update(message).digest("hex") : null,
    message_bytes: messageBytes,
    message_hash_status: message === null ? "missing" : boundedMessage ? "hashed" : "too_large",
    retry_after_present: Object.keys(headers).some(key => key.toLowerCase() === "retry-after"),
  };
}

export function officialResponseObservation(event) {
  if (!["response.completed", "response.failed", "response.incomplete"].includes(event?.type)) return null;
  const response = event.response;
  const label = (value) => typeof value === "string" && /^[A-Za-z0-9_.:/-]{1,160}$/.test(value) ? value : null;
  const count = (value) => Number.isSafeInteger(value) && value >= 0 ? value : null;
  const usage = response?.usage;
  const input = count(usage?.input_tokens), output = count(usage?.output_tokens);
  const cached = count(usage?.input_tokens_details?.cached_tokens);
  const written = count(usage?.input_tokens_details?.cache_write_tokens);
  const writeDetails = usage?.input_tokens_details?.cache_write_tokens_details;
  const written30m = count(writeDetails?.ttl_30m_tokens);
  const written12h = count(writeDetails?.ttl_12h_tokens);
  const reasoning = count(usage?.output_tokens_details?.reasoning_tokens);
  const total = count(usage?.total_tokens);
  const invalid = (input !== null && cached !== null && cached > input)
    || (input !== null && written !== null && written > input)
    || (input !== null && cached !== null && written !== null && cached + written > input)
    || (written !== null && written30m !== null && written12h !== null && written30m + written12h !== written)
    || (output !== null && reasoning !== null && reasoning > output)
    || (input !== null && output !== null && total !== null && total !== input + output);
  return {
    source: "upstream_terminal_event",
    terminal_event: event.type,
    response_id: label(response?.id),
    model: label(response?.model),
    service_tier: label(response?.service_tier),
    usage_status: invalid ? "INCONSISTENT" : input === null || output === null ? "PARTIAL_OR_MISSING" : "REPORTED",
    input_tokens: input, cached_input_tokens: cached,
    cache_write_tokens: written, cache_write_30m_tokens: written30m, cache_write_12h_tokens: written12h,
    uncached_input_tokens: input !== null && cached !== null && cached <= input ? input - cached : null,
    output_tokens: output, reasoning_tokens: reasoning, total_tokens: total,
  };
}

export function officialRequestCorrelation(body, headers) {
  const uuid = value => typeof value === "string"
    && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(value) ? value : null;
  const session = headers?.["session-id"] ?? headers?.session_id;
  const conflict = headers?.["session-id"] !== undefined && headers?.session_id !== undefined
    && headers["session-id"] !== headers.session_id;
  const previous = body?.previous_response_id;
  const lastType = Array.isArray(body?.input) ? body.input.at(-1)?.type : null;
  return {
    source: "caller_request_not_authority",
    client_thread_id: uuid(headers?.["thread-id"]),
    client_session_id: conflict ? null : uuid(session),
    previous_response_id: typeof previous === "string" && /^resp_[A-Za-z0-9_-]{1,155}$/.test(previous) ? previous : null,
    last_input_type: ["message", "function_call_output", "custom_tool_call_output", "compaction"].includes(lastType) ? lastType : null,
  };
}

// The worker session an external turn belongs to, keyed the way the Claude
// bridge keys its sessions: the caller's stable thread plus its context window,
// so a reconnect or a new HTTP request reaches the same session and provider
// cache. Only a digest leaves this function. Null when the caller named no thread.
export function externalThreadSessionKey(body, headers = {}) {
  const label = (value) => typeof value === "string" && /^[A-Za-z0-9_-]{6,128}$/.test(value.trim())
    ? value.trim() : null;
  let turn = body?.client_metadata?.["x-codex-turn-metadata"] ?? headers?.["x-codex-turn-metadata"];
  if (typeof turn === "string") {
    try { turn = JSON.parse(turn); } catch { turn = null; }
  }
  const thread = label(body?.client_metadata?.thread_id) ?? label(turn?.thread_id)
    ?? label(headers?.["thread-id"]) ?? label(headers?.["session-id"]) ?? label(headers?.session_id)
    ?? label(body?.prompt_cache_key);
  if (!thread) return null;
  const window = label(turn?.context_window_id) ?? "default";
  return `thread-${createHash("sha256").update(`${thread}\u0000${window}`).digest("hex").slice(0, 32)}`;
}

export function safeLog(record) {
  const allowed = [
    "timestamp",
    "request_id",
    "model",
    "requested_alias",
    "provider_response",
    "provider_transport_timing",
    "result_delivery",
    "request_correlation",
    "attempt_lifecycle",
    "route",
    "endpoint",
    "transport",
    "status",
    "latency_ms",
    "request_bytes",
    "response_bytes",
    "error_code",
    "diagnostic",
    "tool_count",
    "namespace_count",
    "namespace_member_count",
    "discovered_tool_count",
    "custom_exec_top_level",
    "custom_exec_namespaced",
    "custom_exec_discovered",
    "exec_adapter_projected",
    "provider_request_bytes",
    "provider_prewarm_request_bytes",
    "provider_input_bytes",
    "provider_instruction_bytes",
    "provider_tools_bytes",
    "provider_reasoning_effort",
    "session_scope",
    // Worker continuation and cache observations; booleans, counts and labels only.
    "worker_transport",
    "compacted",
    "compaction_scope",
    "xai_prefix_reused",
    "agy_persistent_session",
    "agy_session_reused",
    "agy_session_turn_index",
    "agy_prefix_reused",
  ];
  const output = {};
  for (const key of allowed) {
    if (record[key] !== undefined) output[key] = record[key];
  }
  output.timestamp ||= new Date().toISOString();
  process.stdout.write(`${JSON.stringify(output)}\n`);
}

export function sseFrame(event) {
  return `event: ${event.type || "message"}\ndata: ${JSON.stringify(event)}\n\n`;
}

export class SseDecoder {
  #buffer = "";

  push(chunk) {
    this.#buffer += Buffer.from(chunk).toString("utf8");
    const frames = this.#buffer.split(/\r?\n\r?\n/);
    this.#buffer = frames.pop() || "";
    return frames.flatMap((frame) => {
      const data = frame
        .split(/\r?\n/)
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trimStart())
        .join("\n");
      if (!data || data === "[DONE]") return [];
      try {
        return [JSON.parse(data)];
      } catch {
        throw new SwitchboardError("INVALID_EXTERNAL_EVENT", "External adapter returned invalid SSE.");
      }
    });
  }

  finish() {
    if (!this.#buffer.trim()) return [];
    const tail = this.#buffer;
    this.#buffer = "";
    return this.push(`${tail}\n\n`);
  }
}

export function responseOutputItems(events) {
  const items = [];
  for (const event of events) {
    if (event?.type === "response.output_item.done" && event.item) items.push(event.item);
    if (event?.type === "response.completed" && Array.isArray(event.response?.output)) {
      return event.response.output;
    }
  }
  return items;
}

export function responseIdFromEvents(events) {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    const id = event?.response?.id;
    if (typeof id === "string" && id) return id;
  }
  return null;
}
