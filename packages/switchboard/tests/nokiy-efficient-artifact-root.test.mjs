import test from "node:test";
import assert from "node:assert/strict";
import { coordinatorContract } from "../src/nokiy-coordinator-policy.mjs";
import { nativeToolItem, NOKIY_TOOL } from "../src/nokiy-native-tool.mjs";

const profile = (slug, workflow = "nokiy-direct") => ({
  slug, workflow, display_name: slug,
  coordinator: { model: slug === "nokiy-efficient" ? "gpt-6.1-sol" : "gpt-6-astra", reasoning_effort: "max" },
  worker: { model: "gpt-6.1-sol", reasoning_effort: "max", service_tier: "default" },
});
const member = {
  draft: { workspace: "/fixture/workspace", artifact_root: "/fixture/artifacts/fresh/worker",
    prompt: "One bounded deliverable", authority_effect: "none", timeout_seconds: null },
  action: { mission: { mission_id: "fixture", task_id: "one", mode: "DELIVERY",
    objective: "One deliverable", current_predicate: "Not implemented" },
    context_summary: "Fixture only", operations: ["read", "command"], read_scopes: ["input.txt"],
    write_scopes: [], target_paths: [], source_read: true, command_templates: [], forbidden_effects: ["deployment"] },
  surface_id: "fixture-surface",
};
const batch = { operation: "run_batch", artifact_root: "/fixture/artifacts/fresh", members: [member] };

// Fake only the native executor advertisement; translating a call executes nothing.
function translated(payload, { workflow = "nokiy-direct", worker = "gpt-6.1-sol", effort = "max" } = {}) {
  const before = structuredClone(payload);
  const result = nativeToolItem({ type: "function_call", name: NOKIY_TOOL,
    call_id: "fixture-call", arguments: JSON.stringify(payload) }, {
    workflow, worker, workerEffort: effort, serviceTier: "default", userTexts: [],
    operations: ["run", "run_ref", "run_batch", "read_result", "read_batch"],
    host: { python: "/fixture/python", script: "/fixture/host.py" },
    target: { type: "function", name: "exec_command", namespace: "functions" },
  });
  assert.deepEqual(payload, before);
  assert.equal(result.name, "exec_command");
  assert.equal(result.namespace, "functions");
  const args = JSON.parse(result.arguments);
  const encoded = args.cmd.match(/ '--payload' '([A-Za-z0-9_-]+)'$/)?.[1];
  assert.ok(encoded, "one native host invocation contains the original payload");
  return JSON.parse(Buffer.from(encoded, "base64url").toString("utf8"));
}

test("Efficient keeps singleton fresh-root batching for an explicitly requested worker", () => {
  const text = coordinatorContract(profile("nokiy-efficient"));
  assert.match(text, /one coherent branch in the SAME native Codex loop/);
  assert.match(text, /When an explicit independent-runtime or selected-worker request requires one installed worker with a fresh artifact root/);
  assert.match(text, /dispatch run_batch explicitly with exactly one member/);
  assert.match(text, /no preliminary run, mkdir or setup call is needed/);
  assert.match(text, /Keep run for an existing valid artifact root/);
  assert.match(text, /never convert or resubmit a potentially executed run as a batch/);
  assert.doesNotMatch(text, /Retain run for a single coherent deliverable/);
  for (const [worker, effort] of [["gpt-6-luna", "max"], ["gpt-6.1-sol", "max"], ["gpt-6-astra", "xhigh"]]) {
    assert.deepEqual(translated(batch, { worker, effort }), {
      schema_version: "nokiy_native_tool_v2", worker, reasoning_effort: effort,
      service_tier: "default", payload: batch,
    });
  }
});

test("existing-root single calls keep their operation, scope and bound identity", () => {
  const payload = { operation: "run", ...structuredClone(member) };
  assert.deepEqual(translated(payload), {
    schema_version: "nokiy_native_tool_v2", worker: "gpt-6.1-sol", reasoning_effort: "max",
    service_tier: "default", payload,
  });
  assert.equal(payload.operation, "run", "no silent conversion to a batch");
});

test("batch recovery is the exact original plan reference, without another member", () => {
  const payload = { operation: "read_batch", plan_path: "/fixture/artifacts/fresh/native-batch-plan-original.json",
    plan_sha256: "a".repeat(64) };
  const recovered = translated(payload);
  assert.deepEqual(recovered.payload, payload);
  assert.equal(recovered.payload.members, undefined);
  const singleRecovery = { operation: "read_result", artifact_root: member.draft.artifact_root,
    request_id: `tura_embedded_${"b".repeat(64)}` };
  assert.deepEqual(translated(singleRecovery).payload, singleRecovery);
});

test("other coordinator modes retain their existing single-action policy and envelopes", () => {
  const direct = coordinatorContract(profile("nokiy-direct"));
  assert.match(direct, /Retain run for a single coherent deliverable/);
  assert.doesNotMatch(direct, /dispatch run_batch explicitly with exactly one member/);
  const plain = coordinatorContract(profile("nokiy", "nokiy"));
  assert.match(plain, /Execute one bounded action at a time; no batch or fan-out/);
  assert.doesNotMatch(plain, /fresh artifact root/);
  const payload = { operation: "run", ...structuredClone(member) };
  assert.deepEqual(translated(payload, { workflow: "nokiy" }), {
    schema_version: "nokiy_native_tool_v3", workflow: "nokiy", worker: "gpt-6.1-sol",
    reasoning_effort: "max", service_tier: "default", payload,
  });
});
