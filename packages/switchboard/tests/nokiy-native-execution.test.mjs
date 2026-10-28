import assert from "node:assert/strict";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import { nativeToolProjection, nativeToolItem, nativeEventProjector, nativeSseProjector } from "../src/nokiy-native-tool.mjs";
import { coordinatorContract } from "../src/nokiy-coordinator-policy.mjs";

const host = { python: "/not/executed/python", script: "/not/executed/host.py", coordinator_mode: "dispatch_only_v1" };
const options = { host, worker: "gpt-6.1-sol", workerEffort: "max", workflow: "nokiy" };
const declaration = 'declare const tools: { exec_command(args: {\n// Native command, not JavaScript.\ncmd: string;\nworkdir?: string;\nmax_output_tokens?: number;\nyield_time_ms?: number;\ntty?: boolean;\nlogin?: boolean;\nshell?: string;\n}): Promise<unknown>; };\ndeclare const tools: { write_stdin(args: {session_id: number; chars?: string;}): Promise<unknown>; };';
const custom = { type: "custom", name: "exec", format: { type: "text" }, description: declaration };
const fn = { type: "function", name: "exec_command", parameters: { type: "object", properties: {
  cmd: { type: "string" }, workdir: { type: "string" }, yield_time_ms: { type: "integer", minimum: 250, maximum: 30000 },
}, required: ["cmd"], additionalProperties: false } };
const body = (tool = custom) => ({ tools: [{ type: "namespace", name: "functions", tools: [structuredClone(tool)] }], input: [] });
const call = (args, type = "custom_tool_call") => ({ type, name: "nokiy_native", id: "native_item", call_id: "native_call",
  [type === "function_call" ? "arguments" : "input"]: JSON.stringify(args) });
const names = (tools) => tools.flatMap((tool) => tool.type === "namespace" ? names(tool.tools) : [tool.name]);

test("native code mode performs one exact command, without a model, JSpace or another execution plane", async () => {
  const request = body(), original = structuredClone(request);
  const projection = nativeToolProjection(request, options);
  assert.deepEqual(names(projection.body.tools), ["nokiy_execute", "nokiy_observe", "nokiy_native"]);
  const args = { cmd: "python -B scripts/ops/start_codex_task.py --task-id exact-task", workdir: "/exact/worktree" };
  const item = nativeToolItem(call(args), projection);
  assert.equal(item.name, "exec");
  assert.equal(item.namespace, "functions");
  assert.equal(item.id, "native_item");
  assert.equal(item.call_id, "native_call");
  const terminal = { exit_code: 0, output: "original terminal result", chunk_id: "original_terminal" };
  for (const initial of [terminal, { session_id: 42, output: "original native receipt\n" }]) {
    const calls = [], waits = [], output = [];
    await runInNewContext(`(async () => { ${item.input} })()`, { text: (x) => output.push(x), tools: {
      exec_command: async (x) => { calls.push(JSON.parse(JSON.stringify(x))); return initial; },
      write_stdin: async (x) => {
        assert.deepEqual(Object.keys(x).sort(), ["chars", "session_id"], "only advertised native wait arguments");
        waits.push(JSON.parse(JSON.stringify(x))); return terminal;
      },
    } });
    assert.deepEqual(calls, [args], "one launch, never replay the original command");
    assert.equal(output.length, 1);
    if (initial === terminal) {
      assert.deepEqual(waits, []);
      assert.equal(output[0], terminal, "short terminal output keeps original native metadata unchanged");
    } else {
      assert.deepEqual(waits, [{ session_id: 42, chars: "" }]);
      assert.equal(output[0].terminal, true);
      assert.equal(output[0].exit_code, terminal.exit_code);
      assert.equal(output[0].chunk_id, terminal.chunk_id);
      assert.equal(output[0].recovery_session_id, initial.session_id);
      assert.equal(output[0].output, initial.output + terminal.output);
    }
  }
  assert.deepEqual(request, original);
  assert.doesNotMatch(item.input, /--payload|spawn|prepare|HOME|CODEX_HOME/);
});

test("function owner preserves its actual parameter schema and native bounds", () => {
  const projection = nativeToolProjection(body(fn), options);
  const tool = projection.body.tools.find((tool) => tool.name === "nokiy_native");
  assert.deepEqual(tool.parameters, fn.parameters);
  const item = nativeToolItem(call({ cmd: "project task", yield_time_ms: 30000 }, "function_call"), projection);
  assert.equal(item.name, "exec_command");
  assert.equal(item.namespace, "functions");
  assert.equal(item.arguments, '{"cmd":"project task","yield_time_ms":30000}');
  for (const args of [{ cmd: "project task", yield_time_ms: 30001 }, { cmd: "task", env: {} }, { cmd: 123 }, {}]) {
    assert.throws(() => nativeToolItem(call(args, "function_call"), projection), { code: "NOKIY_NATIVE_TOOL_INVALID" });
  }
});

