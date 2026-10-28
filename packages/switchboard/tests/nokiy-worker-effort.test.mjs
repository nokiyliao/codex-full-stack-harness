import assert from "node:assert/strict";
import { test } from "node:test";
import { directCatalogEntries, projectNokiyDirect } from "../src/nokiy-direct-route.mjs";
import { resolveWorkflowProfiles } from "../src/nokiy-workflow-profiles.mjs";
import { nativeToolItem } from "../src/nokiy-native-tool.mjs";
import { readFileSync } from "node:fs";

const catalog = { models: ["gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"].map((slug) => ({ slug,
  supported_reasoning_levels: ["low", "medium", "high", "xhigh", "max", "ultra"].map((effort) => ({ effort })),
  multi_agent_reasoning_effort: "xhigh", supports_parallel_tool_calls: true,
})) };
const profiles = [
  { slug: "nokiy", display_name: "Nokiy", workflow: "nokiy", coordinator: { model: "gpt-6-astra", reasoning_effort: "xhigh" } },
  { slug: "nokiy-direct", display_name: "Nokiy Ultra", workflow: "nokiy-direct", coordinator: { model: "gpt-6-astra", reasoning_effort: "max" } },
  { slug: "nokiy-efficient", display_name: "Nokiy Efficient", workflow: "nokiy-direct", coordinator: { model: "gpt-6.1-sol", reasoning_effort: "max" } },
].map((profile) => ({ ...profile,
  worker: { model: "gpt-6.1-sol", reasoning_effort: "max", service_tier: "default" },
  worker_selector: { default_effort: "medium", models: { low: "gpt-6-luna", medium: "gpt-6.1-sol", high: "gpt-6-astra" },
    reasoning_efforts: { low: "max", medium: "max", high: "xhigh" } },
}));
const host = { python: "/never/executed/python", script: "/never/executed/host.py", coordinator_mode: "dispatch_only_v1" };
const request = (model, effort) => ({ model, reasoning: { effort }, parallel_tool_calls: true, input: [],
  tools: [{ type: "namespace", name: "functions", tools: [{ type: "function", name: "exec_command",
    parameters: { type: "object", properties: { cmd: { type: "string" } }, required: ["cmd"] } }] }] });

for (const profile of profiles) {
  for (const selection of ["low", "medium", "high"]) {
    test(`${profile.slug}/${selection}: coordinator and worker efforts remain independently bound through native envelope`, () => {
      const body = request(profile.slug, selection), snapshot = structuredClone(body);
      const projected = projectNokiyDirect(body, { catalog, profiles, host });
      const worker = profile.worker_selector.models[selection];
      const effort = profile.worker_selector.reasoning_efforts[selection];
      assert.equal(projected.body.model, profile.coordinator.model);
      assert.equal(projected.body.reasoning.effort, profile.coordinator.reasoning_effort === "ultra" ? "xhigh" : profile.coordinator.reasoning_effort);
      assert.equal(projected.nativeTool.worker, worker);
      assert.equal(projected.nativeTool.workerEffort, effort);
      const item = nativeToolItem({ type: "function_call", name: "nokiy_execute", call_id: "effort_binding",
        arguments: '{"operation":"read_result","artifact_root":"/original","request_id":"original_request"}' }, projected.nativeTool);
      const command = JSON.parse(item.arguments).cmd;
      const envelope = JSON.parse(Buffer.from(command.match(/'--payload' '([A-Za-z0-9_-]+)'/)[1], "base64url"));
      assert.equal(envelope.worker, worker);
      assert.equal(envelope.reasoning_effort, effort);
      assert.equal(envelope.service_tier, "default");
      assert.equal(item.call_id, "effort_binding");
      assert.deepEqual(body, snapshot);
      assert.equal(projected.body.parallel_tool_calls, profile.workflow === "nokiy" ? false : true);
      const entries = directCatalogEntries(catalog, profiles);
      const row = entries.find((row) => row.slug === profile.slug);
      assert.match(row.description, /Luna\/max, Sol\/max, Astra\/xhigh/);
      assert.match(row.supported_reasoning_levels.find((level) => level.effort === selection).description,
        new RegExp(`${worker} / ${effort} worker`));
    });
  }
}

test("Astra/max retains concurrent batch operations and capacity; parallelism is not an ultra-effort permission", () => {
  const next = projectNokiyDirect(request("nokiy-direct", "medium"), { catalog, profiles, host });
  const prior = structuredClone(profiles);
  prior[1].coordinator.reasoning_effort = "ultra";
  const before = projectNokiyDirect(request("nokiy-direct", "medium"), { catalog, profiles: prior, host });
  assert.ok(next.nativeTool.operations.includes("run_batch"));
  assert.deepEqual(next.nativeTool.operations, before.nativeTool.operations);
  const schema = (result) => result.body.tools.find((tool) => tool.name === "nokiy_execute").parameters;
  assert.deepEqual(schema(next).properties.members, schema(before).properties.members);
  assert.deepEqual(schema(next).properties.max_parallel_workers, schema(before).properties.max_parallel_workers);
  assert.equal(next.body.reasoning.effort, "max");
});

test("legacy selector configuration retains max for every worker without adding schema fields", () => {
  const legacy = structuredClone(profiles);
  for (const profile of legacy) delete profile.worker_selector.reasoning_efforts;
  for (const selection of ["low", "medium", "high"]) {
    const projected = projectNokiyDirect(request("nokiy", selection), { catalog, profiles: legacy, host });
    assert.equal(projected.nativeTool.workerEffort, "max");
    assert.equal(projected.profile.worker_selector.reasoning_efforts, undefined);
  }
  assert.match(directCatalogEntries(catalog, legacy)[0].description, /Luna\/Sol\/Astra workers at max/);
});

