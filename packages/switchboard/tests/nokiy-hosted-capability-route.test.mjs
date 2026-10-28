import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { projectNokiyDirect } from "../src/nokiy-direct-route.mjs";
import { endpointFromPath } from "../src/shared.mjs";

// Synthetic catalog and host descriptors only; these tests never open sockets,
// execute a host/worker, or read native capacity configuration.
const catalog = {
  models: ["gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"].map((slug) => ({
    slug,
    supported_reasoning_levels: ["low", "medium", "high", "max", "xhigh", "ultra"]
      .map((effort) => ({ effort })),
    multi_agent_reasoning_effort: "xhigh",
  })),
};
const fixed = {
  slug: "nokiy-direct-sol", display_name: "Fixed offline fixture", workflow: "nokiy-direct",
  coordinator: { model: "gpt-6-astra", reasoning_effort: "ultra" },
  worker: { model: "gpt-6.1-sol", reasoning_effort: "max", service_tier: "default" },
};
const selected = {
  ...fixed, slug: "nokiy-direct", display_name: "Picker offline fixture",
  worker_selector: {
    default_effort: "medium",
    models: { low: "gpt-6-luna", medium: "gpt-6.1-sol", high: "gpt-6-astra" },
  },
};
const profiles = [
  selected,
  { ...selected, slug: "nokiy", workflow: "nokiy" },
  fixed,
  { ...selected, slug: "nokiy-efficient",
    coordinator: { model: "gpt-6.1-sol", reasoning_effort: "ultra" } },
];
const options = { catalog, profiles, host: null };
// POST capabilities in endpointFromPath's allowlist. /models is a separate
// catalog-only GET route in server.mjs, not a body-bearing alias projection.
const auxiliaryEndpoints = [
  "/alpha/search", "/memories/trace_summarize", "/realtime/calls",
  "/images/generations", "/images/edits",
];

function assertAuxiliary(body, endpoint, profile, overrides = {}) {
  const snapshot = structuredClone(body);
  const result = projectNokiyDirect(body, { ...options, ...overrides, endpoint });
  assert.deepEqual(body, snapshot);
  assert.notEqual(result.body, body);
  assert.deepEqual(result.body, { ...snapshot, model: profile.coordinator.model });
  for (const key of Object.keys(body).filter((key) => key !== "model")) {
    assert.equal(result.body[key], body[key], `${key} is not copied or rewritten`);
  }
  assert.equal(result.requestedAlias, profile.slug);
  assert.equal(result.profile.slug, profile.slug);
  assert.deepEqual(result.profile.coordinator, profile.coordinator);
  assert.deepEqual(result.profile.worker, profile.worker);
  assert.deepEqual(result.profile.worker_selector, profile.worker_selector);
  assert.ok(Object.isFrozen(result.profile));
  assert.equal(result.dispatch, "none");
  assert.equal(result.nativeTool, null);
  assert.equal(result.worker, null);
  assert.equal(result.wireEffort, null);
  assert.equal(result.nativeEffortObserved, null);
}

