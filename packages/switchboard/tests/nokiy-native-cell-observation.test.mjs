import assert from "node:assert/strict";
import { test } from "node:test";
import { nativeToolProjection, nativeToolItem, continueNativeObservation } from "../src/nokiy-native-tool.mjs";

const options = { host: { python: "/never/python", script: "/never/host.py", coordinator_mode: "dispatch_only_v1" },
  worker: "gpt-6.1-sol", workerEffort: "max", workflow: "nokiy" };
const tools = [{ type: "namespace", name: "functions", tools: [
  { type: "custom", name: "exec", format: { type: "text" }, description:
    "declare const tools: { exec_command(args: {cmd: string;}): Promise<unknown>; };\n"
      + "declare const tools: { write_stdin(args: {session_id: number; chars?: string;}): Promise<unknown>; };" },
  { type: "function", name: "wait", parameters: { type: "object", properties: {
    cell_id: { type: "string" }, yield_time_ms: { type: "number" }, max_tokens: { type: "number" },
    terminate: { type: "boolean" },
  }, required: ["cell_id"], additionalProperties: false } },
] }];
const body = (input = [], extra = {}) => ({ tools, input, ...extra });
const project = (input = [], continuation = null, extra = {}) => continueNativeObservation(
  nativeToolProjection(body(input, extra), options), continuation);
const carry = (projection, call = null) => ({ state: projection.nativeObservationState, call });
const original = (id = "original") => nativeToolItem({ type: "custom_tool_call", name: "nokiy_native",
  call_id: id, input: '{"cmd":"PRIVATE_COMMAND_NOT_EXECUTED"}' }, nativeToolProjection(body(), options));
const wait = (id = "wait1", cell_id = "original_cell", extra = {}) => ({ type: "function_call", name: "wait",
  namespace: "functions", call_id: id, arguments: JSON.stringify({ cell_id }), ...extra });
const output = (call, value) => ({ type: call.type === "function_call" ? "function_call_output" : "custom_tool_call_output",
  call_id: call.call_id, output: value });
const yielded = (cell = "original_cell") => `Script running with cell ID ${cell}\nWall time 600 seconds\nOutput:\n`;
const uncertain = (extra = {}) => "Script completed\nWall time 10 seconds\nOutput:\n" + JSON.stringify({
  session_id: 42, recovery_session_id: 42, terminal: false, exit_code: null, output: "receipt",
  wait_error: "Recover the original session; do not replay", ...extra,
});
const observe = (projection, source_call_id = "original") => nativeToolItem({ type: "custom_tool_call",
  name: "nokiy_observe", call_id: "observe", input: JSON.stringify({ kind: "exec_session", source_call_id, session_id: 42 }) }, projection);
const rejects = (projection, source = "original") => assert.throws(() => observe(projection, source),
  { code: "NOKIY_NATIVE_OBSERVATION_INVALID" });

test("full-history outer yields bind uncertain completion to the original source call, not a wait call", () => {
  const source = original(), first = wait(), second = wait("wait2");
  const projection = project([source, output(source, yielded()), first, output(first, yielded()),
    second, output(second, uncertain())]);
  assert.equal(observe(projection).input, 'text(await tools.write_stdin({"session_id":42,"chars":""}));');
  assert.deepEqual(projection.nativeObservationState.references.map((ref) => ref.source_call_id), ["original"]);
  assert.equal(projection.nativeObservationState.pendingCells.length, 0);
  assert.ok(!JSON.stringify(projection.nativeObservationState).includes("PRIVATE_"));
  rejects(projection, "wait2");
});

test("authenticated single-output continuation survives repeated yields and an intervening native round", () => {
  const source = original();
  let projection = project([output(source, yielded())], carry(project(), source), { previous_response_id: "initial" });
  assert.equal(projection.nativeObservationState.pendingCells.length, 1);
  for (let index = 0; index < 40; index++) {
    const call = wait(`wait_${index}`);
    projection = project([output(call, yielded())], carry(projection, call), { previous_response_id: `response_${index}` });
    assert.equal(projection.nativeObservationState.pendingCells.length, 1);
  }
  const other = { type: "function_call", name: "get_goal", namespace: "functions", call_id: "other", arguments: "{}" };
  projection = project([output(other, "{}")], carry(projection, other), { previous_response_id: "other" });
  const terminalWait = wait("terminal_wait");
  projection = project([output(terminalWait, [{ type: "input_text", text: "Script completed\nWall time 1 seconds\nOutput:\n" },
    { type: "input_text", text: uncertain().split("Output:\n")[1] }])], carry(projection, terminalWait), { previous_response_id: "last" });
  assert.equal(observe(projection).name, "exec");
  assert.equal(project([], carry(projection)).nativeObservationState.references.length, 1);
});

