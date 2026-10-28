// Malformed Nokiy intake is a correctable local result, not a transport failure.
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { runInNewContext } from "node:vm";
import { resolveWorkflowProfiles } from "../src/nokiy-workflow-profiles.mjs";
import { NOKIY_TOOL, nativeToolProjection, nativeEventProjector } from "../src/nokiy-native-tool.mjs";
import { createReplayRestorer } from "../src/nokiy-replay-restore.mjs";

let directory, previousCodexHome;
before(() => {
  directory = mkdtempSync(join(tmpdir(), "nokiy-efficient-rejection-"));
  writeFileSync(join(directory, "config.toml"), "[agents]\nmax_threads = 4\n");
  previousCodexHome = process.env.CODEX_HOME;
  process.env.CODEX_HOME = directory;
});
after(() => {
  if (previousCodexHome === undefined) delete process.env.CODEX_HOME;
  else process.env.CODEX_HOME = previousCodexHome;
  rmSync(directory, { recursive: true, force: true });
});

const host = { python: "/not/executed/python", script: "/not/executed/host.py" };
const catalog = { models: ["gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"].map((slug) => ({
  slug, supported_reasoning_levels: ["low", "medium", "high", "max", "xhigh"].map((effort) => ({ effort })),
  context_window: 500000, max_context_window: 500000, supports_parallel_tool_calls: true,
})) };
const efficient = resolveWorkflowProfiles([{ slug: "nokiy-efficient", display_name: "Nokiy Efficient", workflow: "nokiy-direct",
  coordinator: { model: "gpt-6.1-sol", reasoning_effort: "max" },
  worker: { model: "gpt-6.1-sol", reasoning_effort: "max", service_tier: "default" } }], catalog)[0];
const exec = { type: "custom", name: "exec", format: { type: "text" },
  description: "declare const tools: { exec_command(args: {cmd: string; workdir?: string; yield_time_ms?: number; max_output_tokens?: number;}): Promise<unknown>; };" };
const body = { model: "gpt-6.1-sol", reasoning: { effort: "max" }, tools: [exec],
  input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "task" }] }] };

test("efficient malformed nokiy_execute is returned to the model instead of aborting the stream", () => {
  const projection = nativeToolProjection(body, { worker: "gpt-6.1-sol", workflow: "nokiy-direct", host, profile: efficient });
  assert.equal(projection.adaptiveNative, true);
  const project = nativeEventProjector(projection);
  const event = { type: "response.output_item.done", output_index: 0,
    item: { type: "custom_tool_call", id: "ctc_1", call_id: "call_1", name: NOKIY_TOOL, status: "completed", input: "not json" } };
  const projected = project(event);
  assert.equal(projected.item.name, "exec");
  assert.equal(projected.item.call_id, "call_1");
  const printed = JSON.parse(projected.item.input.slice(5, -2));
  assert.equal(printed.status, "REJECTED_LOCAL_TOOL_CALL");
  assert.equal(printed.execution_started, false);
  assert.equal(printed.safe_to_correct_input, true);
  assert.equal(printed.error.code, "NOKIY_NATIVE_TOOL_INVALID");
  assert.match(printed.error.message, /JSON/);
});

const invalidAcceptance = () => ({ operation: "run_batch", artifact_root: "/receipts/batch", members: [],
  post_batch_verifier: { intent: { workspace: "/workspace", artifact_root: "/receipts/final",
    mission: "not a mission object", read_scopes: "src/a.py", verifier: [{ argv: ["/bin/true"] }],
    model: "PRIVATE_VALUE_MUST_NOT_LEAK" } } });

for (const slug of ["nokiy-direct", "nokiy-efficient"]) {
  const profile = { ...efficient, slug, coordinator: slug === "nokiy-direct"
    ? { model: "gpt-6-astra", reasoning_effort: "xhigh" } : efficient.coordinator };
  for (const kind of ["custom", "function"]) test(`${slug} ${kind} acceptance rejection remains correctable and replay-bound`, async () => {
    const tool = kind === "custom" ? { ...exec, namespace: "functions" }
      : { type: "function", name: "exec_command", parameters: { type: "object",
        properties: { cmd: { type: "string" } }, required: ["cmd"], additionalProperties: false } };
    const projection = nativeToolProjection({ ...body, model: profile.coordinator.model,
      reasoning: { effort: profile.coordinator.reasoning_effort }, tools: [tool] }, {
      worker: "gpt-6.1-sol", workerEffort: "xhigh", workflow: "nokiy-direct",
      host: { ...host, coordinator_mode: "dispatch_only_v1" }, profile });
    const field = kind === "custom" ? "input" : "arguments";
    const item = { type: kind === "custom" ? "custom_tool_call" : "function_call", id: `${slug}_${kind}`,
      call_id: `${slug}_${kind}`, name: NOKIY_TOOL, status: "completed", [field]: JSON.stringify(invalidAcceptance()) };
    const path = join(directory, `${slug}_${kind}.jsonl`);
    const restorer = createReplayRestorer({ path });
    const project = nativeEventProjector(projection, { onProjected: restorer.record });
    const mapped = project({ type: "response.output_item.done", item }).item;
    const completed = project({ type: "response.completed", response: { output: [item] } });
    assert.deepEqual(completed.response.output[0], mapped);
    assert.equal(mapped.call_id, item.call_id);
    assert.equal(mapped.name, kind === "custom" ? "exec" : "exec_command");
    assert.ok(!mapped[field].includes(host.script));
    let result;
    if (kind === "custom") await runInNewContext(mapped.input, {
      text: value => { result = value; }, tools: new Proxy({}, { get() { throw Error("No execution allowed"); } }),
    });
    else {
      const cmd = JSON.parse(mapped.arguments).cmd;
      assert.ok(cmd.startsWith("/usr/bin/printf '%s\\n' "));
      const quoted = cmd.slice("/usr/bin/printf '%s\\n' ".length);
      result = JSON.parse(quoted.slice(1, -1).replaceAll("'\\''", "'"));
    }
    assert.equal(result.execution_started, false);
    assert.equal(result.safe_to_correct_input, true);
    assert.equal(result.same_input_retry_allowed, false);
    assert.equal(result.error.code, "NOKIY_NATIVE_TOOL_INVALID");
    const issues = Array.from(result.error.details.native_tool_validation.issues, value => value.field);
    assert.deepEqual(issues, ["post_batch_verifier.intent.model", "post_batch_verifier.intent.mission",
      "post_batch_verifier.intent.read_scopes", "post_batch_verifier.intent.verifier"]);
    assert.ok(!JSON.stringify(result).includes("PRIVATE_VALUE_MUST_NOT_LEAK"));
    assert.deepEqual(createReplayRestorer({ path }).originalCall(mapped), item);
    assert.ok(!mapped[field].includes("'--payload'"), "No host envelope that could become a worker delivery binding");
  });
}

