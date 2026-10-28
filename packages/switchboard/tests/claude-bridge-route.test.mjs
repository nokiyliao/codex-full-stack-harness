import assert from "node:assert/strict";
import http from "node:http";
import test from "node:test";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import WebSocket, { WebSocketServer } from "ws";

import { createClaudeBridge, HOP_HEADER, loadClaudeBridge } from "../src/claude-bridge-route.mjs";
import { createSwitchboardServer } from "../src/server.mjs";

const CAP = "test-capability-abcdefghijklmnopqrstuvwxyz";
const KEY = "bridgekey";
const PREFIX = "/" + CAP + "/backend-api/codex/v1";
const SEALED = "claude-bridge-v1:sealed";

class IdleWorker {
  async runResponses() { throw new Error("external worker must not run"); }
  async compact() { throw new Error("external worker must not run"); }
  async closeSession() {}
  close() {}
}

function listen(server) {
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server.address().port)));
}

// Stand-in for the bridge: a Claude answer per request, recorded for checks.
function fakeBridge({ hold = false } = {}) {
  const requests = [];
  const server = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks).toString() || "null");
    requests.push({ path: req.url, headers: req.headers, body });
    const id = "resp_claude" + String(requests.length).padStart(32, "0");
    res.writeHead(200, { "content-type": "text/event-stream" });
    res.write("data: " + JSON.stringify({ type: "response.created", response: { id } }) + "\n\n");
    if (hold) return;
    const item = { type: "message", role: "assistant", id: "msg_claude" + String(requests.length).padStart(32, "0"),
      content: [{ type: "output_text", text: "claude " + requests.length }] };
    res.write("data: " + JSON.stringify({ type: "response.output_item.done", item }) + "\n\n");
    res.end("data: " + JSON.stringify({ type: "response.completed", response: { id, output: [item] } }) + "\n\n");
  });
  return { server, requests };
}

// The bridge's own prepareForOpenAI contract, reduced to its observable effect.
function fakePrepare(input, token) {
  assert.equal(token, "bridge-token");
  let checkpoints = 0, ids = 0;
  const out = input.map((item) => {
    if (item?.type === "compaction" && String(item.encrypted_content).startsWith("claude-bridge-v1:")) {
      checkpoints++;
      return { type: "message", role: "user", content: [{ type: "input_text", text: "<context_checkpoint>opened</context_checkpoint>" }] };
    }
    if (typeof item?.id === "string" && /_claude[0-9a-f]{32}$/.test(item.id)) { ids++; const { id, ...rest } = item; return rest; }
    return item;
  });
  return checkpoints || ids ? { input: out, checkpoints, ids } : null;
}

async function setup(t, { hold = false } = {}) {
  const bridge = fakeBridge({ hold });
  const bridgePort = await listen(bridge.server);
  const officialRequests = [];
  const official = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    officialRequests.push({ transport: "http", body: JSON.parse(Buffer.concat(chunks).toString()) });
    res.writeHead(200, { "content-type": "text/event-stream" });
    res.end("data: " + JSON.stringify({ type: "response.completed", response: { id: "resp_official", output: [] } }) + "\n\n");
  });
  const officialWss = new WebSocketServer({ server: official });
  officialWss.on("connection", (ws) => ws.on("message", (data) => {
    officialRequests.push({ transport: "ws", body: JSON.parse(data.toString()) });
    ws.send(JSON.stringify({ type: "response.completed", response: { id: "resp_official_ws", output: [] } }));
  }));
  const officialPort = await listen(official);
  const claudeBridge = createClaudeBridge({ port: bridgePort, key: KEY, token: "bridge-token",
    slugs: ["claude-opus"], catalogRows: [{ slug: "claude-opus" }], prepare: fakePrepare });
  const switchboard = await createSwitchboardServer({
    runtime: { installation_capability: CAP, host: "127.0.0.1", port: 0 },
    admission: { official_models: ["gpt-5.6-sol"] },
    xaiWorker: new IdleWorker(), agyWorker: new IdleWorker(), openrouterWorker: new IdleWorker(), webWorker: new IdleWorker(),
    skipFileGate: true,
    officialOrigin: "http://127.0.0.1:" + officialPort,
    claudeBridge,
  });
  const address = await switchboard.listen({ host: "127.0.0.1", port: 0 });
  const clients = [];
  t.after(async () => {
    for (const ws of clients) ws.terminate();
    switchboard.server.closeAllConnections();
    bridge.server.closeAllConnections();
    official.closeAllConnections();
    officialWss.close();
    await switchboard.close();
    await new Promise((resolve) => bridge.server.close(resolve));
    await new Promise((resolve) => official.close(resolve));
  });
  return { port: address.port, bridge, officialRequests, clients };
}

