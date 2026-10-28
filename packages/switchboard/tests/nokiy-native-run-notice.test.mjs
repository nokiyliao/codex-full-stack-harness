import assert from "node:assert/strict";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import { nativeToolProjection, nativeToolItem } from "../src/nokiy-native-tool.mjs";

// Each native deterministic command is a full coordinator round trip. From the third native call
// in a row the result JSON carries an advisory notice; any nokiy_execute cell resets the run.
const host = { python: "/never/executed/python", script: "/never/executed/host.py", coordinator_mode: "dispatch_only_v1" };
const options = { host, worker: "gpt-6.1-sol", workerEffort: "max", workflow: "nokiy" };
const exec = { type: "custom", name: "exec", format: { type: "text" }, description:
  "declare const tools: { exec_command(args: {cmd: string; workdir?: string; yield_time_ms?: number; max_output_tokens?: number;}): Promise<unknown>; };\n"
    + "declare const tools: { write_stdin(args: {session_id: number; chars?: string;}): Promise<unknown>; };" };
const body = () => ({ tools: [{ type: "namespace", name: "functions", tools: [structuredClone(exec)] }], input: [] });
const nativeCell = (cmd) => nativeToolItem({ type: "custom_tool_call", name: "nokiy_native", id: "native_item",
  call_id: "native_call", status: "completed", input: JSON.stringify({ cmd }) }, nativeToolProjection(body(), options)).input;
// A nokiy_execute cell runs the same serialized function without the native-command flag.
const dispatchCell = (cmd) => {
  const cell = nativeCell(cmd);
  assert.ok(cell.endsWith(", true);"));
  return cell.slice(0, -", true);".length) + ");";
};
const session = (storage = new Map()) => ({ storage,
  store: (key, value) => { storage.set(key, value); }, load: (key) => storage.get(key) });
const done = (output = "ok\n") => ({ output, exit_code: 0, wall_time_seconds: 0.1 });

async function run(cell, results, context = {}) {
  const printed = [];
  let index = 0;
  const take = () => results[index++];
  const tools = { async exec_command() { return take(); }, async write_stdin() { return take(); } };
  await runInNewContext("(async () => { " + cell + "\n })()", { tools, text: (value) => printed.push(value), ...context },
    { timeout: 1000 });
  assert.equal(index, results.length);
  assert.equal(printed.length, 1);
  return printed[0];
}

test("without code-mode storage the native result is unchanged", async () => {
  for (let i = 0; i < 4; i++) {
    const result = done();
    assert.equal(await run(nativeCell("true"), [result]), result);
  }
});

test("the third native call in a row carries the notice inside its result JSON", async () => {
  const context = session();
  assert.equal((await run(nativeCell("a"), [done()], context)).nokiy_notice, undefined);
  assert.equal((await run(nativeCell("b"), [done()], context)).nokiy_notice, undefined);
  const third = await run(nativeCell("c"), [done("third\n")], context);
  assert.match(third.nokiy_notice, /^Native call 3 in a row since the last nokiy_execute call; /);
  assert.match(third.nokiy_notice, /nokiy_execute inspect action/);
  // Live 2026-10-18: closeouts ran as 12-21 single-step natives while the notice said they stay native.
  assert.match(third.nokiy_notice, /Admitted deployment and closeout stay native but take one call: put the remaining deterministic steps in one script that stops at the first failed check/);
  assert.equal(third.output, "third\n");
  assert.equal(third.exit_code, 0);
  // The result comes from the vm realm; compare own properties, not prototypes.
  assert.deepEqual({ ...JSON.parse(JSON.stringify(third)) }, { ...third }, "the cell output stays one JSON object");
  assert.match((await run(nativeCell("d"), [done()], context)).nokiy_notice, /^Native call 4 /);
});

test("a nokiy_execute cell resets the run", async () => {
  const context = session();
  for (const cmd of ["a", "b", "c"]) await run(nativeCell(cmd), [done()], context);
  assert.equal(context.storage.get("nokiy_native_run"), 3);
  assert.equal((await run(dispatchCell("dispatch"), [done()], context)).nokiy_notice, undefined);
  assert.equal(context.storage.get("nokiy_native_run"), 0);
  assert.equal((await run(nativeCell("e"), [done()], context)).nokiy_notice, undefined);
});

test("a native command observed to completion through its original session carries the notice", async () => {
  const context = session(new Map([["nokiy_native_run", 2]]));
  const result = await run(nativeCell("long"), [
    { output: "start\n", session_id: 42, wall_time_seconds: 10 },
    { output: "end\n", exit_code: 0, wall_time_seconds: 20 },
  ], context);
  assert.equal(result.terminal, true);
  assert.equal(result.output, "start\nend\n");
  assert.match(result.nokiy_notice, /^Native call 3 /);
});

test("broken code-mode storage never changes the native result", async () => {
  const result = done();
  const context = { store: () => { throw new Error("storage unavailable"); }, load: () => 7 };
  assert.equal(await run(nativeCell("x"), [result], context), result);
});

