import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import http from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import WebSocket, { WebSocketServer } from "ws";
import { createSwitchboardServer } from "../src/server.mjs";

const thread = "12345678-1234-1234-1234-123456789abc";
const tools = [{ type: "namespace", name: "functions", tools: [
  { type: "custom", name: "exec", format: { type: "text" },
    description: 'declare const tools: { exec_command(args: { cmd: string; }): Promise<unknown>; };' },
] }];
const message = { type: "message", role: "user", content: "PRIVATE_PROMPT" };
const request = (extra = {}) => ({ model: "nokiy-direct", reasoning: { effort: "max" },
  tool_choice: "auto", client_metadata: { thread_id: thread }, input: [message], ...extra });
const full = () => request({ input: [{ type: "additional_tools", tools }, message] });
const nativeCall = () => ({ type: "custom_tool_call", name: "nokiy_native", status: "completed",
  id: "native_item", call_id: "native_call", input: '{"cmd":"OFFLINE_FIXTURE_ONLY"}' });
const completed = (id, output = []) => [
  ...output.map((item, output_index) => ({ type: "response.output_item.done", output_index, item })),
  { type: "response.completed", response: { id, status: "completed", output } },
];
function incomplete(id) {
  const item = { type: "custom_tool_call", name: "nokiy_execute", status: "incomplete",
    id: "partial_item", call_id: "partial_call", input: '{"operation":"run"' };
  return [
    { type: "response.created", response: { id, status: "in_progress" } },
    { type: "response.output_item.added", output_index: 0, item: { ...item, status: "in_progress", input: "" } },
    { type: "response.custom_tool_call_input.delta", item_id: item.id, output_index: 0, delta: item.input },
    { type: "response.output_item.done", output_index: 0, item },
    { type: "response.incomplete", response: { id, status: "incomplete",
      incomplete_details: { reason: "max_output_tokens" }, output: [item] } },
  ];
}

