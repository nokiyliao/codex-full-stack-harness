import assert from "node:assert/strict";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import { nativeToolProjection, nativeToolItem, nativeEventProjector, nativeSseProjector } from "../src/nokiy-native-tool.mjs";

const host = { python: "/never/executed/python", script: "/never/executed/host.py", coordinator_mode: "dispatch_only_v1" };
const options = { host, worker: "gpt-6.1-sol", workerEffort: "max", workflow: "nokiy" };
const declaration = 'declare const tools: { write_stdin(args: {\n// Original native numeric handle.\nsession_id: number;\nchars?: string;\nyield_time_ms?: number;\n}): Promise<unknown>; };';
const custom = { type: "custom", name: "exec", description: declaration, format: { type: "text" } };
const session = 42;
const nativeText = `Chunk ID: fixture\nWall time: 0.1234 seconds\nProcess running with session ID ${session}\nOutput:\nRunning`;
const original = { type: "function_call", name: "exec_command", namespace: "functions", id: "original_item",
  call_id: "original_call", arguments: '{"cmd":"original command, never replayed"}' };
const fn = (name, properties = {}) => ({ type: "function", name, parameters: { type: "object", properties,
  ...(name === "write_stdin" ? { required: ["session_id"] } : {}) } });
const names = (tools) => tools.flatMap((tool) => tool.type === "namespace" ? names(tool.tools) : [tool.name]);
const output = (call, value = nativeText) => ({ type: call.type === "function_call" ? "function_call_output" : "custom_tool_call_output",
  call_id: call.call_id, output: value });
function body(input = [original, output(original)], tools = [custom, fn("wait", { cell_id: { type: "string" } })]) {
  return { tools: [{ type: "namespace", name: "functions", tools }], input };
}
function observe(args = {}, type = "custom_tool_call") {
  return { type, name: "nokiy_observe", id: "observation_item", call_id: "observation_call",
    [type === "custom_tool_call" ? "input" : "arguments"]: JSON.stringify({ kind: "exec_session", source_call_id: original.call_id, session_id: session, ...args }) };
}
function reason(request, item, expected) {
  const projection = nativeToolProjection(request, options);
  assert.throws(() => nativeToolItem(item, projection), (error) => {
    assert.equal(error.code, "NOKIY_NATIVE_OBSERVATION_INVALID");
    assert.equal(error.details.native_observation.reason, expected);
    assert.ok(error.details.native_observation.next_action.length < 240);
    return true;
  });
}

test("code-mode-only coordinator observes an authenticated pre-model-switch native function pair", async () => {
  const request = body([original, output(original), { type: "message", role: "user", content: "Continue after model switch." }]);
  const before = structuredClone(request);
  const projection = nativeToolProjection(request, options);
  assert.deepEqual(names(projection.body.tools), ["wait", "nokiy_execute", "nokiy_observe"]);
  const item = nativeToolItem(observe(), projection);
  assert.equal(item.type, "custom_tool_call");
  assert.equal(item.call_id, "observation_call");
  assert.equal(item.id, "observation_item");
  assert.equal(item.name, "exec");
  assert.equal(item.namespace, "functions");
  assert.equal(item.input, 'text(await tools.write_stdin({"session_id":42,"chars":""}));');
  const calls = [], emitted = [];
  await runInNewContext(`(async () => { ${item.input} })()`, { text: (x) => emitted.push(x), tools: {
    write_stdin: async (args) => { calls.push(JSON.parse(JSON.stringify(args))); return { exit_code: 0, output: "terminal fixture" }; },
    exec_command: async () => { assert.fail("Original command must never be replayed"); },
  } });
  assert.deepEqual(calls, [{ session_id: session, chars: "" }]);
  assert.equal(emitted[0].exit_code, 0);
  assert.deepEqual(request, before);
  const dispatch = projection.body.tools.find((tool) => tool.name === "nokiy_execute");
  assert.match(dispatch.description, /Observation is not a worker command template/);
});

test("exact canonical code-mode exec output accepts native completion blocks, not a code cell", () => {
  const call = { ...original, type: "custom_tool_call", name: "exec",
    input: 'const result = await tools.exec_command({"cmd":"original"}); text(result);' };
  delete call.arguments;
  const blocks = [{ type: "input_text", text: "Script completed\nWall time 1.2 seconds\nOutput:\n" },
    { type: "input_text", text: JSON.stringify({ session_id: session, output: "running", wall_time_seconds: 1.2 }) }];
  const projection = nativeToolProjection(body([call, output(call, blocks)]), options);
  assert.equal(nativeToolItem(observe(), projection).input, 'text(await tools.write_stdin({"session_id":42,"chars":""}));');
  reason(body([call, output(call, "Script running with cell ID cell_original\nWall time 1 seconds\nOutput:\n")]),
    observe(), "ORIGINAL_RUNNING_HANDLE_UNVERIFIED");
});

