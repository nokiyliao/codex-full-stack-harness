import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { coordinatorContract } from "../src/nokiy-coordinator-policy.mjs";
import { projectNokiyDirect, ultraWireEffort } from "../src/nokiy-direct-route.mjs";
import { nativeToolItem, NOKIY_TOOL } from "../src/nokiy-native-tool.mjs";
import { DEFAULT_PROFILES, efficientNativeLoop, resolveWorkflowProfiles } from "../src/nokiy-workflow-profiles.mjs";

const host = { python: "/not/executed/python", script: "/not/executed/host.py", coordinator_mode: "dispatch_only_v1" };
const catalog = { models: ["gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"].map((slug) => ({
  slug, supported_reasoning_levels: ["low", "medium", "high", "max", "xhigh", "ultra"].map((effort) => ({ effort })),
  multi_agent_reasoning_effort: "xhigh", context_window: 500000, max_context_window: 500000,
  supports_parallel_tool_calls: true,
})) };
const sol = { model: "gpt-6.1-sol", reasoning_effort: "max", service_tier: "default" };
const profiles = resolveWorkflowProfiles([
  { slug: "nokiy", display_name: "Serial", workflow: "nokiy",
    coordinator: { model: "gpt-6-astra", reasoning_effort: "ultra" }, worker: { ...sol } },
  ...DEFAULT_PROFILES,
  { slug: "nokiy-efficient", display_name: "Efficient", workflow: "nokiy-direct",
    coordinator: { model: "gpt-6.1-sol", reasoning_effort: "max" }, worker: { ...sol } },
], catalog);
const efficient = profiles.find((profile) => profile.slug === "nokiy-efficient");
const exec = { type: "function", name: "exec_command", parameters: { type: "object", properties: {
  cmd: { type: "string" }, yield_time_ms: { type: "integer", minimum: 250, maximum: 30000 },
  max_output_tokens: { type: "integer", minimum: 1 },
  sandbox_permissions: { type: "string", enum: ["use_default", "require_escalated"] },
}, required: ["cmd"], additionalProperties: false } };
function route(profile = efficient, routeHost = host) {
  const effort = profile.coordinator.reasoning_effort === "ultra"
    ? ultraWireEffort(catalog) : profile.coordinator.reasoning_effort;
  const body = { model: profile.slug, reasoning: { effort },
    instructions: "Existing native permissions, owner/CAS and Goal instructions.",
    tools: [{ type: "namespace", name: "functions", tools: [structuredClone(exec)] }],
    input: [{ type: "message", role: "user", content: "Complete the authorized branch." }], parallel_tool_calls: true };
  const before = structuredClone(body);
  const routed = projectNokiyDirect(body, { catalog, profiles, host: routeHost });
  assert.deepEqual(body, before);
  return { body, routed };
}

// Remove only this correction in memory to compare non-Efficient policy bytes.
const post = await readFile(new URL("../src/nokiy-coordinator-policy.mjs", import.meta.url), "utf8");
const changeStart = post.indexOf('\n      + " Group already-known independent reads');
const changeEnd = post.indexOf('\n      + " Before dispatch, match structured grants', changeStart);
assert.ok(changeStart >= 0 && changeEnd > changeStart);
const previous = post.slice(0, changeStart) + post.slice(changeEnd);
const functionStart = previous.indexOf("export function coordinatorContract(profile) {");
assert.ok(functionStart >= 0);
const imports = `import { efficientNativeLoop, EFFICIENT_NATIVE_GUIDANCE } from ${JSON.stringify(new URL("../src/nokiy-workflow-profiles.mjs", import.meta.url).href)};\n`;
const previousContract = (await import(`data:text/javascript;base64,${Buffer.from(imports + previous.slice(functionStart)).toString("base64")}`)).coordinatorContract;

test("actual Efficient routing distinguishes supported artifact delivery from narrative delivery", () => {
  assert.doesNotMatch(previousContract(efficient), /terminal_delivery/);
  for (const routeHost of [host, { ...host, coordinator_mode: undefined }]) {
    const { body, routed } = route(efficient, routeHost);
    const text = routed.body.instructions;
    assert.equal(text, `${body.instructions}\n\n${coordinatorContract(efficient)}`);
    assert.match(text, /artifact\/mutation workers whose parent owns acceptance, use action\.terminal_delivery="evidence_only", which the installed caller\/runtime supports/);
    assert.match(text, /exact persisted same-request terminal_evidence, observed_terminal_delivery and cleanup/);
    assert.match(text, /the parent still owns acceptance/);
    assert.match(text, /Reviews, questions and narrative deliverables use action\.terminal_delivery="assistant_reply"/);
    assert.match(text, /omission remains assistant_reply\. Do not add a narrative-only worker handoff/);
    assert.match(text, /Do not add a narrative-only worker handoff for an artifact result, infer success from absent prose or skip required checks/);
  }
});

test("routed read reuse and optional preload remain bounded by current capabilities and required verification", () => {
  const { routed } = route();
  const text = routed.body.instructions;
  assert.match(text, /Group already-known independent reads in one supported batch or step; keep dependent reads ordered/);
  assert.match(text, /Reuse verified unchanged supplied evidence with its provenance and SHA instead of rereading it/);
  assert.match(text, /Missing context, required tests and fresh changed-file postimages still require verification/);
  assert.match(text, /do not truncate necessary context to reduce calls/);
  assert.match(text, /Only when verified current installed caller\/runtime support explicitly admits action\.source_sections, optionally preload already-known exact authorized local non-DCF UTF-8 source ranges within its advertised limits/);
  assert.match(text, /If support is absent or unverified, omit source_sections and use existing admitted reads/);
  assert.match(text, /Never force this field, infer read authority or add discovery just to fill it/);
  assert.match(text, /partial preimages, not dependency proof or post-edit evidence/);
  assert.match(text, /neither performance gains nor cross-role cache reuse/);
  const description = routed.body.tools.find((tool) => tool.name === NOKIY_TOOL).description;
  assert.match(description, /Only after verifying installed caller support, optionally preload/);
  assert.match(description, /The installed caller\/runtime supports action\.terminal_delivery="evidence_only"/);
});

