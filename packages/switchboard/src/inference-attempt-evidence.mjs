import { createHash, randomUUID } from "node:crypto";
import { parseJsonBody, safeLog } from "./shared.mjs";
import { efficientNativeLoop } from "./nokiy-workflow-profiles.mjs";
import { NOKIY_TOOL } from "./nokiy-native-tool.mjs";

export const ATTEMPT_SCHEMA = "switchboard_inference_attempt/v1";
export const ATTEMPT_SCOPE = "openai_responses_forwarding_v1";
const label = (value) => typeof value === "string" && /^[A-Za-z0-9_.:/-]{1,200}$/.test(value) ? value : null;
const uuid = (value) => typeof value === "string" && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(value) ? value : null;
const count = (value) => Number.isSafeInteger(value) && value >= 0 ? value : null;
const usageFields = ["input_tokens", "cached_input_tokens", "uncached_input_tokens", "output_tokens", "reasoning_tokens", "total_tokens",
  "cache_write_tokens", "cache_write_30m_tokens", "cache_write_12h_tokens"];

const sha256 = (value) => createHash("sha256").update(value).digest("hex");
const identity = (value) => {
  const bytes = Buffer.isBuffer(value) || typeof value === "string" ? value : JSON.stringify(value);
  return { sha256: sha256(bytes), bytes: Buffer.byteLength(bytes) };
};

// Public identifiers are fixed independently of request declarations. A familiar
// namespace never makes an arbitrary child name public; keep only its digest.
const publicToolTypes = new Set(["namespace", "function", "custom", "tool_search"]);
const publicToolNamespaces = new Set(["functions", "codex_app", "mcp__codex_app"]);
const publicToolNames = new Set([NOKIY_TOOL, "exec", "exec_command", "tool_search", "wait", "write_stdin",
  "request_user_input", "request_user_input_async", "get_goal", "update_goal",
  "read_thread", "list_threads", "wait_threads", "send_message_to_thread", "handoff_thread"]);
const privateLabel = (value, publicLabels) => {
  const text = label(value);
  return text === null ? null : publicLabels?.has(text) ? text : `sha256:${sha256(text)}`;
};

export function summarizeToolHandoff(input) {
  if (!Array.isArray(input)) return null;
  const items = input.slice(-16).filter(item => ["function_call", "custom_tool_call",
    "function_call_output", "custom_tool_call_output"].includes(item?.type));
  if (!items.length) return null;
  return { scope: "bounded_tool_transport_metadata_not_execution_proof", window_items: Math.min(input.length, 16),
    preceding_items_omitted: Math.max(0, input.length - 16), entries: items.map(item => {
      const row = { type: item.type, call_id: privateLabel(item.call_id),
        name: privateLabel(item.name, publicToolNames) };
      if (["function_call", "custom_tool_call"].includes(item.type)) {
        const raw = item.type === "function_call" ? item.arguments : item.input;
        if (typeof raw === "string" && Buffer.byteLength(raw) <= 96 * 1024) {
          row.input = identity(raw);
          // Lexical hints only: strings/comments may contain these names.
          row.code_hints = ["apply_patch", "exec_command", "write_stdin"].filter(name =>
            raw.includes(`tools.${name}(`));
          if (item.name === "wait" || item.name === "write_stdin") {
            try {
              const args = JSON.parse(raw);
              const handle = item.name === "wait" ? args.cell_id : args.session_id;
              if (typeof handle === "string" || Number.isSafeInteger(handle))
                row.wait_handle = privateLabel(String(handle));
            } catch { /* Missing identity stays unknown. */ }
          }
        }
      } else {
        const output = item.output;
        const prefix = typeof output === "string" ? output.slice(0, 512)
          : Array.isArray(output) ? output.slice(0, 2).map(part =>
            ["text", "input_text"].includes(part?.type) && typeof part.text === "string"
              ? part.text.slice(0, 512) : "").join("\n") : "";
        const cell = prefix.match(/^Script running with cell ID ([A-Za-z0-9_-]{1,128})\r?\n/);
        row.native_cell_state = cell ? "running" : /^Script completed\r?\n/.test(prefix) ? "completed" : "unknown";
        row.cell_id = cell ? privateLabel(cell[1]) : null;
      }
      return row;
    }) };
}

