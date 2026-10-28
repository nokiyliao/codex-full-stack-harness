import assert from "node:assert/strict";
import http from "node:http";
import test from "node:test";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import WebSocket from "ws";
import { createSwitchboardServer } from "../src/server.mjs";
import { ChatGptWebWorker } from "../src/chatgpt-web-worker.mjs";
import { IsolatedWorkerClient } from "../src/worker-client.mjs";
import { EXTERNAL_EFFECT_SCOPE, EXTERNAL_ORCHESTRATION_PROTOCOL } from "../src/external-admission.mjs";

const identity = { thread_id: "thread-exact", turn_id: "turn-exact" };
const body = { model: "chatgpt-web/pro", input: [], client_metadata: { "x-codex-turn-metadata": JSON.stringify(identity) } };
const tick = () => new Promise(resolve => setTimeout(resolve, 10));
async function until(predicate) {
  for (let i = 0; i < 100 && !predicate(); i++) await tick();
  assert.ok(predicate(), "cancellation did not reach the owned worker within one second");
}
class PendingWorker {
  pending = new Map();
  closed = [];
  interrupted = [];
  async runResponses(request, { sessionKey, onEvent }) {
    return new Promise((resolve, reject) => {
      this.pending.set(sessionKey, { request, resolve, reject });
      onEvent({ type: "response.created", response: { id: sessionKey } });
    });
  }
  async closeSession(sessionKey) {
    this.closed.push(sessionKey);
    const pending = this.pending.get(sessionKey);
    this.pending.delete(sessionKey);
    pending?.reject(Object.assign(new Error("cancelled"), { code: "WEB_REQUEST_CANCELLED", status: 499 }));
  }
  async interruptTurn(sessionKey, request) {
    this.interrupted.push({ sessionKey, request });
    const pending = this.pending.get(sessionKey);
    this.pending.delete(sessionKey);
    pending?.reject(Object.assign(new Error("interrupted"), { code: "WEB_REQUEST_CANCELLED", status: 499 }));
  }
  close() {}
}
async function fixture(t) {
  const worker = new PendingWorker();
  const server = await createSwitchboardServer({
    runtime: { installation_capability: "test-capability-abcdefghijklmnopqrstuvwxyz" },
    admission: {
      status: "ADMITTED", external_batch_admitted: true, authority_effect: EXTERNAL_EFFECT_SCOPE,
      protocol_compatibility: { external_model_orchestration: EXTERNAL_ORCHESTRATION_PROTOCOL },
      policy: { external_models: "allow", effect_scope: EXTERNAL_EFFECT_SCOPE },
      official_models: ["gpt-6-astra"], catalog_sha256: "test", cli_sha256: "test",
    },
    webWorker: worker, agyWorker: worker, xaiWorker: worker, openrouterWorker: worker,
    skipFileGate: true,
  });
  const sockets = new Set();
  server.server.on("connection", socket => { sockets.add(socket); socket.once("close", () => sockets.delete(socket)); });
  const { port } = await server.listen({ host: "127.0.0.1", port: 0 });
  t.after(async () => {
    for (const socket of sockets) socket.destroy();
    for (const key of [...worker.pending.keys()]) await worker.closeSession(key);
    await server.close();
  });
  return { worker, port, path: "/test-capability-abcdefghijklmnopqrstuvwxyz/backend-api/codex/v1/responses" };
}
async function openHttp({ port, path }) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: "127.0.0.1", port, path, method: "POST", headers: { "content-type": "application/json" } }, res => {
      res.once("data", () => resolve({ req, res }));
      res.on("error", () => {});
    });
    req.on("error", reject);
    req.end(JSON.stringify(body));
  });
}

test("HTTP premature close releases only its worker request, not the resumable browser turn", async t => {
  const f = await fixture(t);
  const first = await openHttp(f);
  const second = await openHttp(f);
  t.after(() => { first.req.destroy(); second.req.destroy(); });
  const [firstKey, secondKey] = f.worker.pending.keys();
  first.req.destroy();
  await until(() => !f.worker.pending.has(firstKey));
  assert.ok(f.worker.pending.has(secondKey));
  assert.deepEqual(f.worker.closed, [firstKey]);
  assert.deepEqual(f.worker.interrupted, []);
});

test("explicit Web WS cancellation forwards only the canonical active turn to interrupt", async t => {
  const f = await fixture(t);
  const ws = new WebSocket(`ws://127.0.0.1:${f.port}${f.path}`);
  t.after(() => ws.terminate());
  await new Promise((resolve, reject) => { ws.once("open", resolve); ws.once("error", reject); });
  ws.send(JSON.stringify({ ...body, type: "response.create" }));
  await until(() => f.worker.pending.size === 1);
  ws.send(JSON.stringify({ type: "response.cancel" }));
  await until(() => f.worker.interrupted.length === 1);
  assert.deepEqual(f.worker.interrupted[0].request.client_metadata, body.client_metadata);
  assert.deepEqual(f.worker.closed, []);
});

test("Web WS disconnect closes request transport without issuing irrevocable turn interruption", async t => {
  const f = await fixture(t);
  const ws = new WebSocket(`ws://127.0.0.1:${f.port}${f.path}`);
  t.after(() => ws.terminate());
  await new Promise((resolve, reject) => { ws.once("open", resolve); ws.once("error", reject); });
  ws.send(JSON.stringify({ ...body, type: "response.create" }));
  await until(() => f.worker.pending.size === 1);
  ws.terminate();
  await until(() => f.worker.pending.size === 0);
  assert.deepEqual(f.worker.interrupted, []);
});