describe("Nokiy official auxiliary capability projection", () => {
  test("nokiy-direct hosted search needs no worker picker effort or native tool", () => {
    // Minimal reproducer for the missing-effort failure of request
    // 23551a7f-a14d-4045-8e3e-657835b2eee1, not a copy of its private payload.
    assertAuxiliary({ model: "nokiy-direct", queries: [{ query: "offline routing regression" }] },
      "/alpha/search", selected);
  });

  for (const profile of profiles) {
    for (const endpoint of auxiliaryEndpoints) {
      test(`${profile.slug}: ${endpoint} maps only the alias without inference fields`, () => {
        assert.equal(endpointFromPath(`/offline/backend-api/codex/v1${endpoint}`, "offline"), endpoint);
        assertAuxiliary({ model: profile.slug }, endpoint, profile);
      });
    }
  }

  test("auxiliary payloads remain opaque, including inference-shaped fields and native tools", () => {
    const beforeProfiles = structuredClone(profiles);
    const beforeCatalog = structuredClone(catalog);
    for (const profile of profiles) {
      for (const endpoint of auxiliaryEndpoints) {
        const body = {
          model: profile.slug, instructions: { opaque: "auxiliary instructions" },
          reasoning: { effort: "high", summary: "auto" }, reasoning_effort: "low",
          service_tier: "priority", parallel_tool_calls: true,
          tools: [
            { type: "function", name: "exec_command", parameters: { type: "object" } },
            { type: "function", name: "nokiy_execute", parameters: { type: "object" } },
            { type: "namespace", name: "collaboration", tools: [{ type: "function", name: "spawn_agent" }] },
          ],
          tool_choice: { type: "function", name: "spawn_agent" },
          input: [{ type: "additional_tools", tools: [{ type: "function", name: "resume_agent" }] }],
          metadata: { fixture: "offline" }, prompt: "preserve auxiliary content",
        };
        assertAuxiliary(body, endpoint, profile, {
          host: { python: "/synthetic/python", script: "/synthetic/host.py" },
          continuationTarget: { namespace: "functions", name: "exec_command", type: "function" },
        });
      }
    }
    assert.deepEqual(profiles, beforeProfiles);
    assert.deepEqual(catalog, beforeCatalog);
  });

  test("prewarm/compaction flags and non-object reasoning are not interpreted on auxiliary requests", () => {
    for (const endpoint of auxiliaryEndpoints) {
      for (const reasoning of [null, "opaque", ["high"]]) {
        assertAuxiliary({ model: selected.slug, reasoning, generate: false,
          input: [{ type: "compaction_trigger" }], parallel_tool_calls: true }, endpoint, selected);
      }
    }
  });

  test("auxiliary projection still validates the full profile against the catalog", () => {
    const mutations = [
      (profile) => { profile.worker.model = "unknown-worker"; },
      (profile) => { profile.worker.reasoning_effort = "unknown-effort"; },
      (profile) => { profile.worker.service_tier = "fast"; },
      (profile) => { profile.coordinator.reasoning_effort = "unknown-effort"; },
      (profile) => { profile.worker_selector.default_effort = "high"; },
      (profile) => { profile.worker_selector.models.low = "nokiy-direct"; },
    ];
    for (const mutate of mutations) {
      const invalid = structuredClone(selected);
      mutate(invalid);
      assert.throws(() => projectNokiyDirect({ model: selected.slug }, {
        ...options, profiles: [invalid], endpoint: "/alpha/search",
      }), { code: "NOKIY_WORKFLOW_PROFILE_INVALID", status: 400 });
    }
    for (const invalidCatalog of [null, { models: catalog.models.filter((item) => item.slug !== "gpt-6-astra") }]) {
      assert.throws(() => projectNokiyDirect({ model: selected.slug }, {
        ...options, catalog: invalidCatalog, endpoint: "/alpha/search",
      }), { code: "NOKIY_WORKFLOW_PROFILE_INVALID", status: 400 });
    }
  });

  test("unknown or removed Nokiy modes remain rejected", () => {
    for (const model of ["nokiy-direct-unknown", "nokiy/retired", "nokiy-fast"]) {
      assert.throws(() => projectNokiyDirect({ model }, { ...options, endpoint: "/alpha/search" }),
        { code: "NOKIY_MODE_UNSUPPORTED", status: 400 });
    }
    assert.throws(() => projectNokiyDirect({ model: selected.slug }, {
      ...options, profiles: [fixed], endpoint: "/alpha/search",
    }), { code: "NOKIY_MODE_UNSUPPORTED", status: 400 });
  });

  test("unlisted endpoints are not auxiliary, even with non-execution flags", () => {
    for (const endpoint of ["/alpha/search/retired", "/responses/stream", "/responses/compact/extra",
      "/completions", "/chat/completions", "/embeddings", "", null]) {
      assert.throws(() => projectNokiyDirect({ model: selected.slug, generate: false,
        input: [{ type: "compaction_trigger" }] }, { ...options, endpoint }),
      { code: "ENDPOINT_NOT_SUPPORTED", status: 404 });
    }
  });

  test("auxiliary projection still requires a request object", () => {
    const body = Object.assign([], { model: selected.slug });
    assert.throws(() => projectNokiyDirect(body, { ...options, endpoint: "/alpha/search" }),
      { code: "NOKIY_DIRECT_INVALID_REQUEST", status: 400 });
  });

  test("ordinary routing never depends on auxiliary endpoint or workflow configuration", () => {
    for (const model of ["gpt-6.1-sol", "ordinary-synthetic"]) {
      for (const endpoint of [...auxiliaryEndpoints, "/unlisted"]) {
        const body = { model, instructions: 42, reasoning: null, tools: [], metadata: { unchanged: true } };
        const snapshot = structuredClone(body);
        assert.equal(projectNokiyDirect(body, { endpoint, catalog: null, profiles: [], host: null }), null);
        assert.deepEqual(body, snapshot);
      }
    }
  });
});

