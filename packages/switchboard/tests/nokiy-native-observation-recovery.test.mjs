import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import http from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import WebSocket, { WebSocketServer } from "ws";
import { createSwitchboardServer } from "../src/server.mjs";
import { nativeToolProjection, nativeToolItem, continueNativeObservation } from "../src/nokiy-native-tool.mjs";
import { inputPair } from "../src/nokiy-result-delivery.mjs";
import { createReplayRestorer } from "../src/nokiy-replay-restore.mjs";

const host = { python: "/never/executed/python", script: "/never/executed/host.py", coordinator_mode: "dispatch_only_v1" };
const options = { host, worker: "gpt-6.1-sol", workerEffort: "max", workflow: "nokiy" };
const description = 'declare const tools: { exec_command(args: { cmd: string; }): Promise<unknown>; };\n'
  + 'declare const tools: { write_stdin(args: { session_id: number; chars?: string; }): Promise<unknown>; };';
const tools = [{ type: "namespace", name: "functions", tools: [
  { type: "custom", name: "exec", description, format: { type: "text" } },
] }];
const original = { type: "custom_tool_call", name: "exec", namespace: "functions", id: "original_item",
  call_id: "original_call", input: 'text(await tools.exec_command({"cmd":"PRIVATE_ORIGINAL_COMMAND"}));' };
const other = { ...original, id: "other_item", call_id: "other_call" };
const output = (call = original, session = 42, extra = {}) => ({
  type: call.type === "function_call" ? "function_call_output" : "custom_tool_call_output",
  call_id: call.call_id, output: JSON.stringify({ session_id: session, output: "PRIVATE_NATIVE_OUTPUT", ...extra }),
});
const request = (input = [], extra = {}) => ({ tools, input, ...extra });
const observe = (args = {}, type = "custom_tool_call") => ({ type, name: "nokiy_observe", id: "observe_item",
  call_id: "observe_call", [type === "custom_tool_call" ? "input" : "arguments"]: JSON.stringify({
    kind: "exec_session", source_call_id: original.call_id, session_id: 42, ...args,
  }) });
const project = (body, continuation = null) => continueNativeObservation(nativeToolProjection(body, options), continuation);
const carry = (projection, call = null) => ({ state: projection.nativeObservationState, call });
function rejection(projection, args = {}, expected = "ORIGINAL_PAIR_UNAVAILABLE") {
  let details;
  assert.throws(() => nativeToolItem(observe(args), projection), (error) => {
    assert.equal(error.code, "NOKIY_NATIVE_OBSERVATION_INVALID");
    details = error.details.native_observation;
    assert.equal(details.reason, expected);
    assert.ok(!JSON.stringify(details).includes("PRIVATE_"));
    return true;
  });
  return details;
}
async function executeFixture(item) {
  const waits = [], emitted = [];
  await runInNewContext(`(async () => { ${item.input} })()`, {
    text: (value) => emitted.push(JSON.parse(JSON.stringify(value))),
    tools: {
      write_stdin: async (args) => { waits.push(JSON.parse(JSON.stringify(args))); return { exit_code: 0, output: "fixture terminal" }; },
      exec_command: async () => assert.fail("Observation must not execute or replay a command"),
    },
  });
  return { waits, emitted };
}
async function uncertainNativeOutput(item, session_id = 42) {
  const launches = [], waits = [], emitted = [];
  await runInNewContext(`(async () => { ${item.input} })()`, {
    text: (value) => emitted.push(JSON.parse(JSON.stringify(value))),
    tools: {
      exec_command: async (args) => {
        launches.push(JSON.parse(JSON.stringify(args)));
        return { session_id, output: "PRIVATE_NATIVE_OUTPUT" };
      },
      write_stdin: async (args) => {
        assert.deepEqual(Object.keys(args).sort(), ["chars", "session_id"], "only advertised native wait arguments");
        waits.push(JSON.parse(JSON.stringify(args)));
        throw new Error("fixture observation unavailable");
      },
    },
  });
  assert.deepEqual(launches, [{ cmd: "OFFLINE_FIXTURE_ONLY" }], "launch the original exact command once");
  assert.deepEqual(waits, [{ session_id, chars: "" }]);
  assert.equal(emitted.length, 1);
  assert.equal(emitted[0].terminal, false);
  assert.equal(emitted[0].exit_code, null);
  assert.equal(emitted[0].session_id, session_id);
  assert.equal(emitted[0].recovery_session_id, session_id);
  assert.equal(emitted[0].output, "PRIVATE_NATIVE_OUTPUT");
  assert.match(emitted[0].wait_error, /Recover the original session; do not replay/);
  return { type: "custom_tool_call_output", call_id: item.call_id, output: JSON.stringify(emitted[0]) };
}