test("repeated requests and independent schemas with native IDs retain their capabilities", () => {
  for (let index = 0; index < 80; index++) {
    const tool = structuredClone(fn);
    tool.parameters.$id = "native-command-schema";
    tool.parameters.properties.workdir.description = `Native schema variant ${index % 40}`;
    const projection = nativeToolProjection(body(tool), options);
    assert.ok(projection.nativeExecution);
    assert.equal(nativeToolItem(call({ cmd: "native task" }, "function_call"), projection).name, "exec_command");
  }
});

test("strings stay JSON data, including shell quotes, multiline commands and attempted JavaScript injection", async () => {
  const projection = nativeToolProjection(body(), options);
  for (const cmd of ["printf '%s' 'quoted'", "python -c 'print(1)'\npython -c 'print(2)'", '"); await tools.other({}); //\u2028']) {
    const item = nativeToolItem(call({ cmd }), projection);
    const calls = [];
    await runInNewContext(`(async () => { ${item.input} })()`, { text: () => {}, tools: {
      exec_command: async (x) => { calls.push(x.cmd); return { exit_code: 0, output: "" }; },
      other: () => assert.fail("JSON input must never become injected JavaScript"),
    } });
    assert.deepEqual(calls, [cmd]);
  }
  for (const args of [null, [], { cmd: "task", draft: {} }, { cmd: "task", native_thread_id: "foreign" },
    { cmd: "task", sandbox_permissions: "bypass" }, { cmd: "task", tty: "true" }]) {
    assert.throws(() => nativeToolItem(call(args), projection), { code: "NOKIY_NATIVE_TOOL_INVALID" });
  }
  assert.throws(() => nativeToolItem({ ...call({}), input: "not JSON" }, projection), { code: "NOKIY_NATIVE_TOOL_INVALID" });
  assert.throws(() => nativeToolItem(call({ cmd: "x".repeat(100000) }), projection), { code: "NOKIY_NATIVE_TOOL_INVALID" });
});

test("native permission failures remain failures; the bridge never retries or falls back", async () => {
  const item = nativeToolItem(call({ cmd: "authorized project command" }), nativeToolProjection(body(), options));
  const rejection = new Error("native permission denied"), calls = [];
  await assert.rejects(runInNewContext(`(async () => { ${item.input} })()`, { text: () => assert.fail("No fabricated success"), tools: {
    exec_command: async (args) => { calls.push(args); throw rejection; },
  } }), (error) => error === rejection);
  assert.equal(calls.length, 1);
});

test("remote, ambiguous, missing or incompatible native capabilities never acquire a bridge", () => {
  const requests = [
    body({ ...custom, description: "No exec_command declaration" }),
    body({ ...custom, description: declaration.replace("cmd: string", "cmd: number") }),
    body({ ...custom, description: declaration.replace("cmd: string;", "cmd: string; approval: string;") }),
    body({ ...fn, parameters: { ...fn.parameters, required: [] } }),
    { tools: [{ type: "namespace", name: "remote_mcp", tools: [custom] }], input: [] },
    { tools: [{ type: "namespace", name: "functions", tools: [custom, custom] }], input: [] },
  ];
  for (const request of requests) {
    const projection = nativeToolProjection(request, options);
    assert.equal(projection.nativeExecution, null);
    assert.ok(!names(projection.body.tools).includes("nokiy_native"));
    assert.throws(() => nativeEventProjector(projection)({ type: "response.output_item.done", item: call({ cmd: "not started" }) }),
      { code: "NOKIY_COORDINATOR_TOOL_FORBIDDEN" });
  }
});

test("same-connection continuation retains confirmed schema, never capability from a tool-name binding alone", () => {
  const initial = nativeToolProjection(body(), options);
  const delta = { tools: [], input: [{ type: "message", role: "user", content: "Continue the same task" }] };
  const projection = nativeToolProjection(delta, { ...options, continuationTarget: initial.target,
    continuationCoordinatorBindings: initial.coordinatorBindings });
  assert.equal(nativeToolItem(call({ cmd: "next authorized task stage" }), projection).name, "exec");
  assert.deepEqual(projection.body.input, delta.input);
  const unverified = nativeToolProjection(delta, { ...options,
    continuationTarget: { name: "exec", namespace: "functions", type: "custom" },
    continuationCoordinatorBindings: initial.coordinatorBindings });
  assert.equal(unverified.nativeExecution, null);
  assert.throws(() => nativeToolItem(call({ cmd: "not started" }), unverified), { code: "NOKIY_NATIVE_TOOL_INVALID" });
  const absent = nativeToolProjection(body(), { ...options, host: { ...host, coordinator_mode: undefined } });
  assert.equal(absent.nativeExecution, null, "ordinary native routes remain unchanged");
});