function post(port, path, body, headers = {}) {
  const raw = Buffer.from(JSON.stringify(body));
  return new Promise((resolve, reject) => {
    const req = http.request({ host: "127.0.0.1", port, path, method: "POST",
      headers: { "content-type": "application/json", "content-length": raw.length, ...headers } }, (res) => {
      const chunks = [];
      res.on("data", (chunk) => chunks.push(chunk));
      res.on("end", () => resolve({ status: res.statusCode, text: Buffer.concat(chunks).toString() }));
    });
    req.on("error", reject);
    req.end(raw);
  });
}

function openWs(port, clients) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket("ws://127.0.0.1:" + port + PREFIX + "/responses", { headers: { authorization: "Bearer app" } });
    clients.push(ws);
    ws.once("open", () => resolve(ws));
    ws.once("error", reject);
  });
}

function turn(ws, frame, { until = ["response.completed", "response.failed", "response.incomplete", "error"], after } = {}) {
  return new Promise((resolve, reject) => {
    const events = [];
    const timer = setTimeout(() => reject(new Error("turn timed out")), 5000);
    const onMessage = (data) => {
      const event = JSON.parse(data.toString());
      events.push(event);
      after?.(event);
      if (until.includes(event.type)) { clearTimeout(timer); ws.off("message", onMessage); resolve(events); }
    };
    ws.on("message", onMessage);
    ws.send(JSON.stringify(frame));
  });
}

test("HTTP Claude requests go to the bridge unchanged and stream back", async (t) => {
  const { port, bridge } = await setup(t);
  const body = { model: "claude-opus", input: [{ type: "message", role: "user", content: "hi" }], stream: true };
  const result = await post(port, PREFIX + "/responses", body, { "thread-id": "thread-1" });
  assert.equal(result.status, 200);
  assert.match(result.text, /response\.completed/);
  assert.equal(bridge.requests.length, 1);
  assert.equal(bridge.requests[0].path, "/g/" + KEY + "/backend-api/codex/responses");
  assert.deepEqual(bridge.requests[0].body, body);
  assert.equal(bridge.requests[0].headers["thread-id"], "thread-1");
  assert.equal(bridge.requests[0].headers[HOP_HEADER], "1");
});

test("Claude requests are never routed back to the bridge or to another endpoint", async (t) => {
  const { port, bridge } = await setup(t);
  const looped = await post(port, PREFIX + "/responses", { model: "claude-opus", input: [] }, { [HOP_HEADER]: "1" });
  assert.equal(looped.status, 400);
  assert.match(looped.text, /MODEL_ROUTE_UNAVAILABLE/);
  const compact = await post(port, PREFIX + "/responses/compact", { model: "claude-opus", input: [] });
  assert.equal(compact.status, 400);
  assert.match(compact.text, /CLAUDE_ENDPOINT_UNAVAILABLE/);
  assert.equal(bridge.requests.length, 0);
});

test("GPT HTTP requests have Claude checkpoints opened and bridge ids removed", async (t) => {
  const { port, bridge, officialRequests } = await setup(t);
  const input = [
    { type: "compaction", encrypted_content: SEALED },
    { type: "message", role: "assistant", id: "msg_claude" + "a".repeat(32), content: [{ type: "output_text", text: "x" }] },
    { type: "message", role: "user", content: "next" },
  ];
  const result = await post(port, PREFIX + "/responses", { model: "gpt-5.6-sol", input, stream: true });
  assert.equal(result.status, 200);
  assert.equal(bridge.requests.length, 0);
  const sent = officialRequests.at(-1).body.input;
  assert.equal(JSON.stringify(sent).includes(SEALED), false);
  assert.equal(sent[1].id, undefined);
  assert.equal(sent[2].content, "next");
});

