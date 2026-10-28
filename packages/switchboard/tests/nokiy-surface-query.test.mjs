// Offline surface discovery projection and coordinator boundary tests.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { test } from "node:test";
import { nativeToolProjection, nativeToolItem, nativeEventProjector } from "../src/nokiy-native-tool.mjs";

const host = { python: "/offline/python", script: "/offline/nokiy-native-host.py",
  coordinator_mode: "dispatch_only_v1", parent_result_delivery: "accept_v1" };
const payload = { operation: "query_surfaces", workspace: "/workspace", targets: ["src/source.py"] };

function setup(type, workflow, input = [], continuationCall = null) {
  const target = type === "function"
    ? { type, name: "exec_command", parameters: { type: "object" } }
    : { type, name: "exec", format: { type: "text" } };
  return nativeToolProjection({ ...(continuationCall ? { previous_response_id: "query_response" } : {}),
    tools: [{ type: "namespace", name: "functions", tools: [target,
    { type: "function", name: "get_goal" }, { type: "function", name: "write_stdin" }] }], input },
  { host, worker: "gpt-6.1-sol", workerEffort: "max", workerServiceTier: "default", workflow, continuationCall });
}

function call(value, type) {
  return { type: type === "function" ? "function_call" : "custom_tool_call", name: "nokiy_execute",
    id: "query_item", call_id: "query_call", [type === "function" ? "arguments" : "input"]: JSON.stringify(value) };
}

function decode(item) {
  const text = item.arguments ?? item.input;
  const encoded = text.match(/'--payload' '([A-Za-z0-9_-]+)'/)?.[1];
  assert.ok(encoded);
  return JSON.parse(Buffer.from(encoded, "base64url").toString("utf8"));
}

function workerTerminal() {
  const answer = "Qualified worker result content. ".repeat(256);
  return { schema_version: "nokiy_terminal_summary_v1", status: "RESULT_AVAILABLE", cleanup_pass: true,
    runtime_exit_code: 0, first_typed_blocker: null, fallback_used: false, mission_acceptance: "parent_owned",
    continuation_owner: "codex", request_sha256: "a".repeat(64), request_id: "tura_embedded_" + "a".repeat(64),
    terminal_sha256: "c".repeat(64), native_thread_id: "01234567-89ab-cdef-0123-456789abcdef",
    result_artifact: { path: "/not/read/answer.txt", sha256: createHash("sha256").update(answer).digest("hex"),
      bytes: Buffer.byteLength(answer) }, result_text: answer, result_truncated: false,
    result_inspection: { status: "EVIDENCE_VERIFIED", first_blocker: null, terminal_sha256: "c".repeat(64),
      artifact_integrity: "verified", command_success: "known_success",
      counts: { file_changes: 0, failed: 0, index_complete: true } } };
}

