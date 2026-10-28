import { randomUUID } from "node:crypto";
import { lstatSync, readFileSync } from "node:fs";
import path from "node:path";
import Ajv from "ajv";

import { SseDecoder } from "./shared.mjs";
import { boundExternalHistory, prepareXaiBody, restoreEvent } from "./tool-bridge.mjs";

const RESPONSES_URL = process.env.SWITCHBOARD_OPENROUTER_RESPONSES_URL
  || "https://openrouter.ai/api/v1/responses";
const CHAT_COMPLETIONS_URL = process.env.SWITCHBOARD_OPENROUTER_CHAT_URL
  || "https://openrouter.ai/api/v1/chat/completions";
const CREDENTIAL_PATH = process.env.SWITCHBOARD_OPENROUTER_KEY_PATH
  || "/Users/nokiy/.codex/codex-router/openrouter-api-key.secret";
const REQUEST_TIMEOUT_MS = Number(process.env.SWITCHBOARD_EXTERNAL_TIMEOUT_MS || 330_000);
const ACTION_CONTEXT_BYTES = Number(process.env.SWITCHBOARD_OPENROUTER_ACTION_CONTEXT_BYTES || 64 * 1024);
const TERMINAL_EVENTS = new Set(["response.completed", "response.failed", "response.incomplete", "error"]);
const actionAjv = new Ajv({ strict: false, allErrors: false });

function workerError(code, message, status = 502) {
  return Object.assign(new Error(message), { code, status });
}

function credential() {
  let directory;
  let entry;
  try {
    directory = lstatSync(path.dirname(CREDENTIAL_PATH));
    entry = lstatSync(CREDENTIAL_PATH);
  } catch {
    throw workerError(
      "OPENROUTER_AUTH_UNAVAILABLE",
      "OpenRouter API key is not configured in protected local storage.",
      503,
    );
  }
  const currentUid = process.getuid?.();
  const unsafe = !directory.isDirectory()
    || directory.isSymbolicLink()
    || (directory.mode & 0o077) !== 0
    || !entry.isFile()
    || entry.isSymbolicLink()
    || (entry.mode & 0o077) !== 0
    || (currentUid !== undefined && (directory.uid !== currentUid || entry.uid !== currentUid));
  if (unsafe) {
    throw workerError(
      "OPENROUTER_CREDENTIAL_PERMISSIONS",
      "OpenRouter credential storage must be owner-only, regular, and symlink-free.",
      503,
    );
  }
  const value = readFileSync(CREDENTIAL_PATH, "utf8").trim();
  if (!value) {
    throw workerError("OPENROUTER_AUTH_UNAVAILABLE", "OpenRouter API key is empty.", 503);
  }
  return value;
}

function requestSignal(signal) {
  const timeout = AbortSignal.timeout(REQUEST_TIMEOUT_MS);
  return signal ? AbortSignal.any([signal, timeout]) : timeout;
}

function cancelledError() {
  return workerError(
    "OPENROUTER_REQUEST_CANCELLED",
    "OpenRouter request was cancelled when the downstream session closed.",
    499,
  );
}

async function requestOpenRouter(body, signal, url = RESPONSES_URL) {
  let response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers: {
        authorization: `Bearer ${credential()}`,
        "content-type": "application/json",
        accept: body.stream === false ? "application/json" : "text/event-stream",
      },
      body: JSON.stringify(body),
      signal: requestSignal(signal),
    });
  } catch (error) {
    if (signal?.aborted) throw cancelledError();
    if (typeof error?.code === "string" && error.code.startsWith("OPENROUTER_")) throw error;
    if (error?.name === "AbortError" || error?.name === "TimeoutError") {
      throw workerError("OPENROUTER_TIMEOUT", "OpenRouter request timed out.", 504);
    }
    throw workerError("OPENROUTER_NETWORK_ERROR", "OpenRouter request could not be delivered.");
  }
  if (!response.ok) {
    await response.body?.cancel().catch(() => {});
    const code = response.status === 401 || response.status === 403
      ? "OPENROUTER_AUTH_REJECTED"
      : response.status === 429
        ? "OPENROUTER_RATE_LIMITED"
        : "OPENROUTER_UPSTREAM_REJECTED";
    throw workerError(code, `OpenRouter rejected the request with HTTP ${response.status}.`, response.status);
  }
  return response;
}

