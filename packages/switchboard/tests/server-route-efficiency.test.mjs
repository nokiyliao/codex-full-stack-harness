import assert from "node:assert/strict";
import http from "node:http";
import test from "node:test";

import WebSocket, { WebSocketServer } from "ws";

import { createClaudeBridge } from "../src/claude-bridge-route.mjs";
import { EXTERNAL_EFFECT_SCOPE, EXTERNAL_ORCHESTRATION_PROTOCOL } from "../src/external-admission.mjs";
import { createSwitchboardServer, webSocketCompressionEnabled } from "../src/server.mjs";
import { SwitchboardError } from "../src/shared.mjs";

const CAPABILITY = "test-capability-abcdefghijklmnopqrstuvwxyz";
const PREFIX = `/${CAPABILITY}/backend-api/codex/v1`;
const SEALED = "claude-bridge-v1:fixture-checkpoint";
const CLAUDE_SUFFIX = "claude" + "a".repeat(32);
const user = (text) => ({ type: "message", role: "user", content: text });
const checkpoint = () => ({ type: "compaction", encrypted_content: SEALED });
const openedCheckpoint = () => user("<context_checkpoint>opened fixture</context_checkpoint>");
const claudeOutput = () => [
  checkpoint(),
  { type: "message", role: "assistant", id: `msg_${CLAUDE_SUFFIX}`, content: [{ type: "output_text", text: "claude answer" }] },
  { type: "function_call", id: `fc_${CLAUDE_SUFFIX}`, call_id: "call_fixture", name: "lookup", arguments: "{}" },
];

function prepare(input, token) {
  assert.equal(token, "fixture-token");
  let checkpoints = 0;
  let ids = 0;
  const cleaned = input.map((item) => {
    if (item?.type === "compaction" && item.encrypted_content === SEALED) {
      checkpoints += 1;
      return openedCheckpoint();
    }
    if (typeof item?.id === "string" && item.id.endsWith(CLAUDE_SUFFIX)) {
      ids += 1;
      const { id, ...rest } = item;
      return rest;
    }
    return item;
  });
  return checkpoints || ids ? { input: cleaned, checkpoints, ids } : null;
}

class RecordingWorker {
  constructor(route) {
    this.route = route;
    this.requests = [];
    this.rejectNextContinuation = false;
  }

  async runResponses(body, { onEvent }) {
    this.requests.push(structuredClone(body));
    if (body.previous_response_id && this.rejectNextContinuation) {
      this.rejectNextContinuation = false;
      throw new SwitchboardError("previous_response_not_found", "fixture provider state expired");
    }
    const id = `resp_${this.route}_${this.requests.length}`;
    const output = [{ type: "message", role: "assistant", content: [{ type: "output_text", text: `${this.route} answer` }] }];
    onEvent({ type: "response.created", response: { id } });
    onEvent({ type: "response.completed", response: { id, output } });
  }

  async closeSession() {}
  close() {}
}

const listen = (server) => new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server.address().port)));
const close = (server) => new Promise((resolve) => server.close(resolve));

async function fixture(t) {
  const bridgeRequests = [];
  const bridge = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    bridgeRequests.push(JSON.parse(Buffer.concat(chunks).toString()));
    const id = `resp_claude${String(bridgeRequests.length).padStart(32, "0")}`;
    const output = claudeOutput();
    res.writeHead(200, { "content-type": "text/event-stream" });
    for (const event of [
      { type: "response.created", response: { id } },
      { type: "response.completed", response: { id, output } },
    ]) res.write(`data: ${JSON.stringify(event)}\n\n`);
    res.end();
  });
  const bridgePort = await listen(bridge);
  const official = http.createServer();
  const officialWss = new WebSocketServer({ server: official, perMessageDeflate: true });
  const officialRequests = [];
  const officialConnections = [];
  officialWss.on("connection", (ws, req) => {
    officialConnections.push({ extensions: ws.extensions, headers: req.headers });
    ws.on("message", (raw) => {
      const body = JSON.parse(raw.toString());
      officialRequests.push(body);
      ws.send(JSON.stringify({ type: "response.completed", response: { id: `resp_official_${officialRequests.length}`, output: [] } }));
    });
  });
  const officialPort = await listen(official);
  const workers = Object.fromEntries(["xai", "agy", "web", "openrouter"].map((route) => [route, new RecordingWorker(route)]));
  const switchboard = await createSwitchboardServer({
    runtime: { installation_capability: CAPABILITY, host: "127.0.0.1", port: 0 },
    admission: {
      status: "ADMITTED", external_batch_admitted: true,
      authority_effect: EXTERNAL_EFFECT_SCOPE,
      protocol_compatibility: { external_model_orchestration: EXTERNAL_ORCHESTRATION_PROTOCOL },
      policy: { external_models: "allow", effect_scope: EXTERNAL_EFFECT_SCOPE },
      official_models: ["gpt-5.6-sol"],
    },
    xaiWorker: workers.xai, agyWorker: workers.agy, webWorker: workers.web, openrouterWorker: workers.openrouter,
    skipFileGate: true,
    officialOrigin: `http://127.0.0.1:${officialPort}`,
    claudeBridge: createClaudeBridge({ port: bridgePort, key: "fixture-key", token: "fixture-token", slugs: ["claude-opus"], prepare }),
  });
  const address = await switchboard.listen({ host: "127.0.0.1", port: 0 });
  const clients = [];
  t.after(async () => {
    const downstreamClosed = clients.map((ws) => ws.readyState === WebSocket.CLOSED
      ? Promise.resolve() : new Promise((resolve) => { ws.once("close", resolve); ws.terminate(); }));
    await Promise.all(downstreamClosed);
    for (const ws of officialWss.clients) ws.terminate();
    switchboard.server.closeAllConnections();
    bridge.closeAllConnections();
    official.closeAllConnections();
    await switchboard.close();
    officialWss.close();
    await close(bridge);
    await close(official);
  });
  const connect = async ({ peer = null, headers = {} } = {}) => {
    if (peer) switchboard.server.prependOnceListener("upgrade", (_req, socket) => {
      Object.defineProperty(socket, "remoteAddress", { value: peer });
    });
    const ws = new WebSocket(`ws://127.0.0.1:${address.port}${PREFIX}/responses`, { perMessageDeflate: true, headers });
    clients.push(ws);
    await new Promise((resolve, reject) => { ws.once("open", resolve); ws.once("error", reject); });
    return ws;
  };
  return { bridgeRequests, officialRequests, officialConnections, workers, connect };
}

