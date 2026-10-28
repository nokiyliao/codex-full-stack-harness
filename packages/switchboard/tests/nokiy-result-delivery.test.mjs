import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import http from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import WebSocket, { WebSocketServer } from "ws";
import { createSwitchboardServer } from "../src/server.mjs";
import { compactNokiyResults } from "../src/nokiy-direct-route.mjs";
import { nativeToolProjection, nativeToolItem, nativeEventProjector, nativeSseProjector } from "../src/nokiy-native-tool.mjs";
import { prepareResultDelivery, prepareResultDeliveryDiagnostic, materializeResultDelivery, resultDeliveryProjector, bindPendingResultWait, resultContinuationCall } from "../src/nokiy-result-delivery.mjs";
import { createReplayRestorer } from "../src/nokiy-replay-restore.mjs";

// The delivery transport is the newest developer input item, never an instructions edit.
const deliveryNote = (projected) => {
  const note = projected.input.at(-1);
  assert.equal(note.type, "message");
  assert.equal(note.role, "developer");
  return note.content[0].text;
};
// Simulated model: read the delivery transport wherever the projection placed it.
const transportText = (body) => {
  const last = Array.isArray(body?.input) ? body.input.at(-1) : null;
  return last?.type === "message" && last.role === "developer" ? last.content?.[0]?.text ?? "" : body?.instructions ?? "";
};

const digest = (text) => createHash("sha256").update(text).digest("hex");
const host = { python: "/nonexistent/python", script: "/nonexistent/nokiy-host.py", parent_result_delivery: "accept_v1" };
const payload = { operation: "run_ref", input_path: "/nonexistent/input.json", input_sha256: "b".repeat(64) };
const text = "A source-backed answer.\nEvidence: src/worker.rs:19\nReview \u8b49\u64da.\n"
  + "Unchanged source evidence belongs to the same accepted result.\n".repeat(48);
const thread = "12345678-1234-1234-1234-123456789abc";

function result(answer = text) {
  const artifact = { path: "/not/read/last-message.txt", sha256: digest(answer), bytes: Buffer.byteLength(answer) };
  return {
    schema_version: "nokiy_terminal_summary_v1", status: "RESULT_AVAILABLE", cleanup_pass: true,
    first_typed_blocker: null, fallback_used: false, mission_acceptance: "parent_owned", continuation_owner: "codex",
    runtime_exit_code: 0, request_sha256: "a".repeat(64), request_id: `tura_embedded_${"a".repeat(64)}`,
    native_thread_id: thread, terminal_sha256: "c".repeat(64),
    result_artifact: artifact, result_text: answer, result_truncated: false,
    result_inspection: { status: "EVIDENCE_VERIFIED", first_blocker: null,
      terminal_sha256: "c".repeat(64), command_success: "known_success", artifact_integrity: "verified",
      counts: { file_changes: 0, failed: 0, index_complete: true },
    },
  };
}

function request(type = "function", terminal = result(), hostOverride = host, batch = false) {
  const options = { host: batch ? { ...hostOverride, batch_reference: "direct_v1" } : hostOverride,
    workflow: batch ? "nokiy-direct" : "nokiy", worker: "gpt-6.1-sol", workerEffort: "max" };
  const base = { model: "gpt-6-astra", instructions: "Original instructions.", tools: [
    { type: "namespace", name: "functions", tools: [
      type === "function" ? { type, name: "exec_command", parameters: { type: "object" } }
        : { type, name: "exec", format: { type: "text" } },
    ] },
  ] };
  const projection = nativeToolProjection(base, options);
  const field = type === "function" ? "arguments" : "input";
  const call = nativeToolItem({ type: type === "function" ? "function_call" : "custom_tool_call",
    name: "nokiy_execute", id: "tool_item", call_id: "native_call",
    [field]: JSON.stringify(batch ? { ...payload, operation: "run_batch_ref" } : payload) }, projection);
  const output = { type: type === "function" ? "function_call_output" : "custom_tool_call_output",
    call_id: call.call_id, output: JSON.stringify(terminal) };
  return { body: { ...base, input: [call, output] }, options };
}

function project(type = "function", terminal = result()) {
  const { body, options } = request(type, terminal);
  return nativeToolProjection(body, options);
}

test("delivery survives a host-generation change only with the exact recorded dispatch", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "nokiy-generation-delivery-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  for (const type of ["function", "custom"]) {
    const { body, options } = request(type);
    const field = type === "function" ? "arguments" : "input";
    const call = body.input[0];
    const original = { ...call, name: "nokiy_execute", [field]: JSON.stringify(payload) };
    delete original.namespace;
    const path = join(directory, `${type}.jsonl`);
    const writer = createReplayRestorer({ path });
    writer.record(original, call);
    const retained = createReplayRestorer({ path });
    const next = { ...options, host: { ...options.host, script: "/different/generation/nokiy-host.py" } };
    assert.equal(nativeToolProjection(body, next).resultDelivery, null);
    const bound = { ...next, recordedNativeCall: retained.originalCall };
    assert.ok(nativeToolProjection(body, bound).resultDelivery);
    const delta = { ...body, previous_response_id: "prior_response", input: [body.input[1]] };
    assert.ok(nativeToolProjection(delta, { ...bound, continuationCall: call }).resultDelivery);
    for (const changed of [
      { ...call, [field]: call[field] + " " },
      { ...call, call_id: "another_call" },
    ]) {
      assert.equal(nativeToolProjection({ ...body, input: [changed, { ...body.input[1], call_id: changed.call_id }] }, bound).resultDelivery, null);
    }
    assert.equal(nativeToolProjection(body, { ...bound, workerEffort: "xhigh" }).resultDelivery, null);
    assert.equal(nativeToolProjection(body, { ...bound, worker: "gpt-6-astra" }).resultDelivery, null);
    assert.equal(nativeToolProjection(body, { ...bound, workerServiceTier: "priority" }).resultDelivery, null);
    const forgedStore = createReplayRestorer();
    forgedStore.record({ ...original, name: "not_nokiy_execute" }, call);
    assert.equal(nativeToolProjection(body, { ...next, recordedNativeCall: forgedStore.originalCall }).resultDelivery, null);
    assert.equal(nativeToolProjection(request(type, { ...result(), terminal_sha256: "d".repeat(64) }).body,
      bound).resultDelivery, null);
  }
});

function reconciledResult() {
  const terminal = result();
  terminal.result_inspection.command_success = "known_failure";
  terminal.result_inspection.counts.failed = 1;
  terminal.result_inspection.source_read_rejection_reconciliation = {
    schema_version: "nokiy_source_read_rejection_reconciliation_v1",
    not_attempted_count: 1, unresolved_count: 0, effective_command_success: "known_success",
  };
  return terminal;
}

test("single: authenticated read non-attempts retain history and allow explicit acceptance", () => {
  for (const type of ["function", "custom"]) {
    const terminal = reconciledResult(), before = structuredClone(terminal);
    const context = project(type, terminal).resultDelivery;
    assert.ok(context);
    assert.equal(materializeResultDelivery(acceptance(context), context), text);
    assert.deepEqual(terminal, before);
    assert.equal(terminal.result_inspection.counts.failed, 1);
    assert.equal(terminal.result_inspection.command_success, "known_failure");
    assert.equal(replay(events(acceptance(context)), resultDeliveryProjector(context))
      .at(-1).response.output[0].content[0].text, text);
  }
});

test("single: malformed reconciliation and real effects remain ineligible", () => {
  const changes = [
    t => delete t.result_inspection.source_read_rejection_reconciliation,
    t => t.result_inspection.source_read_rejection_reconciliation = [],
    t => t.result_inspection.command_success = "known_success",
    t => t.result_inspection.status = "INCOMPLETE_EVIDENCE",
    t => t.result_inspection.counts.file_changes = 1,
    t => t.result_inspection.counts.index_complete = false,
    t => t.result_inspection.artifact_integrity = "unknown",
    t => t.cleanup_pass = false,
    t => t.result_inspection.first_blocker = "COMMAND_FAILURE",
  ];
  for (const failed of [true, "1", 0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1]) {
    changes.push(t => t.result_inspection.counts.failed = failed);
  }
  for (const [key, values] of Object.entries({
    schema_version: ["other"], not_attempted_count: [0, 2, true, "1", 1.5],
    unresolved_count: [1, false, "0"], effective_command_success: ["known_failure", "unknown"],
    unexpected: [true],
  })) {
    for (const value of values) changes.push(t => t.result_inspection.source_read_rejection_reconciliation[key] = value);
  }
  for (const change of changes) {
    const terminal = reconciledResult(); change(terminal);
    assert.equal(project("function", terminal).resultDelivery, null);
  }
});

const NO_MATCH = Object.freeze({
  schema_version: "nokiy_search_no_match_reconciliation_v1",
  no_match_count: 1, unresolved_count: 0, effective_command_success: "known_success",
});

test("single: searches whose exit 1 means no match are delivered, alone or with read non-attempts", () => {
  const alone = result();
  alone.result_inspection.command_success = "known_failure";
  alone.result_inspection.counts.failed = 1;
  alone.result_inspection.search_no_match_reconciliation = { ...NO_MATCH };
  const mixed = reconciledResult();
  mixed.result_inspection.counts.failed = 2;
  mixed.result_inspection.search_no_match_reconciliation = { ...NO_MATCH };
  for (const terminal of [alone, mixed]) {
    const before = structuredClone(terminal);
    const context = project("function", terminal).resultDelivery;
    assert.ok(context);
    assert.equal(materializeResultDelivery(acceptance(context), context), text);
    assert.deepEqual(terminal, before);
  }
  const changes = [
    t => t.result_inspection.counts.failed = 2,
    t => t.result_inspection.command_success = "known_success",
    t => t.result_inspection.search_no_match_reconciliation = null,
    t => t.result_inspection.search_no_match_reconciliation = [],
  ];
  for (const [key, values] of Object.entries({
    schema_version: ["nokiy_source_read_rejection_reconciliation_v1", "other"],
    no_match_count: [0, 2, true, "1", 1.5], unresolved_count: [1, false, "0"],
    effective_command_success: ["known_failure", "unproven"], not_attempted_count: [1], unexpected: [true],
  })) {
    for (const value of values) changes.push(t => t.result_inspection.search_no_match_reconciliation[key] = value);
  }
  for (const change of changes) {
    const terminal = structuredClone(alone); change(terminal);
    assert.equal(project("function", terminal).resultDelivery, null);
  }
});