function openRouterReasoningEffort(value) {
  if (value === "low" || value === "medium") return value;
  return "high";
}

function preparedBody(body) {
  const inputAuthority = (Array.isArray(body?.input) ? body.input : [])
    .filter((item) => item?.type === "message"
      && (item.role === "developer" || item.role === "system"));
  let projectedBody = body;
  if (inputAuthority.length && !String(body?.instructions || "").trim()) {
    const projectedInstructions = inputAuthority.map(chatMessageText).filter(Boolean);
    if (projectedInstructions.length !== inputAuthority.length) {
      throw workerError(
        "OPENROUTER_AUTHORITY_PROJECTION_MISSING",
        "OpenRouter cannot project non-text input authority into top-level instructions.",
        422,
      );
    }
    projectedBody = {
      ...body,
      instructions: projectedInstructions.join("\n\n"),
    };
  }
  const prepared = prepareXaiBody(projectedBody, { retainHistoryAuthority: false });
  if (prepared.clean.reasoning && typeof prepared.clean.reasoning === "object") {
    prepared.clean.reasoning = {
      effort: openRouterReasoningEffort(prepared.clean.reasoning.effort),
    };
  }
  const actions = Array.isArray(prepared.clean.tools) ? prepared.clean.tools : [];
  if (actions.length) {
    prepared.clean.input = boundExternalHistory(
      prepared.clean.input,
      ACTION_CONTEXT_BYTES,
      { includeOptionalHistory: false },
    );
    const currentActionResultCount = prepared.clean.input
      .filter((item) => item?.type === "function_call_output"
        || item?.type === "custom_tool_call_output").length;
    const actionSurface = actions.map((tool) => ({
      name: tool.name,
      description: String(tool.description || "").slice(0, 600),
      parameters: tool.parameters || { type: "object" },
    }));
    prepared.clean.instructions = [
      prepared.clean.instructions,
      "OPENROUTER_STRUCTURED_ACTION_V1",
      "Native provider tool calls are disabled for this request. Return exactly one strict JSON object without markdown.",
      'To request a caller action: {"kind":"tool_call","tool_name":"<exact action name>","arguments":{...}}.',
      'To finish without an action: {"kind":"message","text":"<final answer>"}.',
      "Action arguments must pass the exact advertised JSON Schema. Omit optional numeric fields instead of inventing ranges or strings.",
      "Never claim an action completed until a matching caller action result appears in task history.",
      `AVAILABLE_ACTIONS=${JSON.stringify(actionSurface)}`,
    ].filter(Boolean).join("\n\n");
    prepared.clean.input = (Array.isArray(prepared.clean.input) ? prepared.clean.input : [])
      .map((item) => {
        if (item?.type === "function_call") {
          return {
            type: "message",
            role: "assistant",
            content: [{
              type: "output_text",
              text: `[caller action requested call_id=${item.call_id || "unknown"}] ${item.name} ${item.arguments || "{}"}`,
            }],
          };
        }
        if (item?.type === "function_call_output") {
          return {
            type: "message",
            role: "user",
            content: [{
              type: "input_text",
              text: `[caller action result call_id=${item.call_id || "unknown"}] ${String(item.output || "")}`,
            }],
          };
        }
        return item;
      });
    prepared.clean.tools = [];
    delete prepared.clean.tool_choice;
    prepared.openRouterActionCompatibility = { actions, currentActionResultCount };
  }
  return prepared;
}