test("Efficient keeps native fusion, independent runtimes, capacity and exact authority envelopes", () => {
  const { body, routed } = route();
  assert.equal(routed.body.model, "gpt-6.1-sol");
  assert.equal(routed.body.reasoning.effort, "max");
  assert.deepEqual(routed.body.tools.slice(0, -1), body.tools);
  assert.equal(routed.nativeTool.coordinatorBindings, null);
  assert.equal(routed.nativeTool.worker, sol.model);
  assert.equal(routed.nativeTool.workerEffort, sol.reasoning_effort);
  assert.equal(routed.nativeTool.serviceTier, sol.service_tier);
  assert.match(routed.body.instructions, /SAME native Codex loop/);
  assert.match(routed.body.instructions, /honor explicit independent-runtime or selected-worker requests/);
  assert.match(routed.body.instructions, /Prose and write_scopes never grant operations/);
  assert.match(routed.body.instructions, /expected_sha256 set to the returned postimage_sha256/);
  assert.match(routed.body.instructions, /native permission denial, owner conflict or CAS failure/);
  const tool = routed.body.tools.find((item) => item.name === NOKIY_TOOL);
  const capacity = Number(tool.description.match(/\(1 through (\d+) independent members/)?.[1]);
  assert.ok(capacity >= 1 && capacity <= 20);
  const ultra = route(profiles.find((profile) => profile.slug === "nokiy-direct-sol")).routed;
  // V415: only Efficient advertises deferred parent completion; every other field is shared with Ultra.
  const withoutParentCompletion = (value) => Array.isArray(value) ? value.map(withoutParentCompletion)
    : value && typeof value === "object" ? Object.fromEntries(Object.entries(value)
      .filter(([key]) => !["parent_source_targets", "parent_completion"].includes(key))
      .map(([key, entry]) => [key, withoutParentCompletion(entry)])) : value;
  assert.deepEqual(withoutParentCompletion(tool.parameters),
    ultra.body.tools.find((item) => item.name === NOKIY_TOOL).parameters);
  assert.deepEqual(routed.nativeTool.operations, ultra.nativeTool.operations);
  assert.equal(routed.nativeTool.operations.includes("deploy"), false);
  // Translation proves field/grant preservation, not installed runtime admission.
  for (const [delivery, mutation] of [["evidence_only", true], ["assistant_reply", false], [undefined, false]]) {
    const payload = { operation: "run", draft: { workspace: "/not/read/workspace", artifact_root: "/not/read/artifacts",
      prompt: mutation ? "Update the assigned artifact" : "Return the assigned review", authority_effect: "none", timeout_seconds: null },
      action: { mission: { mission_id: "fixture", task_id: "known-context", mode: "DELIVERY",
        objective: "One authorized output", current_predicate: "Output missing" },
        operations: mutation ? ["read", "modify", "command"] : ["read", "command"],
        read_scopes: mutation ? ["input.txt", "output.txt"] : ["input.txt"],
        write_scopes: mutation ? ["output.txt"] : [], target_paths: mutation ? ["output.txt"] : [],
        source_read: true, command_templates: [], forbidden_effects: ["deployment", "broker", "order", "private_db_write", "restart"] } };
    if (delivery !== undefined) payload.action.terminal_delivery = delivery;
    const original = structuredClone(payload);
    const translated = nativeToolItem({ type: "function_call", name: NOKIY_TOOL, call_id: "fixture-call",
      arguments: JSON.stringify(payload) }, routed.nativeTool);
    const encoded = JSON.parse(translated.arguments).cmd.match(/ '--payload' '([A-Za-z0-9_-]+)'$/)?.[1];
    assert.ok(encoded);
    const envelope = JSON.parse(Buffer.from(encoded, "base64url").toString("utf8"));
    assert.deepEqual(envelope, { schema_version: "nokiy_native_tool_v2", worker: sol.model,
      reasoning_effort: sol.reasoning_effort, service_tier: sol.service_tier, payload: original });
    assert.equal(Object.hasOwn(envelope.payload.action, "source_sections"), false);
    assert.deepEqual(payload, original);
  }
});

test("Serial and every configured Ultra policy remain byte-identical and cannot select Efficient by worker model", () => {
  const before = structuredClone(profiles);
  for (const profile of profiles.filter((item) => item !== efficient)) {
    assert.equal(efficientNativeLoop(profile), false);
    assert.equal(coordinatorContract(profile), previousContract(profile));
    const { body, routed } = route(profile);
    assert.equal(routed.body.instructions, `${body.instructions}\n\n${previousContract(profile)}`);
    assert.doesNotMatch(routed.body.instructions, /Group already-known independent reads|artifact\/mutation workers whose parent owns acceptance/);
  }
  for (const profile of [{ ...efficient, workflow: "nokiy" },
    { ...efficient, coordinator: { model: "gpt-6-astra", reasoning_effort: "max" } },
    { ...efficient, coordinator: { model: "gpt-6.1-sol", reasoning_effort: "high" } }]) {
    assert.equal(efficientNativeLoop(profile), false);
  }
  assert.deepEqual(profiles, before);
});