for (const mode of ["nokiy", "nokiy-direct", "nokiy-efficient"]) test(`${mode} exact old dispatch retains observation provenance across host generations`, async (t) => {
  const root = await mkdtemp(join(tmpdir(), "nokiy-observe-generation-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const request = (input = [], extra = {}) => ({ tools, input,
    ...(mode === "nokiy-efficient" ? { model: "gpt-6.1-sol", reasoning: { effort: "max" } } : {}), ...extra });
  const priorOptions = { ...options, workflow: mode === "nokiy" ? "nokiy" : "nokiy-direct", workerEffort: "xhigh",
    ...(mode === "nokiy-efficient" ? { profile: { slug: mode, workflow: "nokiy-direct",
      coordinator: { model: "gpt-6.1-sol", reasoning_effort: "max" } } } : {}),
    host: { ...host, script: "/never/generation-a/host.py" } };
  const nextOptions = { ...priorOptions, host: { ...host, script: "/never/generation-b/host.py" } };
  const modelCall = { type: "custom_tool_call", name: "nokiy_execute", call_id: original.call_id,
    input: JSON.stringify({ operation: "run", draft: { workspace: "/workspace", artifact_root: "/receipts",
      authority_effect: "none", prompt: "Read one assigned file." }, action: { read_scopes: ["src/a.py"] } }) };
  const previous = nativeToolProjection(request(), priorOptions);
  const source = nativeToolItem(modelCall, previous);
  const path = join(root, "replay.jsonl");
  createReplayRestorer({ path }).record(modelCall, source);
  const replay = createReplayRestorer({ path });
  const next = (body, continuation = null, extra = {}) => continueNativeObservation(nativeToolProjection(body, {
    ...nextOptions, recordedNativeCall: replay.originalCall, ...extra,
  }), continuation);
  const full = next(request([source, output(source)]));
  assert.equal(full.nativeObservationState.references.length, 1);
  if (mode === "nokiy-efficient") {
    // Efficient already retains native tools; this change grants no new alias.
    assert.ok(!full.body.tools.some(tool => tool.name === "nokiy_observe"));
    return;
  }
  assert.deepEqual(await executeFixture(nativeToolItem(observe(), full)), {
    waits: [{ session_id: 42, chars: "" }], emitted: [{ exit_code: 0, output: "fixture terminal" }],
  });
  const oldState = continueNativeObservation(nativeToolProjection(request([source, output(source)]), priorOptions));
  const reloaded = next(request([source, output(source)], { previous_response_id: "r1" }), carry(oldState));
  assert.equal(reloaded.nativeObservationState.references.length, 1, "Replayed full pair must not erase valid retained identity");
  assert.equal(nativeToolItem(observe(), reloaded).name, "exec");
  const oldEmpty = continueNativeObservation(previous);
  const delta = next(request([output(source)], { previous_response_id: "r1" }), carry(oldEmpty, source));
  assert.equal(nativeToolItem(observe(), delta).name, "exec");

  const waitTool = { type: "function", name: "wait", parameters: { type: "object", properties: {
    cell_id: { type: "string" }, terminate: { type: "boolean" },
  }, required: ["cell_id"], additionalProperties: false } };
  const waiting = { type: "function_call", name: "wait", namespace: "functions", call_id: "outer_wait",
    arguments: '{"cell_id":"original_cell"}' };
  const yielded = { ...output(source), output: "Script running with cell ID original_cell\nWall time 1 seconds\nOutput:\n" };
  const uncertain = { type: "function_call_output", call_id: waiting.call_id,
    output: "Script completed\nWall time 1 seconds\nOutput:\n" + JSON.stringify({ session_id: 42, recovery_session_id: 42,
      terminal: false, exit_code: null, output: "PRIVATE_NATIVE_OUTPUT" }) };
  const cells = next({ ...request([source, yielded, waiting, uncertain]),
    tools: [{ ...tools[0], tools: [...tools[0].tools, waitTool] }] });
  assert.equal(nativeToolItem(observe(), cells).input, 'text(await tools.write_stdin({"session_id":42,"chars":""}));');
  assert.ok(!JSON.stringify(cells.nativeObservationState).includes("PRIVATE_"));

  const rejects = (body, extra = {}, continuation = null) => assert.throws(
    () => nativeToolItem(observe(), next(body, continuation, extra)), { code: "NOKIY_NATIVE_OBSERVATION_INVALID" });
  rejects(request([source, output(source)]), { recordedNativeCall: null });
  for (const changed of [{ ...source, input: source.input + " " }, { ...source, call_id: "changed_call" },
    { ...source, namespace: "remote_mcp" }]) rejects(request([changed, output(changed)]));
  for (const extra of [{ worker: "different-worker" }, { workerEffort: "max" }, { workerServiceTier: "priority" }]) {
    rejects(request([source, output(source)]), extra);
  }
  for (const value of [JSON.stringify({ session_id: 42, exit_code: 0, terminal: true, output: "done" }),
    JSON.stringify({ output: "log mentions session_id 42" })]) {
    rejects(request([source, { ...output(source), output: value }]));
  }
  rejects(request([source]));
  assert.throws(() => nativeToolItem(observe({ session_id: 43 }), full), { code: "NOKIY_NATIVE_OBSERVATION_INVALID" });
  rejects(request([source, output(source, 43)], { previous_response_id: "r2" }), {}, carry(oldState));
  rejects(request([output(source)], { previous_response_id: "r2" }), {}, {
    state: { ...oldState.nativeObservationState, owner: "foreign" }, call: source,
  });
});

