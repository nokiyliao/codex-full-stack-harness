import assert from "node:assert/strict";
import test from "node:test";
import { runInNewContext } from "node:vm";
import { lowerAcceptanceIntent, nativeToolProjection, nativeToolItem } from "../src/nokiy-native-tool.mjs";

const intent = () => ({ workspace: "/workspace", artifact_root: "/receipts/final",
  mission: { mission_id: "mission", task_id: "acceptance", mode: "DELIVERY",
    objective: "Run the declared final checks", current_predicate: "checks pending" },
  read_scopes: ["src/a.py"], verifier: { catalog_ref: { path: "/catalog.json", sha256: "a".repeat(64), command: "final" } } });

test("reference code mode announces the original plan before waiting for parent completion", async () => {
  const profile = { slug: "nokiy-efficient", workflow: "nokiy-direct",
    coordinator: { model: "gpt-6.1-sol", reasoning_effort: "max" } };
  const exec = { type: "custom", name: "exec", format: { type: "text" }, description:
    "declare const tools: { exec_command(args: {cmd: string;}): Promise<unknown>; };\n"
    + "declare const tools: { write_stdin(args: {session_id: number; chars?: string;}): Promise<unknown>; };" };
  const projection = nativeToolProjection({ model: "gpt-6.1-sol", reasoning: { effort: "max" },
    tools: [{ type: "namespace", name: "functions", tools: [exec] }], input: [] }, {
    profile, workflow: "nokiy-direct", worker: "gpt-6.1-sol", workerEffort: "xhigh",
    host: { python: "/not/executed/python", script: "/not/executed/host.py",
      coordinator_mode: "dispatch_only_v1", batch_reference: "direct_v1" } });
  const item = nativeToolItem({ type: "custom_tool_call", name: "nokiy_execute", call_id: "ref",
    input: JSON.stringify({ operation: "run_batch_ref", input_path: "/batch.json",
      input_sha256: "b".repeat(64), parent_completion: true, observe: "later" }) }, projection);
  const outputs = [];
  const plan = { plan_path: "/original/plan.json", plan_sha256: "c".repeat(64) };
  let starts = 0, waits = 0;
  await runInNewContext(`(async () => {${item.input}\n})()`, { text: value => outputs.push(value), tools: {
    exec_command: async () => { starts++; return { session_id: 7, output: JSON.stringify(plan) + "\n" }; },
    write_stdin: async args => {
      waits++;
      assert.equal(args.session_id, 7);
      assert.equal(args.chars, "");
      assert.equal(args.yield_time_ms, 1000);
      assert.equal(outputs[0].status, "PLAN_AVAILABLE_EXECUTION_PENDING");
      assert.equal(outputs[0].plan_sha256, plan.plan_sha256);
      assert.equal(outputs[0].parent_completion_required, true);
      return { exit_code: 0, output: JSON.stringify({ status: "RESULT_AVAILABLE" }) };
    },
  } });
  assert.equal(starts, 1);
  assert.equal(waits, 1);
  assert.equal(outputs.at(-1).terminal, true);
});

test("explicit acceptance intent lowers to the ordinary read-only packet without mutating input", () => {
  const source = intent(); const before = structuredClone(source);
  const packet = lowerAcceptanceIntent({ intent: source });
  assert.deepEqual(source, before);
  assert.deepEqual(packet.action.mission, source.mission);
  assert.deepEqual(packet.action.read_scopes, source.read_scopes);
  assert.deepEqual(packet.action.verifier_commands, [source.verifier]);
  assert.deepEqual(packet.action.operations, ["read", "command"]);
  assert.deepEqual(packet.action.write_scopes, []);
  assert.deepEqual(packet.action.target_paths, []);
  assert.equal(packet.draft.authority_effect, "none");
  assert.equal(packet.draft.prompt, source.mission.objective);
  assert.equal(packet.action.context_summary, source.mission.objective);
  packet.action.read_scopes.push("other");
  assert.deepEqual(source, before);
});

test("scope and parent targets stay explicit; no worker write-scope inference", () => {
  const source = { ...intent(), read_directories: ["src"], parent_source_targets: ["src/parent.py"], surface_id: "known" };
  const packet = lowerAcceptanceIntent({ intent: source });
  assert.deepEqual(packet.parent_source_targets, source.parent_source_targets);
  assert.deepEqual(packet.action.read_directories, source.read_directories);
  assert.equal(packet.surface_id, "known");
  assert.deepEqual(packet.action.read_scopes, ["src/a.py"]);
});

test("ambiguous formats, missing grants and overrides are rejected", () => {
  for (const field of ["write_scopes", "operations", "model", "native_thread_id", "unknown"]) {
    assert.throws(() => lowerAcceptanceIntent({ intent: { ...intent(), [field]: [] } }));
  }
  for (const field of ["workspace", "artifact_root", "mission", "read_scopes", "verifier"]) {
    const source = intent(); delete source[field];
    assert.throws(() => lowerAcceptanceIntent({ intent: source }));
  }
  assert.throws(() => lowerAcceptanceIntent({ intent: intent(), draft: {} }));
  assert.throws(() => lowerAcceptanceIntent({ intent: { ...intent(), read_scopes: [] } }));
  const ordinary = { draft: {}, action: {} };
  assert.equal(lowerAcceptanceIntent(ordinary), ordinary);
});