function turn(ws, body) {
  return new Promise((resolve, reject) => {
    const events = [];
    const timer = setTimeout(() => { ws.off("message", onMessage); reject(new Error("fixture turn timed out")); }, 5_000);
    const onMessage = (raw) => {
      const event = JSON.parse(raw.toString());
      events.push(event);
      if (!["response.completed", "response.incomplete", "response.failed", "error"].includes(event.type)) return;
      clearTimeout(timer);
      ws.off("message", onMessage);
      resolve(events.at(-1));
    };
    ws.on("message", onMessage);
    ws.send(JSON.stringify({ type: "response.create", ...body }));
  });
}

function canonicalHistory(delta) {
  const [, assistant, call] = claudeOutput();
  const { id: assistantId, ...cleanAssistant } = assistant;
  const { id: callId, ...cleanCall } = call;
  return [openedCheckpoint(), user("original request"), openedCheckpoint(), cleanAssistant, cleanCall, ...delta];
}

test("known Claude WS history is expanded and sanitized exactly once for every non-Claude route", async (t) => {
  const state = await fixture(t);
  const ws = await state.connect();
  const first = await turn(ws, { model: "claude-opus", input: [checkpoint(), user("original request")] });
  const responseId = first.response.id;
  const delta = [{ type: "function_call_output", call_id: "call_fixture", output: "lookup result" }];
  for (const [model, route] of [
    ["gpt-5.6-sol", "openai"], ["grok-4.6", "xai"],
    ["gemini-3.8-flash-high", "agy"], ["chatgpt-web/high", "web"],
  ]) {
    const result = await turn(ws, { model, previous_response_id: responseId, input: delta, tools: [] });
    assert.equal(result.type, "response.completed", `${route} should complete`);
    const sent = route === "openai" ? state.officialRequests.at(-1) : state.workers[route].requests.at(-1);
    assert.deepEqual(sent.input, canonicalHistory(delta), `${route} must not duplicate or omit reconstructed history`);
    assert.equal(Object.hasOwn(sent, "previous_response_id"), false);
    assert.doesNotMatch(JSON.stringify(sent), /claude-bridge-v1:|(?:msg|fc)_claude/);
    if (route === "openai") {
      await turn(ws, { model, previous_response_id: result.response.id, input: [user("official delta")] });
      assert.deepEqual(state.officialRequests.at(-1).input, [user("official delta")]);
      assert.equal(state.officialRequests.at(-1).previous_response_id, result.response.id);
    }
  }
  await turn(ws, { model: "claude-opus", previous_response_id: responseId, input: [user("still Claude")] });
  assert.deepEqual(state.bridgeRequests.at(-1).input, [checkpoint(), user("original request"), ...claudeOutput(), user("still Claude")],
    "cross-route sanitation must not mutate the original Claude history");
});