test("full admitted pair retains metadata only and emits exactly one empty-stdin native wait", async () => {
  const body = request([original, output()]), before = structuredClone(body);
  const projection = project(body);
  assert.equal(projection.nativeObservationState.references.length, 1);
  const reference = projection.nativeObservationState.references[0];
  assert.deepEqual(Object.keys(reference).sort(), ["call_sha256", "session_id", "source_call_id"]);
  assert.equal(reference.source_call_id, original.call_id);
  assert.equal(reference.session_id, 42);
  assert.match(reference.call_sha256, /^[a-f0-9]{64}$/);
  assert.ok(!JSON.stringify(projection.nativeObservationState).includes("PRIVATE_"));
  const item = nativeToolItem(observe(), projection);
  assert.equal(item.input, 'text(await tools.write_stdin({"session_id":42,"chars":""}));');
  assert.equal(item.call_id, "observe_call");
  assert.equal(item.namespace, "functions");
  assert.deepEqual(await executeFixture(item), { waits: [{ session_id: 42, chars: "" }],
    emitted: [{ exit_code: 0, output: "fixture terminal" }] });
  assert.deepEqual(body, before);
});

test("single-output trusted continuation works without result delivery; an intervening control round keeps the original", () => {
  const empty = project(request());
  const first = project(request([output()], { previous_response_id: "r1" }), carry(empty, original));
  assert.equal(first.resultDeliveryEnabled, false);
  assert.equal(first.continuationCall, null);
  assert.equal(nativeToolItem(observe(), first).name, "exec");
  const body = request([output(other, 73)], { previous_response_id: "r2" }), before = structuredClone(body);
  const second = project(body, carry(first, other));
  assert.deepEqual(second.nativeObservationState.references.map((ref) => ref.session_id), [42, 73]);
  assert.equal(nativeToolItem(observe(), second).input, 'text(await tools.write_stdin({"session_id":42,"chars":""}));');
  assert.deepEqual(body, before, "retained evidence must not reconstruct provider input");
  rejection(project(body), {}, "ORIGINAL_PAIR_UNAVAILABLE");
});

