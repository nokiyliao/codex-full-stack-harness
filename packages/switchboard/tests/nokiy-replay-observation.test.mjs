import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, describe, test } from "node:test";

import { projectNokiyDirect } from "../src/nokiy-direct-route.mjs";
import { continueNativeObservation, nativeEventProjector } from "../src/nokiy-native-tool.mjs";
import { createReplayRestorer } from "../src/nokiy-replay-restore.mjs";

// A full request restores the model's own nokiy_execute call for cache alignment, while
// observation and result delivery must still bind the native exec call Codex executed.
const catalog = { models: ["gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"].map((slug) => ({ slug,
  supported_reasoning_levels: ["low", "medium", "high", "max", "xhigh", "ultra"].map((effort) => ({ effort })),
  multi_agent_reasoning_effort: "xhigh" })) };
const profiles = [{ slug: "nokiy-direct", display_name: "Nokiy Ultra observation test", workflow: "nokiy-direct",
  coordinator: { model: "gpt-6-astra", reasoning_effort: "max" },
  worker: { model: "gpt-6.1-sol", reasoning_effort: "max", service_tier: "default" },
  worker_selector: { default_effort: "medium", models: { low: "gpt-6-luna", medium: "gpt-6.1-sol", high: "gpt-6-astra" } } }];
const host = { python: "/synthetic/nokiy/python", script: "/synthetic/nokiy/host.py",
  coordinator_mode: "dispatch_only_v1", parent_result_delivery: "accept_v1" };
const execDescription = ["declare const tools: { exec_command(args: {", "  cmd: string;", "}): Promise<{ output: string; }>; };",
  "declare const tools: { write_stdin(args: {", "  chars?: string;", "  session_id: number;", "}): Promise<{ output: string; }>; };"].join("\n");
const waitTool = { type: "function", name: "wait", parameters: { type: "object", properties: {
  cell_id: { type: "string" }, max_tokens: { type: "number" }, terminate: { type: "boolean" }, yield_time_ms: { type: "number" },
}, required: ["cell_id"], additionalProperties: false } };
const AT = { type: "additional_tools", id: "at_fixture", role: "developer", tools: [{ type: "namespace", name: "functions",
  tools: [{ type: "custom", name: "exec", description: execDescription, format: { type: "text" } }, waitTool] }] };
const BI = { type: "message", role: "developer", content: [{ type: "input_text", text: "BASE" }] };
const user = (text) => ({ type: "message", role: "user", content: [{ type: "input_text", text }] });
const common = { model: "nokiy-direct", tool_choice: "auto", parallel_tool_calls: false,
  reasoning: { effort: "medium", summary: "auto", context: "all_turns" }, store: false, stream: true };
const dispatch = { type: "custom_tool_call", id: "ctc_dispatch", status: "completed", call_id: "call_dispatch", name: "nokiy_execute",
  input: JSON.stringify({ operation: "run", draft: { workspace: "/synthetic/ws", artifact_root: "/synthetic/artifacts",
    prompt: "bounded objective", authority_effect: "none", timeout_seconds: null },
  action: { mission: { mission_id: "m", task_id: "t", mode: "DELIVERY", objective: "o", current_predicate: "p" },
    context_summary: "s", operations: ["read", "command"], inspect: {} } }) };
const yielded = { type: "custom_tool_call_output", call_id: "call_dispatch",
  output: "Script running with cell ID 131\nWall time 7.7 seconds\nOutput:\n" };
const modelWait = { type: "function_call", id: "fc_wait", call_id: "call_wait", name: "wait",
  arguments: JSON.stringify({ cell_id: "131", max_tokens: 4500, yield_time_ms: 1000 }) };

describe("observation after replay restoration", { concurrency: false }, () => {
  let dir, originalHome;
  before(() => {
    originalHome = process.env.CODEX_HOME;
    dir = mkdtempSync(join(tmpdir(), "nokiy-replay-observation-"));
    writeFileSync(join(dir, "config.toml"), "[agents]\nmax_threads = 2\n");
    process.env.CODEX_HOME = dir;
  });
  after(() => {
    if (originalHome === undefined) delete process.env.CODEX_HOME;
    else process.env.CODEX_HOME = originalHome;
    rmSync(dir, { recursive: true, force: true });
  });

  const shownDispatch = () => {
    const first = projectNokiyDirect({ ...common, input: [AT, BI, user("task")] }, { catalog, profiles, host });
    const store = createReplayRestorer({ path: null });
    const event = nativeEventProjector(first.nativeTool, { onProjected: store.record })(
      { type: "response.output_item.done", output_index: 0, item: dispatch });
    assert.equal(event.item.name, "exec");
    return { shown: { ...event.item, id: undefined }, store };
  };

  test("a full replay keeps the restored dispatch's pending cell, so the coordinator wait gets the floor", () => {
    const { shown, store } = shownDispatch();
    // Codex sends a full request, for example after an operator message, ending with that message.
    const full = projectNokiyDirect({ ...common, input: [AT, BI, user("task"), shown, yielded, user("status?")] },
      { catalog, profiles, host, replayRestore: store.restore });
    assert.equal(full.replayRestored, 1);
    // The model still sees its own call, which keeps the cached prefix aligned.
    assert.ok(full.body.input.some((item) => item.call_id === "call_dispatch" && item.name === "nokiy_execute"));
    const projection = continueNativeObservation(full.nativeTool, null);
    assert.deepEqual(projection.nativeObservationState.pendingCells.map((ref) => ref.cell_id), ["131"]);
    const waited = nativeEventProjector(projection)({ type: "response.output_item.done", output_index: 0, item: modelWait }).item;
    assert.equal(JSON.parse(waited.arguments).yield_time_ms, 300000);
  });

  test("a full replay ending in the dispatch output still binds result delivery to the native call", () => {
    const { shown, store } = shownDispatch();
    const completed = { type: "custom_tool_call_output", call_id: "call_dispatch",
      output: "Script completed\nWall time 9.1 seconds\nOutput:\n{\"exit_code\":0,\"output\":\"{}\"}" };
    const full = projectNokiyDirect({ ...common, input: [AT, BI, user("task"), shown, completed] },
      { catalog, profiles, host, replayRestore: store.restore });
    assert.equal(full.replayRestored, 1);
    assert.notEqual(full.nativeTool.resultDeliveryRejection, "NATIVE_DISPATCH_BINDING_MISMATCH");
  });
});