export function parseStructuredAction(
  text,
  actions,
  { allowPlainMessage = false } = {},
) {
  const raw = String(text || "").trim();
  const candidates = [raw];
  const fenced = raw.match(/```(?:json)?\s*([\s\S]*?)```/i)?.[1]?.trim();
  if (fenced) candidates.push(fenced);
  const objectStart = raw.indexOf("{");
  const objectEnd = raw.lastIndexOf("}");
  if (objectStart >= 0 && objectEnd > objectStart) {
    candidates.push(raw.slice(objectStart, objectEnd + 1));
  }
  let parsed = null;
  let bareObject = null;
  for (const candidate of [...new Set(candidates)]) {
    try {
      const value = JSON.parse(candidate);
      if (value && typeof value === "object" && !Array.isArray(value)) {
        if (typeof value.kind === "string") {
          parsed = value;
          break;
        }
        bareObject ||= value;
      }
    } catch {
      // Try the next deterministic representation.
    }
  }
  if (!parsed) {
    const transcriptMarker = /\[caller action requested(?:```[A-Za-z0-9_-]+)?\s+call_id=/g;
    const transcriptMarkers = raw.match(transcriptMarker) || [];
    const transcript = transcriptMarkers.length === 1
      ? raw.match(/\[caller action requested(?:```[A-Za-z0-9_-]+)?\s+call_id=[^\]]+\]\s+([A-Za-z0-9_-]+)\s+(\{[\s\S]*\})\s*$/)
      : null;
    if (transcript) {
      try {
        parsed = {
          kind: "tool_call",
          tool_name: transcript[1],
          arguments: JSON.parse(transcript[2]),
        };
      } catch {
        // The admitted schema check below must not receive malformed arguments.
      }
    }
  }
  if (!parsed && bareObject) {
    const matching = actions.filter((action) => {
      try {
        return actionAjv.compile(action.parameters || { type: "object" })(bareObject);
      } catch {
        return false;
      }
    });
    if (matching.length === 1) {
      parsed = {
        kind: "tool_call",
        tool_name: matching[0].name,
        arguments: bareObject,
      };
    }
  }
  const actionNarration = /\b(?:call|execute|executing|invoke|invoking|run|running|use|using)\b|(?:執行|呼叫|調用|使用工具|下一步|第一步|第二步)/i;
  const incompleteStructuredAction = /^(?:```(?:json)?\s*)?[\[{]/i.test(raw);
  if (!parsed
      && allowPlainMessage
      && raw
      && !actionNarration.test(raw)
      && !incompleteStructuredAction) {
    parsed = { kind: "message", text: raw };
  }
  if (!parsed) {
    throw workerError(
      "OPENROUTER_ACTION_JSON_INVALID",
      "OpenRouter returned a non-JSON action response.",
      422,
    );
  }
  if (parsed?.kind === "message" && typeof parsed.text === "string" && parsed.text.trim()) {
    return { kind: "message", text: parsed.text.trim() };
  }
  if (!parsed?.arguments && typeof parsed?.arguments_json === "string") {
    try {
      parsed.arguments = JSON.parse(parsed.arguments_json);
    } catch {
      throw workerError(
        "OPENROUTER_ACTION_SCHEMA_INVALID",
        "OpenRouter returned malformed action arguments.",
        422,
      );
    }
  }
  const selectedAction = actions.find((action) => action.name === parsed?.tool_name);
  if (parsed?.kind !== "tool_call"
      || typeof parsed.tool_name !== "string"
      || !selectedAction
      || !parsed.arguments
      || typeof parsed.arguments !== "object"
      || Array.isArray(parsed.arguments)) {
    throw workerError(
      "OPENROUTER_ACTION_SCHEMA_INVALID",
      "OpenRouter returned an action outside the admitted tool surface.",
      422,
    );
  }
  let validateArguments;
  try {
    validateArguments = actionAjv.compile(selectedAction.parameters || { type: "object" });
  } catch {
    throw workerError(
      "OPENROUTER_ACTION_SCHEMA_INVALID",
      "The admitted OpenRouter action schema could not be compiled.",
      422,
    );
  }
  if (!validateArguments(parsed.arguments)) {
    throw workerError(
      "OPENROUTER_ACTION_SCHEMA_INVALID",
      "OpenRouter returned arguments that do not satisfy the admitted action schema.",
      422,
    );
  }
  return {
    kind: "tool_call",
    toolName: parsed.tool_name,
    arguments: parsed.arguments,
  };
}

export function structuredActionEvents(action, terminal) {
  const providerResponseId = terminal?.response?.id || `resp_openrouter_${randomUUID()}`;
  const item = action.kind === "tool_call"
    ? {
        type: "function_call",
        id: `fc_openrouter_${providerResponseId}`,
        call_id: `call_openrouter_${providerResponseId}`,
        name: action.toolName,
        arguments: JSON.stringify(action.arguments),
      }
    : {
        type: "message",
        id: `msg_openrouter_${providerResponseId}`,
        role: "assistant",
        status: "completed",
        content: [{ type: "output_text", text: action.text }],
      };
  return [
    {
      type: "response.created",
      response: { id: providerResponseId, status: "in_progress", output: [] },
    },
    { type: "response.output_item.done", output_index: 0, item },
    {
      type: "response.completed",
      response: {
        id: providerResponseId,
        status: "completed",
        output: [item],
        ...(terminal?.response?.usage ? { usage: terminal.response.usage } : {}),
      },
    },
  ];
}

function normalizedResponsesUsage(usage = {}) {
  const input = Number(usage.input_tokens ?? usage.prompt_tokens ?? 0);
  const output = Number(usage.output_tokens ?? usage.completion_tokens ?? 0);
  return {
    input_tokens: input,
    input_tokens_details: usage.input_tokens_details
      || (usage.prompt_tokens_details
        ? { cached_tokens: Number(usage.prompt_tokens_details.cached_tokens || 0) }
        : null),
    output_tokens: output,
    output_tokens_details: usage.output_tokens_details
      || (usage.completion_tokens_details
        ? { reasoning_tokens: Number(usage.completion_tokens_details.reasoning_tokens || 0) }
        : null),
    total_tokens: Number(usage.total_tokens ?? input + output),
  };
}

function chatMessageText(item) {
  if (typeof item?.content === "string") return item.content;
  return (Array.isArray(item?.content) ? item.content : [])
    .map((part) => part?.text || part?.input_text || part?.output_text || "")
    .filter(Boolean)
    .join("\n");
}

function structuredActionChatBody(clean) {
  const messages = [
    { role: "system", content: clean.instructions },
    ...(Array.isArray(clean.input) ? clean.input : [])
      .filter((item) => item?.type === "message")
      .map((item) => ({
        role: item.role === "assistant" ? "assistant" : "user",
        content: chatMessageText(item),
      }))
      .filter((item) => item.content),
  ];
  return {
    model: clean.model,
    messages,
    stream: false,
    response_format: { type: "json_object" },
    plugins: [{ id: "response-healing" }],
    ...(clean.reasoning ? { reasoning: clean.reasoning } : {}),
    ...(Number.isFinite(clean.max_output_tokens)
      ? { max_tokens: clean.max_output_tokens }
      : {}),
  };
}

async function runStructuredActionChat(clean, compatibility, signal) {
  const requestAction = async (requestBody) => {
    const response = await requestOpenRouter(
      structuredActionChatBody(requestBody),
      signal,
      CHAT_COMPLETIONS_URL,
    );
    let result;
    try {
      result = await response.json();
    } catch {
      throw workerError(
        "OPENROUTER_ACTION_RESPONSE_INVALID",
        "OpenRouter returned invalid structured action JSON.",
      );
    }
    const content = result?.choices?.[0]?.message?.content;
    const text = typeof content === "string"
      ? content
      : (Array.isArray(content) ? content.map((part) => part?.text || "").join("") : "");
    return { result, text };
  };

  let repairCount = 0;
  let { result, text } = await requestAction(clean);
  let action;
  try {
    action = parseStructuredAction(
      text,
      compatibility.actions,
      { allowPlainMessage: compatibility.currentActionResultCount > 0 },
    );
  } catch (error) {
    if (!["OPENROUTER_ACTION_JSON_INVALID", "OPENROUTER_ACTION_SCHEMA_INVALID"].includes(error?.code)) throw error;
    repairCount = 1;
    ({ result, text } = await requestAction({
      ...clean,
      instructions: [
        clean.instructions,
        "OPENROUTER_STRUCTURED_ACTION_REPAIR_V1",
        "The prior response was rejected because it was not one complete strict JSON action object.",
        "Return only the required JSON object now. Do not add prose, markdown, or an action transcript.",
      ].join("\n\n"),
    }));
    action = parseStructuredAction(
      text,
      compatibility.actions,
      { allowPlainMessage: compatibility.currentActionResultCount > 0 },
    );
  }
  return {
    action,
    repairCount,
    terminalEvent: {
      type: "response.completed",
      response: { id: result?.id, usage: normalizedResponsesUsage(result?.usage) },
    },
  };
}

async function runResponses(body, emit, signal) {
  const {
    clean,
    bridge,
    diagnostics,
    openRouterActionCompatibility,
  } = preparedBody(body);
  if (openRouterActionCompatibility) {
    const { action, repairCount, terminalEvent } = await runStructuredActionChat(
      clean,
      openRouterActionCompatibility,
      signal,
    );
    for (const event of structuredActionEvents(action, terminalEvent)) {
      const restored = restoreEvent(event, bridge);
      if (restored) emit(restored);
    }
    return {
      status: 200,
      transport: "openrouter-chat-structured-action",
      tool_projection: {
        ...diagnostics,
        structured_action_repair_count: repairCount,
      },
    };
  }
  clean.stream = true;
  const forward = (event) => {
    const restored = restoreEvent(event, bridge);
    if (restored) emit(restored);
  };
  const collect = async (requestBody) => {
    const response = await requestOpenRouter(requestBody, signal);
    if (!response.body || !String(response.headers.get("content-type") || "").includes("text/event-stream")) {
      await response.body?.cancel().catch(() => {});
      throw workerError("OPENROUTER_STREAM_INVALID", "OpenRouter returned a non-streaming response.");
    }
    const decoder = new SseDecoder();
    let terminalEvent = null;
    let structuredText = "";
    let completedText = "";
    const consume = (event) => {
      if (openRouterActionCompatibility) {
        if (event?.type === "response.output_text.delta") structuredText += event.delta || "";
        if (event?.type === "response.output_item.done" && event.item?.type === "message") {
          completedText = responseText({ output: [event.item] }) || completedText;
        }
        if (TERMINAL_EVENTS.has(event?.type)) terminalEvent = event;
      } else {
        if (TERMINAL_EVENTS.has(event?.type)) terminalEvent = event;
        forward(event);
      }
    };
    for await (const chunk of response.body) {
      if (signal.aborted) throw cancelledError();
      for (const event of decoder.push(chunk)) consume(event);
    }
    for (const event of decoder.finish()) consume(event);
    if (!terminalEvent) {
      throw workerError("OPENROUTER_STREAM_INCOMPLETE", "OpenRouter stream ended without a terminal event.");
    }
    return {
      terminalEvent,
      structuredText: structuredText || completedText || responseText(terminalEvent.response),
    };
  };
  let collected = await collect(clean);
  let repairCount = 0;
  if (openRouterActionCompatibility) {
    if (collected.terminalEvent?.type !== "response.completed") {
      throw workerError(
        "OPENROUTER_ACTION_STREAM_FAILED",
        "OpenRouter action compatibility did not complete successfully.",
      );
    }
    let action;
    try {
      action = parseStructuredAction(
        collected.structuredText,
        openRouterActionCompatibility.actions,
      );
    } catch (error) {
      if (!["OPENROUTER_ACTION_JSON_INVALID", "OPENROUTER_ACTION_SCHEMA_INVALID"].includes(error?.code)) throw error;
      repairCount = 1;
      const repairBody = {
        ...clean,
        instructions: [
          clean.instructions,
          "OPENROUTER_STRUCTURED_ACTION_REPAIR_V1",
          "The prior response was rejected because it was not one strict JSON action object.",
          "Return only the required JSON object now. Do not add prose, markdown, or an action transcript.",
        ].join("\n\n"),
      };
      collected = await collect(repairBody);
      if (collected.terminalEvent?.type !== "response.completed") {
        throw workerError(
          "OPENROUTER_ACTION_STREAM_FAILED",
          "OpenRouter action compatibility repair did not complete successfully.",
        );
      }
      action = parseStructuredAction(
        collected.structuredText,
        openRouterActionCompatibility.actions,
      );
    }
    for (const event of structuredActionEvents(action, collected.terminalEvent)) forward(event);
  }
  return {
    status: 200,
    transport: "openrouter-responses",
    tool_projection: {
      ...diagnostics,
      structured_action_repair_count: repairCount,
    },
  };
}

function responseText(response) {
  if (typeof response?.output_text === "string" && response.output_text.trim()) {
    return response.output_text.trim();
  }
  return (Array.isArray(response?.output) ? response.output : [])
    .filter((item) => item?.type === "message")
    .flatMap((item) => Array.isArray(item.content) ? item.content : [])
    .map((part) => part?.text || part?.output_text || "")
    .filter(Boolean)
    .join("\n")
    .trim();
}

async function compact(body, signal) {
  const { clean } = preparedBody({ ...body, tools: [] });
  const summaryBody = {
    model: clean.model,
    store: false,
    stream: false,
    tools: [],
    reasoning: { effort: "low" },
    instructions: [
      "Create a canonical, provider-neutral context summary for another coding model.",
      "Preserve user requirements, decisions, file paths, code state, tool results, blockers, and pending work.",
      "Return only the summary. Do not use tools and do not include private reasoning.",
    ].join(" "),
    input: clean.input || [],
  };
  const response = await requestOpenRouter(summaryBody, signal);
  let parsed;
  try {
    parsed = await response.json();
  } catch {
    throw workerError("OPENROUTER_COMPACT_INVALID", "OpenRouter returned invalid compaction JSON.");
  }
  const summary = responseText(parsed);
  if (!summary) {
    throw workerError("OPENROUTER_COMPACT_EMPTY", "OpenRouter returned an empty canonical summary.");
  }
  return { summary, usage: parsed.usage || {} };
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

process.on("message", (job) => {
  if (!job || typeof job.id !== "string") return;
  if (job.op === "close_session") {
    for (const controller of activeSessions.get(job.session_key) || []) controller.abort();
    send({ id: job.id, kind: "done", result: { status: 200 } });
    return;
  }
  if (job.op === "crash" && process.env.SWITCHBOARD_TEST_ALLOW_CRASH === "1") process.exit(88);
  const sessionKey = job.session_key || job.id;
  const controller = new AbortController();
  registerSession(sessionKey, controller);
  const work = job.op === "compact"
    ? compact(job.body, controller.signal)
    : runResponses(job.body, (event) => {
        send({ id: job.id, kind: "event", event });
      }, controller.signal);
  const completion = Promise.resolve(work).then(
    (result) => send({ id: job.id, kind: "done", result }),
    (error) => {
      const cancelled = controller.signal.aborted || error?.code === "OPENROUTER_REQUEST_CANCELLED";
      send({
        id: job.id,
        kind: "error",
        error: cancelled
          ? {
              code: "OPENROUTER_REQUEST_CANCELLED",
              message: "OpenRouter request was cancelled when the downstream session closed.",
              status: 499,
            }
          : {
              code: error?.code || "OPENROUTER_WORKER_ERROR",
              message: error?.message || "OpenRouter worker failed.",
              status: error?.status || 502,
            },
      });
    },
  ).finally(() => unregisterSession(sessionKey, controller));
  activeWork.add(completion);
  completion.finally(() => activeWork.delete(completion));
});

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
