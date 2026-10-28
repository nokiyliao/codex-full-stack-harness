import assert from "node:assert/strict";
import http from "node:http";
import test from "node:test";

import WebSocket, { WebSocketServer } from "ws";

// Read at module load by server.mjs, so set before the dynamic import below.
process.env.SWITCHBOARD_CACHE_KEEPALIVE_MS = "60";
process.env.SWITCHBOARD_CACHE_KEEPALIVE_MAX = "2";

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

// Official upstream double: a turn whose prompt ends in "call tool" returns a function call.
async function fixture(t) {
  const official = http.createServer();
  const officialWss = new WebSocketServer({ server: official });
  const requests = [];
  officialWss.on("connection", (ws, req) => {
    const connection = { headers: req.headers };
    ws.on("message", (raw) => {
      const body = JSON.parse(raw.toString());
      requests.push({ connection, body });
      const id = `resp_official_${requests.length}`;
      const last = Array.isArray(body.input) ? body.input.at(-1) : null;
      // Refreshes are capped generating requests; the official client never caps output.
      const isRefresh = body.max_output_tokens !== undefined;
      const wantsTool = !isRefresh && last?.content === "call tool";
      const sealed = last?.content !== "call tool unsealed";
      const coldRefresh = isRefresh && body.input?.[0]?.content === "call tool cold";
      const output = wantsTool || !sealed || (!isRefresh && last?.content === "call tool cold")
        ? [{ type: "reasoning", id: `rs_${requests.length}`, summary: [], ...(sealed ? { encrypted_content: "sealed" } : {}) },
          { type: "function_call", id: `fc_${requests.length}`, call_id: "call_1", name: "exec_command", arguments: "{}" }]
        : isRefresh ? [] : [{ type: "message", role: "assistant", content: [{ type: "output_text", text: "done" }] }];
      for (const item of output) ws.send(JSON.stringify({ type: "response.output_item.done", item }));
      ws.send(JSON.stringify({ type: isRefresh ? "response.incomplete" : "response.completed", response: { id, output,
        usage: { input_tokens: 100, input_tokens_details: { cached_tokens: isRefresh && !coldRefresh ? 96 : 0 }, output_tokens: 1 } } }));
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
  return { requests, connect };
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

test("a tool wait replays the accepted prefix as capped same-session generating refreshes", async (t) => {
  const state = await fixture(t);
  const ws = await state.connect();
  const tools = [{ type: "function", name: "exec_command", parameters: { type: "object" } }];
  const first = await turn(ws, { model: MODEL, instructions: "fixture", tools, input: [user("call tool")] });
  assert.equal(first.terminal.type, "response.completed");
  // The downstream never sees keep-alive traffic.
  assert.ok(first.events.every((event) => event.response?.id !== "resp_official_2"));
  await sleep(400);
  const keepalives = state.requests.filter(({ body }) => body.max_output_tokens !== undefined);
  assert.equal(keepalives.length, 2, "MAX caps the refresh count");
  for (const { body, connection } of keepalives) {
    assert.equal(body.model, MODEL);
    assert.equal(body.generate, undefined, "generate:false bypasses the prompt cache");
    assert.equal(body.max_output_tokens, 16);
    assert.equal(body.instructions, "fixture");
    assert.deepEqual(body.tools, tools);
    assert.equal(body.previous_response_id, undefined);
    // The refresh replays the accepted request input, never an unanswered trailing tool call.
    assert.deepEqual(body.input, [user("call tool")]);
    assert.equal(connection.headers.session_id, "fixture-session");
  }
  // The real continuation is untouched, and the next tool wait chains from the full history.
  const second = await turn(ws, { model: MODEL, instructions: "fixture", tools, previous_response_id: first.terminal.response.id,
    input: [{ type: "function_call_output", call_id: "call_1", output: "ok" }, user("call tool")] });
  assert.equal(second.terminal.type, "response.completed");
  const forwarded = state.requests.filter(({ body }) => body.max_output_tokens === undefined);
  assert.equal(forwarded.length, 2);
  // Warmed refreshes moved the continuation to a fresh upstream carrying the full chain.
  assert.equal(forwarded[1].body.previous_response_id, undefined);
  assert.equal(forwarded[1].body.input.length, 5);
  await sleep(150);
  const chained = state.requests.filter(({ body }) => body.max_output_tokens !== undefined).at(-1).body;
  assert.deepEqual(chained.input.map((item) => item.type), ["message", "reasoning", "function_call", "function_call_output", "message"]);
  assert.equal(chained.input[1].encrypted_content, "sealed");
  assert.equal(chained.input[1].id, "rs_1", "reasoning ids replay with their encrypted content");
  assert.equal(Object.hasOwn(chained.input[2], "id"), false, "store:false output ids are not replayed");
});

test("a new downstream request stops pending refreshes and final answers schedule none", async (t) => {
  const state = await fixture(t);
  const ws = await state.connect();
  const tools = [{ type: "function", name: "exec_command", parameters: { type: "object" } }];
  const first = await turn(ws, { model: MODEL, tools, input: [user("call tool")] });
  await turn(ws, { model: MODEL, tools, previous_response_id: first.terminal.response.id,
    input: [{ type: "function_call_output", call_id: "call_1", output: "ok" }] });
  await sleep(300);
  assert.equal(state.requests.filter(({ body }) => body.max_output_tokens !== undefined).length, 0);
});

test("after a warmed wait the continuation moves to a fresh upstream with the full chain", async (t) => {
  const state = await fixture(t);
  const ws = await state.connect();
  const tools = [{ type: "function", name: "exec_command", parameters: { type: "object" } }];
  const first = await turn(ws, { model: MODEL, instructions: "fixture", tools, input: [user("call tool")] });
  await sleep(100); // one successful refresh (MS=60) marks the chain warmed
  const firstConnection = state.requests[0].connection;
  const second = await turn(ws, { model: MODEL, instructions: "fixture", tools, previous_response_id: first.terminal.response.id,
    input: [{ type: "function_call_output", call_id: "call_1", output: "ok" }] });
  assert.equal(second.terminal.type, "response.completed");
  assert.equal(ws.readyState, WebSocket.OPEN, "the downstream continuation is not retired");
  const forwarded = state.requests.filter(({ body }) => body.max_output_tokens === undefined);
  assert.equal(forwarded.length, 2);
  assert.notEqual(forwarded[1].connection, firstConnection, "a fresh upstream carries the continuation");
  assert.equal(forwarded[1].body.previous_response_id, undefined);
  assert.deepEqual(forwarded[1].body.input.map((item) => item.type), ["message", "reasoning", "function_call", "function_call_output"]);
  // Later turns keep chaining normally on the new upstream.
  const third = await turn(ws, { model: MODEL, instructions: "fixture", tools, previous_response_id: second.terminal.response.id,
    input: [user("thanks")] });
  assert.equal(third.terminal.type, "response.completed");
  const last = state.requests.filter(({ body }) => body.max_output_tokens === undefined).at(-1);
  assert.equal(last.body.previous_response_id, second.terminal.response.id);
  assert.equal(last.connection, forwarded[1].connection);
});


test("a refresh that misses the cache stops the keep-alive after one attempt", async (t) => {
  const state = await fixture(t);
  const ws = await state.connect();
  const tools = [{ type: "function", name: "exec_command", parameters: { type: "object" } }];
  const first = await turn(ws, { model: MODEL, tools, input: [user("call tool cold")] });
  assert.equal(first.terminal.type, "response.completed");
  await sleep(400);
  assert.equal(state.requests.filter(({ body }) => body.max_output_tokens !== undefined).length, 1);
});


test("reasoning without encrypted content is not replayable and schedules no refresh", async (t) => {
  const state = await fixture(t);
  const ws = await state.connect();
  const tools = [{ type: "function", name: "exec_command", parameters: { type: "object" } }];
  const first = await turn(ws, { model: MODEL, tools, input: [user("call tool unsealed")] });
  assert.equal(first.terminal.type, "response.completed");
  await sleep(250);
  assert.equal(state.requests.filter(({ body }) => body.max_output_tokens !== undefined).length, 0);
});