// Observe ingress or final transport bytes, never rewrite them. The projection argument is
// router-owned, not client metadata. Missing fields in old receipts mean UNKNOWN.
// Content uses UTF-8 for strings and JSON UTF-8 for structured declarations/content.
export function summarizeRequestProvenance(request, synthetic = false) {
  const unknown = { schema_version: "switchboard_request_provenance/v1", status: "UNKNOWN" };
  try {
    const { wire, encoding, projection } = request ?? {};
    if (wire === undefined) return unknown;
    const raw = Buffer.isBuffer(wire) ? wire : Buffer.from(wire);
    const body = parseJsonBody(raw, encoding);
    const groups = [];
    if (Array.isArray(body.tools)) groups.push({ source: "/tools", kind: "tools", tools: body.tools });
    const instructions = [];
    const instruction = (source, role, index, content) => instructions.push({ source, role, index,
      encoding: typeof content === "string" ? "utf8" : "json-utf8", ...identity(content) });
    if (body.instructions !== undefined) instruction("/instructions", "UNKNOWN", null, body.instructions);
    (Array.isArray(body.input) ? body.input : []).forEach((item, index) => {
      if (["additional_tools", "tool_search_output"].includes(item?.type) && Array.isArray(item.tools))
        groups.push({ source: `/input/${index}/tools`, kind: item.type,
          role: ["system", "developer"].includes(item.role) ? item.role : "UNKNOWN", tools: item.tools });
      if ((item?.type === "message" || item?.type === undefined) && ["system", "developer"].includes(item?.role)
          && item.content !== undefined) instruction(`/input/${index}/content`, item.role, index, item.content);
    });
    const tools = [];
    const walk = (items, source, declaration, namespaces = []) => items.forEach((tool, index) => {
      const at = `${source}/${index}`;
      tools.push({ source: at, declaration, type: privateLabel(tool?.type, publicToolTypes),
        name: privateLabel(tool?.name, tool?.type === "namespace" ? publicToolNamespaces : publicToolNames), namespaces,
        namespace: privateLabel(tool?.namespace, publicToolNamespaces),
        execution: ["client", "server"].includes(tool?.execution) ? tool.execution : null,
        defer_loading: typeof tool?.defer_loading === "boolean" ? tool.defer_loading : null,
        parameters_kind: tool?.parameters === undefined ? "absent" : tool.parameters === null ? "null"
          : Array.isArray(tool.parameters) ? "array" : typeof tool.parameters,
        parameters_type: ["object", "array", "string", "number", "integer", "boolean", "null"].includes(tool?.parameters?.type)
          ? tool.parameters.type : null,
        function_envelope: tool?.function != null, function_name: privateLabel(tool?.function?.name, publicToolNames) });
      if (tool?.type === "namespace" && Array.isArray(tool.tools))
        walk(tool.tools, `${at}/tools`, declaration, [...namespaces, privateLabel(tool.name, publicToolNamespaces)]);
    });
    groups.forEach((group) => walk(group.tools, group.source, group.kind));
    const native = projection?.nativeTool;
    const policy = projection?.receiptPolicy;
    const binding = policy && instructions.find((entry) => entry.sha256 === policy.container_sha256);
    const choice = body.tool_choice;
    const result = { ...unknown, status: "OBSERVED", wire: identity(raw),
      tool_handoff: summarizeToolHandoff(body.input),
      reasoning: {
        scope: "request_wire_not_provider_observed",
        effort: ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"].includes(body.reasoning?.effort)
          ? body.reasoning.effort : "UNKNOWN",
      },
      request_kind: synthetic ? "SYNTHETIC_PREWARM" : body.generate === false ? "PREWARM" : "REQUEST",
      previous_response_id: privateLabel(body.previous_response_id),
      tools: { status: groups.length ? "OBSERVED" : "UNKNOWN", group_count: groups.length,
        count: tools.length, ...(groups.length ? identity(groups) : { sha256: null, bytes: null }),
        entries: tools, truncated: false },
      instructions: { status: instructions.length ? "OBSERVED" : "UNKNOWN", count: instructions.length,
        ...identity(instructions), entries: instructions, truncated: false },
      tool_choice: choice === undefined ? "UNKNOWN" : { ...identity(choice),
        mode: ["auto", "none", "required"].includes(choice) ? choice : null,
        type: privateLabel(choice?.type, publicToolTypes), name: privateLabel(choice?.name, publicToolNames) },
      parallel_tool_calls: typeof body.parallel_tool_calls === "boolean" ? body.parallel_tool_calls : "UNKNOWN",
      resolved: projection?.profile ? { source: "projectNokiyDirect", profile: label(projection.profile.slug),
        workflow: label(projection.profile.workflow), dispatch: label(projection.dispatch),
        adaptive_native: efficientNativeLoop(projection.profile),
        coordinator_bindings: native ? Boolean(native.coordinatorBindings) : "UNKNOWN",
        cache_alignment: label(projection.cacheAlignment) } : "UNKNOWN",
      wrapping: native ? { source: "nativeToolProjection", type: privateLabel(native.target?.type, publicToolTypes),
        name: privateLabel(native.target?.name, publicToolNames), namespace: privateLabel(native.target?.namespace, publicToolNamespaces),
        execution_schema: native.target?.executionSchema ? identity(native.target.executionSchema) : "UNKNOWN",
        // A custom tool named exec does not prove a code-mode inner catalog.
        inner_catalog: "UNKNOWN" } : "UNKNOWN",
      local_nokiy_policy: binding ? { source: "nokiy-direct-route", sha256: policy.sha256,
        bytes: policy.bytes, container_source: binding.source, container_sha256: binding.sha256 } : "UNKNOWN",
      native_no_proactive_subagent_markers: "UNKNOWN",
      truncated: false };
    // Reuse the existing 64 KiB summary ceiling as an output bound, never admission.
    const full = JSON.stringify(result);
    if (Buffer.byteLength(full) > 64 * 1024) {
      result.full_metadata = identity(full);
      for (const section of [result.tools, result.instructions]) {
        section.entries = [];
        section.truncated = section.count > 0;
      }
      result.truncated = true;
    }
    return result;
  } catch { return unknown; } // Diagnostic failure cannot prevent dispatch or expose errors/content.
}

