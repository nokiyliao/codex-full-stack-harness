import { createHash } from "node:crypto";
import { SwitchboardError } from "./shared.mjs";

const PREFIX = '{"nokiy_delivery":"accept_v1",';
const MAX_RESULT_BYTES = 256 * 1024;
const MAX_ACCEPTANCE_BYTES = 8192;
const MAX_BUFFER_BYTES = 4 * 1024 * 1024;
const sha256 = (value) => createHash("sha256").update(value).digest("hex");
const hash = (value) => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
const invalid = () => new SwitchboardError("NOKIY_RESULT_DELIVERY_INVALID",
  "Compact parent acceptance does not match the delivered result; no result was substituted.", 502);
const finalMessage = (item) => item?.type === "message" && item.role === "assistant"
  && (item.phase === undefined || item.phase === "final_answer");
const prefixState = (text) => {
  if (typeof text !== "string") return "ordinary";
  const prefix = text.replace(/[ \t\r\n]/g, "");
  return prefix.startsWith(PREFIX) ? "compact" : PREFIX.startsWith(prefix) ? "pending" : "ordinary";
};

function object(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

const callItem = (item) => ["function_call", "custom_tool_call"].includes(item?.type);
const transparentItem = (item) => item?.type === "reasoning" || (item?.type === "message"
  && item.role === "assistant" && item.phase === "commentary" && item.status === "completed"
  && Array.isArray(item.content) && item.content.every((part) => part?.type === "output_text"));

// Progress text carries no competing execution identity. Other calls or final
// answers remain ambiguous and cannot supply continuation ownership.
export function resultContinuationCall(output) {
  if (!Array.isArray(output)) return null;
  const calls = output.filter(callItem);
  return calls.length === 1 && output.every((item) => item === calls[0] || transparentItem(item))
    ? calls[0] : null;
}

export function inputPair(body, continuationCall) {
  const input = body.input;
  if (!Array.isArray(input) || input.length === 0) return null;
  let index = input.length - 2;
  while (index >= 0 && transparentItem(input[index])) index--;
  const call = input.length === 1 && typeof body.previous_response_id === "string" && body.previous_response_id
    ? continuationCall : input[index];
  const output = input.at(-1);
  const expected = call?.type === "function_call" ? "function_call_output"
    : call?.type === "custom_tool_call" ? "custom_tool_call_output" : null;
  return expected && output?.type === expected && typeof call.call_id === "string"
    && call.call_id && output.call_id === call.call_id ? { call, output, index } : null;
}

// Source kind is observational only; it never grants delivery eligibility.
export function prepareResultDeliveryDiagnostic(body, continuationCall = null, projectCall = null) {
  const call = inputPair(body ?? {}, continuationCall)?.call;
  const bounded = (value) => typeof value === "string" && /^[A-Za-z0-9_.:-]{1,128}$/.test(value) ? value : null;
  const diagnostic = {
    source_call_id: bounded(call?.call_id),
    source_call_type: call?.type ?? null,
    source_tool_name: bounded(call?.name),
    dispatch_operation: null,
    applicability: "unknown",
  };
  if (!diagnostic.source_call_id || !diagnostic.source_tool_name) return diagnostic;
  if (call.name === "nokiy_native") return { ...diagnostic, applicability: "not_applicable" };
  const field = call.type === "function_call" ? "arguments" : "input";
  const raw = call[field];
  // Match the existing native dispatch input bound, without retaining its bytes.
  if (typeof raw !== "string" || Buffer.byteLength(raw) > 192 * 1024) return diagnostic;
  let payload;
  try {
    if (call.name === "nokiy_execute") payload = JSON.parse(raw);
    else if (["exec", "exec_command"].includes(call.name) && typeof projectCall === "function") {
      const encoded = raw.match(/'--payload' '([A-Za-z0-9_-]+)'/)?.[1];
      if (!encoded) return diagnostic;
      const envelope = JSON.parse(Buffer.from(encoded, "base64url").toString("utf8"));
      if (!["nokiy_native_tool_v2", "nokiy_native_tool_v3"].includes(envelope?.schema_version)) return diagnostic;
      payload = envelope.payload;
      // Reuse the native renderer/validator; an envelope in arbitrary arguments
      // or a mutated command cannot establish a dispatch operation.
      const expected = projectCall({ ...call, name: "nokiy_execute", [field]: JSON.stringify(payload) });
      if (expected?.type !== call.type || expected.name !== call.name
          || (expected.namespace ?? null) !== (call.namespace ?? null) || expected[field] !== raw) return diagnostic;
    }
  } catch { return diagnostic; }
  if (!object(payload)) return diagnostic;
  if (["run", "run_ref", "run_batch", "run_batch_ref"].includes(payload.operation)) {
    return { ...diagnostic, dispatch_operation: payload.operation, applicability: "worker_run" };
  }
  if (payload.operation === "read_http") {
    return { ...diagnostic, dispatch_operation: "read_http", applicability: "not_applicable" };
  }
  return diagnostic;
}

const yieldedCell = (output) => typeof output === "string"
  ? output.match(/^Script running with cell ID ([A-Za-z0-9_-]{1,128})\r?\nWall time [\d.]+ seconds\r?\nOutput:\r?\n/)?.[1]
  : null;

function historyPendingDelivery(body, matchesNativeCall, pair) {
  if (!pair || pair.index < 0) return null;
  let args;
  try { args = JSON.parse(pair.call.arguments); } catch { return null; }
  const cellId = args?.cell_id;
  if (typeof cellId !== "string" || !bindPendingResultWait({ cellId }, pair.call)) return null;
  let index = pair.index - 1;
  const seen = new Set([pair.call.call_id]);
  // Walk only the contiguous wait suffix already supplied by the caller. No
  // lookup, new request, memory of another connection, or provider-input edit.
  while (index >= 0) {
    while (index >= 0 && transparentItem(body.input[index])) index--;
    const output = body.input[index--];
    if (yieldedCell(output?.output) !== cellId) return null;
    while (index >= 0 && transparentItem(body.input[index])) index--;
    const call = body.input[index--];
    const expected = call?.type === "function_call" ? "function_call_output"
      : call?.type === "custom_tool_call" ? "custom_tool_call_output" : null;
    if (!expected || output?.type !== expected || typeof call.call_id !== "string" || !call.call_id
        || output.call_id !== call.call_id || seen.has(call.call_id)) return null;
    seen.add(call.call_id);
    if (matchesNativeCall(call)) return bindPendingResultWait({ dispatchCall: call, cellId }, pair.call);
    if (!bindPendingResultWait({ cellId }, call)) return null;
  }
  return null;
}

function decodeOutput(value, depth = 0) {
  if (depth > 2) return null;
  if (Array.isArray(value)) {
    if (value.length === 1 && ["text", "input_text"].includes(value[0]?.type)) {
      value = value[0].text;
    } else if (value.length === 2 && value.every((part) => part?.type === "input_text")
        && typeof value[0].text === "string" && value[0].text.length < 128
        && /^Script completed\nWall time \d+(?:\.\d+)? seconds\nOutput:\n$/.test(value[0].text)) {
      // Native code mode separates its completion header from the JSON terminal.
      value = value[1].text;
    } else return null;
  }
  if (typeof value !== "string" || Buffer.byteLength(value) > MAX_RESULT_BYTES) return null;
  try {
    const parsed = JSON.parse(value);
    if (!object(parsed)) return null;
    // Code mode serializes the original native terminal; do not parse log tails.
    if (typeof parsed.output === "string") {
      if (parsed.exit_code !== 0 || parsed.terminal !== true || parsed.truncated
          || parsed.output_truncated || parsed.session_id != null) return null;
      return decodeOutput(parsed.output, depth + 1);
    }
    // A JSON terminal embedded as an object by the native cell; same envelope checks.
    if (object(parsed.output) && parsed.terminal === true && "recovery_session_id" in parsed) {
      if (parsed.exit_code !== 0 || parsed.truncated || parsed.output_truncated
          || parsed.session_id != null) return null;
      return parsed.output;
    }
    return parsed;
  } catch {
    // The native batch host prints one exact plan reference, then one summary.
    // Never scan arbitrary log tails for a result.
    const lines = value.trim().split(/\r?\n/);
    if (lines.length !== 2) return null;
    try {
      const [plan, result] = lines.map((line) => JSON.parse(line));
      return object(plan) && Object.keys(plan).sort().join() === "plan_path,plan_sha256"
        && typeof plan.plan_path === "string" && plan.plan_path.startsWith("/") && hash(plan.plan_sha256)
        && ["nokiy_direct_batch_summary_v1", "nokiy_direct_batch_summary_v2"].includes(result?.schema_version)
        ? result : null;
    } catch { return null; }
  }
}

function canonicalJsonValue(text) {
  // Reject duplicate keys, rounded numbers and ambiguous lexical conversions.
  // Whitespace is the only normalization permitted before parsing records.
  let quoted = false, escaped = false, compact = "";
  for (const char of text) {
    if (quoted || !/[ \t\r\n]/.test(char)) compact += char;
    if (escaped) escaped = false;
    else if (quoted && char === "\\") escaped = true;
    else if (char === '"') quoted = !quoted;
  }
  try {
    const parsed = JSON.parse(text);
    return JSON.stringify(parsed) === compact ? parsed : null;
  } catch { return null; }
}

function prepareBatchDelivery(result, call) {
  if (result.status !== "RESULT_AVAILABLE" || result.first_typed_blocker != null
      || result.continuation_owner !== "codex" || result.mission_acceptance !== "parent_owned"
      || !Array.isArray(result.members) || !result.members.length || result.members.length > 64) return null;
  const records = [], identities = [], requestIds = new Set(), ids = new Set();
  let key = null, thread = null;
  for (const member of result.members) {
    const binding = member?.delivery_binding, text = member?.result_text;
    const failed = member?.evidence_counts?.failed;
    const settledCommands = binding?.schema_version === "nokiy_batch_delivery_binding_v1"
      ? failed === 0 && binding.not_attempted_count === undefined
      : binding?.schema_version === "nokiy_batch_delivery_binding_v2"
        ? Number.isSafeInteger(failed) && failed > 0 && binding.not_attempted_count === failed
        // v3: searches whose exit 1 means no match, with any authenticated non-attempts.
        : binding?.schema_version === "nokiy_batch_delivery_binding_v3"
          && Number.isSafeInteger(failed) && failed > 0
          && Number.isSafeInteger(binding.not_attempted_count) && binding.not_attempted_count >= 0
          && Number.isSafeInteger(binding.no_match_count) && binding.no_match_count > 0
          && binding.not_attempted_count + binding.no_match_count === failed;
    if (member?.status !== "RESULT_AVAILABLE" || member.cleanup_pass !== true
        || member.first_typed_blocker != null || member.evidence_status !== "EVIDENCE_VERIFIED"
        || member.evidence_first_blocker != null || member.evidence_counts?.file_changes !== 0
        || !settledCommands || member.evidence_counts?.index_complete !== true
        || member.result_truncated !== false || typeof text !== "string" || !text.isWellFormed()
        || !hash(member.request_sha256) || member.request_id !== `tura_embedded_${member.request_sha256}`
        || requestIds.has(member.request_id) || !hash(member.terminal_sha256)
        || typeof member.native_thread_id !== "string"
        || !/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(member.native_thread_id)
        || (thread !== null && thread !== member.native_thread_id)
        || binding.terminal_sha256 !== member.terminal_sha256 || !hash(binding.result_sha256)
        || !Number.isSafeInteger(binding.result_bytes) || binding.result_bytes > MAX_RESULT_BYTES
        || Buffer.byteLength(text) !== binding.result_bytes || sha256(text) !== binding.result_sha256) return null;
    const value = canonicalJsonValue(text);
    if (!object(value) || Object.keys(value).length !== 1) return null;
    const field = Object.keys(value)[0];
    if (!/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(field) || ["constructor", "prototype"].includes(field)
        || (key !== null && key !== field) || !Array.isArray(value[field]) || !value[field].length) return null;
    for (const record of value[field]) {
      if (!object(record) || !Number.isSafeInteger(record.id) || record.id < 1 || ids.has(record.id)) return null;
      ids.add(record.id); records.push(record);
    }
    key = field; thread = member.native_thread_id; requestIds.add(member.request_id);
    identities.push([member.request_id, member.terminal_sha256, binding.result_sha256]);
  }
  records.sort((a, b) => a.id - b.id);
  const text = JSON.stringify({ [key]: records });
  if (Buffer.byteLength(text) > MAX_RESULT_BYTES) return null;
  const context = Object.freeze({ text, composition: "json_records_by_id",
    reference: sha256(JSON.stringify([call.call_id, thread, identities, sha256(text)])) });
  const acceptance = JSON.stringify({ nokiy_delivery: "accept_v1", reference: context.reference, edits: [] });
  return Buffer.byteLength(text) > 2 * (Buffer.byteLength(resultDeliveryInstructions(context))
    + Buffer.byteLength(acceptance)) ? context : null;
}

// Failed records the caller may explain: authenticated source-read non-attempts,
// and searches (rg, grep, git grep) whose exit 1 means nothing matched.
const RECONCILIATIONS = [
  ["source_read_rejection_reconciliation", "nokiy_source_read_rejection_reconciliation_v1", "not_attempted_count"],
  ["search_no_match_reconciliation", "nokiy_search_no_match_reconciliation_v1", "no_match_count"],
];

function inspectedCommandsSettled(inspection) {
  const failed = inspection?.counts?.failed;
  if (failed === 0) return inspection.command_success === "known_success";
  // Mirror the caller's batch binding: historical failures are retained, and
  // every one of them must be explained by an authenticated reconciliation.
  if (!Number.isSafeInteger(failed) || failed <= 0 || inspection.command_success !== "known_failure") return false;
  let explained = 0;
  for (const [key, schema, field] of RECONCILIATIONS) {
    const proof = inspection[key];
    if (proof === undefined) continue;
    if (!object(proof)
        || Object.keys(proof).sort().join() !== ["effective_command_success", field, "schema_version", "unresolved_count"].sort().join()
        || proof.schema_version !== schema || !Number.isSafeInteger(proof[field]) || proof[field] <= 0
        || proof.unresolved_count !== 0 || proof.effective_command_success !== "known_success") return false;
    explained += proof[field];
  }
  return explained === failed;
}

// A delta uses only its same-connection owner. Full input may prove its own
// contiguous dispatch/wait chain; neither path performs a history lookup.
export function prepareResultDelivery(body, matchesNativeCall, continuationCall = null, pendingDelivery = null, onRejection = () => {}) {
  // Fixed reason codes only; observations must never retain source or result text.
  const reject = (reason) => { onRejection(reason); return null; };
  const input = body.input;
  if (body.generate === false) return reject("PREWARM");
  if (!Array.isArray(input) || input.length === 0) return reject("NO_INPUT");
  const pair = inputPair(body, continuationCall);
  if (!pair) return reject("CALL_OUTPUT_PAIR_UNAVAILABLE");
  const { call, output } = pair;
  pendingDelivery = pendingDelivery?.waitCall && JSON.stringify(call) === JSON.stringify(pendingDelivery.waitCall)
    ? pendingDelivery : historyPendingDelivery(body, matchesNativeCall, pair);
  const sourceCall = pendingDelivery?.waitCall && JSON.stringify(call) === JSON.stringify(pendingDelivery.waitCall)
    ? pendingDelivery.dispatchCall : call;
  if (!matchesNativeCall(sourceCall)) return reject("NATIVE_DISPATCH_BINDING_MISMATCH");
  const result = decodeOutput(output.output);
  if (!result) return reject("OUTPUT_FRAMING_UNRECOGNIZED");
  if (["nokiy_direct_batch_summary_v1", "nokiy_direct_batch_summary_v2"].includes(result?.schema_version)) {
    return prepareBatchDelivery(result, call) ?? reject("BATCH_CONTRACT_OR_BENEFIT_REJECTED");
  }
  const inspection = result?.result_inspection;
  if (result?.schema_version !== "nokiy_terminal_summary_v1" || result.status !== "RESULT_AVAILABLE"
      || result.cleanup_pass !== true || result.runtime_exit_code !== 0 || result.first_typed_blocker != null
      || result.first_blocker != null
      || result.fallback_used !== false || result.capability_gap != null
      || result.mission_acceptance !== "parent_owned" || result.continuation_owner !== "codex"
      || inspection?.status !== "EVIDENCE_VERIFIED" || inspection.first_blocker != null
      || inspection.projection_partial === true || inspection.artifact_integrity !== "verified"
      || (inspection.command_success !== "known_success" && !inspectedCommandsSettled(inspection))
      || !hash(result.request_sha256)
      || result.request_id !== `tura_embedded_${result.request_sha256}`
      || !hash(result.terminal_sha256) || result.terminal_sha256 !== inspection.terminal_sha256
      || typeof result.native_thread_id !== "string"
      || !/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(result.native_thread_id)) return reject("TERMINAL_CONTRACT_REJECTED");
  // Exclude recorded file changes. This is not proof of no other effects;
  // semantic review and any fresh authority checks remain with the parent.
  if (inspection.counts?.file_changes !== 0 || !inspectedCommandsSettled(inspection)
      || inspection.counts?.index_complete !== true) return reject("RECORDED_EFFECTS_OR_FAILURES");
  const artifact = result.result_artifact;
  let text = result.result_text;
  if (result.result_truncated !== false) {
    const answer = inspection.parent_review_answer;
    if (answer?.schema_version !== "nokiy_parent_review_answer_v1"
        || answer.parent_acceptance_required !== true || !hash(answer.terminal_content_sha256)
        || answer.artifact?.sha256 !== artifact?.sha256 || answer.artifact?.bytes !== artifact?.bytes
        || answer.artifact?.path !== artifact?.path) return reject("REVIEW_ANSWER_BINDING_REJECTED");
    text = answer.text;
  }
  if (typeof text !== "string" || !text || !text.isWellFormed()
      || Buffer.byteLength(text) > MAX_RESULT_BYTES) return reject("ANSWER_INTEGRITY_REJECTED");
  const answerBytes = Buffer.byteLength(text), answerHash = sha256(text);
  // Complete inline answers belong to the inspected native terminal itself.
  // Null is its explicit no-external-artifact representation, not a missing file.
  const inlineAnswer = artifact === null && result.result_truncated === false;
  if (!inlineAnswer && (!hash(artifact?.sha256) || !Number.isSafeInteger(artifact.bytes)
      || answerBytes !== artifact.bytes || answerHash !== artifact.sha256)) return reject("ANSWER_INTEGRITY_REJECTED");
  const context = Object.freeze({ text, reference: sha256(JSON.stringify([
    call.call_id, result.native_thread_id, result.request_id, result.terminal_sha256, answerHash,
  ])) });
  const minimumAcceptance = JSON.stringify({ nokiy_delivery: "accept_v1", reference: context.reference, edits: [] });
  // Do not spend more prompt/control bytes than a short answer would contain.
  // This is a conservative byte screen, not a model-token or billing estimate.
  const controlBytes = Buffer.byteLength(resultDeliveryInstructions(context)) + Buffer.byteLength(minimumAcceptance);
  return answerBytes > 2 * controlBytes ? context : reject("NO_DELIVERY_BYTE_BENEFIT");
}

// A yielded code cell is not a finished worker. Carry only its original dispatch
// and exact native wait handle through the existing same-connection owner.
export function pendingResultDelivery(body, matchesNativeCall, continuationCall = null, previous = null) {
  if (!Array.isArray(body.input) || body.input.length === 0) return null;
  const pair = inputPair(body, continuationCall);
  if (!pair) return null;
  const { call, output } = pair;
  previous = previous?.waitCall && JSON.stringify(call) === JSON.stringify(previous.waitCall)
    ? previous : historyPendingDelivery(body, matchesNativeCall, pair);
  const dispatchCall = previous?.waitCall && JSON.stringify(call) === JSON.stringify(previous.waitCall)
    ? previous.dispatchCall : call;
  if (!matchesNativeCall(dispatchCall)) return null;
  const cellId = yieldedCell(output.output);
  if (!cellId || (previous && previous.cellId !== cellId)) return null;
  return Object.freeze({ dispatchCall, cellId });
}

export function bindPendingResultWait(pending, call) {
  if (!pending || call?.type !== "function_call" || call.name !== "wait" || call.namespace !== "functions") return null;
  let args;
  try { args = JSON.parse(call.arguments); } catch { return null; }
  if (!args || args.cell_id !== pending.cellId || args.terminate === true) return null;
  return Object.freeze({ ...pending, waitCall: call });
}

export function resultDeliveryInstructions(context) {
  if (context.composition === "json_records_by_id") {
    const mode = context.coordinatorOnly ? "Coordination-only" : "Optional";
    const review = context.coordinatorOnly ? "Reconcile the preassigned acceptance criteria and supplied evidence; do not independently redo source review or regenerate these records. " : "";
    const fallback = context.coordinatorOnly ? "delegate a concrete missing correction or verification, or report the unresolved requirement" : "use normal tools/response instead";
    return `${mode} batch final-answer transport: reference ${context.reference} is the complete deterministic JSON merge of all delivered member answers: their sole shared array field, every record unchanged, sorted by unique positive integer id. ${review}Only if that exact composition satisfies the task and you accept every member's semantic result and evidence, emit exactly {"nokiy_delivery":"accept_v1","reference":"${context.reference}","edits":[]} as your sole final_answer, without a code fence. The native response projection delivers the complete merged JSON without another tool/model call. No filtering, deduplication, edits or inferred records are allowed. If this composition is not the requested answer, evidence is missing, or work remains, ${fallback}. Integrity is not semantic acceptance; this transport grants no permission, effect, cleanup, task or Goal acceptance.`;
  }
  if (context.coordinatorOnly) return `Coordination-only final-answer transport: the complete worker answer is available as reference ${context.reference}. Reconcile task completion with the preassigned acceptance criteria and supplied evidence; do not independently redo source review or regenerate this answer. If accepted, emit exactly {"nokiy_delivery":"accept_v1","reference":"${context.reference}","edits":[]} as your sole final_answer, with no code fence. The native response projection delivers the complete original answer. Edits are disabled for the coordinator: delegate a concrete missing correction or verification before delivery, or report the unresolved requirement. Artifact integrity alone is not semantic acceptance. This transport grants no permission, protected effect, cleanup or task/Goal acceptance.`;
  return `Optional final-answer transport: the immediately preceding native result is available as reference ${context.reference}. Only AFTER your own semantic review accepts the entire answer and all required work is complete, you may emit exactly {"nokiy_delivery":"accept_v1","reference":"${context.reference}","edits":[]} as your sole final_answer message, with no code fence. The native response projection expands it to the complete already-delivered answer, not a link or summary, without another tool/model call. For necessary small corrections, edits may contain at most 8 objects {"old":"exact unique original text","new":"replacement"}; edits must be nonoverlapping and refer to the original answer. The entire acceptance must fit 8192 UTF-8 bytes. This is optional: for rejection, missing evidence, additional work, a substantially different answer, or any uncertainty, write a normal answer/use the existing tools. Never accept merely because artifact verification passed. No permission, effect, cleanup, or task/Goal acceptance is granted by this transport.`;
}

export function materializeResultDelivery(text, context) {
  if (!context || typeof text !== "string" || prefixState(text) !== "compact"
      || Buffer.byteLength(text) > MAX_ACCEPTANCE_BYTES) throw invalid();
  let value;
  try { value = JSON.parse(text); } catch { throw invalid(); }
  if (Object.keys(value).sort().join() !== "edits,nokiy_delivery,reference"
      || value.nokiy_delivery !== "accept_v1" || value.reference !== context.reference
      || !Array.isArray(value.edits) || value.edits.length > 8
      || ((context.coordinatorOnly || context.composition) && value.edits.length !== 0)) throw invalid();
  const edits = value.edits.map((edit) => {
    if (!object(edit) || Object.keys(edit).sort().join() !== "new,old"
        || typeof edit.old !== "string" || !edit.old || typeof edit.new !== "string"
        || !edit.old.isWellFormed() || !edit.new.isWellFormed()) throw invalid();
    const start = context.text.indexOf(edit.old);
    if (start < 0 || context.text.indexOf(edit.old, start + 1) !== -1) throw invalid();
    return { start, end: start + edit.old.length, text: edit.new };
  }).sort((a, b) => a.start - b.start);
  let end = 0, result = "";
  for (const edit of edits) {
    if (edit.start < end) throw invalid();
    result += context.text.slice(end, edit.start) + edit.text;
    end = edit.end;
  }
  result += context.text.slice(end);
  if (Buffer.byteLength(result) > MAX_RESULT_BYTES) throw invalid();
  return result;
}

function acceptedMessage(response, context) {
  const messages = Array.isArray(response?.output)
    ? response.output.filter((item) => item?.type !== "reasoning") : null;
  const item = messages?.[0];
  if (messages?.length !== 1 || !finalMessage(item) || item.status !== "completed"
      || item.content?.length !== 1 || item.content[0]?.type !== "output_text"
      || (item.content[0].annotations ?? []).length !== 0 || response.status !== "completed") throw invalid();
  const text = materializeResultDelivery(item.content[0].text, context);
  return { original: item, text, item: { ...item, content: [{ ...item.content[0], text }] } };
}

// Retain only a potential compact final message until provider completion. Normal
// prose streams unchanged as soon as its prefix differs. IDs, phase and usage
// are upstream-owned; expanded bytes are not reported as provider output tokens.
export function resultDeliveryProjector(context, observe = () => {}, onExpanded = () => {}) {
  if (!context) return (event) => event;
  let pending = [], bytes = 0, itemId = null, streamed = "", compact = false;
  const flush = () => {
    const events = pending;
    pending = []; bytes = 0; itemId = null; streamed = ""; compact = false;
    return events;
  };
  const responseHasMarker = (response) => Array.isArray(response?.output) && response.output.some((item) => finalMessage(item)
    && Array.isArray(item.content) && item.content.some((part) => part.type === "output_text"
      && prefixState(part.text) === "compact"));
  return (event) => {
    if (!event || typeof event !== "object") return event;
    if (["response.failed", "response.incomplete", "error"].includes(event.type)
        || (!event.type && ["failed", "incomplete"].includes(event.response?.status))) observe("provider_failed");
    if ((event.type === "response.completed" || (!event.type && event.response?.status === "completed"))
        && !responseHasMarker(event.response)) observe("ordinary");
    if (!event.type && responseHasMarker(event.response)) {
      const accepted = acceptedMessage(event.response, context);
      observe("expanded");
      onExpanded(accepted.original, accepted.item);
      return { ...event, response: { ...event.response,
        output: event.response.output.map((item) => item === accepted.original ? accepted.item : item) } };
    }
    if (!pending.length) {
      if (event.type === "response.output_item.added" && finalMessage(event.item)
          && typeof event.item.id === "string" && event.item.content?.length === 0) {
        itemId = event.item.id;
      } else if (event.type === "response.completed" && responseHasMarker(event.response)) {
        const accepted = acceptedMessage(event.response, context);
        observe("expanded");
        onExpanded(accepted.original, accepted.item);
        return { ...event, response: { ...event.response,
          output: event.response.output.map((item) => item === accepted.original ? accepted.item : item) } };
      } else return event;
    }
    pending.push(event);
    bytes += Buffer.byteLength(JSON.stringify(event));
    if (bytes > MAX_BUFFER_BYTES) throw invalid();
    if (["response.failed", "response.incomplete", "error"].includes(event.type)) {
      const wasCompact = compact;
      const events = flush();
      return wasCompact ? event : events;
    }
    if (event.type === "response.output_text.delta" && event.item_id === itemId) {
      if (event.content_index !== 0 || typeof event.delta !== "string") throw invalid();
      streamed += event.delta;
      if (prefixState(streamed) === "compact") compact = true;
      else if (prefixState(streamed) === "ordinary") return flush();
      if (Buffer.byteLength(streamed) > MAX_ACCEPTANCE_BYTES) throw invalid();
    }
    if (event.type === "response.output_text.done" && event.item_id === itemId) {
      if (prefixState(event.text) !== "compact") {
        if (compact) throw invalid();
        return flush();
      }
      if (streamed && streamed !== event.text) throw invalid();
      compact = true;
    }
    if (event.type !== "response.completed") return null;
    if (!compact && !responseHasMarker(event.response)) return flush();
    const accepted = acceptedMessage(event.response, context);
    if (accepted.item.id !== itemId || (streamed && streamed !== accepted.original.content[0].text)) throw invalid();
    for (const part of pending) {
      if (part.type?.startsWith("response.output_item.") && part.item?.id !== itemId
          && part.item?.type !== "reasoning") throw invalid();
      if ((part.type?.startsWith("response.output_text.") || part.type?.startsWith("response.content_part."))
          && part.item_id !== itemId) throw invalid();
      if (part.type?.startsWith("response.function_call_arguments.")
          || part.type?.startsWith("response.custom_tool_call_input.")) throw invalid();
    }
    let emittedDelta = false;
    const delivered = flush().flatMap((part) => {
      if (part.type === "response.output_text.delta" && part.item_id === accepted.item.id) {
        if (emittedDelta) return [];
        emittedDelta = true;
        return [{ ...part, delta: accepted.text }];
      }
      if (part.type === "response.output_text.done" && part.item_id === accepted.item.id) {
        if (part.text !== accepted.original.content[0].text || part.content_index !== 0) throw invalid();
        return [{ ...part, text: accepted.text }];
      }
      if (part.type === "response.content_part.done" && part.item_id === accepted.item.id) {
        if (part.part?.text !== accepted.original.content[0].text || part.content_index !== 0) throw invalid();
        return [{ ...part, part: { ...part.part, text: accepted.text } }];
      }
      if (part.type === "response.output_item.done" && part.item?.id === accepted.item.id) {
        if (JSON.stringify(part.item) !== JSON.stringify(accepted.original)) throw invalid();
        return [{ ...part, item: accepted.item }];
      }
      if (part.type === "response.completed") return [{ ...part, response: { ...part.response,
        output: part.response.output.map((item) => item === accepted.original ? accepted.item : item) } }];
      return [part];
    });
    observe("expanded");
    onExpanded(accepted.original, accepted.item);
    return delivered;
  };
}
