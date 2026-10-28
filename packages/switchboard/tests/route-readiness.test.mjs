import assert from "node:assert/strict";
import test from "node:test";
import WebSocket from "ws";
import { createSwitchboardServer } from "../src/server.mjs";

const capability = "fixture-readiness-capability";
const worker = { async closeSession() {}, close() {} };

async function fixture(t, claudeBridge = null) {
  const instance = await createSwitchboardServer({
    runtime: { installation_capability: capability, host: "127.0.0.1", port: 0 },
    admission: { status: "ADMITTED", official_models: ["gpt-fixture"] },
    skipFileGate: true, xaiWorker: worker, agyWorker: worker, webWorker: worker, openrouterWorker: worker,
    claudeBridge,
  });
  const address = await instance.listen({ port: 0 });
  t.after(() => instance.close());
  const origin = `http://127.0.0.1:${address.port}`;
  return { instance, origin, ready: async () => (await fetch(`${origin}/${capability}/ready`)).json(),
    health: async () => (await fetch(`${origin}/health`)).json() };
}

test("readiness distinguishes admitted official route, unhealthy dependency and disabled Claude", async (t) => {
  const state = await fixture(t, { models: new Set(["claude-fixture"]), route_source_sha256: "fixture-loaded-sha",
    async dependencyReadiness() { return { status: "UNREADY", error_code: "CLAUDE_BRIDGE_UNAVAILABLE", provider_readiness: "UNKNOWN" }; } });
  const ready = await state.ready();
  assert.equal(ready.status, "ADMITTED");
  assert.equal(ready.dependencies.claude_bridge.status, "UNREADY");
  assert.equal(ready.dependencies.claude_bridge.provider_readiness, "UNKNOWN");
  assert.equal(ready.claude_route_source_sha256, "fixture-loaded-sha");
  const disabled = await fixture(t);
  assert.equal((await disabled.ready()).dependencies.claude_bridge.status, "DISABLED");
});

test("idle readback counts actual HTTP and WS work instead of idle connections", async (t) => {
  let releaseHttp, releaseWs, startedHttp, startedWs;
  const httpStarted = new Promise((resolve) => { startedHttp = resolve; });
  const wsStarted = new Promise((resolve) => { startedWs = resolve; });
  const httpFinish = new Promise((resolve) => { releaseHttp = resolve; });
  const wsFinish = new Promise((resolve) => { releaseWs = resolve; });
  const state = await fixture(t, {
    models: new Set(["claude-fixture"]),
    async proxyHttp({ res }) { startedHttp(); await httpFinish; res.end("done"); return { status: 200 }; },
    runWsTurn({ send }) {
      startedWs();
      return { abort: releaseWs, done: wsFinish.then(() => {
        send({ type: "response.completed", response: { id: "resp_fixture", output: [] } });
        return { status: 200 };
      }) };
    },
  });
  const pending = fetch(`${state.origin}/${capability}/backend-api/codex/v1/responses`, {
    method: "POST", body: JSON.stringify({ model: "claude-fixture", input: [] }),
  });
  await httpStarted;
  assert.equal((await state.health()).activity.active_http_requests, 1);
  releaseHttp();
  await (await pending).text();
  const ws = new WebSocket(`${state.origin.replace("http:", "ws:")}/${capability}/backend-api/codex/v1/responses`);
  await new Promise((resolve, reject) => { ws.once("open", resolve); ws.once("error", reject); });
  assert.equal((await state.health()).activity.active_ws_turns, 0);
  ws.send(JSON.stringify({ type: "response.create", model: "claude-fixture", input: [] }));
  await wsStarted;
  assert.equal((await state.health()).activity.active_ws_turns, 1);
  const ended = new Promise((resolve) => ws.once("message", resolve));
  releaseWs();
  await ended;
  const health = await state.health();
  assert.equal(health.activity.active_ws_turns, 0);
  assert.equal(health.activity.active_http_requests, 0);
  assert.equal(health.activity.websocket_connections, 1);
  const closed = new Promise((resolve) => ws.once("close", resolve));
  ws.close();
  await closed;
});