test("native projection sends the same host envelope for intent and ordinary packets", () => {
  const fn = { type: "function", name: "exec_command", parameters: { type: "object",
    properties: { cmd: { type: "string" }, yield_time_ms: { type: "integer", maximum: 30000 },
      max_output_tokens: { type: "integer" } }, required: ["cmd"] } };
  const projection = nativeToolProjection({ tools: [fn], input: [] }, {
    worker: "gpt-6.1-sol", workerEffort: "xhigh", workflow: "nokiy-direct",
    host: { python: "/not/executed/python", script: "/not/executed/host.py", coordinator_mode: "dispatch_only_v1" },
  });
  const source = { intent: intent() };
  const description = projection.body.tools.find((tool) => tool.name === "nokiy_execute").description;
  assert.match(description, /PRAGMA query_only=ON/);
  assert.match(description, /verified immutable snapshot/);
  assert.doesNotMatch(description, /file:<path>\?mode=ro&immutable=1/);
  const project = (spec) => nativeToolItem({ type: "function_call", name: "nokiy_execute", call_id: "c",
    arguments: JSON.stringify({ operation: "run_batch", artifact_root: "/batch", members: [], post_batch_verifier: spec }) }, projection);
  assert.deepEqual(project(source), project(lowerAcceptanceIntent(source)));
  assert.throws(() => project({ intent: { ...intent(), parent_source_targets: ["src/parent.py"] } }),
    /Parent completion requires Efficient/);
  assert.throws(() => project({ intent: { ...intent(), verifier: [] } }));
});

test("Efficient intent preserves deferred parent completion and original host binding", () => {
  const profile = { slug: "nokiy-efficient", workflow: "nokiy-direct",
    coordinator: { model: "gpt-6.1-sol", reasoning_effort: "max" },
    worker: { model: "gpt-6.1-sol", reasoning_effort: "xhigh", service_tier: "default" } };
  const fn = { type: "function", name: "exec_command", parameters: { type: "object",
    properties: { cmd: { type: "string" }, yield_time_ms: { type: "integer", maximum: 30000 },
      max_output_tokens: { type: "integer" } }, required: ["cmd"] } };
  const projection = nativeToolProjection({ model: "gpt-6.1-sol", reasoning: { effort: "max" },
    tools: [fn], input: [] }, { profile, workflow: profile.workflow,
    worker: profile.worker.model, workerEffort: profile.worker.reasoning_effort,
    host: { python: "/not/executed/python", script: "/not/executed/host.py", coordinator_mode: "dispatch_only_v1", batch_reference: "direct_v1" } });
  const spec = { intent: { ...intent(), parent_source_targets: ["src/parent.py"] } };
  const project = (gate, observe) => nativeToolItem({ type: "function_call", name: "nokiy_execute",
    call_id: "parent-bound", arguments: JSON.stringify({ operation: "run_batch", observe,
      artifact_root: "/batch", members: [], post_batch_verifier: gate }) }, projection);
  const lowered = project(spec, "later");
  assert.deepEqual(lowered, project(lowerAcceptanceIntent(spec), "later"));
  assert.equal(JSON.parse(lowered.arguments).yield_time_ms, 1000);
  const encoded = JSON.parse(lowered.arguments).cmd.match(/ '--payload' '([A-Za-z0-9_-]+)'$/)[1];
  const envelope = JSON.parse(Buffer.from(encoded, "base64url").toString("utf8"));
  assert.deepEqual(envelope.payload.post_batch_verifier.parent_source_targets, ["src/parent.py"]);
  assert.equal(envelope.worker, "gpt-6.1-sol");
  assert.equal(envelope.reasoning_effort, "xhigh");
  assert.throws(() => project(spec, undefined), /Parent completion requires Efficient/);
  const reference = { operation: "run_batch_ref", input_path: "/existing/batch.json",
    input_sha256: "a".repeat(64), parent_completion: true, observe: "later" };
  const projectReference = (payload) => nativeToolItem({ type: "function_call", name: "nokiy_execute",
    call_id: "reference", arguments: JSON.stringify(payload) }, projection);
  const mapped = projectReference(reference);
  const args = JSON.parse(mapped.arguments);
  assert.equal(args.yield_time_ms, 1000);
  const wire = JSON.parse(Buffer.from(args.cmd.match(/ '--payload' '([A-Za-z0-9_-]+)'$/)[1], "base64url"));
  assert.equal(wire.payload.parent_completion, true);
  assert.equal(wire.payload.input_sha256, reference.input_sha256);
  assert.equal(wire.payload.observe, undefined);
  assert.throws(() => projectReference({ ...reference, observe: undefined }), /Parent completion reference/);
  assert.throws(() => projectReference({ ...reference, parent_completion: false }), /Parent completion reference/);
});