test("WebSocket Claude turns relay events and continue from this connection's history", async (t) => {
  const { port, bridge, clients } = await setup(t);
  const ws = await openWs(port, clients);
  const first = await turn(ws, { type: "response.create", model: "claude-opus",
    input: [{ type: "message", role: "user", content: "one" }], client_metadata: { thread_id: "thread-ws" } });
  assert.equal(first.at(-1).type, "response.completed");
  const responseId = first[0].response.id;
  assert.equal(bridge.requests[0].headers["thread-id"], "thread-ws");
  assert.equal(bridge.requests[0].body.stream, true);
  assert.equal("client_metadata" in bridge.requests[0].body, false);
  await turn(ws, { type: "response.create", model: "claude-opus", previous_response_id: responseId,
    input: [{ type: "message", role: "user", content: "two" }] });
  const expanded = bridge.requests[1].body.input;
  assert.deepEqual(expanded.map((item) => item.content?.[0]?.text ?? item.content), ["one", "claude 1", "two"]);
  const unknown = await turn(ws, { type: "response.create", model: "claude-opus", previous_response_id: "resp_official",
    input: [] });
  assert.equal(unknown.at(-1).error.code, "previous_response_not_found");
  assert.equal(bridge.requests.length, 2);
});

test("WebSocket cancel stops a Claude turn with an incomplete terminal event", async (t) => {
  const { port, clients } = await setup(t, { hold: true });
  const ws = await openWs(port, clients);
  const events = await turn(ws, { type: "response.create", model: "claude-opus", input: [] }, {
    after: (event) => { if (event.type === "response.created") ws.send(JSON.stringify({ type: "response.cancel" })); },
  });
  assert.equal(events.at(-1).type, "response.incomplete");
  assert.equal(events.at(-1).incomplete_details?.reason ?? events.at(-1).response.incomplete_details.reason, "interrupted");
});

test("GPT WebSocket turns are sanitized before reaching OpenAI", async (t) => {
  const { port, officialRequests, clients } = await setup(t);
  const ws = await openWs(port, clients);
  await turn(ws, { type: "response.create", model: "gpt-5.6-sol",
    input: [{ type: "compaction", encrypted_content: SEALED }, { type: "message", role: "user", content: "go" }] });
  const sent = officialRequests.find((row) => row.transport === "ws").body;
  assert.equal(JSON.stringify(sent).includes(SEALED), false);
  assert.equal(sent.input[1].content, "go");
});

test("the served model list keeps admitted rows and appends Claude rows once", () => {
  const bridge = createClaudeBridge({ port: 1, key: KEY, token: "bridge-token", slugs: ["claude-opus"],
    catalogRows: [{ slug: "claude-opus" }], prepare: fakePrepare });
  const merged = bridge.withCatalogRows({ models: [{ slug: "gpt-5.6-sol" }] });
  assert.deepEqual(merged.models.map((row) => row.slug), ["gpt-5.6-sol", "claude-opus"]);
  assert.equal(bridge.withCatalogRows(merged).models.length, 2);
});

async function dependency(t, handler) {
  const server = http.createServer(handler);
  const port = await listen(server);
  t.after(async () => {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  });
  const bridge = createClaudeBridge({ port, key: KEY, token: "bridge-token", slugs: ["claude-opus"], prepare: fakePrepare });
  return { server, bridge };
}

test("dependency readiness checks only authenticated local health and never discloses secrets", async (t) => {
  const requests = [];
  const { bridge } = await dependency(t, (req, res) => {
    requests.push({ path: req.url, method: req.method, token: req.headers["x-claude-bridge-key"] });
    res.end(JSON.stringify({ status: "ok", picker: true, models: ["claude-opus"], auth: "claude_login",
      ignored_secret: "never-return-this", ignored_url: "https://private.invalid/secret" }));
  });
  const result = await bridge.dependencyReadiness();
  assert.equal(result.status, "READY");
  assert.equal(result.provider_readiness, "UNKNOWN");
  assert.equal(result.http_status, 200);
  assert.match(bridge.route_source_sha256, /^[a-f0-9]{64}$/);
  assert.equal(Number.isFinite(result.latency_ms), true);
  assert.equal(Number.isFinite(Date.parse(result.observed_at)), true);
  assert.deepEqual(requests, [{ path: "/health", method: "GET", token: "bridge-token" }]);
  assert.doesNotMatch(JSON.stringify(result), /bridge-token|bridgekey|never-return-this|private\.invalid|https?:/);
});

test("dependency readiness exposes refusal without retries or provider requests", async () => {
  const server = http.createServer();
  const port = await listen(server);
  await new Promise((resolve) => server.close(resolve));
  const bridge = createClaudeBridge({ port, key: KEY, token: "bridge-token", slugs: ["claude-opus"], prepare: fakePrepare });
  const result = await bridge.dependencyReadiness();
  assert.equal(result.status, "UNREADY");
  assert.equal(result.error_code, "CLAUDE_BRIDGE_UNAVAILABLE");
  assert.equal(result.transport_error, "ECONNREFUSED");
  assert.equal(result.provider_readiness, "UNKNOWN");
});

