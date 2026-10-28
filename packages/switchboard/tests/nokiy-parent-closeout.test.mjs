import assert from "node:assert/strict";
import { test } from "node:test";
import { nativeToolProjection, nativeToolItem } from "../src/nokiy-native-tool.mjs";

function setup(type, workflow, enabled = true, input = []) {
  return nativeToolProjection({ tools: [{ type: "namespace", name: "functions", tools: [{
    type, name: type === "function" ? "exec_command" : "exec",
    ...(type === "function" ? { parameters: { type: "object" } } : { format: { type: "text" } }),
  }] }], input }, { workflow, worker: "gpt-6.1-sol", workerEffort: "max",
    host: { python: "/offline/python", script: "/offline/host.py", coordinator_mode: "dispatch_only_v1",
      parent_result_delivery: "accept_v1", ...(enabled ? { parent_closeout: "direct_v1" } : {}) } });
}
const payload = { operation: "closeout", plan_path: "/task/plan.json", plan_sha256: "a".repeat(64) };
const call = (value, type) => ({ id: "closeout-item", call_id: "closeout-call", name: "nokiy_execute",
  type: type === "function" ? "function_call" : "custom_tool_call",
  [type === "function" ? "arguments" : "input"]: JSON.stringify(value) });

for (const workflow of ["nokiy", "nokiy-direct"]) for (const type of ["function", "custom"]) {
  test(`${workflow}/${type} advertises and projects admitted parent closeout without a worker grant`, () => {
    const p = setup(type, workflow);
    const tool = p.body.tools.find(x => x.name === "nokiy_execute");
    assert.match(tool.description, /"operation":"check_closeout"/);
    assert.match(tool.description, /Do not fill missing owners/);
    for (const operation of ["check_closeout", "closeout", "read_closeout"]) {
      assert.ok(p.operations.includes(operation));
      if (type === "function") assert.ok(tool.parameters.properties.operation.enum.includes(operation));
      const value = operation === "read_closeout"
        ? { operation, artifact_root: "/task", action_id: "original" } : { ...payload, operation };
      const original = call(value, type), projected = nativeToolItem(original, p);
      assert.equal(projected.id, original.id);
      assert.equal(projected.call_id, original.call_id);
      assert.equal(projected.name, type === "function" ? "exec_command" : "exec");
      const encoded = (projected.arguments ?? projected.input).match(/'--payload' '([A-Za-z0-9_-]+)'/)[1];
      assert.deepEqual(JSON.parse(Buffer.from(encoded, "base64url")).payload, value);
      const result = { type: type === "function" ? "function_call_output" : "custom_tool_call_output",
        call_id: projected.call_id, output: "Script running with cell ID 7\n" };
      const resumed = setup(type, workflow, true, [projected, result]);
      assert.equal(resumed.resultDelivery, null);
      assert.equal(resumed.pendingResultDelivery, null);
    }
  });
}

test("legacy hosts do not advertise closeout and inputs cannot add worker or owner fields", () => {
  assert.throws(() => nativeToolItem(call(payload, "function"), setup("function", "nokiy", false)),
    { code: "NOKIY_NATIVE_TOOL_INVALID" });
  const p = setup("function", "nokiy");
  for (const field of ["draft", "action", "surface_id", "native_thread_id", "worker"]) {
    assert.throws(() => nativeToolItem(call({ ...payload, [field]: "not allowed" }, "function"), p),
      { code: "NOKIY_NATIVE_TOOL_INVALID" });
  }
  for (const plan_sha256 of [true, "A".repeat(64), "a".repeat(63)]) {
    assert.throws(() => nativeToolItem(call({ ...payload, plan_sha256 }, "function"), p),
      { code: "NOKIY_NATIVE_TOOL_INVALID" });
  }
});
