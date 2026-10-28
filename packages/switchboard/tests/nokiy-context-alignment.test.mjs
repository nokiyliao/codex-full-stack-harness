import assert from "node:assert/strict";
import test from "node:test";
import { alignWorkflowContext } from "../src/picker-catalog.mjs";
import { mergeOfficialAdditions } from "../src/official-catalog-refresh.mjs";

const workflows = { profiles: [
  { slug: "nokiy", coordinator: { model: "gpt-6-astra" } },
  { slug: "nokiy-direct", coordinator: { model: "gpt-6-astra" } },
  { slug: "nokiy-efficient", coordinator: { model: "gpt-6.1-sol" } },
] };
function fixture() {
  return { models: [
    { slug: "gpt-6-astra", context_window: 272000, max_context_window: 872000,
      effective_context_window_percent: 95, auto_compact_token_limit: null },
    { slug: "gpt-6.1-sol", context_window: 500000, max_context_window: 872000 },
    ...workflows.profiles.map(profile => ({ slug: profile.slug, context_window: 272000,
      auto_compact_token_limit: 200000, effective_context_window_percent: 80,
      default_reasoning_level: "medium", instructions: "keep", custom: 42 })),
    { slug: "gpt-6-sol", model_messages: { instructions_template: "keep" } },
    { slug: "grok-4.7", context_window: 200000 },
  ] };
}

test("each alias follows its coordinator, not its worker or maximum capacity", () => {
  const source = fixture(), before = structuredClone(source);
  const aligned = alignWorkflowContext(source, workflows);
  for (const profile of workflows.profiles) {
    const actual = aligned.models.find(row => row.slug === profile.slug);
    const anchor = source.models.find(row => row.slug === profile.coordinator.model);
    assert.equal(actual.context_window, anchor.context_window);
    assert.equal(actual.max_context_window, anchor.max_context_window);
    assert.equal(actual.auto_compact_token_limit, anchor.auto_compact_token_limit);
    assert.equal(actual.effective_context_window_percent, anchor.effective_context_window_percent);
    assert.equal(actual.instructions, "keep");
    assert.equal(actual.default_reasoning_level, "medium");
    assert.equal(actual.custom, 42);
  }
  assert.deepEqual(source, before);
  assert.deepEqual(alignWorkflowContext(aligned, workflows), aligned);
  assert.deepEqual(aligned.models.at(-1), source.models.at(-1));
});

test("refresh repairs stale aliases even when there are no official additions", () => {
  const catalog = fixture();
  const admission = { official_models: ["gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol"], local_nokiy_direct: { profiles: workflows } };
  const remote = { models: catalog.models.filter(row => row.slug.startsWith("gpt-")) };
  const result = mergeOfficialAdditions(catalog, admission, remote);
  assert.deepEqual(result.added, []);
  assert.equal(result.changed, true);
  assert.equal(result.catalog.models.find(row => row.slug === "nokiy-efficient").context_window, 500000);
  assert.equal(mergeOfficialAdditions(result.catalog, result.admission, remote).changed, false);
});

test("context follows later coordinator changes without overwriting alias policy", () => {
  const catalog = alignWorkflowContext(fixture(), workflows);
  Object.assign(catalog.models[0], { context_window: 600000, auto_compact_token_limit: 480000 });
  const result = alignWorkflowContext(catalog, workflows);
  assert.equal(result.models.find(row => row.slug === "nokiy-direct").context_window, 600000);
  assert.equal(result.models.find(row => row.slug === "nokiy-direct").auto_compact_token_limit, 480000);
  assert.equal(result.models.find(row => row.slug === "nokiy-efficient").context_window, 500000);
});

test("missing or invalid coordinator cannot be disguised as synchronized", () => {
  for (const value of [0, -1, 900000, NaN]) {
    const catalog = fixture();
    catalog.models[0].context_window = value;
    assert.throws(() => alignWorkflowContext(catalog, workflows), /Invalid coordinator/);
  }
  const catalog = fixture();
  catalog.models.shift();
  assert.throws(() => alignWorkflowContext(catalog, workflows), /Invalid coordinator/);
  assert.throws(() => alignWorkflowContext(fixture(), { profiles: [workflows.profiles[0], workflows.profiles[0]] }), /Duplicate workflow/);
  assert.deepEqual(alignWorkflowContext(fixture(), null), fixture());
});

test("an explicit profile compaction limit survives coordinator alignment", () => {
  const profiles = { profiles: workflows.profiles.map(profile => profile.slug === "nokiy-direct"
    ? { ...profile, auto_compact_token_limit: 200000 } : profile) };
  const source = fixture();
  Object.assign(source.models[0], { context_window: 500000, auto_compact_token_limit: 400000 });
  const aligned = alignWorkflowContext(source, profiles);
  const bySlug = Object.fromEntries(aligned.models.map(row => [row.slug, row]));
  assert.equal(bySlug["nokiy-direct"].auto_compact_token_limit, 200000);
  assert.equal(bySlug["nokiy-direct"].context_window, 500000, "other context fields still follow the coordinator");
  assert.equal(bySlug.nokiy.auto_compact_token_limit, 400000);
  assert.equal(bySlug["gpt-6-astra"].auto_compact_token_limit, 400000);
  assert.deepEqual(alignWorkflowContext(aligned, profiles), aligned);
  for (const bad of [50_000, 500_000, 1.5, "200000"]) {
    const invalid = { profiles: profiles.profiles.map(profile => profile.slug === "nokiy-direct"
      ? { ...profile, auto_compact_token_limit: bad } : profile) };
    assert.throws(() => alignWorkflowContext(source, invalid), /Invalid compaction limit/, String(bad));
  }
});