describe("Responses admission stays inference-specific", () => {
  const body = {
    model: selected.slug, instructions: "Original inference instructions",
    reasoning: { effort: "medium", summary: "auto" }, input: [],
    tools: [{ type: "function", name: "exec_command", parameters: { type: "object" } }],
  };

  test("missing/invalid picker effort and decoder fields still fail", () => {
    for (const patch of [{ reasoning: undefined }, { reasoning: null }, { reasoning: [] },
      { reasoning: { effort: "unknown" } }, { reasoning_effort: "medium" }, { instructions: 42 }]) {
      assert.throws(() => projectNokiyDirect({ ...body, ...patch }, options),
        { code: "NOKIY_DIRECT_DECODER_MISMATCH", status: 400 });
    }
    assert.throws(() => projectNokiyDirect({ ...body, model: fixed.slug }, options),
      { code: "NOKIY_DIRECT_DECODER_MISMATCH", status: 400 });
  });

  test("valid effort preserves fresh native tools without Nokiy dispatch and forbids native subagent selection", () => {
    const withoutExec = [{ type: "namespace", name: "functions",
      tools: [{ type: "custom", name: "apply_patch", format: { type: "text" } }] }];
    // 2026-10-27: a full request with its own tools but no native exec is answered by the
    // coordinator without dispatch instead of failing the turn, and records what it advertised.
    const answered = projectNokiyDirect({ ...body, tools: withoutExec }, options);
    assert.equal(answered.dispatch, "none");
    assert.equal(answered.nativeTool, null);
    assert.equal(answered.body.model, selected.coordinator.model);
    assert.deepEqual(answered.nativeToolMissing.advertised_tools, ["custom:apply_patch"]);
    const chained = projectNokiyDirect({ ...body, tools: withoutExec, previous_response_id: "resp_1" }, options);
    assert.notEqual(chained.body.tool_choice, "none");
    assert.deepEqual(chained.body.tools, withoutExec);
    assert.equal(chained.nativeTool, null);
    assert.equal(chained.dispatch, "none");
    assert.deepEqual(chained.nativeToolMissing.advertised_tools, ["custom:apply_patch"]);
    // 2026-10-25: a request advertising no tool at all is an app helper call, answered without dispatch.
    const helper = projectNokiyDirect({ ...body, tools: [] }, options);
    assert.equal(helper.dispatch, "none");
    assert.equal(helper.nativeTool, null);
    for (const tool_choice of ["spawn_agent", { type: "function", name: "spawn_agent" }]) {
      assert.throws(() => projectNokiyDirect({ ...body, tool_choice }, options),
        { code: "NOKIY_DIRECT_SUBAGENT_FORBIDDEN", status: 400 });
    }
  });

  test("compact, compaction-trigger, and prewarm preserve selector normalization without dispatch", () => {
    for (const variant of [
      { endpoint: "/responses/compact", fields: {} },
      { endpoint: "/responses", fields: { input: [{ type: "compaction_trigger" }] } },
      { endpoint: "/responses", fields: { generate: false } },
    ]) {
      const request = { ...body, ...variant.fields, reasoning: undefined, tools: [], parallel_tool_calls: true };
      const snapshot = structuredClone(request);
      const result = projectNokiyDirect(request, { ...options, endpoint: variant.endpoint });
      assert.deepEqual(request, snapshot);
      assert.deepEqual(result.body, { ...snapshot, model: selected.coordinator.model, reasoning: { effort: "xhigh" } });
      assert.equal(result.worker, selected.worker.model);
      assert.equal(result.wireEffort, "xhigh");
      assert.equal(result.dispatch, "none");
      assert.equal(result.nativeTool, null);
      assert.throws(() => projectNokiyDirect({ ...request, reasoning: { effort: "unknown" } },
        { ...options, endpoint: variant.endpoint }), { code: "NOKIY_DIRECT_DECODER_MISMATCH", status: 400 });
    }
  });

  test("existing picker and legacy efforts retain their prewarm worker mapping and defaults", () => {
    for (const effort of [undefined, "max", "ultra", "xhigh", "low", "medium", "high"]) {
      const request = { ...body, generate: false, tools: [], reasoning: effort === undefined ? undefined : { effort } };
      const result = projectNokiyDirect(request, options);
      assert.equal(result.worker, selected.worker_selector.models[effort] ?? selected.worker.model);
      assert.equal(result.profile.worker.reasoning_effort, "max");
      assert.equal(result.profile.worker.service_tier, "default");
      assert.equal(result.body.reasoning.effort, "xhigh");
      assert.equal(result.dispatch, "none");
      assert.equal(result.nativeTool, null);
    }
  });
});