test("dependency readiness bounds an unresponsive or oversized health response", async (t) => {
  let requests = 0;
  let seen;
  const arrived = new Promise((resolve) => { seen = resolve; });
  const { bridge } = await dependency(t, () => { requests++; seen(); });
  // Long enough for the request to reach a loaded test process, short enough to prove the bound.
  const result = await bridge.dependencyReadiness({ timeoutMs: 250 });
  assert.equal(result.status, "UNREADY");
  assert.equal(result.error_code, "CLAUDE_BRIDGE_HEALTH_TIMEOUT");
  await Promise.race([arrived, new Promise((resolve) => setTimeout(resolve, 1000))]);
  assert.equal(requests, 1, "one request and no retry");
  const oversized = await dependency(t, (_req, res) => res.end("x".repeat(16_385)));
  assert.equal((await oversized.bridge.dependencyReadiness()).error_code, "CLAUDE_BRIDGE_HEALTH_TOO_LARGE");
});

test("dependency readiness rejects HTTP failures, invalid health and incompatible catalogs", async (t) => {
  for (const [status, body, expected] of [
    [401, { secret: "not-returned" }, "CLAUDE_BRIDGE_HEALTH_HTTP_ERROR"],
    [200, "not JSON", "CLAUDE_BRIDGE_HEALTH_INVALID"],
    [200, { status: "ok", picker: false, models: ["claude-opus"] }, "CLAUDE_BRIDGE_PICKER_UNAVAILABLE"],
    [200, { status: "ok", picker: true, models: ["another-model"] }, "CLAUDE_BRIDGE_MODEL_CATALOG_MISMATCH"],
  ]) {
    const { bridge } = await dependency(t, (_req, res) => {
      res.writeHead(status); res.end(typeof body === "string" ? body : JSON.stringify(body));
    });
    const result = await bridge.dependencyReadiness();
    assert.equal(result.status, "UNREADY");
    assert.equal(result.error_code, expected);
    assert.doesNotMatch(JSON.stringify(result), /not-returned|another-model/);
  }
});

test("WebSocket upstream HTTP failures retain their server classification and code", async (t) => {
  const { bridge } = await dependency(t, (_req, res) => {
    res.writeHead(503, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: { type: "server_error", code: "CLAUDE_OVERLOADED", message: "Try later." } }));
  });
  const events = [];
  const result = await bridge.runWsTurn({ message: { model: "claude-opus", input: [] }, upgradeHeaders: {},
    history: new Map(), send: (event) => events.push(typeof event === "string" ? JSON.parse(event) : event) }).done;
  assert.equal(result.status, 503);
  assert.equal(result.terminal_cause, "upstream_http_error");
  assert.equal(events.at(-1).error.type, "server_error");
  assert.equal(events.at(-1).error.code, "CLAUDE_OVERLOADED");
});

test("WebSocket stream interruption before any response yields a typed terminal failure", async (t) => {
  const { bridge } = await dependency(t, (_req, res) => {
    res.writeHead(200, { "content-type": "text/event-stream" });
    res.end(": no response\n\n");
  });
  const events = [];
  const result = await bridge.runWsTurn({ message: { model: "claude-opus", input: [] }, upgradeHeaders: {},
    history: new Map(), send: (event) => events.push(typeof event === "string" ? JSON.parse(event) : event) }).done;
  assert.equal(result.status, 502);
  assert.equal(result.terminal_cause, "upstream_eof_without_terminal");
  assert.equal(events.at(-1)?.type, "error");
  assert.equal(events.at(-1)?.error.code, "CLAUDE_BRIDGE_STREAM_INCOMPLETE");
});

test("bridge loader honors the existing Switchboard config owner and explicit enable override", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "switchboard-bridge-enable-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const activationPath = join(root, "activation.json");
  // A malformed launch would throw if a disabled route accidentally loaded it.
  await writeFile(join(root, "launch.json"), "not JSON");
  await writeFile(join(root, "gateway-key"), "test-key");
  await writeFile(join(root, "token"), "test-token");
  await writeFile(join(root, "integration-models.json"), "{}");
  await writeFile(join(root, "combined-models.json"), "{}");
  await writeFile(activationPath, JSON.stringify({ routing_binding: { owner: "switchboard", claude_enabled: false } }));
  assert.equal(await loadClaudeBridge({ root, activationPath }), null);
  await assert.rejects(loadClaudeBridge({ root, activationPath, enabled: true }), SyntaxError);
  await writeFile(activationPath, "not JSON");
  assert.equal(await loadClaudeBridge({ root, activationPath, enabled: false }), null);
  await assert.rejects(loadClaudeBridge({ root, activationPath }), SyntaxError);
  await rm(activationPath);
  await assert.rejects(loadClaudeBridge({ root, activationPath }), SyntaxError);
});

