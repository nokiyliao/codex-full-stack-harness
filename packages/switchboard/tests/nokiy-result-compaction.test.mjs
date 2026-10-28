// 2026-10-26: Nokiy result summaries stay in the coordinator context for the rest of a session. A live
// Ultra session carried 1.77M characters of them (169K of SHA-256 values); forwarded requests now carry
// a fixed projection without audit-only digests, identities and coverage metadata.
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, describe, test } from "node:test";

import { compactNokiyResults, projectNokiyDirect } from "../src/nokiy-direct-route.mjs";

const H = (c) => c.repeat(64);
const HEX = /[0-9a-f]{64}/g;
const batchSummary = () => ({
  schema_version: "nokiy_direct_batch_summary_v2", status: "RESULT_AVAILABLE", first_typed_blocker: null,
  continuation_owner: "codex", mission_acceptance: "parent_owned",
  terminal_path_rule: "artifact_root/request_id/terminal.json",
  member_defaults: { reasoning_effort: "xhigh", usage: { coverage: { status: "complete" } } },
  members: [{
    artifact_root: "/work/batch/a",
    changes: { diff: "--- a/x.py\n+++ b/x.py\n@@\n-a\n+b\n", diff_bytes: 30, diff_inline_truncated: false,
      diff_path: "/work/batch/a/changes.diff", diff_sha256: H("2"), schema_version: "nokiy_member_changes_v1",
      files: [{ added: 1, change: "modify", path: "x.py", postimage_sha256: H("3"), preimage_sha256: H("4"), removed: 1 }],
      status: "COMPLETE" },
    cleanup_pass: true, evidence_counts: { commands: 3, failed: 0, file_changes: 1, index_complete: true },
    evidence_status: "EVIDENCE_VERIFIED", first_typed_blocker: null, inspection_summary: { bytes: 100, sha256: H("5") },
    model: "gpt-6.1-sol", native_thread_id: "01a17646-b42e-7501-9b3e-9462ce2eaa2a",
    observed_terminal_delivery: "evidence_only", request_id: "tura_embedded_" + H("1"), request_sha256: H("1"),
    result_text: "{\"terminal_status\":\"done\"}", result_truncated: false, status: "RESULT_AVAILABLE",
    terminal_evidence: { delivery_mode: "evidence_only", runtime_id: "runtime-1", schema_version: "nokiy_terminal_evidence_v1",
      session_id: "full-x", terminal_status: "done" },
    terminal_sha256: H("6"),
    usage: { cached_input_tokens: 10, coverage: { status: "complete" }, input_tokens: 20,
      model_usage_coverage: { status: "complete" }, output_tokens: 5 },
    verifier_delivery: { parent_acceptance_required: true, schema_version: "nokiy_batch_verifier_delivery_v1",
      receipts: [{ execution_proof: "known_success", exit_code: 0, receipt_check: "matched",
        source_event: { event_index: 20, terminal_sha256: H("6"), verifier_index: 0 }, source_postimages: [] }] },
    delivery_binding: { schema_version: "nokiy_batch_delivery_binding_v1", result_sha256: H("7"), terminal_sha256: H("6") },
  }],
  post_batch_verifier: { status: "PASSED", outcome: "passed", exit_code: 0, evidence_authority: "live_return",
    binding: { command: { argv: ["/usr/bin/python3", "/work/tools/check.py"], executable_sha256: H("8"),
      pinned_files: [{ path: "/work/tools/check.py", sha256: H("9") }] },
      request: { request_id: "tura_embedded_" + H("a"), request_file_sha256: H("b"), request_path: "/work/final/request.json" } },
    output_preview: { stdout: "{\"status\": \"PASS\"}" }, receipt_path: "/work/final/receipt.json", receipt_sha256: H("c"),
    source_postimages: [{ path: "x.py", sha256: H("3") }], stdout_sha256: H("d") },
});
const PLAN = JSON.stringify({ plan_path: "/work/batch/native-batch-plan-" + H("e") + ".json", plan_sha256: H("f") });
const output = () => PLAN + "\n" + JSON.stringify(batchSummary()) + "\n";
const toolOutput = (text) => ({ type: "custom_tool_call_output", call_id: "call_batch", output: text });

