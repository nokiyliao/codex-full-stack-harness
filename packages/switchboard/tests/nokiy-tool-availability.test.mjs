// 2026-10-25: Nokiy aliases rejected tool-less app helper requests with NOKIY_DIRECT_NATIVE_TOOL_REQUIRED
// (switchboard log 00:14:45Z and 02:26:11Z), and Efficient rejected whole turns with "unique supported
// advertised native exec" when Codex deferred exec_command out of a large code-mode description.
import assert from "node:assert/strict";
import test from "node:test";
import { projectNokiyDirect } from "../src/nokiy-direct-route.mjs";
import { nativeToolProjection, NOKIY_TOOL } from "../src/nokiy-native-tool.mjs";
import { resolveWorkflowProfiles } from "../src/nokiy-workflow-profiles.mjs";

const host = { python: "/not/executed/python", script: "/not/executed/host.py", coordinator_mode: "dispatch_only_v1" };
const catalog = { models: ["gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"].map((slug) => ({
  slug, supported_reasoning_levels: ["low", "medium", "high", "xhigh", "max"].map((effort) => ({ effort })),
  context_window: 500000, max_context_window: 500000, supports_parallel_tool_calls: true })) };
const selector = { default_effort: "medium", models: { low: "gpt-6-luna", medium: "gpt-6.1-sol", high: "gpt-6-astra" },
  reasoning_efforts: { low: "max", medium: "xhigh", high: "xhigh" } };
const profiles = resolveWorkflowProfiles([
  { slug: "nokiy-direct", display_name: "Nokiy Ultra", workflow: "nokiy-direct",
    coordinator: { model: "gpt-6-astra", reasoning_effort: "xhigh" },
    worker: { model: "gpt-6.1-sol", reasoning_effort: "xhigh", service_tier: "default" }, worker_selector: selector },
  { slug: "nokiy-efficient", display_name: "Nokiy Efficient", workflow: "nokiy-direct",
    coordinator: { model: "gpt-6.1-sol", reasoning_effort: "max" },
    worker: { model: "gpt-6.1-sol", reasoning_effort: "xhigh", service_tier: "default" }, worker_selector: selector },
], catalog);
const helper = (slug, extra = {}) => ({ model: slug, reasoning: { effort: "medium" },
  instructions: "Write a short title for this conversation.", tools: [],
  input: [{ type: "message", role: "user", content: "Fix the dashboard PnL refresh." }], ...extra });
const route = (body, options = {}) => projectNokiyDirect(body, { catalog, profiles, host, ...options });

test("tool-less helper requests use the coordinator model without dispatch policy", () => {
  for (const slug of ["nokiy-efficient", "nokiy-direct"]) {
    const body = helper(slug);
    const before = structuredClone(body);
    const routed = route(body);
    assert.deepEqual(body, before);
    assert.equal(routed.dispatch, "none");
    assert.equal(routed.nativeTool, null);
    assert.equal(routed.body.model, profiles.find((profile) => profile.slug === slug).coordinator.model);
    assert.deepEqual(routed.body.tools, []);
    assert.equal(routed.body.instructions, before.instructions);
    assert.deepEqual(routed.body.reasoning, { effort: "medium" });
  }
});

test("an unsupported helper effort falls back to the configured coordinator effort", () => {
  const routed = route(helper("nokiy-efficient", { reasoning: { effort: "minimal" } }));
  assert.deepEqual(routed.body.reasoning, { effort: "max" });
  assert.equal(routed.wireEffort, "max");
});

test("unbound exec-less chains answer without tool calls and full requests can answer", () => {
  const chained = route(helper("nokiy-efficient", { previous_response_id: "resp_1" }));
  assert.equal(chained.body.tool_choice, "none");
  assert.equal(chained.nativeTool, null);
  const { tools: _omitted, ...withoutTools } = helper("nokiy-efficient");
  assert.equal(route(withoutTools).coordinatorFallback, true);
});

const efficient = profiles.find((profile) => profile.slug === "nokiy-efficient");
const options = { host, profile: efficient, workflow: "nokiy-direct", worker: "gpt-6.1-sol", workerEffort: "xhigh",
  workerServiceTier: "default" };
const codeMode = (description) => ({ model: "gpt-6.1-sol", reasoning: { effort: "max" }, instructions: "Native permissions.",
  tools: [{ type: "namespace", name: "functions", tools: [{ type: "custom", name: "exec", format: { type: "text" }, description }] }],
  input: [{ type: "message", role: "user", content: "Complete the authorized branch." }] });