test("unknown and mismatched handles, duplicate/conflicting current evidence cannot use a retained reference", () => {
  const first = project(request([original, output()]));
  const resumed = project(request([], { previous_response_id: "r2" }), carry(first));
  rejection(resumed, { source_call_id: "unknown_call" });
  rejection(resumed, { session_id: 43 }, "ORIGINAL_RUNNING_HANDLE_UNVERIFIED");
  rejection(resumed, { session_id: "42" }, "HANDLE_ARGUMENTS_INVALID");
  rejection(resumed, { chars: "interrupt" }, "HANDLE_ARGUMENTS_INVALID");
  for (const [input, condition] of [
    [[original, original, output()], "duplicate_call"],
    [[original, output(), output()], "duplicate_output"],
    [[output(), original], "output_before_call"],
    [[original, other, output()], "nonadjacent_pair"],
    [[original, { ...output(), type: "function_call_output" }], "call_output_type_mismatch"],
  ]) {
    const projection = project(request(input, { previous_response_id: "r2" }), carry(first));
    assert.equal(rejection(projection).pair_diagnostics.condition, condition);
    rejection(project(request([], { previous_response_id: "r3" }), carry(projection)));
  }
  for (const input of [[original, output(original, 43)],
    [{ ...original, input: 'text(await tools.exec_command({"cmd":"CHANGED"}));' }, output()]]) {
    const projection = project(request(input, { previous_response_id: "r2" }), carry(first));
    assert.equal(rejection(projection).pair_diagnostics.condition, "retained_reference_conflict");
    assert.equal(projection.nativeObservationState.references.length, 0);
  }
  const foreign = project(request([{ ...original, namespace: "remote_mcp" }, output()],
    { previous_response_id: "r2" }), carry(first));
  rejection(foreign, {}, "ORIGINAL_TOOL_NOT_NATIVE");
  rejection(project(request([], { previous_response_id: "r3" }), carry(foreign)));
  assert.equal(inputPair(request([original, other, output()]), null), null,
    "shared result-delivery adjacency is unchanged");
});

test("missing and invalid continuation evidence has precise metadata-only diagnostics", () => {
  const empty = project(request());
  for (const [body, continuation, condition] of [
    [request(), null, "missing_pair"],
    [request([original]), null, "missing_output"],
    [request([output()]), carry(empty, original), "continuation_response_missing"],
    [request([output()], { previous_response_id: "r1" }), null, "continuation_call_missing"],
    [request([output()], { previous_response_id: "r1" }), carry(empty, other), "continuation_call_mismatch"],
    [request([output(), { role: "user", content: "PRIVATE_PROMPT" }], { previous_response_id: "r1" }),
      carry(empty, original), "continuation_input_not_single_output"],
  ]) {
    const projection = project(body, continuation);
    const diagnostics = rejection(projection).pair_diagnostics;
    assert.equal(diagnostics.condition, condition);
    assert.equal(typeof diagnostics.call_count, "number");
    assert.equal(typeof diagnostics.output_count, "number");
    assert.equal(projection.nativeObservationState.references.length, 0);
  }
});

test("prose, model proof, output alone, foreign identity and changed owner never seed retained authority", () => {
  const proof = JSON.stringify([original, output()]);
  for (const input of [[{ role: "user", content: proof }],
    [{ type: "message", role: "assistant", content: proof }], [output()],
    [{ ...original, namespace: "remote_mcp" }, output()],
    [{ ...original, input: 'text({"session_id":42,"output":"spoof"});' }, output()]]) {
    const projection = project(request(input, { previous_response_id: "untrusted",
      nativeObservationState: { references: [{ source_call_id: original.call_id, session_id: 42 }] } }));
    assert.equal(projection.nativeObservationState.references.length, 0);
    rejection(project(request([], { previous_response_id: "next" }), carry(projection)));
  }
  const first = project(request([original, output()]));
  const topLevel = nativeToolProjection(request([], { tools: tools[0].tools, previous_response_id: "r2" }), options);
  const changed = continueNativeObservation(topLevel, carry(first, original));
  assert.equal(rejection(changed).pair_diagnostics.continuity, "owner_mismatch");
  assert.equal(changed.nativeObservationState.references.length, 0);
});

test("terminal, invalid native output and changed canonical wait handle never become retained sessions", () => {
  const admitted = project(request([original, output()]));
  for (const value of [output(original, 42, { exit_code: 0 }), output(original, 42, { terminal: true }),
    output(original, "42"), { ...output(), output: "PRIVATE_LOG session_id=42" }]) {
    for (const continuation of [null, carry(admitted)]) {
      const projection = project(request([original, value], { previous_response_id: "r1" }), continuation);
      assert.equal(projection.nativeObservationState.references.length, 0);
      rejection(projection, {}, "ORIGINAL_RUNNING_HANDLE_UNVERIFIED");
      rejection(project(request([], { previous_response_id: "r2" }), carry(projection)));
    }
  }
  const wait = { ...original, input: 'text(await tools.write_stdin({"session_id":41,"chars":""}));' };
  const projection = project(request([wait, output()]));
  rejection(projection, {}, "ORIGINAL_RUNNING_HANDLE_UNVERIFIED");
  assert.equal(projection.nativeObservationState.references.length, 0);
});