async function httpProxy(t, bridge) {
  let outcome;
  const server = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    outcome = await bridge.proxyHttp({ req, res, raw: Buffer.concat(chunks), endpoint: "/responses" });
  });
  const port = await listen(server);
  t.after(async () => {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  });
  return { port, outcome: () => outcome };
}

test("HTTP SSE failure diagnostics preserve exact response bytes and the real HTTP status", async (t) => {
  const stream = "event: response.failed\r\ndata: " + JSON.stringify({ type: "response.failed",
    response: { id: "resp_claude_failure", error: { code: "CLAUDE_OVERLOADED", message: "private diagnostic" } } }) + "\r\n\r\n";
  let requests = 0;
  const { bridge } = await dependency(t, (_req, res) => {
    requests++;
    res.writeHead(200, { "content-type": "text/event-stream" });
    res.end(stream);
  });
  const proxy = await httpProxy(t, bridge);
  const result = await post(proxy.port, "/responses", { model: "claude-opus", input: [] });
  assert.equal(result.status, 200);
  assert.equal(result.text, stream);
  assert.equal(proxy.outcome().status, 502);
  assert.equal(proxy.outcome().upstream_http_status, 200);
  assert.equal(proxy.outcome().upstream_terminal_event, "response.failed");
  assert.equal(proxy.outcome().terminal_cause, "upstream_terminal_failure");
  assert.equal(proxy.outcome().upstream_error_code, "CLAUDE_OVERLOADED");
  assert.doesNotMatch(JSON.stringify(proxy.outcome()), /private diagnostic/);
  assert.equal(requests, 1);
});

test("HTTP EOF without an SSE terminal is distinct from a local dependency transport failure", async (t) => {
  const { bridge, server } = await dependency(t, (_req, res) => {
    res.writeHead(200, { "content-type": "text/event-stream" });
    res.end(": no terminal event\n\n");
  });
  const proxy = await httpProxy(t, bridge);
  assert.equal((await post(proxy.port, "/responses", { model: "claude-opus" })).status, 200);
  assert.equal(proxy.outcome().error_code, "CLAUDE_BRIDGE_STREAM_INCOMPLETE");
  assert.equal(proxy.outcome().terminal_cause, "upstream_eof_without_terminal");
  await new Promise((resolve) => server.close(resolve));
  const refused = await post(proxy.port, "/responses", { model: "claude-opus" });
  assert.equal(refused.status, 502);
  assert.equal(JSON.parse(refused.text).error.code, "CLAUDE_BRIDGE_UNAVAILABLE");
  assert.equal(proxy.outcome().terminal_cause, "upstream_request_error");
  assert.match(proxy.outcome().transport_error, /^(ECONNREFUSED|ECONNRESET)$/);
});

test("WebSocket preserves CRLF SSE terminal failures and diagnoses resets without replay", async (t) => {
  let requests = 0;
  const { bridge } = await dependency(t, (_req, res) => {
    requests++;
    res.writeHead(200, { "content-type": "text/event-stream" });
    if (requests === 1) {
      res.end("data: " + JSON.stringify({ type: "response.failed", response: { id: "resp_claude_error",
        error: { type: "server_error", code: "CLAUDE_STOPPED", message: "stopped" } } }) + "\r\n\r\n");
    } else {
      res.write(": connected\n\n");
      setImmediate(() => res.destroy());
    }
  });
  const events = [];
  const run = () => bridge.runWsTurn({ message: { model: "claude-opus", input: [] }, upgradeHeaders: {},
    history: new Map(), send: (event) => events.push(typeof event === "string" ? JSON.parse(event) : event) }).done;
  const failed = await run();
  assert.equal(failed.status, 502);
  assert.equal(failed.error_code, "CLAUDE_STOPPED");
  assert.equal(failed.terminal_cause, "upstream_terminal_failure");
  assert.equal(events.at(-1).type, "response.failed");
  const reset = await run();
  assert.equal(reset.status, 502);
  assert.equal(reset.error_code, "CLAUDE_BRIDGE_STREAM_INTERRUPTED");
  assert.equal(reset.terminal_cause, "upstream_stream_error");
  assert.equal(events.at(-1).type, "error");
  assert.equal(requests, 2);
});

