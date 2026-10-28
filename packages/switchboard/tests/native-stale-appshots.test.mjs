import assert from "node:assert/strict";
import http from "node:http";
import test from "node:test";

import WebSocket, { WebSocketServer } from "ws";

const { EXTERNAL_EFFECT_SCOPE, EXTERNAL_ORCHESTRATION_PROTOCOL } = await import("../src/external-admission.mjs");
const { createSwitchboardServer } = await import("../src/server.mjs");
const { elideNativeStaleAppshots } = await import("../src/nokiy-direct-route.mjs");

const CAPABILITY = "test-capability-abcdefghijklmnopqrstuvwxyz";
const PREFIX = `/${CAPABILITY}/backend-api/codex/v1`;
const MODEL = "gpt-5.6-sol";
const listen = (server) => new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server.address().port)));
const close = (server) => new Promise((resolve) => server.close(resolve));
const shot = (label) => `<appshot app="Google Chrome" window-title="Home">\n${`${label} `.repeat(400)}\n</appshot>`;
const user = (text) => ({ type: "message", role: "user", content: [{ type: "input_text", text }] });
const history = () => [
  { type: "additional_tools", tools: [{ type: "function", name: "exec_command", parameters: { type: "object" } }] },
  { type: "message", role: "developer", content: [{ type: "input_text", text: "base instructions" }] },
  user(`${shot("stale-one")}\nfirst`), user(`${shot("stale-two")}\nsecond`),
  user(`${shot("kept-three")}\nthird`), user(`${shot("kept-four")}\nfourth`),
  { type: "compaction", encrypted_content: "opaque" },
  user(`${shot("after")}\nnow`),
];
const usage = { input_tokens: 10, input_tokens_details: { cached_tokens: 0 }, output_tokens: 1 };
const omitted = (input) => input.filter((item) => item?.role === "user"
  && item.content.some((part) => part.text.includes("Earlier UI snapshot omitted"))).length;

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

test("native full requests to official models omit stale snapshots; Nokiy, chained and other models do not", () => {
  const official = (name) => name === MODEL;
  assert.equal(omitted(elideNativeStaleAppshots({ model: MODEL, input: history() }, official).input), 2);
  assert.equal(elideNativeStaleAppshots({ model: "nokiy-efficient", input: history() }, official), null);
  assert.equal(elideNativeStaleAppshots({ model: MODEL, previous_response_id: "resp_1", input: history() }, official), null);
  assert.equal(elideNativeStaleAppshots({ model: "claude-opus", input: history() }, official), null);
  process.env.SWITCHBOARD_STALE_APPSHOT_ELISION = "0";
  try { assert.equal(elideNativeStaleAppshots({ model: MODEL, input: history() }, official), null); }
  finally { delete process.env.SWITCHBOARD_STALE_APPSHOT_ELISION; }
});

test("a native HTTP request and a full WebSocket request reach the upstream with the same rewritten history", async (t) => {
  const state = await fixture(t);
  assert.equal(await state.post({ model: MODEL, stream: true, input: history() }), 200);
  const ws = await state.connect();
  assert.equal((await turn(ws, { model: MODEL, input: history() })).type, "response.completed");
  assert.equal((await turn(ws, { model: MODEL, previous_response_id: "resp_ws_2", input: [user(shot("delta"))] })).type, "response.completed");
  const [viaHttp, viaWs, delta] = state.requests.map((request) => request.body);
  assert.equal(omitted(viaHttp.input), 2);
  assert.deepEqual(viaWs.input, viaHttp.input);
  assert.equal(viaHttp.input.at(-1).content[0].text.includes("after "), true);
  assert.equal(omitted(delta.input), 0);
});
