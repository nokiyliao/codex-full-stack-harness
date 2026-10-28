import assert from "node:assert/strict";
import { once } from "node:events";
import http from "node:http";
import net from "node:net";
import test from "node:test";

import WebSocket, { WebSocketServer } from "ws";

const { EXTERNAL_EFFECT_SCOPE, EXTERNAL_ORCHESTRATION_PROTOCOL } = await import("../src/external-admission.mjs");
const { createSwitchboardServer } = await import("../src/server.mjs");

const CAPABILITY = "test-capability-abcdefghijklmnopqrstuvwxyz";
const PREFIX = `/${CAPABILITY}/backend-api/codex/v1`;
const MODEL = "gpt-5.6-sol";
const user = (text) => ({ type: "message", role: "user", content: text });
const listen = (server) => new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server.address().port)));
const close = (server) => new Promise((resolve) => server.close(resolve));

class IdleWorker {
  async runResponses() { throw new Error("external route not expected"); }
  async closeSession() {}
  close() {}
}

async function harness(t) {
  const official = http.createServer();
  const officialWss = new WebSocketServer({ server: official });
  const held = [];
  const requests = [];
  officialWss.on("connection", (ws) => {
    ws.on("message", (raw) => {
      const body = JSON.parse(raw.toString());
      requests.push(body);
      const id = `resp_official_${requests.length}`;
      const finish = () => {
        const output = [{ type: "message", role: "assistant", content: [{ type: "output_text", text: "done" }] }];
        ws.send(JSON.stringify({ type: "response.output_item.done", item: output[0] }));
        ws.send(JSON.stringify({ type: "response.completed", response: { id, output,
          usage: { input_tokens: 10, input_tokens_details: { cached_tokens: 0 }, output_tokens: 1 } } }));
      };
      if (body.input?.at(-1)?.content === "hold") held.push(finish); else finish();
    });
  });
  const officialPort = await listen(official);
  const idle = new IdleWorker();
  const switchboard = await createSwitchboardServer({
    runtime: { installation_capability: CAPABILITY, host: "127.0.0.1", port: 0 },
    admission: {
      status: "ADMITTED", external_batch_admitted: true, authority_effect: EXTERNAL_EFFECT_SCOPE,
      protocol_compatibility: { external_model_orchestration: EXTERNAL_ORCHESTRATION_PROTOCOL },
      policy: { external_models: "allow", effect_scope: EXTERNAL_EFFECT_SCOPE }, official_models: [MODEL],
    },
    xaiWorker: idle, agyWorker: idle, webWorker: idle, openrouterWorker: idle,
    skipFileGate: true, officialOrigin: `http://127.0.0.1:${officialPort}`,
  });
  const address = await switchboard.listen({ host: "127.0.0.1", port: 0 });
  t.after(async () => {
    for (const ws of officialWss.clients) ws.terminate();
    official.closeAllConnections();
    await switchboard.close();
    officialWss.close();
    await close(official);
  });
  const connect = async () => {
    const ws = new WebSocket(`ws://127.0.0.1:${address.port}${PREFIX}/responses`, { headers: { session_id: "drain" } });
    await new Promise((resolve, reject) => { ws.once("open", resolve); ws.once("error", reject); });
    ws.closed = new Promise((resolve) => ws.once("close", (code) => resolve(code)));
    return ws;
  };
  return { switchboard, address, held, requests, connect };
}

test("SIGUSR2-style drain waits for in-flight turns and refuses new ones", async (t) => {
  const { switchboard, address, held, requests, connect } = await harness(t);
  const idleClient = await connect();
  const busy = await connect();
  const events = [];
  busy.on("message", (raw) => events.push(JSON.parse(raw.toString())));
  busy.send(JSON.stringify({ type: "response.create", model: MODEL, input: [user("hold")] }));
  while (!held.length) await new Promise((resolve) => setTimeout(resolve, 10));

  const drained = switchboard.drain({ timeoutMs: 5_000, sweepMs: 20 });
  assert.equal(await idleClient.closed, 1012, "an idle connection closes at once");
  // A new process cannot be reached through the draining listener.
  await assert.rejects(new Promise((resolve, reject) => {
    const ws = new WebSocket(`ws://127.0.0.1:${address.port}${PREFIX}/responses`);
    ws.once("open", () => { ws.terminate(); resolve(); });
    ws.once("error", reject);
  }));
  assert.equal(busy.readyState, WebSocket.OPEN, "a turn in flight keeps its connection");
  held.shift()();
  assert.equal(await busy.closed, 1012);
  assert.ok(events.some((event) => event.type === "response.completed"), "the in-flight turn completed first");
  assert.equal(await drained, true);
  assert.equal(requests.length, 1, "no new turn was forwarded while draining");
});

test("idle restart keeps serving new connections until no turn is in flight", async (t) => {
  const { switchboard, held, requests, connect } = await harness(t);
  const idleClient = await connect();
  const busy = await connect();
  busy.send(JSON.stringify({ type: "response.create", model: MODEL, input: [user("hold")] }));
  while (!held.length) await new Promise((resolve) => setTimeout(resolve, 10));
  let settled = false;
  const waiting = switchboard.waitForIdle({ timeoutMs: 5_000, pollMs: 20 }).then((idle) => { settled = true; return idle; });
  // The listener stays open: a new client connects and is served while the restart waits.
  const late = await connect();
  const lateDone = new Promise((resolve) => late.on("message", (raw) => {
    if (JSON.parse(raw.toString()).type === "response.completed") resolve();
  }));
  late.send(JSON.stringify({ type: "response.create", model: MODEL, input: [user("served")] }));
  await lateDone;
  await new Promise((resolve) => setTimeout(resolve, 60));
  assert.equal(settled, false, "a turn in flight keeps the restart waiting");
  held.shift()();
  assert.equal(await waiting, true);
  assert.equal(idleClient.readyState, WebSocket.OPEN, "no client was disconnected before the idle moment");
  assert.equal(requests.length, 2, "both turns reached the upstream");
});

// 2026-10-26: Codex counts each refused WebSocket reconnect against its five-attempt stream retry
// budget and then stays on HTTP for that session, so close() must not wait on sockets that never end.
test("close ends lingering HTTP and WebSocket connections after its grace", async (t) => {
  const { switchboard, address } = await harness(t);
  const partial = net.connect(address.port, "127.0.0.1");
  await once(partial, "connect");
  partial.write("POST " + PREFIX + "/responses HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
    + "Content-Length: 1000\r\n\r\n{");
  const silent = net.connect(address.port, "127.0.0.1");
  await once(silent, "connect");
  silent.write("GET " + PREFIX + "/responses HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
    + "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\nsession_id: silent\r\n\r\n");
  const [handshake] = await once(silent, "data");
  assert.match(handshake.toString(), /^HTTP\/1\.1 101/);
  const ended = Promise.all([once(partial, "close"), once(silent, "close")]);
  const started = Date.now();
  await switchboard.close({ graceMs: 50 });
  assert.ok(Date.now() - started < 2_000, "close does not wait for connections that never finish");
  await ended;
});
