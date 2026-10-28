import assert from "node:assert/strict";
import test from "node:test";
import { latestInputIsOperatorTurn, nativeRunAfterResponse, nativeRunReminder, NATIVE_RUN_REMINDER } from "../src/nokiy-workflow-profiles.mjs";
import { nativeToolProjection } from "../src/nokiy-native-tool.mjs";

const call = (name) => ({ type: "custom_tool_call", name, call_id: "c-" + name, input: "{}" });
const usage = (input_tokens) => ({ input_tokens });

test("native run counts tool steps, keeps waits and resets on dispatch or final answer", () => {
  let state = null;
  state = nativeRunAfterResponse(state, "nokiy-efficient", [call("exec")], usage(120000));
  state = nativeRunAfterResponse(state, "nokiy-efficient", [call("exec"), call("apply_patch")], usage(130000));
  assert.deepEqual({ ...state }, { alias: "nokiy-efficient", steps: 2, inputTokens: 130000 });
  state = nativeRunAfterResponse(state, "nokiy-efficient", [call("wait")], usage(131000));
  assert.equal(state.steps, 2);
  const poll = { type: "custom_tool_call", name: "exec", call_id: "p",
    input: "const r = await tools.mcp__codex_app__wait_threads({targets: []}); text(r);" };
  assert.equal(nativeRunAfterResponse(state, "nokiy-efficient", [poll], usage(132000)).steps, 2);
  const work = { ...poll, input: "await tools.mcp__codex_app__read_thread({}); await tools.exec_command({cmd: 'ls'});" };
  assert.equal(nativeRunAfterResponse(state, "nokiy-efficient", [work], usage(132000)).steps, 3);
  assert.equal(nativeRunAfterResponse(state, "nokiy-efficient", [call("nokiy_execute")], usage(1)).steps, 0);
  assert.equal(nativeRunAfterResponse(state, "nokiy-efficient", [{ type: "message", role: "assistant" }], usage(1)).steps, 0);
  assert.equal(nativeRunAfterResponse(state, "nokiy-direct", [call("exec")], usage(1)).steps, 1);
  assert.equal(nativeRunAfterResponse(state, "gpt-6.1-sol", [call("exec")], usage(1)), null);
});

test("reminder fires once at the alias threshold on a long conversation", () => {
  const threshold = NATIVE_RUN_REMINDER["nokiy-efficient"];
  const long = Object.freeze({ alias: "nokiy-efficient", steps: threshold, inputTokens: 250000 });
  const fired = nativeRunReminder(long, "nokiy-efficient", false);
  assert.match(fired.text, /nokiy_execute/);
  assert.match(fired.text, /250K/);
  assert.deepEqual(fired.diagnostic, { steps: threshold, input_tokens: 250000 });
  assert.equal(fired.state.steps, 0);
  assert.equal(nativeRunReminder({ ...long, steps: threshold - 1 }, "nokiy-efficient", false).text, null);
  assert.equal(nativeRunReminder({ ...long, inputTokens: 90000 }, "nokiy-efficient", false).text, null);
  const operator = nativeRunReminder(long, "nokiy-efficient", true);
  assert.equal(operator.text, null);
  assert.equal(operator.state.steps, 0);
  assert.equal(nativeRunReminder(long, "nokiy-direct", false).text, null);
  const ultra = nativeRunReminder({ alias: "nokiy-direct", steps: NATIVE_RUN_REMINDER["nokiy-direct"], inputTokens: 300000 }, "nokiy-direct", false);
  assert.match(ultra.text, /^Ultra dispatch reminder: /);
  assert.match(ultra.text, /run_batch/);
});

test("only a trailing operator message counts as a new operator turn", () => {
  const user = { type: "message", role: "user", content: [{ type: "input_text", text: "go" }] };
  const developer = { type: "message", role: "developer", content: [] };
  const output = { type: "custom_tool_call_output", call_id: "c", output: "ok" };
  assert.equal(latestInputIsOperatorTurn([user]), true);
  assert.equal(latestInputIsOperatorTurn([output, user, developer]), true);
  assert.equal(latestInputIsOperatorTurn([user, output]), false);
  assert.equal(latestInputIsOperatorTurn([user, output, developer]), false);
  assert.equal(latestInputIsOperatorTurn(null), false);
});

test("projection appends the reminder as the newest developer turn with a replay insertion", () => {
  const host = { python: "/not/executed/python", script: "/not/executed/host.py" };
  const exec = { type: "function", name: "exec_command", parameters: { type: "object", properties: { cmd: { type: "string" } }, required: ["cmd"] } };
  const last = { type: "function_call_output", call_id: "x", output: "done" };
  const body = { model: "gpt-6-astra", tools: [exec], input: [
    { type: "message", role: "user", content: [{ type: "input_text", text: "task" }] },
    { type: "function_call", call_id: "x", name: "exec_command", arguments: "{\"cmd\":\"ls\"}" }, last] };
  const options = { worker: "gpt-6.1-sol", workflow: "nokiy-direct", host };
  const plain = nativeToolProjection(body, options);
  assert.equal(plain.body.input.length, 3);
  assert.equal(plain.deliveryInsertion, undefined);
  const reminded = nativeToolProjection(body, { ...options, runReminder: "Ultra dispatch reminder: test" });
  assert.equal(reminded.body.input.length, 4);
  assert.deepEqual(reminded.body.input.at(-1), { type: "message", role: "developer",
    content: [{ type: "input_text", text: "Ultra dispatch reminder: test" }] });
  assert.equal(reminded.deliveryInsertion.anchor, last);
  assert.equal(reminded.runReminder, "Ultra dispatch reminder: test");
  assert.equal(body.input.length, 3);
});