test("single: complete inline native answers need no invented artifact", () => {
  const terminal = reconciledResult(); terminal.result_artifact = null;
  const before = structuredClone(terminal);
  const context = project("function", terminal).resultDelivery;
  assert.ok(context);
  assert.equal(materializeResultDelivery(acceptance(context), context), text);
  assert.deepEqual(terminal, before);
  const changed = structuredClone(terminal); changed.result_text += "different";
  assert.notEqual(project("function", changed).resultDelivery.reference, context.reference);
  for (const change of [
    t => t.result_truncated = true,
    t => delete t.result_artifact,
    t => t.result_artifact = {},
    t => t.result_artifact = {path: "/bad", sha256: "a".repeat(64), bytes: 1},
    t => t.result_inspection.terminal_sha256 = "d".repeat(64),
    t => t.result_inspection.status = "INCOMPLETE_EVIDENCE",
    t => t.result_inspection.counts.file_changes = 1,
    t => t.result_text = "x".repeat(256 * 1024 + 1),
  ]) {
    const invalid = structuredClone(terminal); change(invalid);
    assert.equal(project("function", invalid).resultDelivery, null);
  }
});

function acceptance(context, edits = []) {
  return JSON.stringify({ nokiy_delivery: "accept_v1", reference: context.reference, edits });
}

function nativeCodeOutput(terminal = result()) {
  return [
    { type: "input_text", text: "Script completed\nWall time 0.2 seconds\nOutput:\n" },
    { type: "input_text", text: JSON.stringify({ terminal: true, exit_code: 0,
      truncated: false, output_truncated: false, recovery_session_id: null,
      output: JSON.stringify(terminal) }) },
  ];
}

function nativeObjectOutput(terminal = result()) {
  return [
    { type: "input_text", text: "Script completed\nWall time 0.2 seconds\nOutput:\n" },
    { type: "input_text", text: JSON.stringify({ terminal: true, exit_code: 0, recovery_session_id: null,
      output: terminal }) },
  ];
}

function events(proposal, { phase = "final_answer", failed = null } = {}) {
  let sequence_number = 0;
  const stamp = (event) => ({ sequence_number: sequence_number++, ...event });
  const item = { id: "assistant_item", type: "message", role: "assistant", phase,
    status: "completed", content: [{ type: "output_text", text: proposal, annotations: [] }] };
  const rows = [
    stamp({ type: "response.created", response: { id: "response_original", status: "in_progress" } }),
    stamp({ type: "response.output_item.added", output_index: 0, item: { ...item, status: "in_progress", content: [] } }),
    stamp({ type: "response.content_part.added", item_id: item.id, output_index: 0, content_index: 0,
      part: { type: "output_text", text: "", annotations: [] } }),
  ];
  for (const delta of [proposal.slice(0, 10), proposal.slice(10, 39), proposal.slice(39)]) {
    rows.push(stamp({ type: "response.output_text.delta", item_id: item.id, output_index: 0, content_index: 0, delta }));
  }
  rows.push(stamp({ type: "response.output_text.done", item_id: item.id, output_index: 0, content_index: 0, text: proposal }));
  rows.push(stamp({ type: "response.content_part.done", item_id: item.id, output_index: 0, content_index: 0, part: item.content[0] }));
  rows.push(stamp({ type: "response.output_item.done", output_index: 0, item }));
  rows.push(stamp({ type: failed || "response.completed", response: { id: "response_original",
    status: failed ? "failed" : "completed", output: [item], model: "original-model", service_tier: "default",
    usage: { input_tokens: 16000, output_tokens: 90, output_tokens_details: { reasoning_tokens: 25 } } } }));
  return rows;
}

function replay(rows, projector) {
  return rows.flatMap((row) => [projector(row)].flat().filter(Boolean));
}

function batchResult() {
  const members = [2, 1].map((id) => {
    const resultText = JSON.stringify({ records: [{ id, answer: text }] }, null, 2);
    const request = String(id).repeat(64), terminal = String(id + 2).repeat(64);
    return { status: "RESULT_AVAILABLE", cleanup_pass: true, first_typed_blocker: null,
      request_id: `tura_embedded_${request}`, request_sha256: request, terminal_sha256: terminal,
      native_thread_id: thread, result_text: resultText, result_truncated: false,
      evidence_status: "EVIDENCE_VERIFIED", evidence_first_blocker: null,
      evidence_counts: { file_changes: 0, failed: 0, index_complete: true },
      delivery_binding: { schema_version: "nokiy_batch_delivery_binding_v1",
        terminal_sha256: terminal, result_sha256: digest(resultText), result_bytes: Buffer.byteLength(resultText) } };
  });
  return { schema_version: "nokiy_direct_batch_summary_v2", status: "RESULT_AVAILABLE",
    first_typed_blocker: null, continuation_owner: "codex", mission_acceptance: "parent_owned", members };
}

