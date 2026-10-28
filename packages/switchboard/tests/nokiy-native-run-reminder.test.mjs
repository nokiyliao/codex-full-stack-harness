import assert from "node:assert/strict";
import test from "node:test";
import { latestInputIsOperatorTurn, nativeRunAfterResponse, nativeRunReminder, NATIVE_RUN_REMINDER } from "../src/nokiy-workflow-profiles.mjs";
import { nativeToolProjection } from "../src/nokiy-native-tool.mjs";
import { nativeRunControlBindings } from "../src/server.mjs";

const call = (name) => ({ type: "custom_tool_call", name, call_id: "c-" + name, input: "{}" });
const usage = (input_tokens) => ({ input_tokens });

test("native run counts tool steps, keeps waits and resets on dispatch or final answer", () => {
  let state = null;
  state = nativeRunAfterResponse(state, "nokiy-efficient", [call("exec")], usage(120000));
  state = nativeRunAfterResponse(state, "nokiy-efficient", [call("exec"), call("apply_patch")], usage(130000));
  assert.deepEqual({ ...state }, { alias: "nokiy-efficient", steps: 2, inputTokens: 130000 });
  state = nativeRunAfterResponse(state, "nokiy-efficient", [call("wait")], usage(131000));
  assert.equal(state.steps, 2);
  assert.equal(nativeRunAfterResponse(state, "nokiy-efficient", [call("nokiy_execute")], usage(1)).steps, 0);
  assert.equal(nativeRunAfterResponse(state, "nokiy-efficient", [{ type: "message", role: "assistant" }], usage(1)).steps, 0);
  assert.equal(nativeRunAfterResponse(state, "nokiy-direct", [call("exec")], usage(1)).steps, 1);
  assert.equal(nativeRunAfterResponse(state, "gpt-6.1-sol", [call("exec")], usage(1)), null);
});

test("bound parent controls do not trigger a worker dispatch reminder", () => {
  const state = { alias: "nokiy-direct", steps: 1, inputTokens: 120000 };
  const bindings = [
    { type: "function_call", name: "get_goal", namespace: "functions" },
    { type: "function_call", name: "list_threads", namespace: "codex_app" },
    { type: "function_call", name: "wait_threads", namespace: "mcp__codex_app" },
    { type: "function_call", name: "write_stdin", namespace: "functions" },
    { type: "custom_tool_call", name: "nokiy_observe", namespace: null },
    { type: "custom_tool_call", name: "nokiy_native", namespace: null },
  ];
  for (const binding of bindings.slice(0, -1)) {
    const output = { ...binding, ...(binding.name === "write_stdin" ? { arguments: '{"session_id":42,"chars":""}' } : {}) };
    const next = nativeRunAfterResponse(state, "nokiy-direct", [output], usage(120000), bindings);
    assert.equal(next.steps, 1, binding.name);
    assert.equal(nativeRunReminder(next, "nokiy-direct", false).text, null);
    const { namespace: _, ...unqualified } = output;
    assert.equal(nativeRunAfterResponse(state, "nokiy-direct", [unqualified], usage(120000), bindings).steps, 1);
  }
  assert.equal(nativeRunAfterResponse(state, "nokiy-direct", [bindings[0], call("nokiy_native")], usage(120000), bindings).steps, 2);
  const foreign = { ...bindings[0], namespace: "external" };
  assert.equal(nativeRunAfterResponse(state, "nokiy-direct", [foreign], usage(120000), bindings).steps, 2);
  const ambiguous = [...bindings, { ...bindings[0], namespace: null }];
  const { namespace: _, ...unqualified } = bindings[0];
  assert.equal(nativeRunAfterResponse(state, "nokiy-direct", [unqualified], usage(120000), ambiguous).steps, 2);
});

test("Efficient derives advisory controls without restricting its native tools", () => {
  const exec = { type: "function", name: "exec_command", parameters: { type: "object", properties: { cmd: { type: "string" } }, required: ["cmd"] } };
  const body = { model: "gpt-6.1-sol", reasoning: { effort: "max" }, tools: [exec,
    { type: "namespace", name: "functions", tools: [
      { type: "function", name: "get_goal", parameters: { type: "object" } },
      { type: "function", name: "write_stdin", parameters: { type: "object", properties: { chars: { type: "string" } } } },
    ] },
    { type: "namespace", name: "codex_app", tools: [
      { type: "function", name: "read_thread", parameters: { type: "object" } },
      { type: "function", name: "send_message_to_thread", parameters: { type: "object" } },
    ] },
  ], input: [] };
  const before = structuredClone(body);
  const advisory = nativeRunControlBindings(body);
  const state = { alias: "nokiy-efficient", steps: 4, inputTokens: 150000 };
  for (const binding of advisory.bindings) {
    const output = { ...binding, ...(binding.name === "write_stdin" ? { arguments: '{"session_id":42}' } : {}) };
    const { namespace: _, ...unqualified } = output;
    for (const item of [output, unqualified]) {
      const next = nativeRunAfterResponse(state, "nokiy-efficient", [item], usage(150000), advisory.bindings);
      assert.equal(next.steps, 4, item.name);
      assert.equal(nativeRunReminder(next, "nokiy-efficient", false).text, null);
    }
  }
  assert.equal(advisory.bindings.length, 4);
  const profile = { slug: "nokiy-efficient", workflow: "nokiy-direct", coordinator: { model: "gpt-6.1-sol", reasoning_effort: "max" } };
  const projection = nativeToolProjection(body, { worker: "gpt-6.1-terra", workflow: "nokiy-direct", profile,
    host: { python: "/not/executed/python", script: "/not/executed/host.py", coordinator_mode: "dispatch_only_v1" } });
  assert.equal(projection.adaptiveNative, true);
  assert.equal(projection.coordinatorBindings, null);
  assert.deepEqual(projection.body.tools.slice(0, body.tools.length), before.tools);
  assert.deepEqual(body, before);
});

