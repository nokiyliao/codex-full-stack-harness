import assert from "node:assert/strict";
import test from "node:test";

import {
  AGY_GEMINI_AUTO_COMPACT_LIMIT,
  AGY_GEMINI_CONTEXT_WINDOW,
  AGY_MILLION_CONTEXT_MODELS,
  ASTRA_MODEL,
  ASTRA_PICKER_VISIBILITY,
  admittedOfficialAdditions,
  WEB_AUTO_COMPACT_LIMIT,
  EXPECTED_TEAM_ID,
  SOL_CONFIGURED_CONTEXT_WINDOW,
  SOL_CONTEXT_MODEL,
  SOL_EFFECTIVE_CONTEXT_WINDOW,
  SOL_MAX_CONTEXT_WINDOW,
  CURRENT_SOL_CONTEXT_MODEL,
  CURRENT_SOL_DEFAULT_CONTEXT_WINDOW,
  buildCandidateCatalog,
  catalogCompatibility,
  configuredWorkflows,
  externalBatchComplete,
  inspectOfficialInstallation,
  withCliRequiredModelFields,
  withOfficialDefaultContext,
} from "../src/catalog.mjs";
import {
  AGY_MODELS,
  EXTERNAL_MODELS,
  EXTERNAL_MULTI_AGENT_MODELS,
  WEB_MODELS,
} from "../src/shared.mjs";

function assertCatalogPartitions(models, expectedExternalSlugs, expectedOfficial, workflows) {
  const expectedAliasSlugs = workflows?.profiles.map(({ slug }) => slug) ?? [];
  const officialEnd = expectedExternalSlugs.length + expectedOfficial.length;
  assert.equal(models.length, officialEnd + expectedAliasSlugs.length,
    "catalog must contain exactly external, official, and configured alias entries");
  assert.equal(new Set(models.map((model) => model.slug)).size, models.length,
    "catalog model slugs must be unique");
  const external = models.slice(0, expectedExternalSlugs.length);
  const copiedOfficial = models.slice(expectedExternalSlugs.length, officialEnd);
  const aliases = models.slice(officialEnd);
  assert.deepEqual(external.map((model) => model.slug), expectedExternalSlugs);
  assert.deepEqual(copiedOfficial, expectedOfficial);
  assert.deepEqual(aliases.map((model) => model.slug), expectedAliasSlugs);
  return { external, copiedOfficial };
}

test("candidate catalog copies installed official objects exactly and admits the complete external batch", async () => {
  const official = await inspectOfficialInstallation();
  assert.equal(official.team_id, EXPECTED_TEAM_ID);
  assert.equal(official.notarized, true);
  const candidate = await buildCandidateCatalog(official);
  const workflows = await configuredWorkflows();
  const expectedOfficial = structuredClone(official.bundled_catalog.models)
    .map(withCliRequiredModelFields).map(withOfficialDefaultContext);
  const expectedSol = expectedOfficial.find((model) => model.slug === SOL_CONTEXT_MODEL);
  expectedSol.max_context_window = SOL_MAX_CONTEXT_WINDOW;
  const expectedAstra = expectedOfficial.find((model) => model.slug === ASTRA_MODEL);
  if (expectedAstra) expectedAstra.visibility = ASTRA_PICKER_VISIBILITY;
  expectedOfficial.push(...await admittedOfficialAdditions(official.bundled_catalog));
  const { external, copiedOfficial } = assertCatalogPartitions(
    candidate.models, EXTERNAL_MODELS, expectedOfficial, workflows);
  assert.ok(candidate.models.every((model) => typeof model.supports_parallel_tool_calls === "boolean"));
  assert.deepEqual(external.map((model) => model.slug), EXTERNAL_MODELS);
  assert.ok(external
    .filter((model) => EXTERNAL_MULTI_AGENT_MODELS.includes(model.slug))
    .every((model) => model.multi_agent_version === "v2"));
  assert.ok(external.every((model) => model.supports_search_tool === true));
  assert.ok(external.every((model) => Array.isArray(model.service_tiers) && model.service_tiers.length === 0));
  const grok = external.find((model) => model.slug === "grok-4.7");
  assert.equal(grok.shell_type, copiedOfficial.find((model) => model.slug === SOL_CONTEXT_MODEL).shell_type);
  assert.equal(grok.supports_parallel_tool_calls, true);
  assert.equal(grok.tool_mode, "code_mode_only");
  assert.equal(grok.multi_agent_version, undefined);
  const web = external.filter((model) => WEB_MODELS.includes(model.slug));
  assert.deepEqual(web.map((model) => model.slug), WEB_MODELS);
  assert.equal(WEB_AUTO_COMPACT_LIMIT, 250_000);
  assert.ok(web.every((model) => model.auto_compact_token_limit === WEB_AUTO_COMPACT_LIMIT));
  assert.ok(web.every((model) => model.auto_compact_token_limit
    < Math.floor(model.context_window * model.effective_context_window_percent / 100)));
  assert.ok(web.every((model) => model.supports_parallel_tool_calls === true));
  assert.ok(web.every((model) => model.multi_agent_version === "v1"));
  assert.equal(web.find((model) => model.slug === "chatgpt-web/high").default_reasoning_level, "high");
  assert.equal(web.find((model) => model.slug === "chatgpt-web/pro").default_reasoning_level, "ultra");
  assert.equal(external.some((model) => model.slug === "stealth/ox-alpha"), false);
  assert.equal(externalBatchComplete(external), true);
  assert.equal(externalBatchComplete(external.filter((model) => model.slug !== "grok-4.7")), false);
  const agy = external.filter((model) => AGY_MODELS.includes(model.slug));
  assert.deepEqual(agy.map((model) => model.slug), AGY_MODELS);
  assert.ok(agy.every((model) => model.supports_parallel_tool_calls === false));
  assert.ok(agy.every((model) => model.display_name === model.slug));
  assert.ok(agy.every((model) => model.description === model.slug));
  assert.equal(external.some((model) => model.slug.startsWith("grok-agy-")), false);
  assert.equal(external.some((model) => model.display_name?.startsWith("AGY -")), false);
  for (const slug of AGY_MILLION_CONTEXT_MODELS) {
    const model = external.find((candidate) => candidate.slug === slug);
    assert.equal(model.context_window, AGY_GEMINI_CONTEXT_WINDOW);
    assert.equal(model.max_context_window, AGY_GEMINI_CONTEXT_WINDOW);
    assert.equal(model.effective_context_window_percent, 95);
    assert.equal(model.auto_compact_token_limit, AGY_GEMINI_AUTO_COMPACT_LIMIT);
    assert.deepEqual(model.truncation_policy, { mode: "tokens", limit: 10_000 });
  }
  const sol = copiedOfficial.find((model) => model.slug === SOL_CONTEXT_MODEL);
  const currentSol = copiedOfficial.find((model) => model.slug === CURRENT_SOL_CONTEXT_MODEL);
  assert.equal(currentSol.context_window, 500000);
  assert.equal(CURRENT_SOL_DEFAULT_CONTEXT_WINDOW, 500000);
  assert.equal(currentSol.max_context_window,
    official.bundled_catalog.models.find(model => model.slug === CURRENT_SOL_CONTEXT_MODEL).max_context_window);
  assert.equal(sol.max_context_window, SOL_MAX_CONTEXT_WINDOW);
  assert.equal(
    (Math.min(SOL_CONFIGURED_CONTEXT_WINDOW, sol.max_context_window)
      * sol.effective_context_window_percent) / 100 | 0,
    SOL_EFFECTIVE_CONTEXT_WINDOW,
  );
  if (expectedAstra) {
    assert.equal(copiedOfficial.find((model) => model.slug === ASTRA_MODEL).visibility, ASTRA_PICKER_VISIBILITY);
  }
  assert.deepEqual(catalogCompatibility(candidate.models, official.bundled_catalog.models), {
    official_anchor_contract_compatible: true,
    external_client_contract_compatible: true,
  });
});