test("a batch summary keeps decisions, diffs, outcomes and postimages without audit digests", () => {
  const input = [toolOutput(output())];
  const snapshot = structuredClone(input);
  const result = compactNokiyResults(input);
  assert.deepEqual(input, snapshot, "the request is not mutated");
  const [planLine, summaryLine] = result.input[0].output.split("\n");
  assert.equal(planLine, PLAN, "the plan identity used for recovery stays byte-identical");
  const summary = JSON.parse(summaryLine);
  const [member] = summary.members;
  assert.equal(summary.schema_version, "nokiy_direct_batch_summary_v2");
  assert.equal(member.request_id, "tura_embedded_" + H("1"));
  assert.equal(member.changes.diff, batchSummary().members[0].changes.diff);
  assert.deepEqual(member.changes.files, [{ added: 1, change: "modify", path: "x.py", postimage_sha256: H("3"), removed: 1 }]);
  assert.equal(Object.hasOwn(member.changes, "diff_path"), false, "a complete inline diff needs no second copy");
  assert.deepEqual(member.terminal_evidence, { delivery_mode: "evidence_only", terminal_status: "done" });
  assert.deepEqual(member.usage, { cached_input_tokens: 10, input_tokens: 20, output_tokens: 5 });
  assert.deepEqual(member.verifier_delivery.receipts, [{ execution_proof: "known_success", exit_code: 0,
    receipt_check: "matched", verifier_index: 0, source_postimages: [] }]);
  for (const key of ["request_sha256", "terminal_sha256", "native_thread_id", "delivery_binding"]) {
    assert.equal(Object.hasOwn(member, key), false, key);
  }
  const verifier = summary.post_batch_verifier;
  assert.deepEqual(verifier.binding, { command: { argv: ["/usr/bin/python3", "/work/tools/check.py"] } });
  assert.deepEqual(verifier.source_postimages, [{ path: "x.py", sha256: H("3") }]);
  assert.equal(verifier.receipt_path, "/work/final/receipt.json");
  assert.equal(verifier.evidence_authority, "live_return");
  assert.deepEqual(summaryLine.match(HEX).sort(), [H("1"), H("3"), H("3")],
    "only the request id and the postimages keep a digest");
  assert.ok(result.stats.chars_after < result.stats.chars_before);
  assert.equal(result.stats.results, 1);
  assert.equal(compactNokiyResults(result.input), null, "the projection is idempotent");
  assert.deepEqual(compactNokiyResults([toolOutput(output())]), result, "the projection is deterministic");
});

test("a truncated inline diff keeps its full diff path; other JSON and other items stay untouched", () => {
  const summary = batchSummary();
  summary.members[0].changes.diff_inline_truncated = true;
  const result = compactNokiyResults([toolOutput(JSON.stringify(summary))]);
  assert.equal(JSON.parse(result.input[0].output).members[0].changes.diff_path, "/work/batch/a/changes.diff");
  const other = JSON.stringify({ schema_version: "nokiy_native_prepare_outcome_v1", request_sha256: H("1") });
  const user = { type: "message", role: "user", content: [{ type: "input_text", text: JSON.stringify(batchSummary()) }] };
  assert.equal(compactNokiyResults([toolOutput(other), user]), null);
});

test("verified definition and delta context semantics survive provider compaction verbatim", () => {
  for (const schema of ["nokiy_source_postimages_v1", "nokiy_source_postimage_delta_v1", "nokiy_source_postimage_delta_v2"]) {
    const definition = schema === "nokiy_source_postimages_v1";
    const context = {
      schema_version: schema, kind: definition ? "verifier_postimage_context" : "verifier_postimage_delta",
      jspace_semantic_sha256: H("a"), notice: "Context only; parent acceptance and missing-source reads remain required.",
      files: [{ path: "x.py", preimage_sha256: H("b"), postimage_sha256: H("3"),
        ...(definition ? { locators: [{ qualified_name: "support", kind: "function", complete: true,
          line: 1, start_line: 1, end_line: 2 }], spans: [{ start_line: 1, end_line: 2,
          text: "def support():\n    return 2\n" }] } : { coverage: "all_text_changes", hunks: [{
            before_start_line: 1, before_line_count: 1, before_text: "old\n",
            after_start_line: 1, after_line_count: 1, after_text: "new\n" }],
          ...(schema.endsWith("v2") ? { locators: [{ qualified_name: "support", kind: "function",
            line: 1, start_line: 1, end_line: 2 }] } : {}) }) }],
    };
    const summary = batchSummary();
    summary.members[0].verifier_delivery.receipts[0].source_postimage_context = context;
    const original = structuredClone(summary);
    const result = compactNokiyResults([toolOutput(JSON.stringify(summary))]);
    const projected = JSON.parse(result.input[0].output);
    assert.deepEqual(projected.members[0].verifier_delivery.receipts[0].source_postimage_context, context);
    assert.equal(projected.members[0].changes.diff, summary.members[0].changes.diff);
    assert.equal(projected.post_batch_verifier.receipt_path, "/work/final/receipt.json");
    assert.deepEqual(projected.post_batch_verifier.source_postimages, summary.post_batch_verifier.source_postimages);
    assert.deepEqual(summary, original);
    assert.equal(compactNokiyResults(result.input), null);
  }
});