test("Web explicit interrupt uses existing local control with exact identity and no replay", async () => {
  const calls = [];
  const worker = new ChatGptWebWorker({
    endpoint: "http://127.0.0.1:17841/v1/responses",
    loadControlConfig: async () => ({ host: "127.0.0.1", port: 17841, controlToken: "test-only-control-token" }),
    transport: async call => {
      calls.push(call);
      return { status: 200, body: Buffer.from(JSON.stringify({ status: "ok", cancelled_http_turns: 1, cancelled_browser_turns: 1, cancelled_compaction_runs: 0 })) };
    },
  });
  await worker.interruptTurn(body);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].endpoint, "http://127.0.0.1:17841/admin/interrupt-turn");
  assert.deepEqual(JSON.parse(calls[0].payload), { threadId: "thread-exact", turnId: "turn-exact" });
  assert.equal(calls[0].headers.authorization, "Bearer test-only-control-token");
});

test("Web explicit interrupt rejects missing canonical identity and foreign control endpoints without submission", async () => {
  let calls = 0;
  const worker = new ChatGptWebWorker({
    endpoint: "http://foreign.invalid:17841/v1/responses",
    loadControlConfig: async () => ({ host: "127.0.0.1", port: 17841, controlToken: "never-send" }),
    transport: async () => { calls++; throw new Error("must not call"); },
  });
  await assert.rejects(worker.interruptTurn({ input: [{ content: JSON.stringify(identity) }] }), { code: "WEB_INTERRUPT_IDENTITY_UNAVAILABLE" });
  await assert.rejects(worker.interruptTurn(body), { code: "WEB_INTERRUPT_CONTROL_UNAVAILABLE" });
  assert.equal(calls, 0);
});

test("Web explicit interrupt delivery uncertainty stays unsettled and is never retried", async () => {
  let calls = 0;
  const worker = new ChatGptWebWorker({
    endpoint: "http://127.0.0.1:17841/v1/responses",
    loadControlConfig: async () => ({ host: "127.0.0.1", port: 17841, controlToken: "secret-not-for-errors" }),
    transport: async () => { calls++; throw new Error("secret-not-for-errors"); },
  });
  await assert.rejects(worker.interruptTurn(body), error => error.code === "WEB_INTERRUPT_DELIVERY_UNSETTLED" && !error.message.includes("secret-not-for-errors"));
  assert.equal(calls, 1);
});

test("isolated Web worker IPC interrupts exactly one retained browser turn and leaves another active", async t => {
  const retained = new Map();
  const interrupted = [];
  const sockets = new Set();
  const local = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const value = JSON.parse(Buffer.concat(chunks));
    if (req.url === "/v1/responses") {
      const metadata = JSON.parse(value.client_metadata["x-codex-turn-metadata"]);
      retained.set(metadata.turn_id, res);
      res.writeHead(200, { "content-type": "text/event-stream" });
      res.write(`event: response.created\ndata: ${JSON.stringify({ type: "response.created", response: { id: metadata.turn_id } })}\n\n`);
      return;
    }
    assert.equal(req.url, "/admin/interrupt-turn");
    assert.equal(req.headers.authorization, "Bearer ipc-test-control");
    interrupted.push(value);
    const stream = retained.get(value.turnId);
    retained.delete(value.turnId);
    res.end(JSON.stringify({ status: "ok", cancelled_http_turns: 1, cancelled_browser_turns: 1, cancelled_compaction_runs: 0 }));
    stream.end(`event: error\ndata: ${JSON.stringify({ type: "error", error: { code: "client_cancelled" } })}\n\n`);
  });
  local.on("connection", socket => { sockets.add(socket); socket.once("close", () => sockets.delete(socket)); });
  await new Promise(resolve => local.listen(0, "127.0.0.1", resolve));
  const port = local.address().port;
  const home = await mkdtemp(join(tmpdir(), "switchboard-web-cancel-"));
  await writeFile(join(home, "config.json"), JSON.stringify({ host: "127.0.0.1", port, controlToken: "ipc-test-control" }));
  const worker = new IsolatedWorkerClient({
    script: fileURLToPath(new URL("../src/chatgpt-web-worker.mjs", import.meta.url)), label: "Test Web",
    env: { CODEX_CHATGPT_WEB_HOME: home, CHATGPT_WEB_ENDPOINT: `http://127.0.0.1:${port}/v1/responses` },
  });
  t.after(async () => {
    worker.close();
    for (const socket of sockets) socket.destroy();
    await new Promise(resolve => local.close(resolve));
  });
  const first = worker.runResponses(body, { sessionKey: "session-first" }).catch(error => error);
  const nextBody = { ...body, client_metadata: { "x-codex-turn-metadata": JSON.stringify({ ...identity, turn_id: "turn-next" }) } };
  const second = worker.runResponses(nextBody, { sessionKey: "session-next" }).catch(error => error);
  await until(() => retained.size === 2);
  await worker.interruptTurn("session-first", body);
  await first;
  assert.deepEqual(interrupted, [{ threadId: "thread-exact", turnId: "turn-exact" }]);
  assert.deepEqual([...retained.keys()], ["turn-next"]);
  await worker.closeSession("session-next");
  await second;
  assert.deepEqual([...retained.keys()], ["turn-next"], "disconnect must retain resumable browser ownership");
  assert.equal(interrupted.length, 1);
});