test("xAI and Web preserve delta continuations, and xAI fallback retains sanitized full history", async (t) => {
  const state = await fixture(t);
  const ws = await state.connect();
  const first = await turn(ws, { model: "claude-opus", input: [checkpoint(), user("original request")] });
  const delta = [{ type: "function_call_output", call_id: "call_fixture", output: "lookup result" }];
  for (const [model, route] of [["grok-4.6", "xai"], ["chatgpt-web/high", "web"]]) {
    const crossed = await turn(ws, { model, previous_response_id: first.response.id, input: delta });
    const continued = await turn(ws, { model, previous_response_id: crossed.response.id, input: [user("delta only")] });
    assert.equal(continued.type, "response.completed");
    assert.deepEqual(state.workers[route].requests.at(-1).input, [user("delta only")]);
    assert.equal(state.workers[route].requests.at(-1).previous_response_id, `resp_${route}_1`);
    if (route !== "xai") continue;
    state.workers.xai.rejectNextContinuation = true;
    const result = await turn(ws, { model, previous_response_id: continued.response.id, input: [user("recover provider state")] });
    assert.equal(result.type, "response.completed");
    const fallback = state.workers.xai.requests.at(-1);
    assert.equal(Object.hasOwn(fallback, "previous_response_id"), false);
    assert.deepEqual(fallback.input.slice(0, canonicalHistory(delta).length), canonicalHistory(delta));
    assert.deepEqual(fallback.input.at(-1), user("recover provider state"));
    assert.equal(fallback.input.filter((item) => item.content === "original request").length, 1);
    assert.doesNotMatch(JSON.stringify(fallback), /claude-bridge-v1:|(?:msg|fc)_claude/);
  }
});

test("AGY linear continuations send only the new items", async (t) => {
  const state = await fixture(t);
  const ws = await state.connect();
  const first = await turn(ws, { model: "gemini-3.8-flash-high", input: [user("agy root")] });
  const second = await turn(ws, {
    model: "gemini-3.8-flash-high",
    previous_response_id: first.response.id,
    input: [user("agy delta")],
  });
  assert.equal(second.type, "response.completed");
  const sent = state.workers.agy.requests.at(-1);
  assert.deepEqual(sent.input, [user("agy delta")]);
  assert.equal(sent.agy_linear_extension, true);
  assert.equal(Object.hasOwn(sent, "previous_response_id"), false);
  const branch = await turn(ws, {
    model: "gemini-3.8-flash-high",
    previous_response_id: first.response.id,
    input: [user("agy branch")],
  });
  assert.equal(branch.type, "response.completed");
  const branched = state.workers.agy.requests.at(-1);
  assert.equal(branched.agy_linear_extension, undefined);
  assert.equal(branched.input[0].content, "agy root");
  assert.equal(branched.input.at(-1).content, "agy branch");
  assert.equal(Object.hasOwn(branched, "previous_response_id"), false);
});

test("unknown WS response IDs retain existing rejection and full-input fallback semantics", async (t) => {
  const state = await fixture(t);
  const ws = await state.connect();
  const missing = await turn(ws, { model: "grok-4.6", previous_response_id: "resp_unknown", input: [] });
  assert.equal(missing.error.code, "CROSS_ROUTE_RESPONSE_STATE_REJECTED");
  assert.equal(missing.status, 409);
  const fallback = await turn(ws, { model: "grok-4.6", previous_response_id: "resp_unknown", input: [user("canonical input")] });
  assert.equal(fallback.type, "response.completed");
  assert.deepEqual(state.workers.xai.requests.at(-1).input, [user("canonical input")]);
  assert.equal(Object.hasOwn(state.workers.xai.requests.at(-1), "previous_response_id"), false);
  const official = await turn(ws, { model: "gpt-5.6-sol", previous_response_id: "resp_unknown_official", input: [] });
  assert.equal(official.type, "response.completed");
  assert.equal(state.officialRequests.at(-1).previous_response_id, "resp_unknown_official");
});

test("loopback WS hops do not negotiate compression", async (t) => {
  const state = await fixture(t);
  const ws = await state.connect({ headers: { host: "api.openai.com", "x-forwarded-for": "192.0.2.1" } });
  assert.equal(ws.extensions, "", "local downstream must not negotiate deflate");
  await turn(ws, { model: "gpt-5.6-sol", input: [user("local upstream")] });
  assert.equal(state.officialConnections.at(-1).extensions, "", "local upstream must not negotiate deflate");
  assert.equal(state.officialConnections.at(-1).headers["sec-websocket-extensions"], undefined);
});

test("non-loopback downstream peers retain negotiated compression", async (t) => {
  const state = await fixture(t);
  const ws = await state.connect({ peer: "192.0.2.1" });
  assert.equal(ws.extensions, "permessage-deflate", "simulated remote peer retains the existing transport policy");
  const result = await turn(ws, { model: "gpt-5.6-sol", input: [user("remote downstream fixture")] });
  assert.equal(result.type, "response.completed");
});

test("WS compression policy recognizes loopback forms and retains remote compression", () => {
  for (const peer of ["localhost", "LOCALHOST", "127.0.0.1", "127.20.30.40", "::1", "[::1]", "::ffff:127.0.0.2", "[::ffff:7f00:2]"]) {
    assert.equal(webSocketCompressionEnabled(peer), false, peer);
  }
  for (const peer of ["api.openai.com", "192.0.2.1", "::ffff:192.0.2.1", "2001:db8::1", "127.example.com", "localhost.example.com"]) {
    assert.equal(webSocketCompressionEnabled(peer), true, peer);
  }
});
