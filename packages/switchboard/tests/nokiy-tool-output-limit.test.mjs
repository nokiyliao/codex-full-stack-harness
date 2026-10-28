import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { withDirectCatalog } from "../src/nokiy-direct-route.mjs";
import { resolveWorkflowProfiles } from "../src/nokiy-workflow-profiles.mjs";

const levels = ["low", "medium", "high", "xhigh", "max", "ultra"].map((effort) => ({ effort }));
const anchorPolicy = { mode: "tokens", limit: 10_000 };
const catalog = { models: ["gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"].map((slug) => ({ slug, supported_reasoning_levels: levels,
  multi_agent_reasoning_effort: "xhigh", supports_parallel_tool_calls: true, context_window: 500000,
  truncation_policy: anchorPolicy })) };
const selector = { default_effort: "medium", models: { low: "gpt-6-luna", medium: "gpt-6.1-sol", high: "gpt-6-astra" },
  reasoning_efforts: { low: "max", medium: "max", high: "xhigh" } };
const profile = (slug, workflow, extra = {}) => ({ slug, display_name: slug, workflow,
  coordinator: { model: "gpt-6-astra", reasoning_effort: "max" },
  worker: { model: "gpt-6.1-sol", reasoning_effort: "max", service_tier: "default" }, worker_selector: selector, ...extra });
const config = (extra) => ({ schema_version: "nokiy_workflow_profiles_v1",
  profiles: [profile("nokiy", "nokiy"), profile("nokiy-direct", "nokiy-direct", extra)] });

test("a profile tool output limit raises only its own catalog truncation policy", () => {
  const models = Object.fromEntries(withDirectCatalog(catalog, config({ tool_output_token_limit: 64000 }))
    .models.map((model) => [model.slug, model]));
  assert.deepEqual(models["nokiy-direct"].truncation_policy, { mode: "tokens", limit: 64000 });
  assert.deepEqual(models.nokiy.truncation_policy, anchorPolicy, "an alias without the field keeps its anchor policy");
  assert.deepEqual(models["gpt-6-astra"].truncation_policy, anchorPolicy, "the native model is unchanged");
  assert.equal(Object.hasOwn(resolveWorkflowProfiles(config({}), catalog)[1], "tool_output_token_limit"), false);
});

test("tool output limits must be integers from 10000 to 262144", () => {
  for (const bad of [0, 9_999, 262_145, 64000.5, "64000", null]) {
    assert.throws(() => resolveWorkflowProfiles(config({ tool_output_token_limit: bad }), catalog), TypeError, String(bad));
  }
  for (const limit of [10_000, 64_000, 262_144]) {
    assert.equal(resolveWorkflowProfiles(config({ tool_output_token_limit: limit }), catalog)[1].tool_output_token_limit, limit);
  }
});

// Codex shows min(requested max_output_tokens, truncation_policy). The route asks result-reading
// cells for at least 64000 tokens, so every installed alias must admit that budget.
test("installed profiles admit the guided result-reading budget", async () => {
  const installed = JSON.parse(await readFile(new URL("../config/nokiy-workflows.json", import.meta.url), "utf8"));
  for (const item of installed.profiles) assert.ok(item.tool_output_token_limit >= 64000, item.slug);
});