test("captured native completion metadata survives text-block flattening without changing handle ownership", () => {
  const call = { ...original, type: "custom_tool_call", name: "exec", call_id: "call_f6a9572bdef84150bcc910cc9a24d9d1",
    input: 'text(await tools.exec_command({"cmd":"captured command shape, never executed"}));' };
  delete call.arguments;
  const header = "Script completed\nWall time 1.2 seconds\nOutput:\n";
  const native = '{"chunk_id":"cd2a42","wall_time_seconds":1.001409208,"session_id":49998,"original_token_count":0,"output":""}';
  const blocks = [{ type: "input_text", text: header }, { type: "input_text", text: native }];
  for (const value of [blocks, header + native, [{ type: "input_text", text: header + native }],
    (header + native).replaceAll("\n", "\r\n")]) {
    const request = body([call, output(call, value)]), before = structuredClone(request);
    const projection = nativeToolProjection(request, options);
    const item = nativeToolItem(observe({ source_call_id: call.call_id, session_id: 49998 }), projection);
    assert.equal(item.input, 'text(await tools.write_stdin({"session_id":49998,"chars":""}));');
    assert.deepEqual(request, before);
    reason(request, observe({ source_call_id: call.call_id, session_id: 49999 }), "ORIGINAL_RUNNING_HANDLE_UNVERIFIED");
  }
});

test("completed framing never scans stdout, recursively unwraps results, or admits terminal and partial JSON", () => {
  const call = { ...original, type: "custom_tool_call", name: "exec",
    input: 'text(await tools.exec_command({"cmd":"fixture, never executed"}));' };
  delete call.arguments;
  const header = "Script completed\nWall time 1.2 seconds\nOutput:\n";
  const running = '{"session_id":42,"output":"fixture"}';
  const invalid = [
    "stdout prefix\n" + header + running,
    header + running + "\nstdout suffix",
    header + running + "\n" + running,
    header + header + running,
    header + JSON.stringify({ output: running }),
    header + JSON.stringify({ session_id: session, output: header + running, exit_code: 0 }),
    header + JSON.stringify({ session_id: session, output: "", terminal: true }),
    header + JSON.stringify({ session_id: "42", output: "" }),
    header + JSON.stringify({ session_id: Number.MAX_SAFE_INTEGER + 1, output: "" }),
    header + JSON.stringify({ session_id: 42.5, output: "" }),
    header + JSON.stringify({ session_id: -42, output: "" }),
    header + JSON.stringify({ session_id: session, output: "" }).slice(0, -1),
    "Script running with cell ID original_cell\nWall time 1.2 seconds\nOutput:\n" + running,
    [{ type: "input_text", text: header }, { type: "image", text: running }],
    header + JSON.stringify({ session_id: session, output: "x".repeat(256 * 1024) }),
  ];
  for (const value of invalid) {
    reason(body([call, output(call, value)]), observe(), "ORIGINAL_RUNNING_HANDLE_UNVERIFIED");
  }
  reason(body([{ ...call, input: 'text({"session_id":42,"output":"spoof"});' }, output(call, header + running)]),
    observe(), "ORIGINAL_TOOL_NOT_NATIVE");
  reason(body([{ ...call, namespace: "remote_mcp" }, output(call, header + running)]),
    observe(), "ORIGINAL_TOOL_NOT_NATIVE");
});

test("framing normalization preserves safe-integer semantics and rejects duplicate original pairs", () => {
  const call = { ...original, type: "custom_tool_call", name: "exec",
    input: 'text(await tools.exec_command({"cmd":"fixture, never executed"}));' };
  delete call.arguments;
  const header = "Script completed\nWall time 1.2 seconds\nOutput:\n";
  for (const number of ["42", "42.0", "4.2e1"]) {
    const value = header + `{"session_id":${number},"output":""}`;
    const projection = nativeToolProjection(body([call, output(call, value)]), options);
    assert.equal(nativeToolItem(observe(), projection).input, 'text(await tools.write_stdin({"session_id":42,"chars":""}));');
  }
  const zero = header + '{"session_id":0,"output":""}';
  assert.equal(nativeToolItem(observe({ session_id: 0 }), nativeToolProjection(body([call, output(call, zero)]), options)).input,
    'text(await tools.write_stdin({"session_id":0,"chars":""}));');
  const value = header + '{"session_id":42,"output":""}';
  reason(body([call, call, output(call, value)]), observe(), "ORIGINAL_PAIR_UNAVAILABLE");
  reason(body([call, output(call, value), output(call, value)]), observe(), "ORIGINAL_PAIR_UNAVAILABLE");
});