test("public terminal notes keep MODEL_REPORTED schema, bindings and acceptance semantics", () => {
  const summary = batchSummary();
  const note = {
    schema_version: "nokiy_task_status_final_reply_v1", status: "MODEL_REPORTED",
    binding: { request_id: "tura_embedded_" + H("1"), request_sha256: H("1"), native_thread_id: "thread",
      runtime_id: "runtime", event_sha256: H("2") },
    source_artifact: { path: "/work/a/core.jsonl", sha256: H("3"), bytes: 100 },
    output_json_pointer: "/task_status/final_reply", text: "Public semantic delta; evidence refs; parent review pending.",
    complete: false, truncated: true, truncation_reason: "MAX_RESULT_BYTES", content_sha256: H("4"),
    content_bytes: 200, parent_acceptance_required: true,
  };
  summary.members[0].task_status_final_reply = note;
  const original = structuredClone(summary);
  const result = compactNokiyResults([toolOutput(JSON.stringify(summary))]);
  const projected = JSON.parse(result.input[0].output);
  assert.deepEqual(projected.members[0].task_status_final_reply, note);
  assert.equal(projected.members[0].result_text, summary.members[0].result_text);
  assert.equal(projected.members[0].changes.diff, summary.members[0].changes.diff);
  assert.equal(projected.members[0].evidence_status, summary.members[0].evidence_status);
  assert.deepEqual(summary, original);
  assert.equal(compactNokiyResults(result.input), null);
  for (const invalid of [{ schema_version: "unknown" }, { status: "EVIDENCE_VERIFIED" }, { parent_acceptance_required: false }]) {
    summary.members[0].task_status_final_reply = { ...note, ...invalid };
    const output = JSON.parse(compactNokiyResults([toolOutput(JSON.stringify(summary))]).input[0].output);
    assert.equal(Object.hasOwn(output.members[0].task_status_final_reply, "schema_version"), false);
    assert.equal(Object.hasOwn(output.members[0].task_status_final_reply.binding, "request_sha256"), false);
  }
});

test("desktop code-mode command results are projected inside their output string or object", () => {
  const stdout = output();
  const wrapped = JSON.stringify({ chunk_id: "abc123", wall_time_seconds: 0.5, exit_code: 0, original_token_count: 9000, output: stdout });
  const item = { type: "custom_tool_call_output", call_id: "call_cell",
    output: [{ type: "input_text", text: "Script completed\nWall time 0.5 seconds\nOutput:\n" }, { type: "input_text", text: wrapped }] };
  const result = compactNokiyResults([item]);
  const [head, body] = result.input[0].output;
  assert.deepEqual(head, item.output[0]);
  const command = JSON.parse(body.text);
  assert.deepEqual(Object.keys(command), ["chunk_id", "wall_time_seconds", "exit_code", "original_token_count", "output"]);
  assert.equal(command.output, compactNokiyResults([toolOutput(stdout)]).input[0].output);
  assert.equal(compactNokiyResults(result.input), null);
  const nested = JSON.stringify({ output: batchSummary(), nokiy_notice: "kept" });
  const legacy = JSON.parse(compactNokiyResults([toolOutput(nested)]).input[0].output);
  assert.equal(legacy.nokiy_notice, "kept");
  assert.equal(Object.hasOwn(legacy.output.members[0], "terminal_sha256"), false);
});

