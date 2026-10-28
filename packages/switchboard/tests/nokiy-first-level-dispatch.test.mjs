// 2026-10-23 i23: the Efficient coordinator read the task's "no recursive Nokiy invocation" as
// forbidding its own worker dispatch and kept both branches in one loop (668 s against native Sol's
// 583 s); in i22 it dispatched and overlapped. The contract and tool now say what recursion means.
import assert from "node:assert/strict";
import { test } from "node:test";
import { coordinatorContract } from "../src/nokiy-coordinator-policy.mjs";
import { NOKIY_TOOL, nativeToolProjection } from "../src/nokiy-native-tool.mjs";

const profile = (slug, workflow, model, effort) => ({ slug, workflow, display_name: slug,
  coordinator: { model, reasoning_effort: effort }, worker: { model: "gpt-6.1-sol", reasoning_effort: "max" } });

test("every profile treats nokiy_execute dispatch as first-level delegation, not recursion", () => {
  for (const configured of [profile("nokiy", "nokiy", "gpt-6-astra", "xhigh"),
    profile("nokiy-direct", "nokiy-direct", "gpt-6-astra", "xhigh"),
    profile("nokiy-efficient", "nokiy-direct", "gpt-6.1-sol", "max")]) {
    const text = coordinatorContract(configured);
    assert.match(text, /Dispatching through nokiy_execute is this workflow's own first-level delegation, not recursive Nokiy invocation/);
    assert.match(text, /an instruction against recursive Nokiy never forbids this dispatch/);
  }
});

test("the dispatch tool itself states that calling it is not recursive Nokiy", () => {
  for (const workflow of ["nokiy", "nokiy-direct"]) {
    const projection = nativeToolProjection({ tools: [{ type: "namespace", name: "functions",
      tools: [{ type: "custom", name: "exec", format: { type: "text" } }] }] },
      { host: { python: "/never/python", script: "/never/host.py" }, worker: "gpt-6.1-sol",
        workerEffort: "max", workerServiceTier: "default", workflow });
    const description = projection.body.tools.find((tool) => tool.name === NOKIY_TOOL).description;
    assert.match(description, /Calling this tool is the parent's own first-level delegation, not recursive Nokiy invocation/);
  }
});