async function loopback(t, respond) {
  const records = [], received = [], write = process.stdout.write;
  t.mock.method(process.stdout, "write", function (chunk, ...args) {
    if (typeof chunk === "string" && chunk.startsWith("{")) {
      records.push(JSON.parse(chunk));
      return true;
    }
    return write.call(this, chunk, ...args);
  });
  const root = await mkdtemp(join(tmpdir(), "nokiy-incomplete-continuation-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const script = join(root, "host.py"), bytes = "# Offline fixture; never executed.\n";
  await writeFile(script, bytes);
  const descriptor = { python: process.execPath, script,
    sha256: createHash("sha256").update(bytes).digest("hex"),
    coordinator_mode: "dispatch_only_v1", parent_result_delivery: "accept_v1" };
  const upstream = http.createServer();
  const wss = new WebSocketServer({ server: upstream });
  wss.on("connection", (socket) => socket.on("message", (bytes) => {
    const body = JSON.parse(bytes.toString());
    received.push(body);
    for (const event of respond(body, received.length)) socket.send(JSON.stringify(event));
  }));
  await new Promise((resolve) => upstream.listen(0, "127.0.0.1", resolve));
  t.after(async () => {
    for (const client of wss.clients) client.terminate();
    await new Promise((resolve) => wss.close(resolve));
    await new Promise((resolve) => upstream.close(resolve));
  });
  const fakeWorker = { close() {}, async closeSession() {} };
  const board = await createSwitchboardServer({
    runtime: { installation_capability: "offline-incomplete-abcdefghijklmnop" },
    admission: { status: "ADMITTED", external_batch_admitted: true,
      official_models: ["gpt-6-astra", "gpt-6.1-sol"],
      protocol_compatibility: { external_model_orchestration: "responses-tool-loop-v2" },
      policy: { external_models: "allow", effect_scope: "local_inference_routing_only" },
      authority_effect: "local_inference_routing_only", local_nokiy_direct: { admitted: true, host: descriptor } },
    workflowProfiles: ["nokiy-direct", "nokiy-direct-alt"].map((slug) => ({ slug,
      display_name: "Offline Ultra fixture", workflow: "nokiy-direct",
      coordinator: { model: "gpt-6-astra", reasoning_effort: "max" },
      worker: { model: "gpt-6.1-sol", reasoning_effort: "max", service_tier: "default" } })),
    modelCatalog: { models: ["gpt-6-astra", "gpt-6.1-sol"].map((slug) => ({ slug,
      supported_reasoning_levels: [{ effort: "max" }, { effort: "ultra" }] })) },
    xaiWorker: fakeWorker, agyWorker: fakeWorker, webWorker: fakeWorker, openrouterWorker: fakeWorker,
    skipFileGate: true, officialOrigin: `http://127.0.0.1:${upstream.address().port}`,
  });
  const address = await board.listen({ host: "127.0.0.1", port: 0 });
  t.after(() => board.close());
  const connect = async () => {
    const socket = new WebSocket(`ws://127.0.0.1:${address.port}/offline-incomplete-abcdefghijklmnop/backend-api/codex/v1/responses`);
    t.after(() => socket.terminate());
    await new Promise((resolve, reject) => { socket.once("open", resolve); socket.once("error", reject); });
    return socket;
  };
  return { connect, records, received,
    dispatches: () => records.filter((record) => record.attempt_lifecycle?.event_type === "attempt_dispatch")
      .map((record) => record.attempt_lifecycle) };
}

function exchange(socket, body) {
  return new Promise((resolve, reject) => {
    const events = [];
    const timer = setTimeout(() => {
      socket.off("message", receive);
      reject(new Error("Offline continuation turn timed out"));
    }, 5000);
    const receive = (bytes) => {
      const event = JSON.parse(bytes.toString());
      events.push(event);
      if (["response.completed", "response.incomplete", "response.failed", "error"].includes(event.type)) {
        clearTimeout(timer);
        socket.off("message", receive);
        if (["error", "response.failed"].includes(event.type)) reject(new Error(JSON.stringify(event)));
        else resolve({ events, type: event.type, response: event.response });
      }
    };
    socket.on("message", receive);
    socket.send(JSON.stringify({ ...body, type: "response.create" }));
  });
}

test("Ultra incomplete keeps exact successor capability without partial tool dispatch or successful delivery", { timeout: 15000 }, async (t) => {
  const fixture = await loopback(t, (_body, index) => index === 1
    ? incomplete("resp_incomplete") : completed("resp_successor", [nativeCall()]));
  const socket = await fixture.connect();
  const first = await exchange(socket, full());
  assert.equal(first.type, "response.incomplete");
  assert.equal(first.response.status, "incomplete");
  assert.deepEqual(first.response.output, []);
  assert.deepEqual(first.events.map((event) => event.type), ["response.created", "response.incomplete"]);
  const terminal = fixture.records.find((record) => record.attempt_lifecycle?.event_type === "attempt_terminal").attempt_lifecycle;
  assert.equal(terminal.terminal_event, "response.incomplete");
  assert.equal(terminal.outcome, "failure");
  const delivery = fixture.records.find((record) => record.provider_transport_timing?.terminal_event === "response.incomplete");
  assert.equal(delivery.provider_transport_timing.completed_ms, null);
  assert.equal(delivery.result_delivery.eligibility, "ineligible");
  assert.equal(delivery.result_delivery.outcome, "unobserved");

  const second = await exchange(socket, request({ previous_response_id: first.response.id }));
  assert.equal(second.response.output[0].name, "exec");
  assert.equal(second.response.output[0].namespace, "functions");
  assert.match(second.response.output[0].input, /tools\.exec_command/);
  assert.equal(fixture.received.length, 2, "one upstream dispatch per request, no replay");
  assert.equal(fixture.received[1].previous_response_id, "resp_incomplete");
  assert.deepEqual(fixture.received[1].input, [message], "no partial calls cached or replayed");
  assert.equal(fixture.received[1].tool_choice, "auto");
  assert.equal(fixture.dispatches()[1].request_provenance.resolved.dispatch, "native-tool");
  assert.equal(fixture.records.some((record) => record.route === "nokiy-native-tool-missing"), false);
});

test("incomplete capability never crosses response, model, catalog or connection mismatches", { timeout: 15000 }, async (t) => {
  const fixture = await loopback(t, (body, index) => body.previous_response_id
    ? completed(`resp_${index}`) : incomplete(`resp_${index}`));
  for (const condition of ["response_mismatch", "model_mismatch", "tools_mismatch", "connection_state_unavailable"]) {
    const socket = await fixture.connect();
    const first = await exchange(socket, full());
    const target = condition === "connection_state_unavailable" ? await fixture.connect() : socket;
    await exchange(target, request({ previous_response_id: condition === "response_mismatch" ? "resp_stale" : first.response.id,
      ...(condition === "model_mismatch" ? { model: "nokiy-direct-alt" } : {}),
      ...(condition === "tools_mismatch" ? { tools: [] } : {}) }));
    const diagnostic = fixture.records.filter((record) => record.route === "nokiy-native-tool-missing").at(-1);
    assert.equal(diagnostic.diagnostic.native_tool_missing.continuation_condition, condition);
    assert.equal(diagnostic.diagnostic.native_tool_missing.continuation_terminal_event,
      condition === "connection_state_unavailable" ? null : "response.incomplete");
    assert.equal(diagnostic.request_id, fixture.dispatches().at(-1).request_id);
    assert.equal(diagnostic.request_correlation.previous_response_id,
      condition === "response_mismatch" ? "resp_stale" : first.response.id);
    assert.equal(fixture.received.at(-1).tool_choice, "none");
    assert.equal(fixture.dispatches().at(-1).request_provenance.resolved.dispatch, "none");
  }
  assert.equal(fixture.received.length, 8);
  assert.doesNotMatch(JSON.stringify(fixture.records), /PRIVATE_PROMPT/);
});

test("an answer-only WS fallback recovers through a freshly advertised supported exec catalog", { timeout: 15000 }, async (t) => {
  const fixture = await loopback(t, (_body, index) => completed(`resp_${index}`, index === 3 ? [nativeCall()] : []));
  const socket = await fixture.connect();
  const first = await exchange(socket, request({ previous_response_id: "unbound" }));
  const second = await exchange(socket, request({ previous_response_id: first.response.id }));
  const third = await exchange(socket, request({ previous_response_id: second.response.id,
    input: [{ type: "additional_tools", tools }, message] }));
  assert.deepEqual(fixture.received.map((body) => body.tool_choice), ["none", "none", "auto"]);
  assert.deepEqual(fixture.dispatches().map((record) => record.request_provenance.resolved.dispatch), ["none", "none", "native-tool"]);
  assert.ok(fixture.received[2].tools.some((tool) => tool.name === "nokiy_execute"));
  assert.equal(fixture.received[2].previous_response_id, second.response.id);
  assert.equal(third.response.output[0].name, "exec");
  assert.equal(fixture.received.length, 3);
});