test("catalog partition checks support absent workflows and reject synthetic corruption", () => {
  const externalSlugs = ["synthetic-external"];
  const expectedOfficial = [{ slug: "synthetic-official", supports_parallel_tool_calls: true }];
  const workflows = { profiles: [{ slug: "workflow-a" }, { slug: "workflow-b" }] };
  const valid = [
    { slug: externalSlugs[0] },
    ...structuredClone(expectedOfficial),
    ...workflows.profiles.map(({ slug }) => ({ slug })),
  ];
  const check = (models) => assertCatalogPartitions(models, externalSlugs, expectedOfficial, workflows);
  check(valid);
  assertCatalogPartitions(
    valid.slice(0, externalSlugs.length + expectedOfficial.length), externalSlugs, expectedOfficial, null);
  assert.throws(() => assertCatalogPartitions(valid, externalSlugs, expectedOfficial, null),
    assert.AssertionError, "unconfigured aliases must not be ignored");

  const modifiedOfficial = structuredClone(valid);
  modifiedOfficial[externalSlugs.length].supports_parallel_tool_calls = false;
  const corruptions = [
    ["modified official object", modifiedOfficial],
    ["missing alias", valid.slice(0, -1)],
    ["duplicate alias", [...valid, { ...valid.at(-1) }]],
    ["duplicate alias replacing expected alias", [...valid.slice(0, -1), { ...valid.at(-2) }]],
    ["unknown alias", [...valid, { slug: "workflow-unknown" }]],
    ["unknown alias replacing expected alias", [...valid.slice(0, -1), { slug: "workflow-unknown" }]],
  ];
  for (const [description, models] of corruptions) {
    assert.throws(() => check(models), assert.AssertionError, description);
  }
});

test("catalog compatibility ignores presentation drift but rejects client tool-contract drift", async () => {
  const official = await inspectOfficialInstallation();
  const candidate = await buildCandidateCatalog(official);
  const presentationOnly = structuredClone(official.bundled_catalog.models);
  presentationOnly[0].description = "presentation-only change";
  presentationOnly[0].priority += 100;
  presentationOnly.push({
    ...structuredClone(presentationOnly[0]),
    slug: "new-official-model",
  });
  assert.deepEqual(catalogCompatibility(candidate.models, presentationOnly), {
    official_anchor_contract_compatible: true,
    external_client_contract_compatible: true,
  });

  const incompatible = structuredClone(official.bundled_catalog.models);
  incompatible.find((model) => model.slug === SOL_CONTEXT_MODEL).shell_type = "future_incompatible_exec";
  assert.deepEqual(catalogCompatibility(candidate.models, incompatible), {
    official_anchor_contract_compatible: false,
    external_client_contract_compatible: false,
  });

  const incomplete = candidate.models.filter((model) => model.slug !== "grok-4.7");
  assert.deepEqual(catalogCompatibility(incomplete, official.bundled_catalog.models), {
    official_anchor_contract_compatible: true,
    external_client_contract_compatible: false,
  });

  const missingAnchor = official.bundled_catalog.models
    .filter((model) => model.slug !== SOL_CONTEXT_MODEL);
  assert.deepEqual(catalogCompatibility(candidate.models, missingAnchor), {
    official_anchor_contract_compatible: false,
    external_client_contract_compatible: false,
  });
});