test("retention is bounded to 32 references and current native number bounds still control observation", () => {
  let projection = project(request([original, output()]));
  for (let index = 0; index < 32; index++) {
    const call = { ...other, call_id: `bounded_${index}` };
    projection = project(request([call, output(call, index + 100)], { previous_response_id: `r${index}` }), carry(projection));
  }
  assert.equal(projection.nativeObservationState.references.length, 32);
  rejection(project(request([], { previous_response_id: "last" }), carry(projection)));
  assert.equal(nativeToolItem(observe(), project(request([original, output()]))).name, "exec");
  const fnTools = [{ type: "namespace", name: "functions", tools: [
    { type: "function", name: "exec_command", parameters: { type: "object", properties: { cmd: { type: "string" } }, required: ["cmd"] } },
    { type: "function", name: "write_stdin", parameters: { type: "object", properties: {
      session_id: { type: "integer", maximum: 10 }, chars: { type: "string" },
    }, required: ["session_id"] } },
  ] }];
  const call = { type: "function_call", name: "exec_command", namespace: "functions", call_id: original.call_id, arguments: '{"cmd":"fixture"}' };
  const first = project(request([call, output(call)], { tools: fnTools }));
  const resumed = project(request([], { tools: fnTools, previous_response_id: "r2" }), carry(first));
  assert.throws(() => nativeToolItem(observe({}, "function_call"), resumed), (error) => {
    assert.equal(error.details.native_observation.reason, "NATIVE_SCHEMA_MISMATCH"); return true;
  });
});