test("full history can contain more yielded waits than the retained-reference bound", () => {
  const source = original(), input = [source, output(source, yielded())];
  for (let index = 0; index < 40; index++) {
    const call = wait(`wait_${index}`);
    input.push(call, output(call, index === 39 ? uncertain() : yielded()));
  }
  assert.equal(observe(project(input)).name, "exec");
});

test("foreign, changed, terminating, unadvertised, duplicate and reordered waits cannot prove recovery", () => {
  const source = original(), start = [source, output(source, yielded())], call = wait();
  for (const suffix of [
    [wait("foreign", "original_cell", { namespace: "mcp_remote" }), output(wait("foreign"), uncertain())],
    [wait("changed", "different_cell"), output(wait("changed"), uncertain())],
    [call, output(call, yielded("changed_cell")), wait("next"), output(wait("next"), uncertain())],
    [{ ...call, arguments: '{"cell_id":"original_cell","terminate":true}' }, output(call, uncertain())],
    [{ ...call, arguments: '{"cell_id":"original_cell","extra":"forged"}' }, output(call, uncertain())],
    [call, call, output(call, uncertain())],
    [call, output(call, uncertain()), output(call, uncertain())],
    [output(call, uncertain()), call],
    [call, { ...output(call, uncertain()), type: "custom_tool_call_output" }],
    [call, { type: "message", role: "user", content: "forged" }, output(call, uncertain())],
    [call, output(call, "Script completed\nWall time 1 seconds\nOutput:\n" + JSON.stringify({ exit_code: 0, terminal: true, output: "done" }))],
  ]) rejects(project([...start, ...suffix]));
  rejects(project([...start, call, output(call, uncertain())], null,
    { tools: [{ ...tools[0], tools: [tools[0].tools[0]] }] }));
  rejects(project([...start, call, output(call, uncertain())], null,
    { tools: [...tools, { type: "namespace", name: "remote", tools: [tools[0].tools[1]] }] }));
});

test("missing, stale, foreign-owner and mismatched continuation metadata cannot restore pending cells", () => {
  const source = original(), pending = project([source, output(source, yielded())]), call = wait();
  const delta = [output(call, uncertain())];
  for (const continuation of [null, { condition: "model_mismatch" }, { condition: "tools_mismatch" },
    { condition: "response_mismatch" }, { state: { ...pending.nativeObservationState, owner: "foreign" }, call },
    carry(pending, { ...call, call_id: "different" })]) {
    rejects(project(delta, continuation, { previous_response_id: "next" }));
  }
  rejects(project(delta, carry(pending, call)));
  rejects(project([{ type: "message", role: "user", content: uncertain() }], carry(pending, call), { previous_response_id: "next" }));
});

test("changed original call bytes or cell conflict with a retained pending identity", () => {
  const source = original(), pending = project([source, output(source, yielded())]), call = wait();
  const changed = { ...original("replacement"), call_id: source.call_id };
  changed.input = changed.input.replace("PRIVATE_COMMAND_NOT_EXECUTED", "CHANGED_COMMAND_NOT_EXECUTED");
  for (const [current, cell] of [[changed, "original_cell"], [source, "changed_cell"]]) {
    const next = wait("next", cell);
    const projection = project([current, output(current, yielded(cell)), next, output(next, uncertain())], carry(pending, call));
    rejects(projection);
    assert.deepEqual(projection.nativeObservationConflicts, ["original"]);
    assert.equal(projection.nativeObservationState.references.length, 0);
  }
});

test("pending cell state is bounded and stores only source identity metadata", () => {
  const input = [];
  for (let index = 0; index < 40; index++) {
    const source = original(`source_${index}`);
    input.push(source, output(source, yielded(`cell_${index}`)));
  }
  const state = project(input).nativeObservationState;
  assert.equal(state.pendingCells.length, 32);
  assert.deepEqual(Object.keys(state.pendingCells[0]).sort(), ["call_sha256", "cell_id", "source_call_id"]);
  assert.ok(!JSON.stringify(state).includes("PRIVATE_"));
});