test("array outputs and run summaries are projected by the same rule", () => {
  const run = { schema_version: "nokiy_terminal_summary_v1", status: "RESULT_AVAILABLE", request_id: "tura_embedded_" + H("1"),
    request_sha256: H("1"), native_thread_id: "01a17646-b42e-7501-9b3e-9462ce2eaa2a", terminal_path: "/work/a/terminal.json",
    terminal_sha256: H("6"), result_text: "done", result_inspection: { schema_version: "nokiy_result_inspection_v1",
      terminal_sha256: H("6"), commands: [{ command: "pytest -q", exit_code: 0, event_sha256: H("2"), command_sha256: H("4") }] } };
  const item = { type: "function_call_output", call_id: "call_run",
    output: [{ type: "input_text", text: "Output:\n" + JSON.stringify(run) }] };
  const result = compactNokiyResults([item]);
  const text = result.input[0].output[0].text;
  assert.ok(text.startsWith("Output:\n"));
  const projected = JSON.parse(text.slice("Output:\n".length));
  assert.deepEqual(projected.result_inspection, { commands: [{ command: "pytest -q", exit_code: 0 }] });
  assert.equal(projected.terminal_path, "/work/a/terminal.json");
  assert.deepEqual(text.match(HEX), [H("1")]);
});

describe("Nokiy projection compacts result summaries on full and chained requests", { concurrency: false }, () => {
  let home, originalHome;
  before(() => {
    originalHome = process.env.CODEX_HOME;
    home = mkdtempSync(join(tmpdir(), "nokiy-result-compaction-"));
    writeFileSync(join(home, "config.toml"), "[agents]\nmax_threads = 2\n");
    process.env.CODEX_HOME = home;
  });
  after(() => {
    if (originalHome === undefined) delete process.env.CODEX_HOME; else process.env.CODEX_HOME = originalHome;
    rmSync(home, { recursive: true, force: true });
  });
  const catalog = { models: ["gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"].map((slug) => ({ slug,
    supported_reasoning_levels: ["low", "medium", "high", "max", "xhigh", "ultra"].map((effort) => ({ effort })),
    multi_agent_reasoning_effort: "xhigh" })) };
  const profiles = [{ slug: "nokiy", display_name: "Nokiy compaction test", workflow: "nokiy",
    coordinator: { model: "gpt-6-astra", reasoning_effort: "xhigh" },
    worker: { model: "gpt-6.1-sol", reasoning_effort: "max", service_tier: "default" },
    worker_selector: { default_effort: "medium", models: { low: "gpt-6-luna", medium: "gpt-6.1-sol", high: "gpt-6-astra" } } }];
  const options = { endpoint: "/responses", catalog, profiles, host: { python: "/synthetic/python", script: "/synthetic/host.py" } };
  const AT = { type: "additional_tools", role: "developer", tools: [{ type: "custom", name: "exec",
    description: "declare const tools: { exec_command(args: { cmd: string; }): Promise<{ output: string; }>; };", format: { type: "text" } }] };
  const common = { model: "nokiy", tool_choice: "auto", parallel_tool_calls: false, reasoning: { effort: "medium", summary: "auto" },
    store: false, stream: true, include: ["reasoning.encrypted_content"], prompt_cache_key: "fixture-thread" };
  const history = () => [AT, { type: "message", role: "user", content: [{ type: "input_text", text: "run the batch" }] },
    { type: "custom_tool_call", call_id: "call_batch", name: "exec", input: "text('batch')" }, toolOutput(output())];
  const summaryOf = (projected) => projected.body.input.find((item) => item.type === "custom_tool_call_output").output;

  test("full and chained requests forward the same compact summary", () => {
    const full = projectNokiyDirect({ ...common, input: history() }, options);
    const chained = projectNokiyDirect({ ...common, previous_response_id: "resp_fixture", tools: AT.tools,
      input: [toolOutput(output())] }, options);
    assert.equal(full.resultCompaction.results, 1);
    assert.equal(chained.resultCompaction.results, 1);
    assert.equal(summaryOf(full), summaryOf(chained));
    assert.equal(summaryOf(full).includes(H("6")), false);
  });

  test("SWITCHBOARD_NOKIY_RESULT_COMPACTION=0 forwards summaries unchanged", () => {
    process.env.SWITCHBOARD_NOKIY_RESULT_COMPACTION = "0";
    try {
      const disabled = projectNokiyDirect({ ...common, input: history() }, options);
      assert.equal(disabled.resultCompaction, null);
      assert.equal(summaryOf(disabled), output());
    } finally { delete process.env.SWITCHBOARD_NOKIY_RESULT_COMPACTION; }
  });
});