test("batch: complete bound records merge only after explicit parent acceptance", () => {
  const terminal = batchResult(), before = structuredClone(terminal);
  const { body, options } = request("function", terminal, host, true);
  const projection = nativeToolProjection(body, options), context = projection.resultDelivery;
  assert.ok(context);
  assert.equal(context.composition, "json_records_by_id");
  assert.deepEqual(JSON.parse(context.text), { records: [{ id: 1, answer: text }, { id: 2, answer: text }] });
  assert.deepEqual(terminal, before);
  assert.equal(projection.body.instructions, body.instructions, "prompt prefix stays cacheable");
  assert.match(deliveryNote(projection.body), /accept every member's semantic result/);
  assert.equal(materializeResultDelivery(acceptance(context), context), context.text);
  assert.throws(() => materializeResultDelivery(acceptance(context, [{ old: "records", new: "other" }]), context));
  const delivered = replay(events(acceptance(context)), resultDeliveryProjector(context)).at(-1).response;
  assert.equal(delivered.output[0].content[0].text, context.text);
  assert.equal(delivered.usage.output_tokens, 90);
  const failed = replay(events(acceptance(context), { failed: "response.failed" }), resultDeliveryProjector(context));
  assert.equal(failed.at(-1).type, "response.failed");
  assert.ok(!failed.some((event) => event.type === "response.output_text.delta"));
});

test("batch: v2 binds authenticated non-attempt counts without hiding failed history", () => {
  const terminal = batchResult(), member = terminal.members[0];
  member.evidence_counts.failed = 1;
  member.delivery_binding.schema_version = "nokiy_batch_delivery_binding_v2";
  member.delivery_binding.not_attempted_count = 1;
  const before = structuredClone(terminal);
  const { body, options } = request("function", terminal, host, true);
  assert.ok(nativeToolProjection(body, options).resultDelivery);
  assert.deepEqual(terminal, before);
  for (const count of [undefined, null, true, 0, -1, 1.5, 2, "1"]) {
    const changed = structuredClone(terminal);
    changed.members[0].delivery_binding.not_attempted_count = count;
    assert.equal(project("function", changed).resultDelivery, null);
  }
  for (const version of ["nokiy_batch_delivery_binding_v1", "unknown"]) {
    const changed = structuredClone(terminal);
    changed.members[0].delivery_binding.schema_version = version;
    assert.equal(project("function", changed).resultDelivery, null);
  }
  for (const mutate of [r => { r.members[0].evidence_counts.failed = 0; },
    r => { r.members[0].evidence_counts.file_changes = 1; },
    r => { r.members[0].evidence_status = "INCOMPLETE_EVIDENCE"; },
    r => { r.members[0].evidence_first_blocker = "COMMAND_FAILURE"; }]) {
    const changed = structuredClone(terminal); mutate(changed);
    assert.equal(project("function", changed).resultDelivery, null);
  }
});

test("batch: v3 binds searches without matches, with any non-attempts, to the failed count", () => {
  for (const [failed, notAttempted, noMatch] of [[1, 0, 1], [3, 1, 2]]) {
    const terminal = batchResult(), member = terminal.members[0];
    member.evidence_counts.failed = failed;
    Object.assign(member.delivery_binding, { schema_version: "nokiy_batch_delivery_binding_v3",
      not_attempted_count: notAttempted, no_match_count: noMatch });
    const { body, options } = request("function", terminal, host, true);
    assert.ok(nativeToolProjection(body, options).resultDelivery);
  }
  const valid = batchResult();
  valid.members[0].evidence_counts.failed = 2;
  Object.assign(valid.members[0].delivery_binding, { schema_version: "nokiy_batch_delivery_binding_v3",
    not_attempted_count: 1, no_match_count: 1 });
  for (const [key, value] of [["no_match_count", 0], ["no_match_count", 2], ["no_match_count", true],
    ["no_match_count", undefined], ["not_attempted_count", -1], ["not_attempted_count", undefined],
    ["not_attempted_count", 1.5], ["schema_version", "nokiy_batch_delivery_binding_v2"]]) {
    const changed = structuredClone(valid);
    changed.members[0].delivery_binding[key] = value;
    assert.equal(project("function", changed).resultDelivery, null);
  }
  const zero = structuredClone(valid);
  zero.members[0].evidence_counts.failed = 0;
  assert.equal(project("function", zero).resultDelivery, null);
});

test("batch: exact native plan/summary framing works in function and code-mode outputs", () => {
  const terminal = batchResult();
  const framed = JSON.stringify({ plan_path: "/plan.json", plan_sha256: "d".repeat(64) }) + "\n" + JSON.stringify(terminal);
  for (const type of ["function", "custom"]) {
    const { body, options } = request(type, terminal, host, true);
    const outputs = [framed, [{ type: "input_text", text: "Script completed\nWall time 0.2 seconds\nOutput:\n" },
      { type: "input_text", text: JSON.stringify({ terminal: true, exit_code: 0, output: framed }) }]];
    for (const output of outputs) {
      body.input[1].output = output;
      assert.ok(nativeToolProjection(body, options).resultDelivery);
    }
    for (const output of ["log\n" + framed, framed + "\nlog", framed.replace('"plan_sha256"', '"unknown"')]) {
      body.input[1].output = output;
      assert.equal(nativeToolProjection(body, options).resultDelivery, null);
    }
    body.input[1].output = framed;
    body.input[1].call_id = "another-call";
    assert.equal(nativeToolProjection(body, options).resultDelivery, null);
  }
});

test("batch: old, changed, uncertain, duplicate or ambiguous results remain ordinary output", () => {
  const mutations = [
    (r) => { r.status = "BLOCKED"; }, (r) => { r.first_typed_blocker = "blocked"; },
    (r) => { r.continuation_owner = "other"; }, (r) => { r.mission_acceptance = "worker"; },
    (r) => { r.members = []; }, (r) => { r.members.push(r.members[0]); },
    (r) => { delete r.members[0].delivery_binding; },
    (r) => { r.members[0].result_text += "tampered"; },
    (r) => { r.members[0].result_truncated = true; },
    (r) => { r.members[0].evidence_counts.file_changes = 1; },
    (r) => { r.members[0].evidence_counts.failed = 1; },
    (r) => { r.members[0].evidence_counts.index_complete = false; },
    (r) => { r.members[0].native_thread_id = "22345678-1234-1234-1234-123456789abc"; },
    (r) => { r.members[0].cleanup_pass = false; },
    (r) => { r.members[0].delivery_binding.terminal_sha256 = "f".repeat(64); },
    (r) => { r.members[0].delivery_binding.result_bytes++; },
  ];
  for (const mutate of mutations) {
    const terminal = batchResult(); mutate(terminal);
    assert.equal(project("function", terminal).resultDelivery, null);
  }
  for (const bad of ['{"records":[{"id":1,"answer":"duplicate id"}]}',
    '{"records":[{"id":3,"id":4}]}', '{"records":[{"id":9007199254740993}]}',
    '{"records":[{"id":3,"value":1.0}]}', '{"other":[{"id":3}]}',
    '{"records":[],"extra":true}', '{"records":[]}', '{"records":[{"id":"3"}]}']) {
    const terminal = batchResult(), member = terminal.members[0];
    member.result_text = bad;
    member.delivery_binding.result_bytes = Buffer.byteLength(bad);
    member.delivery_binding.result_sha256 = digest(bad);
    assert.equal(project("function", terminal).resultDelivery, null, bad);
  }
  const tiny = batchResult();
  for (const [i, member] of tiny.members.entries()) {
    member.result_text = JSON.stringify({ records: [{ id: i + 1 }] });
    member.delivery_binding.result_bytes = Buffer.byteLength(member.result_text);
    member.delivery_binding.result_sha256 = digest(member.result_text);
  }
  assert.equal(project("function", tiny).resultDelivery, null);
});

for (const type of ["function", "custom"]) {
  test(`${type}: opt-in requires byte-exact admitted host dispatch and paired successful output`, () => {
    const { body, options } = request(type);
    const before = structuredClone(body);
    const projection = nativeToolProjection(body, options);
    assert.ok(projection.resultDelivery);
    assert.equal(projection.resultDelivery.text, text);
    assert.equal(projection.body.instructions, body.instructions, "prompt prefix stays cacheable");
    assert.deepEqual(projection.body.input.slice(0, -1), body.input, "transport is appended after the history");
    assert.match(deliveryNote(projection.body), /AFTER your own semantic review/);
    assert.match(deliveryNote(projection.body), /without another tool\/model call/);
    assert.deepEqual(body, before);
    assert.equal(nativeToolProjection(body, { ...options, host: { ...host, parent_result_delivery: undefined } }).resultDelivery, undefined);
    const field = type === "function" ? "arguments" : "input";
    body.input[0][field] = body.input[0][field].replace("nonexistent/python", "other/python");
    assert.equal(nativeToolProjection(body, options).resultDelivery, null);
  });

  test(`${type}: same-call code-mode terminal wrapper is accepted, logs and uncertain output are not`, () => {
    const { body, options } = request(type);
    body.input[1].output = JSON.stringify({ terminal: true, exit_code: 0,
      output: body.input[1].output, output_truncated: false, recovery_session_id: 123 });
    assert.ok(nativeToolProjection(body, options).resultDelivery);
    const wrapped = JSON.parse(body.input[1].output);
    for (const mutation of [{ session_id: 123 }, { terminal: false }, { exit_code: 1 }, { truncated: true }]) {
      body.input[1].output = JSON.stringify({ ...wrapped, ...mutation });
      assert.equal(nativeToolProjection(body, options).resultDelivery, null);
    }
    body.input[1].output = `ordinary log\n${wrapped.output}`;
    assert.equal(nativeToolProjection(body, options).resultDelivery, null);
    body.input[1].output = [{ type: "text", text: wrapped.output }];
    assert.ok(nativeToolProjection(body, options).resultDelivery);
  });
}

test("verified hydrated answer is used without reading its artifact or trusting truncated preview", () => {
  const terminal = result();
  terminal.result_truncated = true;
  terminal.result_text = text.slice(0, 10);
  terminal.result_inspection.parent_review_answer = {
    schema_version: "nokiy_parent_review_answer_v1", parent_acceptance_required: true,
    terminal_content_sha256: "d".repeat(64), artifact: terminal.result_artifact, text,
  };
  assert.equal(project("function", terminal).resultDelivery.text, text);
  terminal.result_inspection.parent_review_answer.text += "tampered";
  assert.equal(project("function", terminal).resultDelivery, null);
});

test("omitting the duplicate preview preserves complete verified delivery", () => {
  const terminal = result();
  terminal.result_truncated = true;
  terminal.result_text = "";
  terminal.result_text_omission = "VERIFIED_PARENT_REVIEW_ANSWER";
  terminal.result_inspection.parent_review_answer = {
    schema_version: "nokiy_parent_review_answer_v1", parent_acceptance_required: true,
    terminal_content_sha256: "d".repeat(64), artifact: terminal.result_artifact, text,
  };
  for (const type of ["function", "custom"]) {
    assert.equal(project(type, terminal).resultDelivery.text, text);
  }
  delete terminal.result_inspection.parent_review_answer;
  assert.equal(project("function", terminal).resultDelivery, null);
});

test("native code-mode content blocks preserve exact terminal validation", () => {
  const { body, options } = request("custom");
  body.input[1].output = nativeCodeOutput();
  assert.equal(nativeToolProjection(body, options).resultDelivery.text, text);
  for (const mutate of [
    (parts) => { parts[0].text = "Script running with cell ID 1"; },
    (parts) => { parts[0].text += "ordinary log\n"; },
    (parts) => { parts.push({ type: "input_text", text: "extra output" }); },
    (parts) => { parts[1].type = "image"; },
    (parts) => { parts[1].text = `unstructured log\n${parts[1].text}`; },
    (parts) => { const value = JSON.parse(parts[1].text); value.session_id = 1; parts[1].text = JSON.stringify(value); },
    (parts) => { const value = JSON.parse(parts[1].text); value.terminal = false; parts[1].text = JSON.stringify(value); },
    (parts) => { const value = JSON.parse(parts[1].text); value.output_truncated = true; parts[1].text = JSON.stringify(value); },
  ]) {
    body.input[1].output = nativeCodeOutput(); mutate(body.input[1].output);
    assert.equal(nativeToolProjection(body, options).resultDelivery, null, mutate.toString());
  }
});

test("native code-mode object terminals keep exact terminal validation", () => {
  const { body, options } = request("custom");
  body.input[1].output = nativeObjectOutput();
  assert.equal(nativeToolProjection(body, options).resultDelivery.text, text);
  for (const mutate of [
    (value) => { value.session_id = 1; },
    (value) => { value.terminal = false; },
    (value) => { value.output_truncated = true; },
    (value) => { value.exit_code = 1; },
    (value) => { delete value.recovery_session_id; },
    (value) => { value.output.status = "BLOCKED"; },
  ]) {
    const parts = nativeObjectOutput();
    const value = JSON.parse(parts[1].text);
    mutate(value);
    parts[1].text = JSON.stringify(value);
    body.input[1].output = parts;
    assert.equal(nativeToolProjection(body, options).resultDelivery, null, mutate.toString());
  }
});

test("delta-only output needs the owner's exact previous call and never changes provider input", () => {
  const { body, options } = request("custom");
  const [call, output] = body.input;
  const delta = { ...body, input: [output], previous_response_id: "previous_response" };
  const before = structuredClone(delta);
  const projection = nativeToolProjection(delta, { ...options, continuationCall: call });
  assert.equal(projection.resultDelivery.text, text);
  assert.deepEqual(delta, before);
  assert.deepEqual(projection.body.input.slice(0, -1), [output], "do not resend the omitted tool call");
  assert.equal(nativeToolProjection(delta, options).resultDelivery, null);
  assert.equal(nativeToolProjection({ ...delta, previous_response_id: null }, { ...options, continuationCall: call }).resultDelivery, null);
  assert.equal(nativeToolProjection(delta, { ...options, continuationCall: { ...call, call_id: "foreign" } }).resultDelivery, null);
  assert.equal(nativeToolProjection({ ...delta, input: [output, output] }, { ...options, continuationCall: call }).resultDelivery, null);
  assert.equal(nativeToolProjection({ ...delta, input: [output, { role: "user", content: "new task" }] },
    { ...options, continuationCall: call }).resultDelivery, null);
});

test("native yielded result follows only the original cell and exact wait call", () => {
  const { body, options } = request("custom");
  const dispatch = body.input[0];
  const running = { ...body, previous_response_id: "dispatch_response", input: [{
    type: "custom_tool_call_output", call_id: dispatch.call_id,
    output: "Script running with cell ID 7\nWall time 0.0 seconds\nOutput:\n",
  }] };
  const projection = nativeToolProjection(running, { ...options, continuationCall: dispatch });
  assert.equal(projection.resultDelivery, null);
  assert.equal(projection.pendingResultDelivery.cellId, "7");
  const wait = { type: "function_call", name: "wait", namespace: "functions", id: "wait_item", call_id: "wait_call",
    arguments: JSON.stringify({ cell_id: "7", yield_time_ms: 10000 }) };
  const pending = bindPendingResultWait(projection.pendingResultDelivery, wait);
  assert.ok(pending);
  for (const patch of [{ name: "exec_command" }, { namespace: "remote" },
    { arguments: '{"cell_id":"8"}' }, { arguments: '{"cell_id":"7","terminate":true}' }]) {
    assert.equal(bindPendingResultWait(projection.pendingResultDelivery, { ...wait, ...patch }), null);
  }
  const complete = { ...body, previous_response_id: "wait_response", input: [{
    type: "function_call_output", call_id: wait.call_id, output: nativeCodeOutput(),
  }] };
  const before = structuredClone(complete);
  const resumed = nativeToolProjection(complete, { ...options, continuationCall: wait, continuationPendingDelivery: pending });
  assert.equal(resumed.resultDelivery.text, text);
  assert.equal(resumed.pendingResultDelivery, null);
  assert.deepEqual(resumed.body.input.slice(0, -1), complete.input);
  assert.deepEqual(complete, before);
  assert.equal(nativeToolProjection(complete, { ...options, continuationCall: wait }).resultDelivery, null);
  assert.equal(nativeToolProjection(complete, { ...options, continuationCall: { ...wait, call_id: "foreign" }, continuationPendingDelivery: pending }).resultDelivery, null);
  const repeated = { ...complete, input: [{ ...complete.input[0], output: running.input[0].output }] };
  assert.equal(nativeToolProjection(repeated, { ...options, continuationCall: wait, continuationPendingDelivery: pending }).pendingResultDelivery.cellId, "7");
  repeated.input[0].output = repeated.input[0].output.replace("ID 7", "ID 8");
  assert.equal(nativeToolProjection(repeated, { ...options, continuationCall: wait, continuationPendingDelivery: pending }).pendingResultDelivery, null);
});

test("full in-request wait history retains the dispatch without a continuation registry", () => {
  const { body, options } = request("custom");
  const dispatch = body.input[0];
  const yielded = { type: 'custom_tool_call_output', call_id: dispatch.call_id,
    output: 'Script running with cell ID 7\nWall time 0.0 seconds\nOutput:\n' };
  const commentary = { type: 'message', role: 'assistant', phase: 'commentary', status: 'completed',
    content: [{ type: 'output_text', text: 'Waiting for the original worker.', annotations: [] }] };
  const wait = { type: 'function_call', namespace: 'functions', name: 'wait', id: 'full_wait',
    call_id: 'full_wait_call', arguments: '{"cell_id":"7","yield_time_ms":10000}' };
  const completed = { type: 'function_call_output', call_id: wait.call_id, output: nativeCodeOutput() };
  const complete = { ...body, input: [dispatch, yielded, commentary, wait, completed] };
  const before = structuredClone(complete);
  assert.equal(nativeToolProjection(complete, options).resultDelivery?.text, text);
  assert.deepEqual(complete, before);
  for (const mutate of [
    input => { input[3].arguments = '{"cell_id":"8"}'; },
    input => { input[3].arguments = '{"cell_id":"7","terminate":true}'; },
    input => { input[3].namespace = 'remote'; },
    input => { input[4].call_id = 'foreign'; },
    input => { input[1].output = input[1].output.replace('ID 7', 'ID 8'); },
    input => { input[0].input = input[0].input.replace('nonexistent/python', 'foreign/python'); },
    input => { input.splice(2, 0, { type: 'message', role: 'user', content: 'New assignment' }); },
    input => { input.splice(2, 0, { type: 'function_call', namespace: 'functions', name: 'other',
      call_id: 'other_call', arguments: '{}' }, { type: 'function_call_output', call_id: 'other_call', output: '{}' }); },
    input => { input[2].phase = 'final_answer'; },
  ]) {
    const variant = structuredClone(complete); mutate(variant.input);
    assert.equal(nativeToolProjection(variant, options).resultDelivery, null, mutate.toString());
  }
  const priorWait = { ...wait, id: 'first_wait', call_id: 'first_wait_call' };
  const priorYield = { type: 'function_call_output', call_id: priorWait.call_id, output: yielded.output };
  const repeated = { ...complete, input: [dispatch, yielded, commentary, priorWait, priorYield, wait, completed] };
  assert.equal(nativeToolProjection(repeated, options).resultDelivery.text, text);
  const running = { ...repeated, input: [...repeated.input.slice(0, -1), { ...completed, output: yielded.output }] };
  assert.equal(nativeToolProjection(running, options).pendingResultDelivery.cellId, '7');
  repeated.input[3].call_id = wait.call_id; repeated.input[4].call_id = wait.call_id;
  assert.equal(nativeToolProjection(repeated, options).resultDelivery, null);
});

test("continuation ownership permits commentary but rejects competing calls and final answers", () => {
  const call = { type: 'function_call', name: 'wait', namespace: 'functions', call_id: 'wait_call', arguments: '{"cell_id":"7"}' };
  const commentary = { type: 'message', role: 'assistant', phase: 'commentary', status: 'completed',
    content: [{ type: 'output_text', text: 'Progress', annotations: [] }] };
  assert.equal(resultContinuationCall([commentary, { type: 'reasoning' }, call]), call);
  for (const output of [
    [commentary], [call, call], [call, { ...commentary, phase: 'final_answer' }],
    [call, { ...commentary, role: 'user' }], [call, { ...commentary, status: 'in_progress' }],
    [call, { ...commentary, content: [{ type: 'function_call', name: 'hidden' }] }],
  ]) assert.equal(resultContinuationCall(output), null);
});

test("failed, foreign, incomplete, mutated, or misbound evidence retains the ordinary route", () => {
  const changes = [
    (r) => { r.cleanup_pass = false; }, (r) => { r.status = "BLOCKED"; },
    (r) => { r.first_typed_blocker = "UNKNOWN_EFFECT"; }, (r) => { r.fallback_used = true; },
    (r) => { r.runtime_exit_code = 1; }, (r) => { r.capability_gap = {}; },
    (r) => { r.result_text += "tampered"; }, (r) => { r.result_artifact.bytes++; },
    (r) => { r.result_artifact.sha256 = "f".repeat(64); }, (r) => { r.result_truncated = true; },
    (r) => { r.native_thread_id = "missing"; }, (r) => { r.request_id += "foreign"; },
    (r) => { r.terminal_sha256 = "f".repeat(64); },
    (r) => { r.result_inspection.status = "INCOMPLETE_EVIDENCE"; },
    (r) => { r.result_inspection.projection_partial = true; },
    (r) => { r.result_inspection.command_success = "unproven"; },
    (r) => { r.result_inspection.counts.file_changes = 1; },
    (r) => { r.result_inspection.counts.failed = 1; },
    (r) => { r.result_inspection.counts.index_complete = false; },
  ];
  for (const mutate of changes) {
    const terminal = result(); mutate(terminal);
    assert.equal(project("function", terminal).resultDelivery, null, mutate.toString());
  }
  for (const mutate of [
    (input) => { input.at(-1).call_id = "foreign"; },
    (input) => { input.at(-1).type = "message"; input.at(-1).role = "user"; },
    (input) => { input.push({ role: "user", content: "new task" }); },
    (input) => { input[0].name = "unrelated_tool"; },
    (input) => { input[0].namespace = "foreign"; },
    (input) => { input.shift(); },
  ]) {
    const { body, options } = request(); mutate(body.input);
    const projection = nativeToolProjection(body, options);
    assert.equal(projection.resultDelivery, null, mutate.toString());
    assert.equal(projection.body.instructions, body.instructions);
  }
});

test("acceptance performs exact nonoverlapping corrections, including Unicode, without rewriting other bytes", () => {
  const context = project().resultDelivery;
  assert.equal(materializeResultDelivery(acceptance(context), context), text);
  const edits = [{ old: "source-backed", new: "verified source-backed" },
    { old: "\u8b49\u64da", new: "\u8b49\u64da\u5b8c\u6574" }];
  assert.equal(materializeResultDelivery(acceptance(context, edits), context),
    text.replace("source-backed", "verified source-backed").replace("\u8b49\u64da", "\u8b49\u64da\u5b8c\u6574"));
  const foreign = project("function", { ...result(), native_thread_id: "87654321-1234-1234-1234-123456789abc" }).resultDelivery;
  assert.notEqual(context.reference, foreign.reference);
  assert.throws(() => materializeResultDelivery(acceptance(foreign), context), { code: "NOKIY_RESULT_DELIVERY_INVALID" });
});

test("pretty-printed compact JSON is accepted without exposing its control text", () => {
  const projection = project();
  const proposal = `\n${JSON.stringify(JSON.parse(acceptance(projection.resultDelivery)), null, 2)}\n`;
  const output = replay(events(proposal), nativeEventProjector(projection));
  assert.equal(output.at(-1).response.output[0].content[0].text, text);
  assert.equal(output.filter((event) => event.type === "response.output_text.delta").map((event) => event.delta).join(""), text);
});

test("missing, ambiguous, overlapping, chained, oversized, or extended acceptance fails closed", () => {
  const context = project().resultDelivery;
  const badEdits = [
    [{ old: "not present", new: "invented" }], [{ old: "e", new: "ambiguous" }],
    [{ old: "source-backed", new: "x" }, { old: "source-backed answer", new: "y" }],
    [{ old: "source-backed", new: "first-edit" }, { old: "first-edit", new: "second-edit" }],
    [{ old: "", new: "prefix" }], [{ old: "answer", new: "x".repeat(9000) }],
    [{ old: "answer", new: "changed", path: "/not/allowed" }],
    [{ old: "answer", new: "\ud800" }],
  ];
  for (const edits of badEdits) assert.throws(() => materializeResultDelivery(acceptance(context, edits), context),
    { code: "NOKIY_RESULT_DELIVERY_INVALID" });
  for (const proposal of [acceptance(context) + " trailing", acceptance(context).replace('"edits":[]', '"edits":[],"accept_all":true'),
    acceptance(context).replace("accept_v1", "accept_v2")]) {
    assert.throws(() => materializeResultDelivery(proposal, context), { code: "NOKIY_RESULT_DELIVERY_INVALID" });
  }
});

test("one completed stream expands all final text surfaces and preserves provider IDs, phase and usage", () => {
  const projection = project();
  const proposal = acceptance(projection.resultDelivery, [{ old: "source-backed", new: "verified source-backed" }]);
  const rows = events(proposal), snapshot = structuredClone(rows);
  const transform = nativeEventProjector(projection);
  const beforeTerminal = rows.slice(0, -1).flatMap((row) => [transform(row)].flat().filter(Boolean));
  assert.deepEqual(beforeTerminal, [rows[0]], "no accepted content is published before actual provider completion");
  const output = [...beforeTerminal, ...transform(rows.at(-1))];
  const expected = text.replace("source-backed", "verified source-backed");
  assert.equal(output.filter((e) => e.type === "response.output_text.delta").map((e) => e.delta).join(""), expected);
  assert.equal(output.find((e) => e.type === "response.output_text.done").text, expected);
  assert.equal(output.find((e) => e.type === "response.content_part.done").part.text, expected);
  assert.equal(output.find((e) => e.type === "response.output_item.done").item.content[0].text, expected);
  const complete = output.at(-1);
  assert.equal(complete.response.output[0].content[0].text, expected);
  assert.equal(complete.response.id, "response_original");
  assert.equal(complete.response.output[0].id, "assistant_item");
  assert.equal(complete.response.output[0].phase, "final_answer");
  assert.deepEqual(complete.response.usage, rows.at(-1).response.usage);
  assert.equal(complete.response.service_tier, rows.at(-1).response.service_tier);
  assert.deepEqual(rows, snapshot);
  assert.ok(output.every((row, i) => i === 0 || row.sequence_number > output[i - 1].sequence_number));
});

test("SSE handles byte-split Unicode and event arrays; WS uses the identical event projector", () => {
  const projection = project();
  const proposal = acceptance(projection.resultDelivery, [{ old: "\u8b49\u64da", new: "\u8b49\u64da\u5b8c\u6574" }]);
  const rows = events(proposal);
  const expected = replay(rows, nativeEventProjector(projection));
  const raw = Buffer.from(rows.map((row) => `data: ${JSON.stringify(row)}\r\n\r\n`).join("") + "data: [DONE]\r\n\r\n");
  const sse = nativeSseProjector(nativeEventProjector(projection));
  let wire = "";
  for (let offset = 0; offset < raw.length; offset++) wire += sse.push(raw.subarray(offset, offset + 1));
  wire += sse.end();
  const decoded = wire.trim().split("\n\n").filter((frame) => frame !== "data: [DONE]")
    .map((frame) => JSON.parse(frame.slice(6)));
  assert.deepEqual(decoded, expected);
  assert.ok(wire.endsWith("data: [DONE]\n\n"));
});

test("normal text, commentary, unenabled routing and JSON nonstream replies retain their contracts", () => {
  const projection = project();
  for (const rows of [events("ordinary final answer"), events(acceptance(projection.resultDelivery), { phase: "commentary" })]) {
    assert.deepEqual(replay(rows, nativeEventProjector(projection)), rows);
  }
  const rows = events("ordinary final answer");
  const transform = nativeEventProjector(projection);
  const early = rows.slice(0, 4).flatMap((event) => [transform(event)].flat().filter(Boolean));
  assert.deepEqual(early, rows.slice(0, 4), "ordinary prose is not delayed until completion");
  const compact = events(acceptance(projection.resultDelivery));
  assert.deepEqual(replay(compact, resultDeliveryProjector(null)), compact);
  const original = { response: compact.at(-1).response };
  const converted = nativeEventProjector(projection)(original);
  assert.equal(converted.response.output[0].content[0].text, text);
  assert.deepEqual(converted.response.usage, original.response.usage);
});

test("provider failure/incomplete never expands or fabricates completion", () => {
  const projection = project();
  for (const failed of ["response.failed", "response.incomplete"]) {
    const rows = events(acceptance(projection.resultDelivery), { failed });
    assert.deepEqual(replay(rows, nativeEventProjector(projection)), [rows[0], rows.at(-1)]);
  }
});

test("mismatched stream/final, extra effects, foreign references and annotated markers never materialize", () => {
  const projection = project();
  const changes = [
    (rows) => { rows.find((e) => e.type === "response.output_text.done").text += "tamper"; },
    (rows) => { rows.find((e) => e.type === "response.content_part.done").content_index = 1; },
    (rows) => { rows.at(-1).response.output[0] = { ...rows.at(-1).response.output[0], id: "foreign" }; },
    (rows) => { rows.at(-1).response.output.push({ type: "function_call", name: "side_effect" }); },
    (rows) => { rows.at(-1).response.output[0].content[0].annotations = [{ type: "unsupported" }]; },
    (rows) => { rows.splice(-1, 0, { type: "response.output_item.done", item: {
      id: "extra_call", type: "function_call", name: "side_effect" } }); },
  ];
  for (const mutate of changes) {
    const rows = events(acceptance(projection.resultDelivery)); mutate(rows);
    assert.throws(() => replay(rows, nativeEventProjector(projection)), { code: "NOKIY_RESULT_DELIVERY_INVALID" });
  }
});

test("no full input pair means no implicit history lookup or registry", () => {
  assert.equal(prepareResultDelivery({ previous_response_id: "old", input: [{ type: "function_call_output",
    call_id: "native_call", output: JSON.stringify(result()) }] }, () => true), null);
  const { body } = request();
  body.generate = false;
  assert.equal(prepareResultDelivery(body, () => true), null);
});

test("short answers do not acquire a larger acceptance prompt or control envelope", () => {
  const { body, options } = request("function", result("Short accepted answer."));
  const projection = nativeToolProjection(body, options);
  assert.equal(projection.resultDelivery, null);
  assert.equal(projection.body.instructions, body.instructions);
});

async function loopback(t, upstreamEvents, hostOptions = {}) {
  const root = await mkdtemp(join(tmpdir(), "nokiy-delivery-loopback-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const script = join(root, "host.py");
  await writeFile(script, "# Not executed; loopback fixture only.\n");
  const descriptor = { ...host, ...hostOptions, python: process.execPath, script,
    sha256: digest("# Not executed; loopback fixture only.\n") };
  const upstream = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const rows = upstreamEvents(JSON.parse(Buffer.concat(chunks).toString("utf8")));
    res.writeHead(200, { "content-type": "text/event-stream" });
    for (const row of rows) res.write(`data: ${JSON.stringify(row)}\n\n`);
    res.end("data: [DONE]\n\n");
  });
  const wss = new WebSocketServer({ server: upstream });
  wss.on("connection", (socket) => socket.on("message", (bytes) => {
    for (const event of upstreamEvents(JSON.parse(bytes.toString()))) socket.send(JSON.stringify(event));
  }));
  await new Promise((resolve) => upstream.listen(0, "127.0.0.1", resolve));
  t.after(async () => {
    for (const client of wss.clients) client.terminate();
    await new Promise((resolve) => wss.close(resolve));
    await new Promise((resolve) => upstream.close(resolve));
  });
  const fakeWorker = { close() {}, async closeSession() {} };
  const profiles = [{ slug: "nokiy", display_name: "Offline serial fixture", workflow: "nokiy",
    coordinator: { model: "gpt-6-astra", reasoning_effort: "max" },
    worker: { model: "gpt-6.1-sol", reasoning_effort: "max", service_tier: "default" } }];
  const board = await createSwitchboardServer({
    runtime: { installation_capability: "offline-result-delivery-abcdefghijklmnop" },
    admission: { status: "ADMITTED", external_batch_admitted: true,
      official_models: ["gpt-6-astra", "gpt-6.1-sol"],
      protocol_compatibility: { external_model_orchestration: "responses-tool-loop-v2" },
      policy: { external_models: "allow", effect_scope: "local_inference_routing_only" },
      authority_effect: "local_inference_routing_only", local_nokiy_direct: { admitted: true, host: descriptor } },
    workflowProfiles: profiles,
    modelCatalog: { models: ["gpt-6-astra", "gpt-6.1-sol"].map((slug) => ({ slug,
      supported_reasoning_levels: [{ effort: "max" }, { effort: "ultra" }] })) },
    xaiWorker: fakeWorker, agyWorker: fakeWorker, webWorker: fakeWorker, openrouterWorker: fakeWorker,
    skipFileGate: true, officialOrigin: `http://127.0.0.1:${upstream.address().port}`,
  });
  const address = await board.listen({ host: "127.0.0.1", port: 0 });
  t.after(() => board.close());
  const base = `127.0.0.1:${address.port}/offline-result-delivery-abcdefghijklmnop/backend-api/codex/v1/responses`;
  return { base, descriptor, profiles };
}

test("actual HTTP delivery accepts an exact dispatch emitted by a prior host generation", { timeout: 10000 }, async (t) => {
  const raw = { type: "custom_tool_call", name: "nokiy_execute", id: "old_generation_item",
    call_id: "old_generation_call", input: JSON.stringify(payload), status: "completed" };
  const old = await loopback(t, () => [
    { type: "response.output_item.done", output_index: 0, item: raw },
    { type: "response.completed", response: { id: "old_generation_response", status: "completed", output: [raw] } },
  ]);
  const { body } = request("custom");
  Object.assign(body, { model: "nokiy", reasoning: { effort: "max" }, stream: true,
    input: [{ type: "message", role: "user", content: "Complete the original bounded action." }] });
  const exchange = async (base, requestBody) => {
    const response = await fetch(`http://${base}`, { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify(requestBody) });
    assert.equal(response.status, 200);
    return (await response.text()).trim().split("\n\n").filter((frame) => frame !== "data: [DONE]")
      .map((frame) => JSON.parse(frame.slice(6))).at(-1).response;
  };
  const first = await exchange(old.base, body);
  const dispatch = first.output[0];
  assert.match(dispatch.input, new RegExp(old.descriptor.script.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  let calls = 0;
  const next = await loopback(t, (requestBody) => {
    calls++;
    const reference = transportText(requestBody).match(/available as reference ([a-f0-9]{64})/)?.[1];
    return events(reference ? acceptance({ reference }) : "MISSING_PRIOR_GENERATION_BINDING");
  });
  assert.notEqual(next.descriptor.script, old.descriptor.script);
  const delivered = await exchange(next.base, { ...body, input: [dispatch,
    { type: "custom_tool_call_output", call_id: dispatch.call_id, output: nativeCodeOutput() }] });
  assert.equal(delivered.output[0].content[0].text, text);
  assert.equal(calls, 1);
});

test("WebSocket commentary plus dispatch/wait keeps compact final delivery", { timeout: 10000 }, async (t) => {
  let calls = 0;
  const commentary = { id: 'commentary_item', type: 'message', role: 'assistant', phase: 'commentary',
    status: 'completed', content: [{ type: 'output_text', text: 'Waiting on the original action.', annotations: [] }] };
  const { base } = await loopback(t, (body) => {
    calls++;
    if (calls < 3) {
      const call = calls === 1
        ? { type: 'custom_tool_call', name: 'nokiy_execute', id: 'dispatch_item', call_id: 'dispatch_call',
          input: JSON.stringify(payload), status: 'completed' }
        : { type: 'function_call', name: 'wait', id: 'wait_item', call_id: 'wait_call',
          arguments: '{"cell_id":"7","yield_time_ms":10000}', status: 'completed' };
      return [
        { type: 'response.output_item.done', item: commentary },
        { type: 'response.output_item.done', item: call },
        { type: 'response.completed', response: { id: 'chain_' + calls, status: 'completed', output: [commentary, call] } },
      ];
    }
    const reference = transportText(body).match(/available as reference ([a-f0-9]{64})/)?.[1];
    return events(reference ? acceptance({ reference }) : 'MISSING_DISPATCH_WAIT_LINEAGE');
  }, { coordinator_mode: 'dispatch_only_v1' });
  const socket = new WebSocket(`ws://${base}`);
  t.after(() => socket.terminate());
  await new Promise((resolve, reject) => { socket.once('open', resolve); socket.once('error', reject); });
  const exchange = (body) => new Promise((resolve, reject) => {
    const receive = (bytes) => {
      const event = JSON.parse(bytes.toString());
      if (event.type === 'error' || event.type === 'response.completed') {
        socket.off('message', receive);
        if (event.type === 'error') reject(new Error(JSON.stringify(event)));
        else resolve(event.response);
      }
    };
    socket.on('message', receive);
    socket.send(JSON.stringify({ ...body, type: 'response.create' }));
  });
  const { body } = request('custom');
  body.tools[0].tools.push({ type: 'function', name: 'wait', parameters: { type: 'object' } });
  Object.assign(body, { model: 'nokiy', reasoning: { effort: 'max' }, input: [] });
  const dispatched = await exchange(body);
  const call = dispatched.output.find(item => item.type === 'custom_tool_call');
  const waited = await exchange({ ...body, previous_response_id: dispatched.id, input: [{
    type: 'custom_tool_call_output', call_id: call.call_id,
    output: 'Script running with cell ID 7\nWall time 0.0 seconds\nOutput:\n',
  }] });
  const wait = waited.output.find(item => item.type === 'function_call');
  const delivered = await exchange({ ...body, previous_response_id: waited.id, input: [{
    type: 'function_call_output', call_id: wait.call_id, output: nativeCodeOutput(),
  }] });
  assert.equal(delivered.output[0].content[0].text, text);
  assert.equal(delivered.usage.output_tokens, 90);
  assert.equal(calls, 3);
});

test("actual HTTP full-history completion recovers the wait lineage with one upstream request", { timeout: 10000 }, async (t) => {
  let calls = 0;
  const { base, descriptor } = await loopback(t, body => {
    calls++;
    const reference = transportText(body).match(/available as reference ([a-f0-9]{64})/)?.[1];
    return events(reference ? acceptance({ reference }) : 'MISSING_HTTP_HISTORY_LINEAGE');
  });
  const { body } = request('custom', result(), descriptor);
  const dispatch = body.input[0];
  const wait = { type: 'function_call', name: 'wait', namespace: 'functions', id: 'http_wait',
    call_id: 'http_wait_call', arguments: '{"cell_id":"7"}' };
  Object.assign(body, { model: 'nokiy', reasoning: { effort: 'max' }, stream: true, input: [dispatch,
    { type: 'custom_tool_call_output', call_id: dispatch.call_id,
      output: 'Script running with cell ID 7\nWall time 0.0 seconds\nOutput:\n' }, wait,
    { type: 'function_call_output', call_id: wait.call_id, output: nativeCodeOutput() }] });
  const response = await fetch(`http://${base}`, { method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body) });
  assert.equal(response.status, 200);
  const rows = (await response.text()).trim().split('\n\n').filter(frame => frame !== 'data: [DONE]')
    .map(frame => JSON.parse(frame.slice(6)));
  assert.equal(rows.at(-1).response.output[0].content[0].text, text);
  assert.equal(rows.at(-1).response.usage.output_tokens, 90);
  assert.equal(calls, 1);
});

test("actual HTTP/SSE and WebSocket server paths deliver the same expanded result with one upstream call each", { timeout: 10000 }, async (t) => {
  let calls = 0;
  const logs = [], write = process.stdout.write;
  process.stdout.write = function (chunk, ...args) {
    try { logs.push(JSON.parse(String(chunk))); } catch { /* Non-log test output. */ }
    return write.call(this, chunk, ...args);
  };
  t.after(() => { process.stdout.write = write; });
  const { base, descriptor } = await loopback(t, (body) => {
    calls++;
    assert.equal(body.model, "gpt-6-astra");
    const reference = transportText(body).match(/available as reference ([a-f0-9]{64})/)?.[1];
    assert.ok(reference);
    return events(acceptance({ reference }));
  });
  const { body } = request("custom", result(), descriptor);
  Object.assign(body, { model: "nokiy", reasoning: { effort: "max" }, stream: true });
  body.input[0].request_id = "untrusted_tool_request";
  const response = await fetch(`http://${base}`, { method: "POST",
    headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  assert.equal(response.status, 200, await response.clone().text());
  const rows = (await response.text()).trim().split("\n\n").filter((frame) => frame !== "data: [DONE]")
    .map((frame) => JSON.parse(frame.slice(6)));
  const socket = new WebSocket(`ws://${base}`);
  t.after(() => socket.terminate());
  await new Promise((resolve, reject) => { socket.once("open", resolve); socket.once("error", reject); });
  const socketRows = [];
  await new Promise((resolve, reject) => {
    socket.on("error", reject);
    socket.on("message", (data) => {
      const event = JSON.parse(data.toString()); socketRows.push(event);
      if (event.type === "error") reject(new Error(JSON.stringify(event)));
      if (event.type === "response.completed") resolve();
    });
    const wsBody = structuredClone(body);
    wsBody.input[0].call_id = wsBody.input[1].call_id = "ws_native_call";
    socket.send(JSON.stringify({ ...wsBody, type: "response.create" }));
  });
  assert.deepEqual(socketRows, rows);
  assert.equal(rows.at(-1).response.output[0].content[0].text, text);
  assert.equal(calls, 2, "no completion retry, secondary model call, or worker dispatch");
  for (const transport of ["http", "ws"]) {
    const log = logs.find(row => row.transport === transport && row.result_delivery?.outcome === "expanded");
    assert.ok(log, `${transport} must record actual expansion`);
    assert.deepEqual(log.result_delivery, { eligibility: "eligible", outcome: "expanded",
      source_call_id: transport === "http" ? "native_call" : "ws_native_call",
      source_call_type: "custom_tool_call", source_tool_name: "exec",
      dispatch_operation: "run_ref", applicability: "worker_run" });
    assert.equal(typeof log.request_id, "string");
    assert.notEqual(log.request_id, "untrusted_tool_request");
  }
});

test("delivery observation distinguishes ordinary generation from expansion without retaining content", () => {
  for (const compact of [false, true]) {
    const projection = project(), projector = nativeEventProjector(projection);
    assert.deepEqual(projector.resultDeliveryObservation(), { eligibility: "eligible", outcome: "unobserved" });
    replay(events(compact ? acceptance(projection.resultDelivery) : "ordinary answer"), projector);
    assert.deepEqual(projector.resultDeliveryObservation(), {
      eligibility: "eligible", outcome: compact ? "expanded" : "ordinary",
    });
    assert.equal(JSON.stringify(projector.resultDeliveryObservation()).includes(text), false);
  }
});

test("delivery observation never counts failed or invalid acceptance as expansion", () => {
  const projection = project();
  for (const failed of ["response.failed", "response.incomplete", "error"]) {
    const projector = nativeEventProjector(projection);
    replay(events(acceptance(projection.resultDelivery), { failed }), projector);
    assert.deepEqual(projector.resultDeliveryObservation(), { eligibility: "eligible", outcome: "provider_failed" });
  }
  const invalid = nativeEventProjector(projection);
  assert.throws(() => replay(events(acceptance({ reference: "0".repeat(64) })), invalid));
  assert.deepEqual(invalid.resultDeliveryObservation(), { eligibility: "eligible", outcome: "projection_failed" });
});

test("delivery observation distinguishes disabled, ineligible and eligible JSON completion", () => {
  const { body, options } = request();
  for (const [eligibility, candidate] of [
    ["disabled", nativeToolProjection(body, { ...options, host: { ...host, parent_result_delivery: undefined } })],
    ["ineligible", nativeToolProjection({ ...body, input: [] }, options)],
  ]) {
    const projector = nativeEventProjector(candidate);
    replay(events("ordinary answer"), projector);
    assert.deepEqual(projector.resultDeliveryObservation(), { eligibility, outcome: "unobserved",
      ...(eligibility === "ineligible" ? { reason: "NO_INPUT" } : {}) });
  }
  const projection = project();
  for (const compact of [true, false]) {
    const projector = nativeEventProjector(projection);
    const response = events(compact ? acceptance(projection.resultDelivery) : "ordinary").at(-1).response;
    projector({ response });
    assert.deepEqual(projector.resultDeliveryObservation(), {
      eligibility: "eligible", outcome: compact ? "expanded" : "ordinary",
    });
  }
});

test("delivery diagnostics identify the first failed gate without exposing payloads", () => {
  const original = request();
  const cases = [
    ["PREWARM", body => { body.generate = false; }],
    ["NO_INPUT", body => { body.input = []; }],
    ["CALL_OUTPUT_PAIR_UNAVAILABLE", body => { body.input.at(-1).call_id = "foreign"; }],
    ["NATIVE_DISPATCH_BINDING_MISMATCH", body => { body.input[0].arguments += " "; }],
    ["OUTPUT_FRAMING_UNRECOGNIZED", body => { body.input.at(-1).output = "sensitive invalid output"; }],
    ["TERMINAL_CONTRACT_REJECTED", body => { body.input.at(-1).output = JSON.stringify({ ...result(), cleanup_pass: false }); }],
    ["RECORDED_EFFECTS_OR_FAILURES", body => { const value = result(); value.result_inspection.counts.failed = 1;
      body.input.at(-1).output = JSON.stringify(value); }],
    ["ANSWER_INTEGRITY_REJECTED", body => { body.input.at(-1).output = JSON.stringify({ ...result(), result_text: "wrong" }); }],
    ["NO_DELIVERY_BYTE_BENEFIT", body => { body.input.at(-1).output = JSON.stringify(result("short")); }],
  ];
  for (const [reason, mutate] of cases) {
    const body = structuredClone(original.body); mutate(body);
    const before = structuredClone(body);
    const projection = nativeToolProjection(body, original.options);
    assert.equal(projection.resultDelivery, null, reason);
    const observation = nativeEventProjector(projection).resultDeliveryObservation();
    assert.deepEqual(observation, { eligibility: "ineligible", outcome: "unobserved", reason });
    assert.deepEqual(body, before);
    assert.deepEqual(projection.body.input, before.input);
    assert.equal(JSON.stringify(observation).includes("sensitive"), false);
  }
  const delta = { ...original.body, previous_response_id: "same_connection", input: [original.body.input.at(-1)] };
  const missing = nativeToolProjection(delta, original.options);
  assert.equal(missing.resultDeliveryRejection, "CALL_OUTPUT_PAIR_UNAVAILABLE");
  const bound = nativeToolProjection(delta, { ...original.options, continuationCall: original.body.input[0] });
  assert.ok(bound.resultDelivery);
  assert.equal(bound.resultDeliveryRejection, undefined);
});

test("source diagnostics bind direct worker operations to the actual pair and preserve delivery guards", () => {
  for (const type of ["function", "custom"]) for (const operation of ["run", "run_ref"]) {
    const { body, options } = request(type);
    const field = type === "function" ? "arguments" : "input";
    const projection = nativeToolProjection({ ...body, input: [] }, options);
    const original = { ...body.input[0], name: "nokiy_execute", call_id: "actual_source",
      [field]: JSON.stringify(operation === "run_ref" ? payload : { operation,
        draft: { prompt: "sensitive_prompt", credential: "sensitive_credential" }, request_id: "untrusted_tool_request" }) };
    const projectCall = item => nativeToolItem(item, projection);
    const source = projectCall(original);
    const output = { ...body.input[1], call_id: source.call_id };
    body.input = [{ ...source, call_id: "neighbor" }, { ...output, call_id: "neighbor" }, source, output];
    const before = structuredClone(body);
    const diagnostic = prepareResultDeliveryDiagnostic(body, body.input[0], projectCall);
    assert.deepEqual(diagnostic, { source_call_id: "actual_source", source_call_type: source.type,
      source_tool_name: source.name, dispatch_operation: operation, applicability: "worker_run" });
    assert.doesNotMatch(JSON.stringify(diagnostic), /sensitive|untrusted|arguments|input_path|cmd|prompt|credential/);
    assert.deepEqual(body, before);
    assert.deepEqual(prepareResultDeliveryDiagnostic({ ...body, input: [original, output] }),
      { ...diagnostic, source_tool_name: "nokiy_execute" });
    const context = nativeToolProjection(body, options).resultDelivery;
    assert.ok(context);
    assert.equal(materializeResultDelivery(acceptance(context), context), text);
    assert.throws(() => materializeResultDelivery(acceptance({ reference: "f".repeat(64) }), context),
      { code: "NOKIY_RESULT_DELIVERY_INVALID" });
    const delta = { ...body, previous_response_id: "same_connection", input: [output] };
    assert.deepEqual(prepareResultDeliveryDiagnostic(delta, source, projectCall), diagnostic);
    assert.equal(prepareResultDeliveryDiagnostic(delta, body.input[0], projectCall).source_call_id, null);
    const forged = structuredClone(body);
    forged.input.at(-2)[field] = type === "function"
      ? JSON.stringify({ cmd: "sensitive_command", unrelated: source[field] }) : `// sensitive_command\n${source[field]}`;
    assert.equal(prepareResultDeliveryDiagnostic(forged, null, projectCall).applicability, "unknown");
    assert.equal(prepareResultDeliveryDiagnostic(forged, null, projectCall).dispatch_operation, null);
    source[field] += " ";
    const rejected = nativeToolProjection(body, options);
    assert.equal(rejected.resultDelivery, null);
    assert.equal(rejected.resultDeliveryRejection, "NATIVE_DISPATCH_BINDING_MISMATCH");
  }
});

test("native commands and read_http are not worker deliveries, and never retain raw arguments", () => {
  for (const type of ["function", "custom"]) for (const kind of ["nokiy_native", "read_http", "wrapped_read_http"]) {
    const { body, options } = request(type);
    const field = type === "function" ? "arguments" : "input";
    const projection = nativeToolProjection({ ...body, input: [] }, options);
    const projectCall = item => nativeToolItem(item, projection);
    let source = { ...body.input[0], name: kind === "nokiy_native" ? kind : "nokiy_execute",
      [field]: JSON.stringify(kind === "nokiy_native" ? { cmd: "sensitive_command", credential: "sensitive_credential" }
        : { operation: "read_http", urls: ["https://sensitive_credential@example.invalid"], prompt: "sensitive_prompt" }) };
    if (kind === "wrapped_read_http") source = projectCall(source);
    body.input[0] = source;
    const diagnostic = prepareResultDeliveryDiagnostic(body, null, projectCall);
    assert.deepEqual(diagnostic, { source_call_id: source.call_id, source_call_type: source.type,
      source_tool_name: source.name, dispatch_operation: kind === "nokiy_native" ? null : "read_http",
      applicability: "not_applicable" });
    assert.doesNotMatch(JSON.stringify(diagnostic), /sensitive|urls|arguments|credential|prompt|cmd/);
    const rejected = nativeToolProjection(body, options);
    assert.equal(rejected.resultDelivery, null);
    assert.equal(rejected.resultDeliveryRejection, "NATIVE_DISPATCH_BINDING_MISMATCH");
  }
});

test("source diagnostics leave missing, invalid and unrecognized sources unknown without guessing", () => {
  const unknown = { source_call_id: null, source_call_type: null, source_tool_name: null,
    dispatch_operation: null, applicability: "unknown" };
  const { body } = request();
  for (const missing of [null, {}, { input: [] }, { ...body, input: [body.input[1]] },
    { ...body, input: [body.input[0], { ...body.input[1], call_id: "foreign" }] }]) {
    assert.deepEqual(prepareResultDeliveryDiagnostic(missing), unknown);
  }
  for (const raw of ["{", "x".repeat(192 * 1024 + 1), '{"operation":"unknown_secret"}',
    '{"operation":{"credential":"sensitive_credential"}}', '[]']) {
    const candidate = structuredClone(body);
    Object.assign(candidate.input[0], { name: "nokiy_execute", arguments: raw });
    const diagnostic = prepareResultDeliveryDiagnostic(candidate);
    assert.equal(diagnostic.applicability, "unknown");
    assert.equal(diagnostic.dispatch_operation, null);
    assert.doesNotMatch(JSON.stringify(diagnostic), /sensitive|secret|credential/);
  }
  for (const id of ["x".repeat(129), "invalid\nidentity"]) {
    const candidate = structuredClone(body);
    candidate.input[0].call_id = candidate.input[1].call_id = id;
    const diagnostic = prepareResultDeliveryDiagnostic(candidate);
    assert.equal(diagnostic.source_call_id, null, "never truncate distinct identities into one prefix");
    assert.equal(diagnostic.applicability, "unknown");
  }
  const candidate = structuredClone(body);
  candidate.input[0].name = "x".repeat(129);
  assert.equal(prepareResultDeliveryDiagnostic(candidate).source_tool_name, null);
  assert.equal(prepareResultDeliveryDiagnostic(candidate).applicability, "unknown");
});

test("coordinator delivery delegates missing corrections and forbids rewriting accepted content", () => {
  const { body, options } = request("custom", result(), { ...host, coordinator_mode: "dispatch_only_v1" });
  const projection = nativeToolProjection(body, options);
  assert.equal(projection.resultDelivery.coordinatorOnly, true);
  assert.match(deliveryNote(projection.body), /do not independently redo source review/);
  assert.match(deliveryNote(projection.body), /Artifact integrity alone is not semantic acceptance/);
  assert.doesNotMatch(deliveryNote(projection.body), /AFTER your own semantic review/);
  assert.equal(materializeResultDelivery(acceptance(projection.resultDelivery), projection.resultDelivery), text);
  assert.throws(() => materializeResultDelivery(acceptance(projection.resultDelivery,
    [{ old: "source-backed", new: "different" }]), projection.resultDelivery));
});

test("batch coordinator preserves coordination-only acceptance instead of regenerating records", () => {
  for (const type of ["custom", "function"]) {
    const { body, options } = request(type, batchResult(), { ...host, coordinator_mode: "dispatch_only_v1" }, true);
    const projection = nativeToolProjection(body, options), context = projection.resultDelivery;
    assert.equal(context.coordinatorOnly, true);
    assert.match(deliveryNote(projection.body), /Coordination-only batch/);
    assert.match(deliveryNote(projection.body), /do not independently redo source review or regenerate/);
    assert.match(deliveryNote(projection.body), /accept every member's semantic result and evidence/);
    assert.match(deliveryNote(projection.body), /delegate a concrete missing correction or verification/);
    assert.equal(materializeResultDelivery(acceptance(context), context), context.text);
    assert.throws(() => materializeResultDelivery(acceptance(context, [{ old: "records", new: "changed" }]), context));
    const ordinary = replay(events("Evidence missing; not accepted."), nativeEventProjector(projection));
    assert.equal(ordinary.at(-1).response.output[0].content[0].text, "Evidence missing; not accepted.");
  }
});

for (const transport of ["http", "ws"]) {
  test(`${transport}: actual coordinator server rejects a hidden shell call`, { timeout: 10000 }, async (t) => {
    let calls = 0;
    const { base } = await loopback(t, (body) => {
      calls++;
      assert.deepEqual(body.tools.map((tool) => tool.name), ["nokiy_execute"]);
      return [{ type: "response.output_item.done", item: { type: "custom_tool_call", name: "exec",
        namespace: "functions", id: "forbidden_item", call_id: "forbidden_call", input: "arbitrary code" } }];
    }, { coordinator_mode: "dispatch_only_v1" });
    const { body } = request("custom");
    Object.assign(body, { model: "nokiy", reasoning: { effort: "max" }, stream: true, input: [] });
    let responseText;
    if (transport === "http") {
      const response = await fetch(`http://${base}`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
      responseText = await response.text();
    } else {
      const socket = new WebSocket(`ws://${base}`);
      t.after(() => socket.terminate());
      await new Promise((resolve, reject) => { socket.once("open", resolve); socket.once("error", reject); });
      responseText = await new Promise((resolve, reject) => {
        socket.once("error", reject);
        socket.once("message", (data) => resolve(data.toString()));
        socket.send(JSON.stringify({ ...body, type: "response.create" }));
      });
    }
    assert.match(responseText, /NOKIY_COORDINATOR_TOOL_FORBIDDEN/);
    assert.match(responseText, /"reason":"call_not_advertised"/);
    assert.match(responseText, /"stage":"response.output_item.done"/);
    assert.doesNotMatch(responseText, /arbitrary code|forbidden_call/);
    assert.equal(calls, 1);
  });
}

test("HTTP and WS normalize the official unqualified wait shape without exposing raw tools", { timeout: 10000 }, async (t) => {
  let calls = 0;
  const item = { type: "function_call", name: "wait", id: "fc_unqualified_wait", call_id: "call_original_wait",
    arguments: '{"cell_id":"1","yield_time_ms":10000}', status: "completed" };
  const { base } = await loopback(t, (body) => {
    calls++;
    assert.deepEqual(body.tools.map((tool) => tool.name), ["functions", "nokiy_execute"]);
    assert.deepEqual(body.tools[0].tools.map((tool) => tool.name), ["wait"]);
    return [
      { type: "response.created", response: { id: "resp_wait", status: "in_progress" } },
      { type: "response.output_item.added", output_index: 0, item: { ...item, arguments: "", status: "in_progress" } },
      { type: "response.function_call_arguments.delta", item_id: item.id, delta: item.arguments },
      { type: "response.output_item.done", output_index: 0, item },
      { type: "response.completed", response: { id: "resp_wait", status: "completed", output: [item] } },
    ];
  }, { coordinator_mode: "dispatch_only_v1" });
  const { body } = request("custom");
  Object.assign(body, { model: "nokiy", reasoning: { effort: "max" }, stream: true, input: [] });
  body.tools[0].tools.push({ type: "function", name: "wait", parameters: { type: "object" } });
  const response = await fetch(`http://${base}`, { method: "POST",
    headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  assert.equal(response.status, 200);
  const rows = (await response.text()).trim().split("\n\n").filter((frame) => frame !== "data: [DONE]")
    .map((frame) => JSON.parse(frame.slice(6)));
  const socket = new WebSocket(`ws://${base}`);
  t.after(() => socket.terminate());
  await new Promise((resolve, reject) => { socket.once("open", resolve); socket.once("error", reject); });
  const wsRows = [];
  await new Promise((resolve, reject) => {
    socket.on("error", reject);
    socket.on("message", (bytes) => {
      const event = JSON.parse(bytes.toString()); wsRows.push(event);
      if (event.type === "error") reject(new Error(JSON.stringify(event)));
      if (event.type === "response.completed") resolve();
    });
    socket.send(JSON.stringify({ ...body, type: "response.create" }));
  });
  assert.deepEqual(wsRows, rows);
  assert.deepEqual(rows.find((event) => event.type === "response.output_item.done").item, { ...item, namespace: "functions" });
  assert.deepEqual(rows.at(-1).response.output, [{ ...item, namespace: "functions" }]);
  assert.equal(calls, 2);
});

for (const variant of ["valid", "wrong_call", "wrong_response", "changed_tools", "changed_model", "changed_profile",
  "new_user", "new_connection", "intervening_response", "multiple_calls"]) {
  test(`WebSocket native delta: ${variant}`, { timeout: 10000 }, async (t) => {
    const received = [];
    const { base, descriptor, profiles } = await loopback(t, (body) => {
      received.push(body);
      if (received.length > 1) {
        const reference = transportText(body).match(/available as reference ([a-f0-9]{64})/)?.[1];
        return events(reference ? acceptance({ reference }) : "ordinary answer");
      }
      const item = { type: "custom_tool_call", name: "nokiy_execute", id: "tool_item", call_id: "native_call",
        input: JSON.stringify(payload), status: "completed" };
      const output = variant === "multiple_calls" ? [item, { ...item, id: "second_item", call_id: "second_call" }] : [item];
      return [
        { type: "response.created", response: { id: "previous_response", status: "in_progress" } },
        ...output.flatMap((item, output_index) => [
          { type: "response.output_item.added", output_index, item: { ...item, status: "in_progress" } },
          { type: "response.output_item.done", output_index, item },
        ]),
        { type: "response.completed", response: { id: "previous_response", status: "completed", output } },
      ];
    });
    const open = async () => {
      const socket = new WebSocket(`ws://${base}`);
      t.after(() => socket.terminate());
      await new Promise((resolve, reject) => { socket.once("open", resolve); socket.once("error", reject); });
      return socket;
    };
    const send = (socket, body) => new Promise((resolve, reject) => {
      const done = (data) => {
        const event = JSON.parse(data.toString());
        if (!["response.completed", "error"].includes(event.type)) return;
        socket.off("message", done); socket.off("error", failed); resolve(event);
      };
      const failed = (error) => { socket.off("message", done); reject(error); };
      socket.once("error", failed); socket.on("message", done);
      socket.send(JSON.stringify({ ...body, type: "response.create" }));
    });
    let socket = await open();
    const { body } = request("custom", result(), descriptor);
    Object.assign(body, { model: "nokiy", reasoning: { effort: "max" }, stream: true, input: [] });
    const first = await send(socket, body);
    assert.equal(first.response.output[0].name, "exec");
    const delta = { ...body, previous_response_id: first.response.id,
      input: [{ type: "custom_tool_call_output", call_id: "native_call", output: nativeCodeOutput() }] };
    if (variant === "wrong_call") delta.input[0].call_id = "foreign";
    if (variant === "wrong_response") delta.previous_response_id = "foreign";
    if (variant === "changed_tools") delta.tools = [...body.tools, { type: "function", name: "other", parameters: {} }];
    if (variant === "changed_model") delta.model = "gpt-6-astra";
    if (variant === "changed_profile") profiles[0].worker.model = "gpt-6-astra";
    if (variant === "new_user") delta.input.push({ role: "user", content: "new task" });
    if (variant === "new_connection") { socket.terminate(); socket = await open(); }
    if (variant === "intervening_response") await send(socket, { ...body, input: [{ role: "user", content: "new task" }] });
    const final = await send(socket, delta);
    if (variant === "changed_profile") {
      assert.equal(final.type, "error");
      assert.match(JSON.stringify(final), /NOKIY_WORKFLOW_CHANGED_DURING_CONTINUATION/);
      assert.equal(received.length, 1, "changed profile is rejected before upstream inference");
    } else {
      assert.equal(final.type, "response.completed");
      assert.equal(final.response.output[0].content[0].text, variant === "valid" ? text : "ordinary answer");
      // Only the transport note is appended and, on the Nokiy route, each result summary takes its
      // fixed projection; the client transcript is never rebuilt.
      const sent = received.at(-1).input;
      const note = sent.at(-1)?.role === "developer" ? sent.at(-1) : null;
      const forwarded = variant === "changed_model" ? delta.input : compactNokiyResults(delta.input)?.input ?? delta.input;
      assert.deepEqual(note ? sent.slice(0, -1) : sent, forwarded, "do not rebuild the provider transcript");
      if (variant === "valid") assert.match(deliveryNote(received.at(-1)), /available as reference/);
      assert.equal(received.length, variant === "intervening_response" ? 3 : 2);
    }
  });
}