test("native function observation uses only its actual advertised native schema", () => {
  const tools = [fn("exec_command"), fn("write_stdin", { session_id: { type: "integer", minimum: 1 }, chars: { type: "string" } })];
  const projection = nativeToolProjection(body(undefined, tools), options);
  const item = nativeToolItem(observe({}, "function_call"), projection);
  assert.deepEqual(item, { ...observe({}, "function_call"), name: "write_stdin", namespace: "functions",
    arguments: '{"session_id":42,"chars":""}' });
  const schema = projection.body.tools.find((tool) => tool.name === "nokiy_observe").parameters;
  assert.deepEqual(schema.properties.session_id, { type: "integer", minimum: 1 });
  assert.equal(schema.additionalProperties, false);
});

test("same-connection original call supports a delta; unbound or omitted history cannot authorize it", () => {
  const delta = { ...body([output(original)]), previous_response_id: "original_response" };
  const projection = nativeToolProjection(delta, { ...options, continuationCall: original });
  assert.equal(nativeToolItem(observe(), projection).name, "exec");
  reason(delta, observe(), "ORIGINAL_PAIR_UNAVAILABLE");
  reason(body([output(original)]), observe(), "ORIGINAL_PAIR_UNAVAILABLE");
});

test("exact native poll call may emit the same original handle for a following observation", () => {
  const call = { ...original, type: "custom_tool_call", name: "exec",
    input: 'text(await tools.write_stdin({"session_id":42,"chars":""}));' };
  delete call.arguments;
  const projection = nativeToolProjection(body([call, output(call, '{"session_id":42,"output":"still running"}')]), options);
  assert.equal(nativeToolItem(observe(), projection).input, call.input);
  reason(body([call, output(call, '{"session_id":43,"output":"changed"}')]), observe({ session_id: 43 }), "ORIGINAL_RUNNING_HANDLE_UNVERIFIED");
});

test("missing, string, ambiguous, remote and arbitrary-code provenance fails closed", () => {
  const cases = [
    [body([{ type: "message", role: "user", content: nativeText }]), observe(), "ORIGINAL_PAIR_UNAVAILABLE"],
    [body([original, { ...output(original), call_id: "other_call" }]), observe(), "ORIGINAL_PAIR_UNAVAILABLE"],
    [body([original, original, output(original)]), observe(), "ORIGINAL_PAIR_UNAVAILABLE"],
    [body([original, output(original), output(original)]), observe(), "ORIGINAL_PAIR_UNAVAILABLE"],
    [body([{ ...original, namespace: "remote_mcp" }, output(original)]), observe(), "ORIGINAL_TOOL_NOT_NATIVE"],
    [body([{ ...original, name: "user_script" }, output(original)]), observe(), "ORIGINAL_TOOL_NOT_NATIVE"],
    [body(), observe({ session_id: "42" }), "HANDLE_ARGUMENTS_INVALID"],
    [body(), observe({ session_id: 1.5 }), "HANDLE_ARGUMENTS_INVALID"],
    [body(), observe({ session_id: 43 }), "ORIGINAL_RUNNING_HANDLE_UNVERIFIED"],
    [body(), observe({ chars: "interrupt" }), "HANDLE_ARGUMENTS_INVALID"],
    [body(), observe({ kind: "exec_cell", session_id: "42" }), "HANDLE_KIND_MISMATCH"],
    [body([original, output(original, "Wall time: 1 seconds\nProcess exited with code 0\nOutput:\nProcess running with session ID 42")]), observe(), "ORIGINAL_RUNNING_HANDLE_UNVERIFIED"],
  ];
  for (const [request, item, expected] of cases) reason(request, item, expected);
  const customCall = { ...original, type: "custom_tool_call", name: "exec", input: 'text({"session_id":42});' };
  reason(body([customCall, output(customCall, '{"session_id":42}')]), observe(), "ORIGINAL_TOOL_NOT_NATIVE");
});

test("string or terminal code output cannot become a numeric running session", () => {
  const call = { ...original, type: "custom_tool_call", name: "exec", input: 'text(await tools.exec_command({"cmd":"original"}));' };
  for (const result of [{ session_id: "42" }, { session_id: session, exit_code: 0 },
    { session_id: session, terminal: true }, { exit_code: 0 }, { output: "Process running with session ID 42" }]) {
    reason(body([call, output(call, JSON.stringify(result))]), observe(), "ORIGINAL_RUNNING_HANDLE_UNVERIFIED");
  }
});