test("Efficient binds a deferred exec_command through its stable core fields", () => {
  const description = "Run JavaScript.\n\nSome deferred nested tools may be omitted from this description. They are still available on the global `tools` object and listed in `ALL_TOOLS`.";
  const projection = nativeToolProjection(codeMode(description), options);
  assert.deepEqual(projection.target.executionSchema, { type: "object", properties: { cmd: { type: "string" },
    yield_time_ms: { type: "number" }, max_output_tokens: { type: "number" } }, required: ["cmd"], additionalProperties: false });
  assert.ok(projection.body.tools.some((tool) => tool.name === NOKIY_TOOL));
});

test("a code-mode tool with neither a declaration nor deferral still cannot enable native execution", () => {
  assert.throws(() => nativeToolProjection(codeMode("Run JavaScript only."), options), /unique supported advertised native exec/);
});

test("fallback removes deferred subagents and clears a forced choice of a removed tool", () => {
  for (const type of ["additional_tools", "tool_search_output"]) {
    const body = helper("nokiy-efficient", {
      tools: [{ type: "function", name: "spawn_agent" }],
      tool_choice: { type: "function", name: "spawn_agent" },
      input: [{ type, tools: [{ type: "namespace", name: "collaboration", tools: [
        { type: "function", name: "spawn_agent" },
      ] }, { type: "function", name: "read_file" }] }],
    });
    const before = structuredClone(body), routed = route(body);
    assert.deepEqual(body, before);
    assert.deepEqual(routed.body.tools, []);
    assert.equal(routed.body.tool_choice, "auto");
    assert.deepEqual(routed.body.input[0].tools, [{ type: "function", name: "read_file" }]);
  }
});

test("an exec-less Lite prefix uses the coordinator and records its actual tools", () => {
  const routed = route({ model: "nokiy-efficient", reasoning: { effort: "medium" },
    input: [{ type: "additional_tools", tools: [{ type: "function", name: "read_file" }] }] });
  assert.equal(routed.coordinatorFallback, true);
  assert.equal(routed.body.tools, undefined);
  assert.deepEqual(routed.nativeToolMissing.advertised_tools, ["function:read_file"]);
});

test("an unsupported native exec binding falls back without inventing an execution schema", () => {
  const body = { ...codeMode("Run JavaScript only."), model: "nokiy-efficient", reasoning: { effort: "medium" } };
  const routed = route(body);
  assert.equal(routed.dispatch, "none");
  assert.equal(routed.coordinatorFallback, true);
  assert.equal(routed.nativeTool, null);
  assert.deepEqual(routed.body.tools, body.tools);
  assert.equal(routed.nativeToolMissing.reason, "unsupported_native_exec_binding");
});

test("exec names with unsupported tool types answer through the coordinator", () => {
  for (const slug of ["nokiy-efficient", "nokiy-direct"]) {
    for (const tool of [{ type: "function", name: "exec" }, { type: "custom", name: "exec_command" }]) {
      for (const lite of [false, true]) {
        const body = helper(slug, { tools: [tool] });
        if (lite) {
          delete body.tools;
          delete body.instructions;
          body.input.unshift({ type: "additional_tools", tools: [tool] });
        }
        const routed = route(body);
        assert.equal(routed.coordinatorFallback, true);
        assert.equal(routed.nativeTool, null);
        assert.equal(routed.dispatch, "none");
        assert.equal(routed.body.model, profiles.find((profile) => profile.slug === slug).coordinator.model);
        assert.equal(routed.nativeToolMissing.reason, "unsupported_native_exec_binding");
        assert.deepEqual(routed.nativeToolMissing.advertised_tools, [`${tool.type}:${tool.name}`]);
      }
    }
  }
});

test("fallback preserves native replay pairs rather than restoring unavailable Nokiy calls", () => {
  const body = helper("nokiy-efficient", { tools: [{ type: "function", name: "read_file" }],
    input: [{ type: "function_call", name: "exec_command", call_id: "old", arguments: "{}" },
      { type: "function_call_output", call_id: "old", output: "evidence" }] });
  const routed = route(body, { replayRestore() { throw Error("fallback must not restore projected calls"); } });
  assert.deepEqual(routed.body.input, body.input);
});
