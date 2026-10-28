import { createHash } from "node:crypto";
import { SwitchboardError } from "./shared.mjs";
import { inputPair } from "./nokiy-result-delivery.mjs";
import { checkCoordinatorCall } from "./nokiy-coordinator-policy.mjs";

export const NOKIY_OBSERVE = "nokiy_observe";
const MAX_REFERENCES = 32;
const sourceCallId = (value) => typeof value === "string" && /^[A-Za-z0-9_.:-]{1,128}$/.test(value);
const numericSession = (value) => Number.isSafeInteger(value) && value >= 0;
const denied = (reason, nextAction, diagnostics = null) => new SwitchboardError("NOKIY_NATIVE_OBSERVATION_INVALID",
  "The original native session was not observed; no command or worker was started.", 422,
  { native_observation: { reason, next_action: nextAction, ...(diagnostics ? { pair_diagnostics: diagnostics } : {}) } });

export function hasNativeSessionControl(tool) {
  const declaration = tool?.description?.match(/declare const tools: \{ write_stdin\(args: \{([\s\S]*?)\}\): Promise</)?.[1];
  return typeof declaration === "string" && /\bsession_id:\s*number\s*;/.test(declaration)
    && /\bchars\?:\s*string\s*;/.test(declaration);
}

export function observationControl(body, target) {
  if (![null, "functions"].includes(target.namespace ?? null)) return null;
  if (target.type === "custom" && target.sessionControl === true) return Object.freeze({ ...target });
  const found = [];
  function visit(tools, namespace = null) {
    for (const tool of tools || []) {
      if (tool?.type === "namespace" && tool.name === "functions" && namespace === null) visit(tool.tools, tool.name);
      if (tool?.type !== "function" || tool.name !== "write_stdin") continue;
      const properties = tool.parameters?.properties;
      if (!["number", "integer"].includes(properties?.session_id?.type) || properties?.chars?.type !== "string") continue;
      if (!Array.isArray(tool.parameters.required) || !tool.parameters.required.includes("session_id")
          || tool.parameters.required.some((name) => !["session_id", "chars"].includes(name))
          || (properties.chars.const !== undefined && properties.chars.const !== "")
          || (properties.chars.enum && !properties.chars.enum.includes(""))) continue;
      found.push({ type: "function", name: tool.name, namespace, sessionSchema: structuredClone(properties.session_id) });
    }
  }
  visit(body.tools);
  for (const item of body.input || []) if (["additional_tools", "tool_search_output"].includes(item?.type)) visit(item.tools);
  return found.length === 1 ? Object.freeze(found[0]) : null;
}

export function observationTool(control) {
  const description = 'Observe an original same-thread native exec_command session without starting a model or replaying its command. JSON {"kind":"exec_session","source_call_id":"exact native call that emitted the handle","session_id":42}. The handle must be a nonnegative safe integer from its paired native tool output, not user text, within any advertised native bounds. Verified original references can survive intervening tools only along the same authenticated owner/continuation chain; they are bounded and never reconstructed from model proof. This is not a code-mode cell: functions.wait uses its original string cell_id. Native ownership remains controlling. Polling sends only empty stdin and uses native default timing/output limits. Without an admitted original pair or retained reference, use original-ID artifact recovery; never guess a handle or redispatch.';
  if (control.type === "custom") return { type: "custom", name: NOKIY_OBSERVE, description, format: { type: "text" } };
  return { type: "function", name: NOKIY_OBSERVE, description, parameters: { type: "object", properties: {
    kind: { type: "string", enum: ["exec_session"] }, source_call_id: { type: "string", minLength: 1, maxLength: 128 },
    session_id: { ...control.sessionSchema, type: "integer" },
  }, required: ["kind", "source_call_id", "session_id"], additionalProperties: false } };
}

function pairIndex(body) {
  const pairs = new Map();
  for (const [index, item] of (Array.isArray(body.input) ? body.input : []).entries()) {
    const call = ["function_call", "custom_tool_call"].includes(item?.type);
    const output = ["function_call_output", "custom_tool_call_output"].includes(item?.type);
    if ((!call && !output) || !sourceCallId(item.call_id)) continue;
    const pair = pairs.get(item.call_id) ?? { call_count: 0, output_count: 0 };
    pair[call ? "call_count" : "output_count"]++;
    pair[call ? "call_index" : "output_index"] = index;
    pairs.set(item.call_id, pair);
  }
  return pairs;
}

function originalPair(body, id, continuationCall, pairs = pairIndex(body)) {
  const input = Array.isArray(body.input) ? body.input : [];
  const entry = pairs.get(id) ?? { call_count: 0, output_count: 0 };
  const diagnostics = { call_count: entry.call_count, output_count: entry.output_count };
  const result = (condition, pair = null) => ({ pair, diagnostics: { ...diagnostics, condition } });
  if (entry.call_count > 1) return result("duplicate_call");
  if (entry.output_count > 1) return result("duplicate_output");
  if (!entry.call_count && !entry.output_count) return result("missing_pair");
  if (!entry.output_count) return result("missing_output");
  if (!entry.call_count) {
    // Codex may append context messages (environment updates, user steering) after the
    // continuation output. They answer no call, so the output still pairs with the carried call.
    if (entry.output_index !== 0 || input.slice(1).some((item) => item?.type !== "message")) {
      return result("continuation_input_not_single_output");
    }
    if (typeof body.previous_response_id !== "string" || !body.previous_response_id) return result("continuation_response_missing");
    if (!continuationCall) return result("continuation_call_missing");
    if (continuationCall.call_id !== id) return result("continuation_call_mismatch");
    const pair = inputPair(input.length === 1 ? body : { ...body, input: [input[0]] }, continuationCall);
    return result(pair ? "continuation_pair" : "call_output_type_mismatch", pair);
  }
  const call = input[entry.call_index], output = input[entry.output_index];
  if (output.type !== (call.type === "function_call" ? "function_call_output" : "custom_tool_call_output")) {
    return result("call_output_type_mismatch");
  }
  if (entry.output_index < entry.call_index) return result("output_before_call");
  // Keep the shared result-delivery adjacency contract; no reordered-pair admission.
  const pair = inputPair({ input: input.slice(0, entry.output_index + 1) }, null);
  return pair?.call === call ? result("full_pair", pair) : result("nonadjacent_pair");
}

function nativeCallIdentity(call, projection) {
  const bindings = [];
  const add = (type, name, namespace) => bindings.push({ type, name, namespace });
  function visit(tools, namespace = null) {
    for (const tool of tools || []) {
      if (tool?.type === "namespace") visit(tool.tools, tool.name);
      else if (["function", "custom"].includes(tool?.type) && tool.name === call.name) {
        add(tool.type === "function" ? "function_call" : "custom_tool_call", tool.name, namespace);
      }
    }
  }
  visit(projection.observationBody.tools);
  for (const item of projection.observationBody.input || []) {
    if (["additional_tools", "tool_search_output"].includes(item?.type)) visit(item.tools);
  }
  // The confirmed native exec owner exposes these methods behind code mode;
  // historical function calls can retain that identity after a model switch.
  if (call.type === "function_call" && ["exec_command", "write_stdin"].includes(call.name)) {
    add(call.type, call.name, projection.target.namespace);
  } else if (call.type === "custom_tool_call" && call.name === projection.target.name) {
    add(call.type, call.name, projection.target.namespace);
  }
  const unique = [...new Map(bindings.map((binding) => [JSON.stringify(binding), binding])).values()];
  try {
    const normalized = checkCoordinatorCall(call, unique, { stage: "original_native_observation" });
    if ((normalized.namespace ?? null) === (projection.target.namespace ?? null)) return normalized;
  } catch (error) {
    if (error.details?.coordinator_rejection?.reason === "call_ambiguous") {
      throw denied("ORIGINAL_TOOL_AMBIGUOUS", "An omitted namespace must resolve to one advertised native identity; recover exact owner metadata instead of guessing.");
    }
  }
  throw denied("ORIGINAL_TOOL_NOT_NATIVE", "Use only the original advertised native tool owner, never an MCP or foreign session.");
}

function canonicalCodeCall(call) {
  const match = call.input?.match(/^const result = await tools\.(exec_command|write_stdin)\((.*)\); text\(result\);$/)
    || call.input?.match(/^text\(await tools\.(exec_command|write_stdin)\((.*)\)\);$/);
  if (!match) return null;
  try {
    const args = JSON.parse(match[2]);
    if (!args || typeof args !== "object" || Array.isArray(args)) return null;
    if (match[1] === "exec_command" && typeof args.cmd === "string") return { method: match[1] };
    if (match[1] === "write_stdin" && numericSession(args.session_id) && (args.chars === undefined || args.chars === "")) {
      return { method: match[1], sessionId: args.session_id };
    }
  } catch { /* Only exact JSON bridge calls can prove the result's origin. */ }
  return null;
}

function outputText(value, codeMode) {
  if (Array.isArray(value)) {
    if (!value.length || (!codeMode && value.length !== 1)
        || value.some((part) => !["input_text", "text"].includes(part?.type) || typeof part.text !== "string")
        || value.reduce((size, part) => size + Buffer.byteLength(part.text), 0) > 256 * 1024) return null;
    value = value.map((part) => part.text).join("");
  }
  if (typeof value !== "string" || Buffer.byteLength(value) > 256 * 1024) return null;
  return codeMode ? value.replace(/^Script completed\r?\nWall time \d+(?:\.\d+)? seconds\r?\nOutput:\r?\n/, "") : value;
}

function observedSession(output, codeMode) {
  const text = outputText(output, codeMode);
  if (typeof text !== "string" || Buffer.byteLength(text) > 256 * 1024) return null;
  if (codeMode || text.startsWith("{")) {
    try {
      const result = JSON.parse(text);
      return result && typeof result.output === "string" && numericSession(result.session_id)
        && result.exit_code == null && result.terminal !== true
        ? result.session_id : null;
    } catch { return null; }
  }
  // Native metadata precedes Output; a handle printed by the command is not metadata.
  const match = text.match(/^(?:Chunk ID: [A-Za-z0-9_-]{1,128}\n)?Wall time: \d+(?:\.\d+)? seconds\nProcess running with session ID (0|[1-9]\d*)\n(?:Original token count: \d+\n)?Output:\n/);
  return match && numericSession(Number(match[1])) ? Number(match[1]) : null;
}

const callDigest = (call) => createHash("sha256").update(JSON.stringify([call.type, call.name, call.namespace ?? null,
  call.type === "function_call" ? call.arguments : call.input])).digest("hex");
const yieldedCell = (output) => outputText(output, true)?.match(
  /^Script running with cell ID ([A-Za-z0-9_-]{1,128})\r?\nWall time \d+(?:\.\d+)? seconds\r?\nOutput:\r?\n/)?.[1] ?? null;

// A non-wait call whose paired output yielded this cell is its visible origin; the tracker's
// verdict on it stays authoritative. Waits that yielded the same cell only observed it.
export function cellOriginVisible(projection, cellId) {
  const input = Array.isArray(projection.observationBody?.input) ? projection.observationBody.input : [];
  const calls = new Map(input.filter((item) => ["function_call", "custom_tool_call"].includes(item?.type)
    && sourceCallId(item.call_id)).map((item) => [item.call_id, item]));
  const continuation = projection.nativeObservationContinuationCall;
  return input.some((item) => {
    if (!["function_call_output", "custom_tool_call_output"].includes(item?.type) || yieldedCell(item.output) !== cellId) return false;
    const call = calls.get(item.call_id) ?? (continuation?.call_id === item.call_id ? continuation : null);
    return Boolean(call) && !(call.type === "function_call" && call.name === "wait");
  });
}

function waitControl(projection, previous) {
  const bindings = [];
  function visit(tools, namespace = null) {
    for (const tool of tools || []) {
      if (tool?.type === "namespace") visit(tool.tools, tool.name);
      else if (tool?.name === "wait" && tool.type === "function") {
        let schema = null;
        try { schema = JSON.parse(JSON.stringify(tool.parameters)); } catch { /* Non-JSON schemas cannot supply defaults. */ }
        bindings.push({ type: "function_call", name: "wait", namespace,
          valid: tool.parameters?.properties?.cell_id?.type === "string"
            && tool.parameters.required?.includes("cell_id"),
          fields: Object.freeze(Object.keys(tool.parameters?.properties ?? {})), schema });
      }
    }
  }
  visit(projection.observationBody.tools);
  for (const item of projection.observationBody.input || []) {
    if (["additional_tools", "tool_search_output"].includes(item?.type)) visit(item.tools);
  }
  if (!bindings.length) return previous ?? null;
  return bindings.length === 1 && bindings[0].valid && bindings[0].namespace === "functions"
    ? Object.freeze(bindings[0]) : null;
}

function waitCell(call, control) {
  if (!control || call?.type !== "function_call" || call.name !== "wait"
      || (call.namespace ?? "functions") !== "functions") return null;
  try {
    const args = JSON.parse(call.arguments);
    return args && typeof args === "object" && !Array.isArray(args)
      && typeof args.cell_id === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(args.cell_id)
      && (args.terminate === undefined || args.terminate === false)
      && Object.keys(args).every((name) => control.fields.includes(name)) ? args.cell_id : null;
  } catch { return null; }
}

function retainCellObservations(projection, previous, continuationCall, pairs, references, conflicts, matchesNativeCall) {
  const control = waitControl(projection, previous?.cellWaitControl);
  const pending = new Map((control ? previous?.pendingCells ?? [] : []).map((ref) => [ref.source_call_id, ref]));
  const input = projection.observationBody.input ?? [];
  for (const [id, indexes] of pairs) {
    const call = indexes.call_count ? input[indexes.call_index] : continuationCall;
    const cell = waitCell(call, control);
    const source = call?.type === "custom_tool_call" && matchesNativeCall(call);
    if (!cell && !source) continue;
    const evidence = originalPair(projection.observationBody, id, continuationCall, pairs);
    if (source) {
      const retained = pending.get(id);
      pending.delete(id);
      if (!evidence.pair || !control) continue;
      const cellId = yieldedCell(evidence.pair.output.output);
      if (!cellId) continue;
      try {
        const native = nativeCallIdentity(call, projection);
        const ref = { source_call_id: id, call_sha256: callDigest(native), cell_id: cellId };
        references.delete(id);
        if (retained && (retained.call_sha256 !== ref.call_sha256 || retained.cell_id !== ref.cell_id)) {
          conflicts.push(id);
          continue;
        }
        pending.set(id, Object.freeze(ref));
        if (pending.size > MAX_REFERENCES) pending.delete(pending.keys().next().value);
      } catch { /* An unverified owner cannot establish a pending cell. */ }
      continue;
    }
    const owners = [...pending.values()].filter((ref) => ref.cell_id === cell);
    if (owners.length !== 1) {
      for (const ref of owners) pending.delete(ref.source_call_id);
      continue;
    }
    const ref = owners[0];
    if (evidence.pair && yieldedCell(evidence.pair.output.output) === cell) continue;
    pending.delete(ref.source_call_id);
    references.delete(ref.source_call_id);
    if (!evidence.pair) continue;
    const sessionId = observedSession(evidence.pair.output.output, true);
    if (numericSession(sessionId)) references.set(ref.source_call_id, Object.freeze({ ...ref, session_id: sessionId }));
  }
  return { pendingCells: Object.freeze([...pending.values()]), cellWaitControl: control };
}

function verifiedReference(pair, projection, matchesNativeCall) {
  const call = nativeCallIdentity(pair.call, projection);
  const code = call.type === "custom_tool_call" && call.name === projection.target.name ? canonicalCodeCall(call) : null;
  const native = call.type === "function_call" && ["exec_command", "write_stdin"].includes(call.name);
  if (!native && !code && !matchesNativeCall(call)) {
    throw denied("ORIGINAL_TOOL_NOT_NATIVE", "Only native function calls or exact native bridge code can establish handle provenance; arbitrary code/log text cannot.");
  }
  const sessionId = observedSession(pair.output.output, call.type === "custom_tool_call");
  if (!numericSession(sessionId) || (code?.sessionId !== undefined && code.sessionId !== sessionId)) {
    throw denied("ORIGINAL_RUNNING_HANDLE_UNVERIFIED", "Use the exact running handle from native metadata; terminal, missing, string, changed, or log-only handles are unknown. Never replay.");
  }
  return Object.freeze({ source_call_id: call.call_id, session_id: sessionId,
    call_sha256: callDigest(call) });
}

// Internal continuation only: the server supplies this state after its existing
// connection/model/tools/response/profile checks. Never read proof from body fields.
export function retainNativeObservations(projection, continuation, matchesNativeCall) {
  if (!projection?.nativeObservation) return projection;
  const owner = JSON.stringify([projection.target.type, projection.target.name, projection.target.namespace ?? null]);
  const sameOwner = continuation?.state?.owner === owner;
  const references = new Map((sameOwner ? continuation.state.references : []).map((ref) => [ref.source_call_id, ref]));
  const continuationCall = continuation?.state && !sameOwner ? null
    : sameOwner ? continuation.call : projection.continuationCall;
  const pairs = pairIndex(projection.observationBody), conflicts = [];
  // One index scan; validate only the bounded recent candidates and any retained
  // IDs explicitly present now, so conflicting current evidence cannot be hidden.
  const candidates = new Set([...references.keys()].filter((id) => pairs.has(id)));
  for (const id of [...pairs.keys()].slice(-MAX_REFERENCES)) candidates.add(id);
  for (const id of candidates) {
    const evidence = originalPair(projection.observationBody, id, continuationCall, pairs);
    let reference;
    try { if (evidence.pair) reference = verifiedReference(evidence.pair, projection, matchesNativeCall); }
    catch (error) {
      if (error.code !== "NOKIY_NATIVE_OBSERVATION_INVALID") throw error;
    }
    const previous = references.get(id);
    references.delete(id);
    if (!reference) continue;
    if (previous && (previous.call_sha256 !== reference.call_sha256 || previous.session_id !== reference.session_id)) {
      conflicts.push(id);
      continue;
    }
    references.set(id, reference);
  }
  const cells = retainCellObservations(projection, sameOwner ? continuation.state : null,
    continuationCall, pairs, references, conflicts, matchesNativeCall);
  return Object.freeze({ ...projection,
    nativeObservationState: Object.freeze({ owner, references: Object.freeze([...references.values()].slice(-MAX_REFERENCES)), ...cells }),
    nativeObservationContinuationCall: continuationCall,
    nativeObservationConflicts: Object.freeze(conflicts),
    nativeObservationContinuity: sameOwner ? "bound" : continuation?.state ? "owner_mismatch"
      : continuation?.condition ?? "not_retained" });
}

export function projectNativeObservation(item, projection, matchesNativeCall) {
  const control = projection.nativeObservation;
  if (!control) throw denied("NATIVE_OBSERVATION_NOT_ADVERTISED", "Native write_stdin must be advertised; use original-ID artifact recovery when unavailable.");
  if (item.type !== (control.type === "custom" ? "custom_tool_call" : "function_call")) {
    throw denied("CALL_TYPE_MISMATCH", "Use the advertised observation call type; do not change native call/output pairing.");
  }
  const raw = control.type === "custom" ? item.input : item.arguments;
  let args;
  try { if (typeof raw === "string" && Buffer.byteLength(raw) <= 4096) args = JSON.parse(raw); } catch { /* Diagnosed below. */ }
  if (args?.kind !== "exec_session") throw denied("HANDLE_KIND_MISMATCH", "An exec cell requires native wait and its original string cell_id; never coerce a numeric session into a cell.");
  if (Object.keys(args).sort().join() !== "kind,session_id,source_call_id" || !numericSession(args.session_id)
      || !sourceCallId(args.source_call_id)) {
    throw denied("HANDLE_ARGUMENTS_INVALID", "Supply only kind, exact source_call_id, and the original numeric session_id; stdin and commands are forbidden.");
  }
  const schema = control.sessionSchema;
  if (schema && ((schema.minimum !== undefined && args.session_id < schema.minimum)
      || (schema.maximum !== undefined && args.session_id > schema.maximum)
      || (schema.enum && !schema.enum.includes(args.session_id)))) {
    throw denied("NATIVE_SCHEMA_MISMATCH", "Use only the original numeric handle accepted by the current native session_id schema; never coerce or relax the native contract.");
  }
  const evidence = originalPair(projection.observationBody, args.source_call_id,
    projection.nativeObservationState ? projection.nativeObservationContinuationCall : projection.continuationCall);
  const diagnostics = { ...evidence.diagnostics, continuity: projection.nativeObservationContinuity ?? "not_retained" };
  const unavailable = () => denied("ORIGINAL_PAIR_UNAVAILABLE", "Recover the exact original native call/output pair or original-ID artifact; user-pasted handles are not evidence. Do not replay the command.", diagnostics);
  if (projection.nativeObservationConflicts?.includes(args.source_call_id)) {
    diagnostics.condition = "retained_reference_conflict";
    throw unavailable();
  }
  let reference;
  if (evidence.pair) {
    const retained = projection.nativeObservationState?.references.find((ref) => ref.source_call_id === args.source_call_id);
    const cell = yieldedCell(evidence.pair.output.output);
    reference = cell && retained?.cell_id === cell
      && retained.call_sha256 === callDigest(nativeCallIdentity(evidence.pair.call, projection))
      ? retained : verifiedReference(evidence.pair, projection, matchesNativeCall);
  }
  else if (!evidence.diagnostics.call_count && !evidence.diagnostics.output_count) {
    reference = projection.nativeObservationState?.references.find((ref) => ref.source_call_id === args.source_call_id);
  }
  if (!reference) throw unavailable();
  const sessionId = reference.session_id;
  if (sessionId !== args.session_id) {
    throw denied("ORIGINAL_RUNNING_HANDLE_UNVERIFIED", "Use the exact running handle from native metadata; terminal, missing, string, changed, or log-only handles are unknown. Never replay.");
  }
  const nativeArgs = { session_id: sessionId, chars: "" };
  return { ...item, name: control.name, ...(control.namespace ? { namespace: control.namespace } : {}),
    [control.type === "custom" ? "input" : "arguments"]: control.type === "custom"
      ? `text(await tools.write_stdin(${JSON.stringify(nativeArgs)}));` : JSON.stringify(nativeArgs) };
}
