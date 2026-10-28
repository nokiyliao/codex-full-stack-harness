// observe:"later" (2026-10-23): a parent with independent work of its own lets the Nokiy cell
// yield its handle at once and waits on that same cell later, instead of blocking until the worker ends.
import assert from "node:assert/strict";
import { test } from "node:test";
import { NOKIY_TOOL, nativeToolProjection, nativeToolItem, continueNativeObservation } from "../src/nokiy-native-tool.mjs";

const host = { python: "/never/python", script: "/never/host.py" };
const base = { worker: "gpt-6.1-sol", workerEffort: "max", workerServiceTier: "default" };
const execTool = (type) => type === "function"
  ? { type, name: "exec_command", parameters: { type: "object" } }
  : { type, name: "exec", format: { type: "text" } };
const projection = (type = "custom", workflow = "nokiy-direct") => nativeToolProjection(
  { tools: [{ type: "namespace", name: "functions", tools: [execTool(type)] }] }, { host, ...base, workflow });
const batch = { operation: "run_batch", artifact_root: "/artifacts/batch", members: [{ draft: { workspace: "/ws",
  artifact_root: "/artifacts/batch/a", prompt: "Assigned branch", authority_effect: "workspace" }, action: { context_summary: "bounded" } }] };
const call = (payload, type = "custom", id = "dispatch") => ({ type: type === "function" ? "function_call" : "custom_tool_call",
  name: NOKIY_TOOL, call_id: id, [type === "function" ? "arguments" : "input"]: JSON.stringify(payload) });
const pragma = (input) => JSON.parse(input.match(/^\/\/ @exec: (.+)$/m)[1]);
const envelope = (input) => JSON.parse(Buffer.from(input.match(/'--payload' '([A-Za-z0-9_-]+)'/)[1], "base64url").toString("utf8"));

test("observe later yields the cell within seconds and never reaches the host envelope", () => {
  const later = nativeToolItem(call({ ...batch, observe: "later" }), projection());
  const blocking = nativeToolItem(call(batch), projection());
  assert.deepEqual(pragma(later.input), { yield_time_ms: 15000, max_output_tokens: 262144 });
  assert.deepEqual(pragma(blocking.input), { yield_time_ms: 600000, max_output_tokens: 262144 });
  assert.deepEqual(envelope(later.input).payload, batch);
  // Only the first yield and V417's short overlap observations differ; the cell still waits for the
  // original terminal itself.
  assert.equal(later.input.replace(/^\/\/ @exec: .+$/m, "").replace(/, false, false, true\);$/, ");"),
    blocking.input.replace(/^\/\/ @exec: .+$/m, ""));
});

test("observe is limited to dispatching operations and the exact value later", () => {
  for (const payload of [{ operation: "read_result", artifact_root: "/a", request_id: "tura_embedded_x", observe: "later" },
    { ...batch, observe: "now" }, { ...batch, observe: true }]) {
    assert.throws(() => nativeToolItem(call(payload), projection()), /observe accepts only "later"/);
  }
});

test("function targets already return a session at once, so observe later changes nothing there", () => {
  const later = nativeToolItem(call({ ...batch, observe: "later" }, "function"), projection("function"));
  const blocking = nativeToolItem(call(batch, "function"), projection("function"));
  assert.equal(later.arguments, blocking.arguments);
});

test("a deferred cell is still recognized as the projected Nokiy call and its later wait keeps the floor", () => {
  const options = { host: { ...host, coordinator_mode: "dispatch_only_v1" }, ...base, workflow: "nokiy-direct" };
  const tools = [{ type: "namespace", name: "functions", tools: [
    { type: "custom", name: "exec", format: { type: "text" }, description:
      "declare const tools: { exec_command(args: {cmd: string;}): Promise<unknown>; };\n"
        + "declare const tools: { write_stdin(args: {session_id: number; chars?: string;}): Promise<unknown>; };" },
    { type: "function", name: "wait", parameters: { type: "object", properties: {
      cell_id: { type: "string" }, yield_time_ms: { type: "number" }, max_tokens: { type: "number" },
      terminate: { type: "boolean" } }, required: ["cell_id"], additionalProperties: false } }] }];
  const project = (input) => continueNativeObservation(nativeToolProjection({ tools, input }, options), null);
  const source = nativeToolItem(call({ ...batch, observe: "later" }), project([]));
  const yielded = { type: "custom_tool_call_output", call_id: source.call_id,
    output: "Script running with cell ID deferred_cell\nWall time 15 seconds\nOutput:\n" };
  const own = { type: "custom_tool_call", name: "exec", namespace: "functions", call_id: "own_work",
    input: "text(await tools.exec_command({cmd: 'make test'}));" };
  const ownOutput = { type: "custom_tool_call_output", call_id: "own_work", output: "Script completed\nOutput:\nok" };
  const projected = project([source, yielded, own, ownOutput]);
  assert.deepEqual(projected.nativeObservationState.pendingCells.map((ref) => ref.cell_id), ["deferred_cell"]);
  const wait = nativeToolItem({ type: "function_call", name: "wait", namespace: "functions", call_id: "later_wait",
    arguments: JSON.stringify({ cell_id: "deferred_cell" }) }, projected);
  assert.equal(JSON.parse(wait.arguments).yield_time_ms, 300000);
});