test("advisory continuation preserves definitions but revokes invalid and ambiguous hints", () => {
  const tool = { type: "function", name: "read_thread", namespace: "codex_app", parameters: { type: "object" } };
  const state = { alias: "nokiy-efficient", steps: 4, inputTokens: 150000 };
  const initial = nativeRunControlBindings({ input: [{ type: "tool_search_output", tools: [tool] }] });
  const continued = nativeRunControlBindings({ input: [{ type: "function_call_output", output: "done" }] }, initial);
  assert.equal(continued.bindings.length, 1);
  const invalid = nativeRunControlBindings({ tools: [{ ...tool, parameters: null }] }, continued);
  assert.equal(invalid.bindings.length, 0);
  const duplicate = nativeRunControlBindings({ tools: [tool, tool] }, continued);
  assert.equal(duplicate.bindings.length, 0);
  assert.equal(nativeRunControlBindings({}, duplicate).bindings.length, 0);
  const collided = nativeRunControlBindings({ tools: [tool, { ...tool, namespace: "foreign" }] });
  const output = { type: "function_call", name: "read_thread", arguments: "{}" };
  assert.equal(nativeRunAfterResponse(state, "nokiy-efficient", [output], usage(150000), collided.bindings).steps, 5);
  assert.equal(nativeRunAfterResponse(state, "nokiy-efficient", [{ ...output, namespace: "codex_app" }], usage(150000), collided.bindings).steps, 4);
  const retainedCollision = nativeRunControlBindings({}, collided);
  assert.equal(nativeRunAfterResponse(state, "nokiy-efficient", [output], usage(150000), retainedCollision.bindings).steps, 5);
  const conflictingHistory = nativeRunControlBindings({ tools: [tool], input: [
    { type: "additional_tools", tools: [{ ...tool, namespace: "foreign", description: "first" }] },
    { type: "additional_tools", tools: [{ ...tool, namespace: "foreign", description: "changed" }] },
  ] }, collided);
  assert.equal(conflictingHistory.catalogKnown, false);
  const afterUnknown = nativeRunControlBindings({ tools: [tool] }, conflictingHistory);
  assert.equal(afterUnknown.catalogKnown, false);
  assert.equal(nativeRunAfterResponse(state, "nokiy-efficient", [output], usage(150000), afterUnknown.bindings).steps, 5);
  assert.equal(nativeRunAfterResponse(state, "nokiy-efficient", [{ ...output, namespace: "codex_app" }], usage(150000), afterUnknown.bindings).steps, 4);
  const unbound = nativeRunControlBindings({ tools: [tool], previous_response_id: "unknown-chain" });
  assert.equal(unbound.bindings[0].unqualifiedUnique, false);
});

test("Efficient stdin input and mixed implementation are not control-only steps", () => {
  const advisory = nativeRunControlBindings({ tools: [{ type: "function", name: "write_stdin", parameters: { type: "object" } }] });
  const state = { alias: "nokiy-efficient", steps: 4, inputTokens: 150000 };
  for (const args of ['{"session_id":42,"chars":"run"}', "[]", "null", "bad json"]) {
    const output = { type: "function_call", name: "write_stdin", arguments: args };
    assert.equal(nativeRunAfterResponse(state, "nokiy-efficient", [output], usage(150000), advisory.bindings).steps, 5);
  }
  const empty = { type: "function_call", name: "write_stdin", arguments: '{"session_id":42,"chars":""}' };
  assert.equal(nativeRunAfterResponse(state, "nokiy-efficient", [empty], usage(150000), advisory.bindings).steps, 4);
  assert.equal(nativeRunAfterResponse(state, "nokiy-efficient", [empty, call("exec")], usage(150000), advisory.bindings).steps, 5);
});

test("opaque code and foreign wait names are not classified by poll substrings", () => {
  const state = { alias: "nokiy-direct", steps: 1, inputTokens: 120000 };
  for (const input of [
    'const label = "read_thread"; await tools.web__run({});',
    'await tools.mcp__codex_app__wait_threads({}); await tools.web__run({});',
    'await tools.mcp__codex_app__wait_threads({}); await tools.exec_command({cmd: "ls"});',
    'const r = await tools.mcp__codex_app__wait_threads({}); text(r);',
  ]) {
    assert.equal(nativeRunAfterResponse(state, "nokiy-direct", [{ ...call("exec"), input }], usage(120000)).steps, 2);
  }
  assert.equal(nativeRunAfterResponse(state, "nokiy-direct", [{ ...call("wait"), namespace: "external" }], usage(120000)).steps, 2);
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
  assert.match(ultra.text, /App\/cross-session/);
  assert.match(ultra.text, /deployment/);
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