for (const workflow of ["nokiy", "nokiy-direct"]) {
  for (const type of ["custom", "function"]) {
    test(`${workflow}/${type} advertises metadata-only discovery with separate bounded fields`, () => {
      const projection = setup(type, workflow);
      const tool = projection.body.tools.find((item) => item.name === "nokiy_execute");
      assert.match(tool.description, /"operation":"query_surfaces"/);
      assert.match(tool.description, /starts no preparation or worker/);
      assert.match(tool.description, /Empty or ambiguous candidates are diagnostic results, never grants/);
      if (type === "function") {
        assert.ok(tool.parameters.properties.operation.enum.includes("query_surfaces"));
        assert.equal(tool.parameters.properties.workspace.maxLength, 4096);
        assert.deepEqual(tool.parameters.properties.targets.items, { type: "string", minLength: 1, maxLength: 512 });
        assert.equal(tool.parameters.properties.targets.minItems, 1);
        assert.equal(tool.parameters.properties.targets.maxItems, 4);
        assert.equal(tool.parameters.properties.targets.uniqueItems, true);
        assert.equal(tool.parameters.additionalProperties, false);
        assert.equal(tool.parameters.properties.draft.properties.targets, undefined);
      }
      const original = call(payload, type);
      const projected = nativeEventProjector(projection)({ type: "response.output_item.done", item: original }).item;
      assert.equal(projected.id, original.id);
      assert.equal(projected.call_id, original.call_id);
      assert.equal(projected.name, type === "function" ? "exec_command" : "exec");
      assert.equal(projected.namespace, "functions");
      assert.deepEqual(decode(projected), { schema_version: workflow === "nokiy" ? "nokiy_native_tool_v3" : "nokiy_native_tool_v2",
        ...(workflow === "nokiy" ? { workflow } : {}), worker: "gpt-6.1-sol", reasoning_effort: "max",
        service_tier: "default", payload });
      assert.equal(projection.resultDelivery, null);
      assert.equal(projection.pendingResultDelivery, null);
    });

    test(`${workflow}/${type} query output cannot become worker delivery or pending wait ownership`, () => {
      const initial = setup(type, workflow);
      const projected = nativeToolItem(call(payload, type), initial);
      for (const output of ["Script running with cell ID 7\nWall time 0.0 seconds\nOutput:\n", JSON.stringify(workerTerminal()),
      JSON.stringify({ status: "SURFACE_CANDIDATES", common_surface_ids: [], authority_effect: "none",
        selection_grants_read_write: false, provider_execution_started: false, native_mutation_count: 0 })]) {
        const result = { type: type === "function" ? "function_call_output" : "custom_tool_call_output",
          call_id: projected.call_id, output };
        const resumed = setup(type, workflow, [projected, result]);
        assert.equal(resumed.resultDelivery, null);
        assert.equal(resumed.pendingResultDelivery, null);
        const continuation = setup(type, workflow, [result], projected);
        assert.equal(continuation.resultDelivery, null);
        assert.equal(continuation.pendingResultDelivery, null);
      }
      const worker = nativeToolItem(call({ operation: "run_ref", input_path: "/offline/input.json",
        input_sha256: "b".repeat(64) }, type), initial);
      const workerResult = { type: type === "function" ? "function_call_output" : "custom_tool_call_output",
        call_id: worker.call_id, output: JSON.stringify(workerTerminal()) };
      assert.ok(setup(type, workflow, [worker, workerResult]).resultDelivery,
        "positive control: the exact same terminal is otherwise valid worker delivery");
      workerResult.output = "Script running with cell ID 7\nWall time 0.0 seconds\nOutput:\n";
      assert.ok(setup(type, workflow, [worker, workerResult]).pendingResultDelivery,
        "positive control: the exact same yield is otherwise valid worker wait ownership");
    });
  }
}

test("surface query rejects unknown fields and unsafe or unbounded paths before native projection", () => {
  const projection = setup("function", "nokiy");
  const invalid = [null, {}, [], "src/source.py", [], ["a", "a"], ["a", "b", "c", "d", "e"],
    [null], [""], ["."], [".."], ["/absolute.py"], ["a/../source.py"], ["a/./source.py"],
    ["a//source.py"], ["a/"], [".git/config"], ["a/.git/config"], ["*.py"], ["a?.py"],
    ["[a].py"], ["{a}.py"], ["a\\b.py"], ["a\x00b.py"], ["a\nb.py"], ["a\rb.py"], ["a".repeat(513)]];
  for (const targets of invalid) assert.throws(() => nativeToolItem(call({ ...payload, targets }, "function"), projection),
    { code: "NOKIY_NATIVE_TOOL_INVALID" });
  for (const field of ["draft", "action", "surface_id", "input_id", "credentials", "native_thread_id"]) {
    assert.throws(() => nativeToolItem(call({ ...payload, [field]: "not allowed" }, "function"), projection),
      { code: "NOKIY_NATIVE_TOOL_INVALID" });
  }
  for (const workspace of [null, {}, "relative", "/workspace/", "/workspace/.", "/workspace/../workspace", "//workspace"])
    assert.throws(() => nativeToolItem(call({ ...payload, workspace }, "function"), projection), { code: "NOKIY_NATIVE_TOOL_INVALID" });
});

test("surface query bounds use Unicode characters rather than UTF-8 bytes or UTF-16 units", () => {
  const projection = setup("function", "nokiy");
  for (const name of ["\u96ea".repeat(512), "\u{1f600}".repeat(512), "-leading.py", "quote'\".py"]) {
    const value = { ...payload, targets: [name] };
    assert.deepEqual(decode(nativeToolItem(call(value, "function"), projection)).payload, value);
  }
  assert.throws(() => nativeToolItem(call({ ...payload, targets: ["\u{1f600}".repeat(513)] }, "function"), projection),
    { code: "NOKIY_NATIVE_TOOL_INVALID" });
});