test("absent or incompatible declaration is not invented from exec alone", () => {
  for (const description of ["", declaration.replace("session_id: number", "session_id: string"),
    declaration.replace("write_stdin", "remote_write_stdin")]) {
    const projection = nativeToolProjection(body(undefined, [{ ...custom, description }]), options);
    assert.equal(projection.nativeObservation, null);
    assert.ok(!names(projection.body.tools).includes("nokiy_observe"));
    assert.throws(() => nativeToolItem(observe(), projection), (error) => {
      assert.equal(error.details.native_observation.reason, "NATIVE_OBSERVATION_NOT_ADVERTISED"); return true;
    });
  }
});

test("native number schema admits zero and canonical native-function JSON without a fake minimum", () => {
  const value = { session_id: 0, output: "running" };
  const projection = nativeToolProjection(body([original, output(original, JSON.stringify(value))]), options);
  assert.equal(nativeToolItem(observe({ session_id: 0 }), projection).input, 'text(await tools.write_stdin({"session_id":0,"chars":""}));');
  const tools = [fn("exec_command"), fn("write_stdin", { session_id: { type: "number" }, chars: { type: "string" } })];
  const direct = nativeToolProjection(body([original, output(original, JSON.stringify(value))], tools), options);
  assert.deepEqual(direct.body.tools.find((tool) => tool.name === "nokiy_observe").parameters.properties.session_id, { type: "integer" });
  assert.equal(nativeToolItem(observe({ session_id: 0 }, "function_call"), direct).arguments, '{"session_id":0,"chars":""}');
  const text = nativeText.replace("session ID 42", "session ID 0");
  assert.equal(nativeToolItem(observe({ session_id: 0 }), nativeToolProjection(body([original, output(original, text)]), options)).name, "exec");
  reason(body([original, output(original, JSON.stringify({ output: JSON.stringify(value) }))]), observe({ session_id: 0 }), "ORIGINAL_RUNNING_HANDLE_UNVERIFIED");
  reason(body([original, output(original, `Wall time: 1 seconds\nProcess exited with code 0\nOutput:\n${JSON.stringify(value)}`)]), observe({ session_id: 0 }), "ORIGINAL_RUNNING_HANDLE_UNVERIFIED");
});

test("unqualified historical native calls reuse unique binding inference, never remote ambiguity", () => {
  const unqualified = { ...original };
  delete unqualified.namespace;
  const request = body([unqualified, output(unqualified, '{"session_id":42,"output":"running"}')]);
  const before = structuredClone(request);
  assert.equal(nativeToolItem(observe(), nativeToolProjection(request, options)).namespace, "functions");
  assert.deepEqual(request, before);
  request.tools.push({ type: "namespace", name: "remote_mcp", tools: [fn("exec_command")] });
  reason(request, observe(), "ORIGINAL_TOOL_AMBIGUOUS");
  reason(body([{ ...unqualified, namespace: "remote_mcp" }, output(unqualified)]), observe(), "ORIGINAL_TOOL_NOT_NATIVE");
});

test("foreign exec namespaces and incomplete native schemas cannot create observation authority", () => {
  const remote = { tools: [{ type: "namespace", name: "remote_mcp", tools: [custom] }], input: [] };
  const projection = nativeToolProjection(remote, options);
  assert.equal(projection.nativeObservation, null);
  const invalid = fn("write_stdin", { session_id: { type: "integer" }, chars: { type: "string" } });
  invalid.parameters.required.push("approval");
  assert.equal(nativeToolProjection(body(undefined, [fn("exec_command"), invalid]), options).nativeObservation, null);
  const bounded = fn("write_stdin", { session_id: { type: "integer", minimum: 1, maximum: 10 }, chars: { type: "string" } });
  reason(body(undefined, [fn("exec_command"), bounded]), observe({}, "function_call"), "NATIVE_SCHEMA_MISMATCH");
});

test("SSE observation mapping preserves pairing and blocks non-advertised raw exec", () => {
  const projection = nativeToolProjection(body(), options);
  const parser = nativeSseProjector(nativeEventProjector(projection));
  const call = observe();
  assert.equal(parser.push(Buffer.from(`data: ${JSON.stringify({ type: "response.output_item.added", item: { ...call, input: "" } })}\n\n`)), "");
  assert.equal(parser.push(Buffer.from(`data: ${JSON.stringify({ type: "response.custom_tool_call_input.delta", item_id: call.id, delta: call.input })}\n\n`)), "");
  const text = parser.push(Buffer.from(`data: ${JSON.stringify({ type: "response.output_item.done", item: call })}\n\n`));
  const result = JSON.parse(text.slice(6));
  assert.equal(result.item.call_id, call.call_id);
  assert.equal(result.item.name, "exec");
  assert.equal(result.item.namespace, "functions");
  assert.throws(() => nativeEventProjector(projection)({ type: "response.output_item.done", item: { ...call, name: "exec" } }),
    { code: "NOKIY_COORDINATOR_TOOL_FORBIDDEN" });
});