test("SSE waits for complete validated arguments, preserves call pairing and does not enable raw exec", async () => {
  const projection = nativeToolProjection({ ...body(), tool_choice: { type: "custom", name: "nokiy_native" } }, options);
  const parser = nativeSseProjector(nativeEventProjector(projection));
  const item = call({ cmd: "exact project task" });
  assert.equal(parser.push(Buffer.from(`data: ${JSON.stringify({ type: "response.output_item.added", item: { ...item, input: "" } })}\n\n`)), "");
  assert.equal(parser.push(Buffer.from(`data: ${JSON.stringify({ type: "response.custom_tool_call_input.delta", item_id: item.id, delta: item.input })}\n\n`)), "");
  const frame = parser.push(Buffer.from(`data: ${JSON.stringify({ type: "response.output_item.done", item })}\n\n`));
  const actual = JSON.parse(frame.slice(6)).item;
  assert.equal(actual.call_id, item.call_id);
  assert.equal(actual.input, nativeToolItem(item, projection).input);
  const calls = [], output = [], terminal = { exit_code: 0, output: "exact project terminal" };
  await runInNewContext(`(async () => { ${actual.input} })()`, { text: (x) => output.push(x), tools: {
    exec_command: async (args) => { calls.push(JSON.parse(JSON.stringify(args))); return terminal; },
    write_stdin: () => assert.fail("A terminal command must not be observed again"),
  } });
  assert.deepEqual(calls, [{ cmd: "exact project task" }]);
  assert.deepEqual(output, [terminal]);
  assert.equal(output[0], terminal);
  assert.throws(() => nativeEventProjector(projection)({ type: "response.output_item.done", item: { ...item, name: "exec", namespace: "functions" } }),
    { code: "NOKIY_COORDINATOR_TOOL_FORBIDDEN" });
});

test("native command sessions use existing original-pair observation, not worker terminal delivery", () => {
  const initial = nativeToolProjection(body(), options);
  const original = nativeToolItem(call({ cmd: "long task" }), initial);
  const request = { ...body(), input: [original, { type: "custom_tool_call_output", call_id: original.call_id,
    output: '{"session_id":42,"output":"native task running"}' }] };
  const projection = nativeToolProjection(request, { ...options, host: { ...host, parent_result_delivery: "accept_v1" } });
  const poll = nativeToolItem({ type: "custom_tool_call", name: "nokiy_observe", call_id: "poll", input: JSON.stringify({
    kind: "exec_session", source_call_id: original.call_id, session_id: 42,
  }) }, projection);
  assert.equal(poll.input, 'text(await tools.write_stdin({"session_id":42,"chars":""}));');
  assert.equal(projection.resultDelivery, null);
});

test("worker route and context economy remain unchanged; deterministic native control is not semantic parent work", () => {
  const initial = nativeToolProjection(body(), options);
  const legacy = nativeToolProjection(body({ ...custom, description: "" }), options);
  const dispatch = call({ operation: "read_result", artifact_root: "/original", request_id: "original_request" });
  dispatch.name = "nokiy_execute";
  assert.deepEqual(nativeToolItem(dispatch, initial), nativeToolItem(dispatch, legacy));
  assert.equal(initial.worker, legacy.worker);
  assert.equal(initial.workerEffort, legacy.workerEffort);
  assert.deepEqual(initial.operations, legacy.operations);
  assert.deepEqual(initial.body.tools.find((tool) => tool.name === "nokiy_execute"), legacy.body.tools.find((tool) => tool.name === "nokiy_execute"));
  const text = coordinatorContract({ display_name: "Nokiy", workflow: "nokiy", coordinator: { model: "gpt-6-astra", reasoning_effort: "max" },
    worker: { model: "gpt-6.1-sol", reasoning_effort: "max" } });
  assert.match(text, /does not transfer semantic worker work to the parent/);
  assert.match(text, /not worker draft\/action\/DCF command templates/);
  assert.match(text, /Do not use this channel to evade a real native permission denial/);
});

test("dispatch guidance tells coordinators that named external evidence files are inlined", () => {
  // 2026-10-22 i6: Ultra coordinators listed frozen tests outside draft.workspace and lost about ten
  // minutes to unsafe_path refusals and capability gaps before any implementation started.
  const flat = (tools) => tools.flatMap((tool) => tool.type === "namespace" ? flat(tool.tools) : [tool]);
  for (const workflow of ["nokiy", "nokiy-direct"]) {
    const dispatch = flat(nativeToolProjection(body(custom), { ...options, workflow }).body.tools)
      .find((item) => item.name === "nokiy_execute");
    assert.ok(dispatch, "the dispatch tool is advertised");
    assert.match(dispatch.description, /External evidence: an exact regular UTF-8 file outside draft\.workspace/);
    assert.match(dispatch.description, /the host inlines its exact bytes with SHA-256 into the worker prompt while the prompt stays within 64 KiB/);
  }
});

test("native command guidance folds every known step into one round trip", () => {
  const flat = (tools) => tools.flatMap((tool) => tool.type === "namespace" ? flat(tool.tools) : [tool]);
  for (const tool of [custom, fn]) {
    const native = flat(nativeToolProjection(body(tool), options).body.tools).find((item) => item.name === "nokiy_native");
    assert.ok(native, "the native command tool is advertised");
    assert.match(native.description, /separate model round trip that re-reads this whole thread/);
    assert.match(native.description, /chain dependent steps with && or explicit checks/);
    assert.doesNotMatch(native.description, /parallel tool calls/);
  }
});