test("selector effort declarations remain exact and catalog-validated, including default consistency", () => {
  const mutations = [
    (profile) => { profile.worker_selector.reasoning_efforts.high = "unknown"; },
    (profile) => { profile.worker_selector.reasoning_efforts.high = null; },
    (profile) => { profile.worker_selector.reasoning_efforts.high = 0; },
    (profile) => { delete profile.worker_selector.reasoning_efforts.high; },
    (profile) => { profile.worker_selector.reasoning_efforts.extra = "max"; },
    (profile) => { profile.worker_selector.reasoning_efforts = []; },
    (profile) => { profile.worker_selector.reasoning_efforts.medium = "xhigh"; },
    (profile) => { profile.coordinator.reasoning_effort = "unknown"; },
  ];
  for (const mutate of mutations) {
    const invalid = structuredClone(profiles);
    mutate(invalid[0]);
    assert.throws(() => resolveWorkflowProfiles(invalid, catalog));
  }
  const unsupported = structuredClone(catalog);
  unsupported.models.find((model) => model.slug === "gpt-6-astra").supported_reasoning_levels = [{ effort: "max" }];
  assert.throws(() => resolveWorkflowProfiles(profiles, unsupported));
  const resolved = resolveWorkflowProfiles(profiles, catalog);
  assert.ok(Object.isFrozen(resolved[0].worker_selector.reasoning_efforts));
});

test("compact and prewarm use configured coordinator effort without changing worker selection", () => {
  for (const model of ["nokiy-direct", "nokiy-efficient"]) {
    for (const variant of [{ endpoint: "/responses/compact" }, { endpoint: "/responses", generate: false }]) {
      const body = { model, generate: variant.generate, input: [], tools: [] };
      const projected = projectNokiyDirect(body, { catalog, profiles, host, endpoint: variant.endpoint });
      assert.equal(projected.dispatch, "none");
      assert.equal(projected.nativeTool, null);
      assert.equal(projected.body.model, profiles.find((profile) => profile.slug === model).coordinator.model);
      assert.equal(projected.body.reasoning.effort, "max");
      assert.equal(projected.profile.worker.model, "gpt-6.1-sol");
      assert.equal(projected.profile.worker.reasoning_effort, "max");
    }
  }
});

test("Efficient legacy picker efforts retain Sol 6.1/max for both coordinator and default worker", () => {
  for (const effort of ["max", "ultra", "xhigh"]) {
    const body = request("nokiy-efficient", effort), snapshot = structuredClone(body);
    const projected = projectNokiyDirect(body, { catalog, profiles, host });
    assert.equal(projected.body.model, "gpt-6.1-sol");
    assert.equal(projected.body.reasoning.effort, "max");
    assert.equal(projected.nativeTool.worker, "gpt-6.1-sol");
    assert.equal(projected.nativeTool.workerEffort, "max");
    assert.deepEqual(body, snapshot);
  }
});

test("four picker levels bind Luna, Terra, Sol and Astra through the native envelope for every mode", () => {
  const configured = JSON.parse(readFileSync(new URL("../config/nokiy-workflows.json", import.meta.url)));
  const fourCatalog = structuredClone(catalog);
  for (const row of fourCatalog.models) row.context_window = 500000;
  fourCatalog.models.push({ ...structuredClone(catalog.models[1]), slug: "gpt-6.1-terra" });
  const expected = { low: "gpt-6-luna", medium: "gpt-6.1-terra", high: "gpt-6.1-sol", xhigh: "gpt-6-astra" };
  for (const profile of configured.profiles) {
    const row = directCatalogEntries(fourCatalog, configured).find(row => row.slug === profile.slug);
    assert.equal(row.default_reasoning_level, "medium");
    assert.deepEqual(row.supported_reasoning_levels.map(level => level.effort), Object.keys(expected));
    assert.match(row.description, /picker low\/medium\/high\/xhigh dispatches/);
    for (const [selection, worker] of Object.entries(expected)) {
      const projected = projectNokiyDirect(request(profile.slug, selection), { catalog: fourCatalog, profiles: configured, host });
      assert.equal(projected.body.model, profile.coordinator.model);
      assert.equal(projected.body.reasoning.effort, profile.coordinator.reasoning_effort);
      assert.equal(projected.nativeTool.worker, worker);
      assert.equal(projected.nativeTool.workerEffort, profile.worker_selector.reasoning_efforts[selection]);
      const item = nativeToolItem({ type: "function_call", name: "nokiy_execute", call_id: "terra_binding",
        arguments: '{"operation":"read_result","artifact_root":"/original","request_id":"original_request"}' }, projected.nativeTool);
      const envelope = JSON.parse(Buffer.from(JSON.parse(item.arguments).cmd.match(/'--payload' '([A-Za-z0-9_-]+)'/)[1], "base64url"));
      assert.equal(envelope.worker, worker);
      assert.equal(envelope.reasoning_effort, profile.worker_selector.reasoning_efforts[selection]);
      assert.equal(envelope.service_tier, "default");
    }
    for (const legacy of ["max", "ultra"]) {
      assert.equal(projectNokiyDirect(request(profile.slug, legacy), { catalog: fourCatalog, profiles: configured, host }).nativeTool.worker, "gpt-6.1-terra");
    }
  }
  const invalid = structuredClone(configured);
  invalid.profiles[0].worker_selector.models.medium = "gpt-6.1-sol";
  assert.throws(() => resolveWorkflowProfiles(invalid, fourCatalog));
  const unavailable = structuredClone(fourCatalog);
  unavailable.models = unavailable.models.filter(model => model.slug !== "gpt-6.1-terra");
  assert.throws(() => resolveWorkflowProfiles(configured, unavailable));
});
