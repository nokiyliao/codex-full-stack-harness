import assert from "node:assert/strict";
import http from "node:http";
import test from "node:test";

import WebSocket, { WebSocketServer } from "ws";

// Read at module load by nokiy-direct-route.mjs, so set before the dynamic imports below.
process.env.SWITCHBOARD_PREDICTION_TAIL_BYTES = "2048";

const { EXTERNAL_EFFECT_SCOPE, EXTERNAL_ORCHESTRATION_PROTOCOL } = await import("../src/external-admission.mjs");
const { createSwitchboardServer } = await import("../src/server.mjs");
const { trimNativePrediction } = await import("../src/nokiy-direct-route.mjs");

const CAPABILITY = "test-capability-abcdefghijklmnopqrstuvwxyz";
const PREFIX = `/${CAPABILITY}/backend-api/codex/v1`;
const MODEL = "gpt-5.6-sol";
const listen = (server) => new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server.address().port)));
const close = (server) => new Promise((resolve) => server.close(resolve));
const prediction = { "x-codex-turn-metadata": JSON.stringify({ thread_source: "composer_predictions", prediction_source_thread_id: "parent" }) };
const history = () => [
  { type: "additional_tools", tools: [{ type: "function", name: "exec_command", parameters: { type: "object" } }] },
  { type: "message", role: "developer", content: [{ type: "input_text", text: "base instructions" }] },
  ...Array.from({ length: 24 }, (_, index) => ({ type: "message", role: index % 2 ? "assistant" : "user",
    content: [{ type: index % 2 ? "output_text" : "input_text", text: "turn " + index + " " + "x".repeat(400) }] })),
];
const usage = { input_tokens: 10, input_tokens_details: { cached_tokens: 0 }, output_tokens: 1 };

class IdleWorker {
  async runResponses() { throw new Error("external route not expected"); }
  async closeSession() {}
  close() {}
}

async function fixture(t) {
  const requests = [];
  const official = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (chunk) => { raw += chunk; });
    req.on("end", () => {
      requests.push({ transport: "http", body: JSON.parse(raw) });
      const event = { type: "response.completed", response: { id: "resp_http_" + requests.length, output: [], usage } };
      res.writeHead(200, { "content-type": "text/event-stream" });
      res.end("event: response.completed\ndata: " + JSON.stringify(event) + "\n\n");
    });
  });
  const officialWss = new WebSocketServer({ server: official });
  officialWss.on("connection", (ws) => {
    ws.on("message", (raw) => {
      requests.push({ transport: "ws", body: JSON.parse(raw.toString()) });
      ws.send(JSON.stringify({ type: "response.completed", response: { id: "resp_ws_" + requests.length, output: [], usage } }));
    });
  });
  const officialPort = await listen(official);
  const idle = new IdleWorker();
  const switchboard = await createSwitchboardServer({
    runtime: { installation_capability: CAPABILITY, host: "127.0.0.1", port: 0 },
    admission: {
      status: "ADMITTED", external_batch_admitted: true,
      authority_effect: EXTERNAL_EFFECT_SCOPE,
      protocol_compatibility: { external_model_orchestration: EXTERNAL_ORCHESTRATION_PROTOCOL },
      policy: { external_models: "allow", effect_scope: EXTERNAL_EFFECT_SCOPE },
      official_models: [MODEL],
    },
    xaiWorker: idle, agyWorker: idle, webWorker: idle, openrouterWorker: idle,
    skipFileGate: true,
    officialOrigin: "http://127.0.0.1:" + officialPort,
  });
  const address = await switchboard.listen({ host: "127.0.0.1", port: 0 });
  const clients = [];
  t.after(async () => {
    await Promise.all(clients.map((ws) => ws.readyState === WebSocket.CLOSED
      ? Promise.resolve() : new Promise((resolve) => { ws.once("close", resolve); ws.terminate(); })));
    for (const ws of officialWss.clients) ws.terminate();
    switchboard.server.closeAllConnections();
    official.closeAllConnections();
    await switchboard.close();
    officialWss.close();
    await close(official);
  });
  const connect = async () => {
    const ws = new WebSocket("ws://127.0.0.1:" + address.port + PREFIX + "/responses", { headers: { session_id: "fixture-session" } });
    clients.push(ws);
    await new Promise((resolve, reject) => { ws.once("open", resolve); ws.once("error", reject); });
    return ws;
  };
  const post = async (body) => {
    const response = await fetch("http://127.0.0.1:" + address.port + PREFIX + "/responses", {
      method: "POST", headers: { "content-type": "application/json", session_id: "fixture-session" }, body: JSON.stringify(body) });
    await response.text();
    return response.status;
  };
  return { requests, connect, post };
}

function turn(ws, body) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { ws.off("message", onMessage); reject(new Error("fixture turn timed out")); }, 5_000);
    const onMessage = (raw) => {
      const event = JSON.parse(raw.toString());
      if (!["response.completed", "response.failed", "error"].includes(event.type)) return;
      clearTimeout(timer);
      ws.off("message", onMessage);
      resolve(event);
    };
    ws.on("message", onMessage);
    ws.send(JSON.stringify({ type: "response.create", ...body }));
  });
}

function assertTrimmed(input) {
  const full = history();
  assert.ok(input.length < full.length, "the prediction history is trimmed");
  assert.deepEqual(input.slice(0, 2), full.slice(0, 2), "the native prefix is kept byte-identical");
  assert.equal(input[2].role, "user", "the tail starts at a user message");
  assert.deepEqual(input.at(-1), full.at(-1), "the newest item is kept");
}

test("native composer predictions keep the prefix and a recent tail; Nokiy and chained requests are left alone", () => {
  const trimmed = trimNativePrediction({ model: MODEL, client_metadata: prediction, input: history() });
  assertTrimmed(trimmed.input);
  assert.ok(trimmed.stats.after_bytes < trimmed.stats.before_bytes);
  assert.equal(trimNativePrediction({ model: "nokiy-direct", client_metadata: prediction, input: history() }), null);
  assert.equal(trimNativePrediction({ model: MODEL, previous_response_id: "resp_1", client_metadata: prediction, input: history() }), null);
  assert.equal(trimNativePrediction({ model: MODEL, input: history() }), null);
});

test("a native WebSocket prediction reaches the official upstream trimmed, an ordinary turn unchanged", async (t) => {
  const state = await fixture(t);
  const ws = await state.connect();
  assert.equal((await turn(ws, { model: MODEL, client_metadata: prediction, input: history() })).type, "response.completed");
  assert.equal((await turn(ws, { model: MODEL, input: history() })).type, "response.completed");
  const [predicted, ordinary] = state.requests.filter((request) => request.transport === "ws").map((request) => request.body);
  assertTrimmed(predicted.input);
  assert.deepEqual(ordinary.input, history());
});

test("a native HTTP prediction reaches the official upstream trimmed", async (t) => {
  const state = await fixture(t);
  assert.equal(await state.post({ model: MODEL, stream: true, client_metadata: prediction, input: history() }), 200);
  assert.equal(await state.post({ model: MODEL, stream: true, input: history() }), 200);
  const [predicted, ordinary] = state.requests.filter((request) => request.transport === "http").map((request) => request.body);
  assertTrimmed(predicted.input);
  assert.deepEqual(ordinary.input, history());
});
