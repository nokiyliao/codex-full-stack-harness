import assert from "node:assert/strict";
import http from "node:http";
import test from "node:test";

import WebSocket, { WebSocketServer } from "ws";

// Read at module load by server.mjs: warm rotation after 100 ms, unconditional only after 60 s.
process.env.SWITCHBOARD_UPSTREAM_ROTATE_MS = "60000";
process.env.SWITCHBOARD_UPSTREAM_ROTATE_WARM_MS = "100";
process.env.SWITCHBOARD_UPSTREAM_WARM_IDLE_MS = "600";

const { EXTERNAL_EFFECT_SCOPE, EXTERNAL_ORCHESTRATION_PROTOCOL } = await import("../src/external-admission.mjs");
const { createSwitchboardServer } = await import("../src/server.mjs");

const CAPABILITY = "test-capability-abcdefghijklmnopqrstuvwxyz";
const PREFIX = `/${CAPABILITY}/backend-api/codex/v1`;
const MODEL = "gpt-5.6-sol";
const user = (text) => ({ type: "message", role: "user", content: text });
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const listen = (server) => new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server.address().port)));
const close = (server) => new Promise((resolve) => server.close(resolve));
class IdleWorker { async runResponses() { throw new Error("unexpected"); } async closeSession() {} close() {} }

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
      const output = [{ type: "message", role: "assistant", content: [{ type: "output_text", text: `done ${requests.length}` }] }];
      ws.send(JSON.stringify({ type: "response.output_item.done", item: output[0] }));
      ws.send(JSON.stringify({ type: "response.completed", response: { id, output,
        usage: { input_tokens: 1, input_tokens_details: { cached_tokens: 0 }, output_tokens: 1 } } }));
    });
  });
  const officialPort = await listen(official);
  const idle = new IdleWorker();
  const switchboard = await createSwitchboardServer({
    runtime: { installation_capability: CAPABILITY, host: "127.0.0.1", port: 0 },
    admission: { status: "ADMITTED", external_batch_admitted: true, authority_effect: EXTERNAL_EFFECT_SCOPE,
      protocol_compatibility: { external_model_orchestration: EXTERNAL_ORCHESTRATION_PROTOCOL },
      policy: { external_models: "allow", effect_scope: EXTERNAL_EFFECT_SCOPE }, official_models: [MODEL] },
    xaiWorker: idle, agyWorker: idle, webWorker: idle, openrouterWorker: idle,
    skipFileGate: true, officialOrigin: `http://127.0.0.1:${officialPort}`,
  });
  const address = await switchboard.listen({ host: "127.0.0.1", port: 0 });
  const clients = [];
  t.after(async () => {
    for (const ws of clients) ws.terminate();
    for (const ws of officialWss.clients) ws.terminate();
    switchboard.server.closeAllConnections();
    official.closeAllConnections();
    await switchboard.close();
    officialWss.close();
    await close(official);
  });
  const ws = new WebSocket(`ws://127.0.0.1:${address.port}${PREFIX}/responses`, { headers: { session_id: "warm" } });
  clients.push(ws);
  await new Promise((resolve, reject) => { ws.once("open", resolve); ws.once("error", reject); });
  const turn = (body) => new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("timeout")), 5_000);
    const onMessage = (raw) => {
      const event = JSON.parse(raw.toString());
      if (!["response.completed", "error"].includes(event.type)) return;
      clearTimeout(timer); ws.off("message", onMessage); resolve(event);
    };
    ws.on("message", onMessage);
    ws.send(JSON.stringify({ type: "response.create", ...body }));
  });
  return { requests, turn };
}

test("a warm aged upstream rotates while the chain is still cached", async (t) => {
  const { requests, turn } = await fixture(t);
  const first = await turn({ model: MODEL, input: [user("one")] });
  await sleep(200);
  const second = await turn({ model: MODEL, previous_response_id: first.response.id, input: [user("two")] });
  assert.equal(second.type, "response.completed");
  assert.equal(requests[1].connection, 2, "rotated while warm");
  assert.equal(requests[1].body.previous_response_id, undefined);
});

test("an idle aged upstream is kept until the unconditional limit", async (t) => {
  const { requests, turn } = await fixture(t);
  const first = await turn({ model: MODEL, input: [user("one")] });
  await sleep(800);
  const second = await turn({ model: MODEL, previous_response_id: first.response.id, input: [user("two")] });
  assert.equal(second.type, "response.completed");
  assert.equal(requests[1].connection, 1, "a cold chain keeps its connection-local state");
  assert.equal(requests[1].body.previous_response_id, first.response.id);
});

