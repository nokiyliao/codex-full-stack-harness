import assert from "node:assert/strict";
import { test } from "node:test";
import { withDirectCatalog } from "../src/nokiy-direct-route.mjs";
import { resolveWorkflowProfiles } from "../src/nokiy-workflow-profiles.mjs";
import { alignWorkflowContext } from "../src/picker-catalog.mjs";

const levels = ["low", "medium", "high", "xhigh", "max", "ultra"].map((effort) => ({ effort }));
const catalog = { models: ["gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"].map((slug) => ({ slug, supported_reasoning_levels: levels,
  multi_agent_reasoning_effort: "xhigh", supports_parallel_tool_calls: true, context_window: 500000,
  ...(slug === "gpt-6-astra" ? { auto_compact_token_limit: 400000 } : {}) })) };
const selector = { default_effort: "medium", models: { low: "gpt-6-luna", medium: "gpt-6.1-sol", high: "gpt-6-astra" },
  reasoning_efforts: { low: "max", medium: "max", high: "xhigh" } };
const profile = (slug, workflow, extra = {}) => ({ slug, display_name: slug, workflow,
  coordinator: { model: "gpt-6-astra", reasoning_effort: "max" },
  worker: { model: "gpt-6.1-sol", reasoning_effort: "max", service_tier: "default" }, worker_selector: selector, ...extra });
const config = (extra) => ({ schema_version: "nokiy_workflow_profiles_v1",
  profiles: [profile("nokiy", "nokiy"), profile("nokiy-direct", "nokiy-direct", extra)] });

test("a profile compaction limit overrides only its own catalog entry", () => {
  const models = Object.fromEntries(withDirectCatalog(catalog, config({ auto_compact_token_limit: 272000 }))
    .models.map((model) => [model.slug, model]));
  assert.equal(models["nokiy-direct"].auto_compact_token_limit, 272000);
  assert.equal(models.nokiy.auto_compact_token_limit, 400000, "serial Nokiy keeps the Astra anchor limit");
  assert.equal(models["gpt-6-astra"].auto_compact_token_limit, 400000, "the native model is unchanged");
  assert.equal(resolveWorkflowProfiles(config({ auto_compact_token_limit: 272000 }), catalog)[1].auto_compact_token_limit, 272000);
  assert.equal(Object.hasOwn(resolveWorkflowProfiles(config({}), catalog)[1], "auto_compact_token_limit"), false);
});

test("compaction limits must be integers below the coordinator context window", () => {
  for (const bad of [50_000, 500_000, 600_000, 272000.5, "272000", null]) {
    assert.throws(() => resolveWorkflowProfiles(config({ auto_compact_token_limit: bad }), catalog), TypeError, String(bad));
  }
});

// Admission resolves profiles against the official bundled projection (Astra 272K window,
// 872K maximum) before operator overlays restore the served 500K window.
test("profiles admit against the official projection; the served window is enforced at alignment", () => {
  const official = { models: catalog.models.map((model) => ({ ...model, context_window: 272000, max_context_window: 872000 })) };
  const entry = withDirectCatalog(official, config({ auto_compact_token_limit: 272000 }))
    .models.find((model) => model.slug === "nokiy-direct");
  assert.equal(entry.auto_compact_token_limit, 272000);
  assert.throws(() => resolveWorkflowProfiles(config({ auto_compact_token_limit: 872000 }), official), TypeError);
  const served = { models: official.models.map((model) => ({ ...model, context_window: 500000 })) };
  const workflows = config({ auto_compact_token_limit: 272000 });
  const aligned = alignWorkflowContext({ models: [...served.models, entry] }, workflows);
  assert.equal(aligned.models.find((model) => model.slug === "nokiy-direct").auto_compact_token_limit, 272000);
  assert.throws(() => alignWorkflowContext({ models: [...official.models, entry] }, workflows),
    (error) => error.code === "PICKER_WORKFLOW_CONTEXT_INVALID");
});
