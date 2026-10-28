import assert from "node:assert/strict";
import { test } from "node:test";
import { nativeToolProjection } from "../src/nokiy-native-tool.mjs";

const exec = { type: "custom", name: "exec", format: { type: "text" }, description:
  "declare const tools: { exec_command(args: {cmd: string;}): Promise<unknown>; };" };

test("the dispatch tool routes multi-step read-and-record chains to one inspect action", () => {
  for (const workflow of ["nokiy", "nokiy-direct"]) {
    const projection = nativeToolProjection({ tools: [{ type: "namespace", name: "functions", tools: [exec] }], input: [] },
      { host: { python: "/never/python", script: "/never/host.py" }, worker: "gpt-6.1-sol", workflow });
    const tool = projection.body.tools.find((item) => item.name === "nokiy_execute");
    assert.match(tool.description, /Inspect \(read-and-record chains\): when the next step is three or more dependent read-only checks/);
    assert.match(tool.description, /JSON receipts, headless browser pages via playwright-cli\)/);
    // Five refusals (2026-10-09..18) put artifact_root inside the workspace; the template says where it goes.
    assert.match(tool.description, /"artifact_root":"absolute existing task-local artifact directory outside the workspace"/);
    assert.match(tool.description, /inspect:\{\} plus mission and context_summary/);
    assert.match(tool.description, /the repository and everything outside artifact_root\/inspect\/<id>\/evidence and temporary directories are read-only/);
    assert.match(tool.description, /no read_scopes, write_scopes, target_paths, command_templates or surface_id/);
    assert.match(tool.description, /Keep single reads and every repository change on their existing routes/);
    // Live 2026-10-19: missions forbade .py helpers, so workers re-typed the same setup every step.
    assert.match(tool.description, /Scripts and helper modules the worker writes there are task evidence, not repository source/);
    const batchClause = /Independent inspect actions may share one run_batch, also as compact members; a member that writes the repository cannot join them/;
    if (workflow === "nokiy-direct") assert.match(tool.description, batchClause);
    else assert.doesNotMatch(tool.description, batchClause);
  }
});