// One sequencer per server instance, not a ledger or a new dispatch authority.
// Dispatch means the local transport invocation, NOT proven provider execution.
export function createInferenceAttemptEvidence({
  emit = (event) => safeLog({ attempt_lifecycle: event }),
  processInstanceId = randomUUID(),
  now = () => performance.now(),
} = {}) {
  let sequence = 0;
  let active = 0;
  let emissionErrors = 0;
  const write = (event) => {
    const record = { schema_version: ATTEMPT_SCHEMA, scope: ATTEMPT_SCOPE,
      process_instance_id: processInstanceId, sequence: ++sequence, ...event };
    try { emit(record); } catch { emissionErrors++; } // Accounting must not change inference policy.
    return record;
  };
  return {
    // Existing authenticated readiness brackets a prospective capture. An open
    // request or a lost emission makes the consumer's closed-span proof fail.
    checkpoint() {
      return write({ event_type: "checkpoint", active_requests: active, emission_error_count: emissionErrors });
    },
    start({ requestId, modelRequested, correlation, transport, request }) {
      const base = {
        request_id: label(requestId), endpoint: "/responses", transport,
        provider_requested: null, provider_selected: "openai", provider_observed: null,
        model_requested: label(modelRequested),
        request_correlation: { client_thread_id: uuid(correlation?.client_thread_id),
          client_session_id: uuid(correlation?.client_session_id) },
      };
      let ordinal = 0;
      let attempt = null;
      let finished = false;
      active++;
      write({ ...base, event_type: "request_start", attempt_id: null, attempt_ordinal: 0,
        ...(request ? { request_provenance: summarizeRequestProvenance(request) } : {}),
        upstream_dispatch_started: false, provider_execution_started: false });
      const terminal = ({ outcome = "unknown", terminalEvent = null, observation = null, errorCode = null } = {}) => {
        const usage = observation ? Object.fromEntries(usageFields.map((key) => [key, count(observation[key])])) : null;
        return {
          outcome: ["success", "failure", "cancel", "unknown"].includes(outcome) ? outcome : "unknown",
          terminal_event: label(terminalEvent), error_code: label(errorCode),
          upstream_dispatch_started: ordinal > 0,
          // A sent request alone cannot establish whether an engine ran. Positive
          // upstream usage can; absent/failed/cancelled usage remains null, not 0.
          provider_execution_started: usage && [usage.input_tokens, usage.output_tokens, usage.total_tokens].some((n) => n > 0)
            ? true : ordinal > 0 ? null : false,
          response_id: label(observation?.response_id), model_observed: label(observation?.model),
          service_tier_observed: label(observation?.service_tier),
          usage_status: observation?.usage_status === "REPORTED" ? "REPORTED"
            : observation?.usage_status === "INCONSISTENT" ? "INCONSISTENT" : "UNKNOWN_OR_MISSING",
          usage,
        };
      };
      const endAttempt = (result) => {
        if (!attempt || attempt.ended || finished) return false;
        attempt.ended = true;
        write({ ...base, event_type: "attempt_terminal", attempt_id: attempt.id,
          attempt_ordinal: ordinal, model_dispatched: attempt.model, ...terminal(result),
          provider_output_deltas: {
            scope: "pre_projection_observed_deltas_not_token_usage",
            first_content_delta_ms: attempt.firstContentMs,
            categories: Object.keys(attempt.output).length ? attempt.output : null,
            ...(attempt.toolCalls.length ? { completed_tool_calls: attempt.toolCalls,
              completed_tool_calls_truncated: attempt.toolCallsTruncated } : {}),
          } });
        return true;
      };
      return {
        dispatch(model, request) {
          if (finished || (attempt && !attempt.ended)) { emissionErrors++; return null; }
          ordinal++;
          attempt = { id: `${processInstanceId}:${base.request_id}:${ordinal}`, model: label(model), ended: false,
            started: now(), firstContentMs: null, output: {}, toolCalls: [], toolCallsTruncated: false };
          write({ ...base, event_type: "attempt_dispatch", attempt_id: attempt.id,
            attempt_ordinal: ordinal, model_dispatched: attempt.model,
            upstream_dispatch_started: true, provider_execution_started: null,
            request_provenance: summarizeRequestProvenance(request) });
          return attempt.id;
        },
        observeOutput(event) {
          if (!attempt || attempt.ended || finished) return;
          try {
            if (event?.type === "response.output_item.done"
                && ["function_call", "custom_tool_call"].includes(event.item?.type)) {
              if (attempt.toolCalls.length < 16) {
                const metadata = summarizeToolHandoff([event.item]);
                attempt.toolCalls.push({ elapsed_ms: Math.max(0, now() - attempt.started),
                  ...metadata.entries[0] });
              } else attempt.toolCallsTruncated = true;
            }
            const category = {
              "response.output_text.delta": "text",
              "response.function_call_arguments.delta": "function_arguments",
              "response.custom_tool_call_input.delta": "custom_tool_input",
            }[event?.type];
            if (typeof category !== "string" || typeof event.delta !== "string" || !event.delta) return;
            const elapsed = Math.max(0, now() - attempt.started);
            attempt.firstContentMs ??= elapsed;
            const bucket = attempt.output[category] ||= { utf8_bytes: 0, events: 0 };
            bucket.utf8_bytes += Buffer.byteLength(event.delta, "utf8");
            bucket.events++;
          } catch { emissionErrors++; }
        },
        endAttempt,
        finish(result) {
          if (finished) return false;
          endAttempt(result);
          finished = true;
          active--;
          const { usage, ...summary } = terminal(result);
          write({ ...base, ...summary, event_type: "request_terminal", attempt_id: null,
            attempt_ordinal: 0, attempt_count: ordinal,
            ...(result?.syntheticRequest ? {
              request_provenance: summarizeRequestProvenance(result.syntheticRequest, true) } : {}) });
          return true;
        },
      };
    },
  };
}
