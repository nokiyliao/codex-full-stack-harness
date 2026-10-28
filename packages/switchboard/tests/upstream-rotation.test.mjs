import assert from "node:assert/strict";
import http from "node:http";
import test from "node:test";

import WebSocket, { WebSocketServer } from "ws";

// Read at module load by server.mjs, so set before the dynamic import below.
process.env.SWITCHBOARD_UPSTREAM_ROTATE_MS = "150";

const { EXTERNAL_EFFECT_SCOPE, EXTERNAL_ORCHESTRATION_PROTOCOL } = await import("../src/external-admission.mjs");
const { createSwitchboardServer } = await import("../src/server.mjs");

const CAPABILITY = "test-capability-abcdefghijklmnopqrstuvwxyz";
const PREFIX = `/${CAPABILITY}/backend-api/codex/v1`;
const MODEL = "gpt-5.6-sol";
const user = (text) => ({ type: "message", role: "user", content: text });
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const listen = (server) => new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server.address().port)));
const close = (server) => new Promise((resolve) => server.close(resolve));

class IdleWorker {
  async runResponses() { throw new Error("external route not expected"); }
  async closeSession() {}
  close() {}
}

async function fixture(t) {
  const official = http.createServer();
  const officialWss = new WebSocketServer({ server: official });
  const requests = [];
  let connections = 0;
  officialWss.on("connection", (ws) => {
    const connection = ++connections;
    ws.on("message", (raw) => {
      const body = JSON.parse(raw.toString());
      requests.push({ connection, body });
      const id = `resp_official_${requests.length}`;
      // Reasoning without encrypted_content cannot be replayed, so it leaves no chain.
      const unreplayable = JSON.stringify(body.input ?? []).includes("unreplayable");
      const output = [...(unreplayable ? [{ type: "reasoning", summary: [] }] : []),
        { type: "message", role: "assistant", content: [{ type: "output_text", text: `done ${requests.length}` }] }];
      for (const item of output) ws.send(JSON.stringify({ type: "response.output_item.done", item }));
      ws.send(JSON.stringify({ type: "response.completed", response: { id, output,
        usage: { input_tokens: 100, input_tokens_details: { cached_tokens: 0 }, output_tokens: 1 } } }));
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
    officialOrigin: `http://127.0.0.1:${officialPort}`,
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
    const ws = new WebSocket(`ws://127.0.0.1:${address.port}${PREFIX}/responses`, { headers: { session_id: "fixture-session" } });
    clients.push(ws);
    await new Promise((resolve, reject) => { ws.once("open", resolve); ws.once("error", reject); });
    return ws;
  };
  return { requests, connect, connections: () => connections };
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
      resolve({ terminal: event, events });
    };
    ws.on("message", onMessage);
    ws.send(JSON.stringify({ type: "response.create", ...body }));
  });
}

test("an aged upstream is rotated before a chained turn, carrying the exact chain", async (t) => {
  const state = await fixture(t);
  const ws = await state.connect();
  const first = await turn(ws, { model: MODEL, instructions: "fixture", input: [user("one")] });
  assert.equal(first.terminal.type, "response.completed");
  // A young connection keeps chaining on the same upstream.
  const second = await turn(ws, { model: MODEL, instructions: "fixture", previous_response_id: first.terminal.response.id, input: [user("two")] });
  assert.equal(state.requests[1].connection, 1);
  assert.equal(state.requests[1].body.previous_response_id, first.terminal.response.id);
  await sleep(200);
  const third = await turn(ws, { model: MODEL, instructions: "fixture", previous_response_id: second.terminal.response.id, input: [user("three")] });
  assert.equal(third.terminal.type, "response.completed");
  const rotated = state.requests[2];
  assert.equal(rotated.connection, 2, "the aged upstream was replaced");
  assert.equal(rotated.body.previous_response_id, undefined);
  assert.deepEqual(rotated.body.input.map((item) => item.content?.[0]?.text ?? item.content),
    ["one", "done 1", "two", "done 2", "three"]);
  // The client keeps chaining to the response produced on the fresh upstream.
  const fourth = await turn(ws, { model: MODEL, instructions: "fixture", previous_response_id: third.terminal.response.id, input: [user("four")] });
  assert.equal(fourth.terminal.type, "response.completed");
  assert.equal(state.requests[3].connection, 2);
  assert.equal(state.requests[3].body.previous_response_id, third.terminal.response.id);
  assert.deepEqual(state.requests[3].body.input, [user("four")]);
});

test("a full request on an aged upstream opens a fresh connection unchanged", async (t) => {
  const state = await fixture(t);
  const ws = await state.connect();
  await turn(ws, { model: MODEL, instructions: "fixture", input: [user("one")] });
  await sleep(200);
  const full = await turn(ws, { model: MODEL, instructions: "fixture", input: [user("again")] });
  assert.equal(full.terminal.type, "response.completed");
  assert.equal(state.requests[1].connection, 2);
  assert.deepEqual(state.requests[1].body.input, [user("again")]);
  // An unknown continuation is never rewritten: it stays on the current upstream.
  await sleep(200);
  const unknown = await turn(ws, { model: MODEL, instructions: "fixture", previous_response_id: "resp_unknown", input: [user("x")] });
  assert.equal(unknown.terminal.type, "response.completed");
  assert.equal(state.requests[2].connection, 2);
  assert.equal(state.requests[2].body.previous_response_id, "resp_unknown");
});

test("a chained turn with no chain to carry stays on the aged upstream", async (t) => {
  const logs = [], write = process.stdout.write;
  process.stdout.write = function (chunk, ...args) {
    try { logs.push(JSON.parse(String(chunk))); } catch { /* Non-log test output. */ }
    return write.call(this, chunk, ...args);
  };
  t.after(() => { process.stdout.write = write; });
  const state = await fixture(t);
  const ws = await state.connect();
  const first = await turn(ws, { model: MODEL, instructions: "fixture", input: [user("unreplayable")] });
  assert.equal(first.terminal.type, "response.completed");
  await sleep(200);
  // Before the fix this read the absent chain and failed the turn with SWITCHBOARD_INTERNAL_ERROR.
  const chained = await turn(ws, { model: MODEL, instructions: "fixture", previous_response_id: first.terminal.response.id, input: [user("two")] });
  assert.equal(chained.terminal.type, "response.completed", JSON.stringify(chained.terminal));
  assert.equal(state.requests[1].connection, 1);
  assert.equal(state.requests[1].body.previous_response_id, first.terminal.response.id);
  assert.deepEqual(state.requests[1].body.input, [user("two")]);
  const completion = logs.find((record) => record.route === "openai" && record.status === 200
    && record.diagnostic?.upstream_rotation_deferred);
  assert.equal(completion?.diagnostic.upstream_rotation_deferred, "age");
  assert.equal(logs.some((record) => record.error_code === "SWITCHBOARD_INTERNAL_ERROR"), false);
});

