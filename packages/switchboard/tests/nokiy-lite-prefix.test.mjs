import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, describe, test } from "node:test";

import { projectNokiyDirect, withLitePrefix } from "../src/nokiy-direct-route.mjs";
import { summarizeRequestProvenance } from "../src/inference-attempt-evidence.mjs";

// Codex Responses Lite: no top-level instructions/tools; tools and base instructions are
// conversation prefix items, and WebSocket deltas chain through previous_response_id.
const catalog = {
  models: ["gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"].map((slug) => ({
    slug, supported_reasoning_levels: ["low", "medium", "high", "max", "xhigh", "ultra"].map((effort) => ({ effort })),
    multi_agent_reasoning_effort: "xhigh",
  })),
};
const selector = { default_effort: "medium", models: { low: "gpt-6-luna", medium: "gpt-6.1-sol", high: "gpt-6-astra" } };
const worker = { model: "gpt-6.1-sol", reasoning_effort: "max", service_tier: "default" };
const profiles = [
  { slug: "nokiy", display_name: "Nokiy lite test", workflow: "nokiy",
    coordinator: { model: "gpt-6-astra", reasoning_effort: "xhigh" }, worker: { ...worker }, worker_selector: selector },
  { slug: "nokiy-direct", display_name: "Nokiy Ultra lite test", workflow: "nokiy-direct",
    coordinator: { model: "gpt-6-astra", reasoning_effort: "max" }, worker: { ...worker }, worker_selector: selector },
];
const hosts = {
  plain: { python: "/synthetic/nokiy/python", script: "/synthetic/nokiy/host.py" },
  coordinator: { python: "/synthetic/nokiy/python", script: "/synthetic/nokiy/host.py",
    coordinator_mode: "dispatch_only_v1", parent_result_delivery: "accept_v1" },
};
const execDescription = [
  "declare const tools: { exec_command(args: {", "  cmd: string;", "  workdir?: string;", "}): Promise<{ output: string; }>; };",
  "declare const tools: { write_stdin(args: {", "  chars?: string;", "  session_id: number;", "}): Promise<{ output: string; }>; };",
].join("\n");
const AT = { type: "additional_tools", id: "at_fixture", role: "developer", tools: [
  { type: "custom", name: "exec", description: execDescription, format: { type: "text" } },
  { type: "function", name: "wait", parameters: { type: "object", properties: { cell_id: { type: "string" } }, required: ["cell_id"] } },
  { type: "namespace", name: "collaboration", tools: [{ type: "function", name: "spawn_agent", parameters: { type: "object" } }] },
] };
const BI = { type: "message", role: "developer", id: "msg_fixture", content: [{ type: "input_text", text: "BASE" }] };
const user = (text) => ({ type: "message", role: "user", content: [{ type: "input_text", text }] });
const common = (slug) => ({ model: slug, tool_choice: "auto", parallel_tool_calls: false,
  reasoning: { effort: "medium", summary: "auto", context: "all_turns" }, store: false, stream: true,
  include: ["reasoning.encrypted_content"], prompt_cache_key: "fixture-thread" });
const template = (body) => {
  const { input: _input, previous_response_id: _previous, generate: _generate, ...rest } = body;
  return rest;
};