async function loopback(t) {
  const root = await mkdtemp(join(tmpdir(), "nokiy-observation-recovery-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const script = join(root, "host.py"), bytes = "# Offline fixture; never executed.\n";
  await writeFile(script, bytes);
  const descriptor = { ...host, python: process.execPath, script,
    sha256: createHash("sha256").update(bytes).digest("hex") };
  let upstreamCalls = 0;
  const upstream = http.createServer();
  const wss = new WebSocketServer({ server: upstream });
  wss.on("connection", (socket) => socket.on("message", (bytes) => {
    upstreamCalls++;
    const body = JSON.parse(bytes.toString());
    const step = !body.previous_response_id ? 1 : body.previous_response_id === "r1" ? 2 : 3;
    const item = step < 3 ? { type: "custom_tool_call", name: "nokiy_native", status: "completed",
      id: step === 1 ? "original_item" : "other_item", call_id: step === 1 ? "original_call" : "other_call",
      input: '{"cmd":"OFFLINE_FIXTURE_ONLY"}' } : { ...observe(), status: "completed" };
    socket.send(JSON.stringify({ type: "response.output_item.done", item }));
    socket.send(JSON.stringify({ type: "response.completed", response: { id: `r${step}`, status: "completed", output: [item] } }));
  }));
  await new Promise((resolve) => upstream.listen(0, "127.0.0.1", resolve));
  t.after(async () => {
    for (const client of wss.clients) client.terminate();
    await new Promise((resolve) => wss.close(resolve));
    await new Promise((resolve) => upstream.close(resolve));
  });
  const fakeWorker = { close() {}, async closeSession() {} };
  const profiles = ["nokiy", "nokiy-alt"].map((slug) => ({ slug, display_name: "Offline observation fixture", workflow: "nokiy",
    coordinator: { model: "gpt-6-astra", reasoning_effort: "max" },
    worker: { model: "gpt-6.1-sol", reasoning_effort: "max", service_tier: "default" } }));
  const board = await createSwitchboardServer({
    runtime: { installation_capability: "offline-observation-abcdefghijklmnop" },
    admission: { status: "ADMITTED", external_batch_admitted: true,
      official_models: ["gpt-6-astra", "gpt-6.1-sol"],
      protocol_compatibility: { external_model_orchestration: "responses-tool-loop-v2" },
      policy: { external_models: "allow", effect_scope: "local_inference_routing_only" },
      authority_effect: "local_inference_routing_only", local_nokiy_direct: { admitted: true, host: descriptor } },
    workflowProfiles: profiles,
    modelCatalog: { models: ["gpt-6-astra", "gpt-6.1-sol"].map((slug) => ({ slug,
      supported_reasoning_levels: [{ effort: "max" }, { effort: "ultra" }] })) },
    xaiWorker: fakeWorker, agyWorker: fakeWorker, webWorker: fakeWorker, openrouterWorker: fakeWorker,
    skipFileGate: true, officialOrigin: `http://127.0.0.1:${upstream.address().port}`,
  });
  const address = await board.listen({ host: "127.0.0.1", port: 0 });
  t.after(() => board.close());
  const url = `ws://127.0.0.1:${address.port}/offline-observation-abcdefghijklmnop/backend-api/codex/v1/responses`;
  const connect = async () => {
    const socket = new WebSocket(url);
    t.after(() => socket.terminate());
    await new Promise((resolve, reject) => { socket.once("open", resolve); socket.once("error", reject); });
    return socket;
  };
  return { connect, profiles, upstreamCalls: () => upstreamCalls };
}
function exchange(socket, body) {
  return new Promise((resolve, reject) => {
    const receive = (bytes) => {
      const event = JSON.parse(bytes.toString());
      if (event.type === "error" || event.type === "response.completed") {
        socket.off("message", receive);
        if (event.type === "error") reject(new Error(JSON.stringify(event)));
        else resolve(event.response);
      }
    };
    socket.on("message", receive);
    socket.send(JSON.stringify({ ...body, type: "response.create" }));
  });
}

test("actual WS continuation survives an intervening native round but rejects stale, foreign, model and tools continuity", { timeout: 15000 }, async (t) => {
  const fixture = await loopback(t);
  for (const mode of ["valid", "stale", "foreign", "model", "tools"]) {
    const socket = await fixture.connect();
    const body = request([], { model: "nokiy", reasoning: { effort: "max" } });
    const first = await exchange(socket, body);
    assert.equal(first.output[0].name, "exec");
    assert.equal(first.output[0].call_id, original.call_id);
    const second = await exchange(socket, { ...body, previous_response_id: first.id,
      input: [await uncertainNativeOutput(first.output[0])] });
    assert.equal(second.output[0].call_id, "other_call");
    const target = mode === "foreign" ? await fixture.connect() : socket;
    const third = await exchange(target, { ...body, previous_response_id: mode === "stale" ? "stale" : second.id,
      input: [await uncertainNativeOutput(second.output[0], 73)],
      ...(mode === "model" ? { model: "nokiy-alt" } : {}),
      ...(mode === "tools" ? { tools: [...tools, { type: "function", name: "unrelated", parameters: { type: "object" } }] } : {}),
    });
    const result = await executeFixture(third.output[0]);
    if (mode === "valid") {
      assert.deepEqual(result.waits, [{ session_id: 42, chars: "" }]);
      assert.deepEqual(result.emitted, [{ exit_code: 0, output: "fixture terminal" }]);
    } else {
      assert.deepEqual(result.waits, []);
      assert.equal(result.emitted[0].execution_started, false);
      assert.equal(result.emitted[0].error.details.native_observation.reason, "ORIGINAL_PAIR_UNAVAILABLE");
      assert.equal(result.emitted[0].error.details.native_observation.pair_diagnostics.condition, "missing_pair");
      assert.equal(result.emitted[0].error.details.native_observation.pair_diagnostics.continuity,
        { stale: "response_mismatch", foreign: "connection_state_unavailable", model: "model_mismatch", tools: "tools_mismatch" }[mode]);
    }
  }
  assert.equal(fixture.upstreamCalls(), 15, "one upstream request per turn, never replay");
  const socket = await fixture.connect();
  const body = request([], { model: "nokiy", reasoning: { effort: "max" } });
  const first = await exchange(socket, body);
  const second = await exchange(socket, { ...body, previous_response_id: first.id,
    input: [await uncertainNativeOutput(first.output[0])] });
  fixture.profiles[0] = { ...fixture.profiles[0], display_name: "Changed profile identity" };
  await assert.rejects(exchange(socket, { ...body, previous_response_id: second.id,
    input: [await uncertainNativeOutput(second.output[0], 73)] }),
    /NOKIY_WORKFLOW_CHANGED_DURING_CONTINUATION/);
  assert.equal(fixture.upstreamCalls(), 17, "changed workflow is rejected before upstream dispatch");
});