describe("Responses Lite Nokiy prefix", { concurrency: false }, () => {
  let fixtureHome, originalHome;
  before(() => {
    originalHome = process.env.CODEX_HOME;
    fixtureHome = mkdtempSync(join(tmpdir(), "nokiy-lite-prefix-"));
    writeFileSync(join(fixtureHome, "config.toml"), "[agents]\nmax_threads = 2\n");
    process.env.CODEX_HOME = fixtureHome;
  });
  after(() => {
    if (originalHome === undefined) delete process.env.CODEX_HOME;
    else process.env.CODEX_HOME = originalHome;
    if (fixtureHome) rmSync(fixtureHome, { recursive: true, force: true });
  });

  for (const profile of profiles) for (const [hostName, host] of Object.entries(hosts)) {
    const options = { endpoint: "/responses", catalog, profiles, host };
    const name = `${profile.slug}/${hostName}`;

    test(`${name}: receipt binds actual Lite tools, resolved projection and local policy without changing the body`, () => {
      const projected = projectNokiyDirect({ ...common(profile.slug), input: [AT, BI, user("private task")] }, options);
      const before = structuredClone(projected.body), wire = JSON.stringify(projected.body);
      const receipt = summarizeRequestProvenance({ wire, projection: projected });
      assert.deepEqual(projected.body, before);
      assert.equal(JSON.stringify(projected.body), wire);
      assert.equal(receipt.resolved.profile, profile.slug);
      assert.equal(receipt.resolved.workflow, profile.workflow);
      assert.equal(receipt.resolved.coordinator_bindings, hostName === "coordinator");
      assert.equal(receipt.wrapping.type, "custom");
      assert.equal(receipt.wrapping.name, "exec");
      assert.equal(receipt.wrapping.inner_catalog, "UNKNOWN");
      assert.ok(receipt.tools.entries.some((tool) => tool.name === "nokiy_execute"));
      assert.ok(receipt.tools.entries.every((tool) => tool.source.startsWith("/input/0/tools/")));
      assert.equal(receipt.local_nokiy_policy.sha256, projected.receiptPolicy.sha256);
      assert.equal(receipt.local_nokiy_policy.container_source, "/input/2/content");
      assert.equal(JSON.stringify(receipt).includes("private task"), false);
      const chained = projectNokiyDirect({ ...common(profile.slug), previous_response_id: "resp_fixture", input: [user("next")] },
        { ...options, continuationTarget: projected.nativeTool.target,
          continuationCoordinatorBindings: projected.nativeTool.coordinatorBindings });
      const delta = summarizeRequestProvenance({ wire: JSON.stringify(chained.body), projection: chained });
      assert.equal(delta.tools.status, "UNKNOWN");
      assert.equal(delta.instructions.status, "UNKNOWN");
      assert.equal(delta.local_nokiy_policy, "UNKNOWN");
    });

    test(`${name}: full turn carries tools and contract as prefix items, not top-level fields`, () => {
      const body = { ...common(profile.slug), input: [AT, BI, user("task")] };
      const snapshot = structuredClone(body);
      const projected = projectNokiyDirect(body, options);
      assert.deepEqual(body, snapshot);
      assert.equal(projected.dispatch, "native-tool");
      assert.equal("tools" in projected.body, false);
      assert.equal("instructions" in projected.body, false);
      const [tools, base, contract, task, ...rest] = projected.body.input;
      assert.deepEqual(rest, []);
      assert.equal(tools.type, "additional_tools");
      assert.equal(tools.id, "at_fixture");
      assert.ok(tools.tools.some((tool) => tool.name === "nokiy_execute"));
      assert.equal(tools.tools.some((tool) => tool.name === "spawn_agent" || tool.name === "collaboration"), false);
      assert.deepEqual(base, BI);
      assert.equal(contract.role, "developer");
      assert.match(contract.content[0].text, /nokiy_execute/);
      assert.deepEqual(task, user("task"));
    });

    test(`${name}: chained turn and chained compaction inherit the stored prefix`, () => {
      const first = projectNokiyDirect({ ...common(profile.slug), input: [AT, BI, user("task")] }, options);
      const continuation = { ...options, continuationTarget: first.nativeTool.target,
        continuationCoordinatorBindings: first.nativeTool.coordinatorBindings };
      const turn = projectNokiyDirect({ ...common(profile.slug), previous_response_id: "resp_1", input: [user("next")] }, continuation);
      assert.equal("tools" in turn.body, false);
      assert.equal("instructions" in turn.body, false);
      assert.deepEqual(turn.body.input, [user("next")]);
      const delta = [{ type: "custom_tool_call_output", call_id: "call_1", output: "done" }, { type: "compaction_trigger" }];
      const compaction = projectNokiyDirect({ ...common(profile.slug), previous_response_id: "resp_2", input: delta }, continuation);
      assert.equal(compaction.cacheAlignment, "lite_chained");
      assert.equal(compaction.dispatch, "none");
      assert.equal(compaction.nativeTool, null);
      assert.deepEqual(compaction.body.input, delta);
      assert.deepEqual(template(compaction.body), template(turn.body));
      // Without a bound continuation the lite delta still needs no projection.
      const unbound = projectNokiyDirect({ ...common(profile.slug), previous_response_id: "resp_3", input: delta }, options);
      assert.equal(unbound.cacheAlignment, "lite_chained");
      assert.deepEqual(template(unbound.body), template(turn.body));
    });

    test(`${name}: an unbound chained delta that advertises exec keeps top-level fields`, () => {
      const delta = { ...common(profile.slug), previous_response_id: "resp_9",
        input: [{ type: "tool_search_output", tools: AT.tools }, user("next")] };
      const projected = projectNokiyDirect(delta, options);
      assert.equal(projected.dispatch, "native-tool");
      assert.ok(Array.isArray(projected.body.tools) && projected.body.tools.some((tool) => tool.name === "nokiy_execute"));
      assert.equal(typeof projected.body.instructions, "string");
      assert.equal(projected.body.input.at(-1).role, "user");
    });

    test(`${name}: full compaction and prewarm reuse the executing prefix byte for byte`, () => {
      const history = [AT, BI, user("task"), { type: "message", role: "assistant", content: [{ type: "output_text", text: "ok" }] }];
      const turn = projectNokiyDirect({ ...common(profile.slug), input: history }, options);
      const compaction = projectNokiyDirect({ ...common(profile.slug), input: [...history, { type: "compaction_trigger" }] }, options);
      assert.equal(compaction.cacheAlignment, "lite_full");
      assert.equal(compaction.dispatch, "none");
      assert.deepEqual(compaction.body.input, [...turn.body.input, { type: "compaction_trigger" }]);
      assert.deepEqual(template(compaction.body), template(turn.body));
      const prewarm = projectNokiyDirect({ ...common(profile.slug), generate: false, input: [AT, BI] }, options);
      assert.equal(prewarm.dispatch, "none");
      assert.deepEqual(prewarm.body.input, turn.body.input.slice(0, 3));
      assert.deepEqual(template(prewarm.body), template(turn.body));
    });
  }

  test("withLitePrefix adds a tools item and keeps the contract after the native prefix", () => {
    const tool = { type: "custom", name: "nokiy_execute" };
    const input = [BI, user("task")];
    const result = withLitePrefix(input, [tool], "CONTRACT");
    assert.deepEqual(input, [BI, user("task")]);
    assert.deepEqual(result[0], { type: "additional_tools", role: "developer", tools: [tool] });
    assert.deepEqual(result[1], BI);
    assert.deepEqual(result[2], { type: "message", role: "developer", content: [{ type: "input_text", text: "CONTRACT" }] });
    assert.deepEqual(result[3], user("task"));
    const withoutBase = withLitePrefix([AT, user("task")], [tool], "CONTRACT");
    assert.equal(withoutBase[0].tools.at(-1), tool);
    assert.equal(withoutBase[1].role, "developer");
    assert.deepEqual(withoutBase[2], user("task"));
  });

  test("Efficient full and Lite receipts retain direct/custom adapter provenance and effective native mode", () => {
    const profile = { slug: "nokiy-efficient", display_name: "Efficient fixture", workflow: "nokiy-direct",
      coordinator: { model: "gpt-6.1-sol", reasoning_effort: "max" }, worker };
    const fn = { type: "function", name: "exec_command", parameters: { type: "object",
      properties: { cmd: { type: "string" } }, required: ["cmd"], additionalProperties: false } };
    for (const tool of [AT.tools[0], fn]) for (const lite of [false, true]) {
      const body = { ...common(profile.slug), reasoning: { effort: "max" },
        ...(lite ? {} : { tools: [tool], instructions: "native base" }),
        input: [...(lite ? [{ ...AT, tools: [tool] }, BI] : []), user("secret")] };
      const projected = projectNokiyDirect(body, { endpoint: "/responses", catalog, profiles: [profile], host: hosts.coordinator });
      const before = structuredClone(projected.body), wire = JSON.stringify(projected.body);
      const receipt = summarizeRequestProvenance({ wire, projection: projected });
      assert.deepEqual(projected.body, before);
      assert.equal(JSON.stringify(projected.body), wire);
      assert.equal(receipt.resolved.adaptive_native, true);
      assert.equal(receipt.resolved.coordinator_bindings, false);
      assert.equal(receipt.resolved.profile, "nokiy-efficient");
      assert.equal(receipt.wrapping.type, tool.type);
      assert.equal(receipt.wrapping.name, tool.name);
      assert.match(receipt.wrapping.execution_schema.sha256, /^[a-f0-9]{64}$/);
      assert.equal(receipt.wrapping.inner_catalog, "UNKNOWN");
      assert.equal(receipt.local_nokiy_policy.sha256, projected.receiptPolicy.sha256);
      assert.ok(receipt.tools.entries.some((entry) => entry.name === "nokiy_execute"));
      assert.ok(receipt.tools.entries.some((entry) => entry.name === tool.name));
      const removed = { ...projected.body, instructions: undefined, input: [user("spoofed Nokiy policy")] };
      assert.equal(summarizeRequestProvenance({ wire: JSON.stringify(removed), projection: projected }).local_nokiy_policy, "UNKNOWN");
    }
  });
});

