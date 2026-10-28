import assert from "node:assert/strict";
import http from "node:http";
import test from "node:test";
import { gzipSync } from "node:zlib";

import WebSocket, { WebSocketServer } from "ws";

import {
  EXTERNAL_EFFECT_SCOPE,
  EXTERNAL_ORCHESTRATION_PROTOCOL,
} from "../src/external-admission.mjs";
import { createSwitchboardServer } from "../src/server.mjs";
import { SseDecoder, SwitchboardError, decodeBody, encodeJsonBody } from "../src/shared.mjs";

class FakeWorker {
  constructor(route) {
    this.route = route;
    this.requests = [];
  }

  async runResponses(body, { onEvent }) {
    this.requests.push(structuredClone(body));
    const id = `resp_${this.route}_${this.requests.length}`;
    const events = [
      { type: "response.created", response: { id } },
      {
        type: "response.output_item.done",
        item: {
          type: "message",
          role: "assistant",
          id: `msg_${this.route}_${this.requests.length}`,
          content: [{ type: "output_text", text: `${this.route} ok` }],
        },
      },
      { type: "response.completed", response: { id, usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 } } },
    ];
    for (const event of events) onEvent?.(event);
    return { status: 200, events };
  }

  async compact(body) {
    this.requests.push(structuredClone(body));
    return {
      summary: `${this.route} portable summary`,
      provider_item: this.route === "xai"
        ? { type: "compaction", id: "cmp_private", encrypted_content: "private-opaque-canary" }
        : null,
      usage: { total_tokens: 3 },
    };
  }

  async closeSession() {}
  close() {}
}

class CancellableWorker extends FakeWorker {
  constructor(route) {
    super(route);
    this.closedSessions = [];
    this.pending = null;
  }

  async runResponses(body, { onEvent, sessionKey, ownerKey = sessionKey }) {
    this.requests.push(structuredClone(body));
    const id = `resp_${this.route}_cancel`;
    onEvent?.({ type: "response.created", response: { id } });
    return new Promise((_resolve, reject) => {
      this.pending = { sessionKey, ownerKey, reject };
    });
  }

  // As in the real workers, closing the owner or the session aborts the job.
  async closeSession(sessionKey) {
    this.closedSessions.push(sessionKey);
    if (this.pending?.sessionKey !== sessionKey && this.pending?.ownerKey !== sessionKey) return;
    this.pending.reject(new SwitchboardError(
      "AGY_REQUEST_CANCELLED",
      "AGY request was cancelled when the downstream session closed.",
      499,
    ));
    this.pending = null;
  }
}

class InjectedWebFailureWorker extends FakeWorker {
  constructor() {
    super("web");
    this.outboundIdentities = [];
  }

  async runResponses(body) {
    this.requests.push(structuredClone(body));
    const outboundIdentity = `web-outbound-${this.requests.length}`;
    this.outboundIdentities.push(outboundIdentity);
    if (body.input[0].content === "pre-submit") {
      throw new SwitchboardError("WEB_PROVIDER_UNAVAILABLE", "Injected Web pre-submit failure.");
    }
    throw new SwitchboardError(
      "WEB_DELIVERY_UNCERTAIN",
      "Injected submitted Web delivery has no terminal response.",
    );
  }
}

function listen(server) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolve(server.address()));
  });
}

function closeServer(server) {
  return new Promise((resolve) => server.close(resolve));
}

async function request({ port, path, body = null, encoding = "identity", headers = {}, method = "POST" }) {
  const raw = body === null ? Buffer.alloc(0) : encodeJsonBody(body, encoding);
  return new Promise((resolve, reject) => {
    const req = http.request({
      host: "127.0.0.1",
      port,
      path,
      method,
      headers: {
        ...(raw.length ? { "content-type": "application/json", "content-encoding": encoding, "content-length": raw.length } : {}),
        ...headers,
      },
    }, (res) => {
      const chunks = [];
      res.on("data", (chunk) => chunks.push(chunk));
      res.on("end", () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks) }));
    });
    req.on("error", reject);
    if (raw.length) req.write(raw);
    req.end();
  });
}

function terminalWsTurn(ws, frame) {
  return new Promise((resolve, reject) => {
    const events = [];
    const timer = setTimeout(() => reject(new Error("WebSocket turn timed out")), 5_000);
    const onMessage = (data) => {
      const event = JSON.parse(data.toString());
      events.push(event);
      if (["response.completed", "response.failed", "response.incomplete", "error"].includes(event.type)) {
        clearTimeout(timer);
        ws.off("message", onMessage);
        resolve(events);
      }
    };
    ws.on("message", onMessage);
    ws.send(JSON.stringify(frame));
  });
}

test("official HTTP settles the first terminal cause without retrying or leaking diagnostics", { timeout: 30_000 }, async (t) => {
  const deferred = () => {
    let resolve;
    const promise = new Promise((done) => { resolve = done; });
    return { promise, resolve };
  };
  const bounded = async (promise) => {
    let timer;
    try {
      return await Promise.race([promise, new Promise((_resolve, reject) => {
        timer = setTimeout(() => reject(new Error("Official HTTP lifecycle timed out")), 3_000);
      })]);
    } finally {
      clearTimeout(timer);
    }
  };
  let active;
  const completeBody = '{"ok":"http-body-secret"}';
  const httpErrorBody = '{"error":{"type":"rate_limit_error","message":"http-body-secret"}}';
  const streamChunk = "data: http-body-secret\n\n";
  const terminalFrame = (type) => `data: ${JSON.stringify({ type,
    response: { id: "resp_terminal", output: [], status: type.slice("response.".length),
      model: "gpt-5.6-sol", service_tier: "default",
      usage: { input_tokens: 100, output_tokens: 20, total_tokens: 120,
        input_tokens_details: { cached_tokens: 80 }, output_tokens_details: { reasoning_tokens: 15 } } },
  })}\n\n`;
  const official = http.createServer(async (req, res) => {
    for await (const _chunk of req) { /* consume the complete request before cancelling */ }
    const state = active;
    state.requests += 1;
    state.officialResponse = res;
    res.once("close", () => state.officialClosed.resolve());
    state.received.resolve(req);
    if (state.mode === "reset") return req.socket.destroy();
    if (["stream", "stream-reset"].includes(state.mode)) {
      res.writeHead(200, { "content-type": "text/event-stream", "x-canary": "http-header-secret" });
      res.write(streamChunk);
    } else if (state.mode === "sse-close") {
      res.writeHead(200, { "content-type": "text/event-stream",
        ...(state.encoding ? { "content-encoding": state.encoding } : {}) });
      if (state.fragmented) {
        for (const byte of state.streamBody) {
          res.write(Buffer.from([byte]));
          await new Promise((resolve) => setImmediate(resolve));
        }
      } else res.write(state.streamBody);
    } else if (["complete", "http-error"].includes(state.mode)) {
      res.writeHead(state.mode === "complete" ? 200 : 429, { "content-type": "application/json", "x-canary": "http-header-secret" });
      res.end(state.mode === "complete" ? completeBody : httpErrorBody);
    }
  });
  const officialAddress = await listen(official);
  const officialOrigin = `http://127.0.0.1:${officialAddress.port}`;
  const runtime = { installation_capability: "test-capability-abcdefghijklmnopqrstuvwxyz", host: "127.0.0.1", port: 0 };
  const switchboard = await createSwitchboardServer({
    runtime,
    admission: { official_models: ["gpt-5.6-sol"] },
    xaiWorker: new FakeWorker("xai"),
    agyWorker: new FakeWorker("agy"),
    openrouterWorker: new FakeWorker("openrouter"),
    webWorker: new FakeWorker("web"),
    skipFileGate: true,
    officialOrigin,
  });
  const address = await switchboard.listen({ host: "127.0.0.1", port: 0 });
  t.after(async () => {
    active?.releaseEvents?.();
    switchboard.server.closeAllConnections();
    official.closeAllConnections();
    await switchboard.close();
    await closeServer(official);
  });
  switchboard.server.on("request", (_req, res) => {
    const state = active;
    state.downstream = res;
    res.once("close", () => state.downstreamClosed.resolve());
    if (state.rejectWrite || state.backpressure) {
      const writeChunk = res.write;
      t.mock.method(res, "write", function (chunk, callback) {
        const ready = writeChunk.call(this, chunk, (error) => callback?.(
          state.rejectWrite ? new Error("injected local write failure") : error));
        if (!state.backpressure) return ready;
        queueMicrotask(() => {
          state.pauseObserved = state.proxyResponse.isPaused();
          res.emit("drain");
          state.resumeObserved = !state.proxyResponse.isPaused();
        });
        return false;
      });
    }
  });

  const write = process.stdout.write;
  t.mock.method(process.stdout, "write", function (chunk, ...args) {
    if (typeof chunk === "string" && chunk.startsWith("{")) {
      let record;
      try { record = JSON.parse(chunk); } catch { /* not a switchboard log */ }
      if (record?.endpoint === "/responses") {
        active.records.push(record);
        active.logged.resolve(record);
      }
    }
    return write.call(this, chunk, ...args);
  });
  const makeRequest = http.request;
  t.mock.method(http, "request", function (...args) {
    const outgoing = makeRequest.apply(this, args);
    if (!(args[0] instanceof URL) || args[0].origin !== officialOrigin) return outgoing;
    const state = active;
    state.outboundRequests += 1;
    state.owned = outgoing;
    state.initialErrorListeners = outgoing.listenerCount("error");
    const captureResponse = (response) => {
      state.proxyResponse = response;
      response.once("close", () => state.proxyResponseClosed.resolve());
    };
    outgoing.once("response", captureResponse);
    outgoing.once("close", () => {
      outgoing.off("response", captureResponse);
      state.ownedClosed.resolve();
    });
    if (state.mode === "held-cancel") {
      // Delay native destroy notifications, not destruction itself. Cancellation
      // must settle even without an upstream error/end/close to drive it.
      const emit = outgoing.emit;
      const held = [];
      let hold = true;
      t.mock.method(outgoing, "emit", function (event, ...values) {
        if (hold && ["error", "close"].includes(event)) {
          held.push([event, values]);
          if (event === "close") state.heldClose.resolve();
          return true;
        }
        return emit.call(this, event, ...values);
      });
      state.releaseEvents = () => {
        hold = false;
        for (const [event, values] of held.splice(0)) emit.call(outgoing, event, ...values);
      };
    }
    return outgoing;
  });

  const cases = [
    { name: "cancellation before headers, followed by delayed reset", mode: "held-cancel", status: 499, cause: "downstream_close", phase: "before_headers", bytes: 0 },
    { name: "mid-stream cancellation", mode: "stream", status: 499, cause: "downstream_close", phase: "response_body", bytes: Buffer.byteLength(streamChunk) },
    { name: "genuine upstream reset before headers", mode: "reset", status: 502, cause: "upstream_error", phase: "before_headers", bytes: 0, code: "ECONNRESET" },
    { name: "genuine upstream reset mid-stream", mode: "stream-reset", status: 502, cause: "upstream_response_error", phase: "response_body", bytes: Buffer.byteLength(streamChunk), code: "ECONNRESET" },
    { name: "upstream error wins over later downstream destruction", mode: "error-first", status: 503, cause: "upstream_error", phase: "before_headers", bytes: 0, code: "EAI_AGAIN" },
    { name: "normal completion is not cancellation on close", mode: "complete", status: 200, cause: "completed", phase: "response_end", bytes: Buffer.byteLength(completeBody) },
    { name: "official HTTP error response is preserved", mode: "http-error", status: 429, cause: "completed", phase: "response_end", bytes: Buffer.byteLength(httpErrorBody), code: "rate_limit_error" },
    ...["response.completed", "response.failed", "response.incomplete", "error"].map((type) => ({
      name: ["response.failed", "error"].includes(type)
        ? `proxy ends after forwarded ${type}` : `client closes after forwarded ${type}`,
      mode: "sse-close", status: 200,
      body: terminalFrame(type), terminalEvent: type,
      cause: ["response.failed", "error"].includes(type) ? "upstream_terminal_failure"
        : type === "response.completed" ? "client_closed_after_completed" : "client_closed_after_terminal",
      phase: ["response.failed", "error"].includes(type) ? "terminal_forwarded" : "response_body",
      code: type === "response.completed" ? undefined : `OFFICIAL_${type.replaceAll(".", "_").toUpperCase()}`,
    })),
    { name: "fragmented UTF8 and CRLF terminal frame", mode: "sse-close", status: 200,
      body: `: http-body-secret\r\ndata: ${JSON.stringify({ type: "response.completed", response: { id: "resp_terminal", status: "completed", text: "測試" } })}\r\n\r\n`,
      fragmented: true, terminalEvent: "response.completed", cause: "client_closed_after_completed" },
    { name: "unterminated completed frame is not delivered completion", mode: "sse-close", status: 499,
      body: terminalFrame("response.completed").trimEnd(), cause: "downstream_close" },
    { name: "terminal name in ordinary text is not completion", mode: "sse-close", status: 499,
      body: 'data: {"type":"response.output_text.delta","delta":"response.completed"}\n\n', cause: "downstream_close" },
    { name: "compressed SSE stays byte preserving and unclassified", mode: "sse-close", status: 499,
      body: gzipSync(terminalFrame("response.completed")), encoding: "gzip", cause: "downstream_close" },
    { name: "failed local terminal write cannot prove delivery", mode: "sse-close", status: 499,
      body: terminalFrame("response.completed"), rejectWrite: true, cause: "downstream_close" },
    { name: "terminal forwarding preserves backpressure", mode: "sse-close", status: 200,
      body: terminalFrame("response.completed"), backpressure: true,
      terminalEvent: "response.completed", cause: "client_closed_after_completed" },
  ];
  for (const expected of cases) {
    if (expected.mode === "sse-close") {
      expected.bytes = Buffer.byteLength(expected.body);
      expected.phase ??= "response_body";
    }
    await t.test(expected.name, async () => {
      const state = active = {
        mode: expected.mode, requests: 0, outboundRequests: 0, records: [],
        streamBody: expected.body === undefined ? null : Buffer.from(expected.body),
        encoding: expected.encoding, fragmented: expected.fragmented,
        rejectWrite: expected.rejectWrite, backpressure: expected.backpressure,
        received: deferred(), logged: deferred(), heldClose: deferred(),
        ownedClosed: deferred(), officialClosed: deferred(), proxyResponseClosed: deferred(),
        downstreamClosed: deferred(),
        clientChunk: deferred(), clientDone: deferred(),
        clientChunks: [],
      };
      const raw = encodeJsonBody({ model: "gpt-5.6-sol", input: [{ type: "message", role: "user", content: "http-body-secret" }], stream: true });
      const client = http.request({
        host: "127.0.0.1", port: address.port, method: "POST",
        path: `/${runtime.installation_capability}/backend-api/codex/v1/responses?canary=http-url-secret`,
        headers: { "content-type": "application/json", "content-length": raw.length, authorization: "Bearer http-token-secret",
          session_id: "01a13501-d183-7833-97c8-a458039be661" },
      }, (response) => {
        const chunks = [];
        let bytes = 0;
        response.on("data", (chunk) => {
          chunks.push(chunk);
          state.clientChunks.push(chunk);
          bytes += chunk.length;
          if (bytes >= expected.bytes) state.clientChunk.resolve();
        });
        response.on("error", () => {});
        response.on("end", () => state.clientDone.resolve({ status: response.statusCode, body: Buffer.concat(chunks).toString("utf8") }));
      });
      client.on("error", () => {});
      client.end(raw);
      try {
        const upstreamRequest = await bounded(state.received.promise);
        if (expected.mode === "held-cancel") {
          client.destroy();
          await bounded(state.logged.promise);
          await bounded(state.heldClose.promise);
          assert.equal(state.owned.destroyed, true);
          state.releaseEvents();
        } else if (["stream", "stream-reset", "sse-close"].includes(expected.mode)) {
          await bounded(state.clientChunk.promise);
          if (expected.mode === "sse-close") assert.deepEqual(Buffer.concat(state.clientChunks), state.streamBody);
          if (expected.mode !== "stream-reset") client.destroy();
          else upstreamRequest.socket.destroy();
        } else if (expected.mode === "error-first") {
          const error = Object.assign(new Error("http-error-secret https://http-url-secret"), { code: "EAI_AGAIN", status: 503 });
          state.owned.emit("error", error);
          state.downstream.destroy();
          assert.equal(error.officialHttpLifecycle.terminal_cause, "upstream_error");
          assert.equal(error.message, "http-error-secret https://http-url-secret");
        } else {
          const result = await bounded(state.clientDone.promise);
          assert.equal(result.status, expected.status);
          if (expected.mode === "complete") assert.equal(result.body, completeBody);
          else if (expected.mode === "http-error") assert.equal(result.body, httpErrorBody);
          else assert.equal(JSON.parse(result.body).error.code, "SWITCHBOARD_INTERNAL_ERROR");
        }
        const record = await bounded(state.logged.promise);
        await bounded(state.ownedClosed.promise);
        await bounded(state.officialClosed.promise);
        await bounded(state.downstreamClosed.promise);
        if (state.proxyResponse) await bounded(state.proxyResponseClosed.promise);
        await new Promise((resolve) => setImmediate(resolve));
        assert.equal(state.records.length, 1);
        assert.equal(state.requests, 1);
        assert.equal(state.outboundRequests, 1);
        assert.equal(record.status, expected.status);
        assert.equal(record.request_correlation.client_session_id, "01a13501-d183-7833-97c8-a458039be661");
        assert.equal(record.request_correlation.source, "caller_request_not_authority");
        if (expected.mode === "sse-close" && !expected.encoding && !expected.fragmented
            && expected.terminalEvent?.startsWith("response.")
            && expected.body === terminalFrame(expected.terminalEvent)) {
          assert.equal(record.provider_response.response_id, "resp_terminal");
          assert.equal(record.provider_response.uncached_input_tokens, 20);
          assert.equal(record.provider_response.total_tokens, 120);
          assert.equal(record.provider_response.usage_status, "REPORTED");
        }
        assert.equal(record.route, expected.status === 502 || expected.status === 503 ? "switchboard" : "openai");
        assert.equal(record.error_code, expected.code || (expected.status === 499 ? "OFFICIAL_HTTP_DOWNSTREAM_CLOSED" : undefined));
        assert.deepEqual(Object.keys(record.diagnostic), ["official_http"]);
        const diagnostic = record.diagnostic.official_http;
        assert.equal(diagnostic.terminal_cause, expected.cause);
        assert.equal(diagnostic.phase, expected.phase);
        assert.equal(diagnostic.downstream_close_observed,
          expected.cause !== "upstream_terminal_failure" && ["held-cancel", "stream", "sse-close"].includes(expected.mode));
        if (expected.mode === "sse-close") {
          assert.equal(diagnostic.sse_terminal_forwarded, expected.terminalEvent ?? null);
          if (expected.encoding) assert.equal(diagnostic.sse_observation, "unsupported_content_encoding");
          if (expected.backpressure) {
            assert.equal(state.pauseObserved, true);
            assert.equal(state.resumeObserved, true);
          }
        }
        assert.equal(diagnostic.headers_sent, expected.bytes > 0);
        assert.equal(diagnostic.upstream_response_received, expected.bytes > 0);
        assert.equal(diagnostic.upstream_response_complete, ["complete", "http-error"].includes(expected.mode));
        assert.equal(diagnostic.request_bytes, raw.length);
        assert.equal(diagnostic.response_bytes, expected.bytes);
        assert.equal(state.owned.listenerCount("error"), state.initialErrorListeners);
        assert.equal(state.owned.listenerCount("response"), 0);
        assert.equal(state.downstream.listenerCount("close"), 0);
        const serialized = JSON.stringify(state.records);
        for (const secret of ["http-body-secret", "http-token-secret", "http-url-secret", "http-header-secret", "http-error-secret", runtime.installation_capability]) {
          assert.equal(serialized.includes(secret), false);
        }
      } finally {
        state.releaseEvents?.();
        client.destroy();
        state.officialResponse?.destroy();
        if (state.owned) await bounded(state.ownedClosed.promise);
        await new Promise((resolve) => setImmediate(resolve));
      }
    });
  }
});

test("switchboard preserves official raw HTTP and routes one WS across all models", async (t) => {
  const usageRecords = [];
  const originalWrite = process.stdout.write;
  t.mock.method(process.stdout, "write", function (chunk, ...args) {
    if (typeof chunk === "string" && chunk.startsWith("{")) {
      try {
        const record = JSON.parse(chunk);
        if (record.provider_response) usageRecords.push(record);
      } catch { /* not a request log */ }
    }
    return originalWrite.call(this, chunk, ...args);
  });
  const officialRequests = [];
  const officialWsModels = [];
  const webReasoning = { type: "reasoning", id: "rs_67606512f5c64226b4747d86b4f5872c", summary: [{ type: "summary_text", text: "Pro 思考" }], encrypted_content: null };
  const official = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const raw = Buffer.concat(chunks);
    officialRequests.push({ url: req.url, headers: req.headers, raw });
    res.writeHead(200, { "content-type": "application/json", "x-codex-turn-state": "official-state" });
    res.end(JSON.stringify({ ok: true }));
  });
  const officialWss = new WebSocketServer({ noServer: true });
  official.on("upgrade", (req, socket, head) => {
    officialWss.handleUpgrade(req, socket, head, (ws) => officialWss.emit("connection", ws));
  });
  officialWss.on("connection", (ws) => {
    ws.on("message", (data) => {
      const body = JSON.parse(data.toString());
      officialWsModels.push(body.model);
      assert.equal(body.input?.some((item) => item.id === webReasoning.id), false);
      assert.equal(body.input?.some((item) => Object.hasOwn(item, "is_canonical_summary")), false);
      if (body.previous_response_id === "invalid_for_connection") {
        ws.send(JSON.stringify({ type: "error", status: 400,
          error: { type: "invalid_request_error", message: "Invalid `previous_response_id`." } }));
        return;
      }
      const id = `resp_official_${officialWsModels.length}`;
      ws.send(JSON.stringify({ type: "response.created", response: { id } }));
      ws.send(JSON.stringify({
        type: "response.output_item.done",
        item: { type: "message", role: "assistant", id: `msg_${id}`, content: [{ type: "output_text", text: "official ok" }] },
      }));
      ws.send(JSON.stringify({ type: "response.completed", response: { id,
        model: body.model, usage: { input_tokens: 80, output_tokens: 10, total_tokens: 90,
          input_tokens_details: { cached_tokens: 64 } } } }));
    });
  });
  const officialAddress = await listen(official);

  const runtime = { installation_capability: "test-capability-abcdefghijklmnopqrstuvwxyz", host: "127.0.0.1", port: 0 };
  const admission = {
    status: "ADMITTED",
    external_batch_admitted: true,
    authority_effect: EXTERNAL_EFFECT_SCOPE,
    protocol_compatibility: { external_model_orchestration: EXTERNAL_ORCHESTRATION_PROTOCOL },
    policy: { external_models: "allow", effect_scope: EXTERNAL_EFFECT_SCOPE },
    official_models: ["gpt-5.6-sol"],
    catalog_sha256: "catalog-sha",
    cli_sha256: "cli-sha",
  };
  const xai = new FakeWorker("xai");
  const agy = new FakeWorker("agy");
  const openrouter = new FakeWorker("openrouter");
  const web = new FakeWorker("web");
  const switchboard = await createSwitchboardServer({
    runtime,
    admission,
    xaiWorker: xai,
    agyWorker: agy,
    openrouterWorker: openrouter,
    webWorker: web,
    modelCatalog: {
      models: [
        { slug: "gpt-5.6-sol" },
        { slug: "gemini-3.8-flash-high" },
        { slug: "chatgpt-web/high" },
      ],
    },
    skipFileGate: true,
    officialOrigin: `http://127.0.0.1:${officialAddress.port}`,
  });
  const address = await switchboard.listen({ host: "127.0.0.1", port: 0 });
  t.after(async () => {
    await switchboard.close();
    officialWss.close();
    await closeServer(official);
  });
  const base = "/test-capability-abcdefghijklmnopqrstuvwxyz/backend-api/codex/v1";

  const modelResponse = await request({
    port: address.port,
    path: `${base}/models`,
    method: "GET",
  });
  assert.equal(modelResponse.status, 200);
  assert.deepEqual(JSON.parse(modelResponse.body).models.map((model) => model.slug), [
    "gpt-5.6-sol",
    "gemini-3.8-flash-high",
    "chatgpt-web/high",
  ]);
  assert.equal(officialRequests.length, 0);

  const officialBody = { model: "gpt-5.6-sol", input: [{ type: "message", role: "user", content: "official" }] };
  const officialRaw = encodeJsonBody(officialBody, "zstd");
  const officialResponse = await request({
    port: address.port,
    path: `${base}/responses?beta=1`,
    body: officialBody,
    encoding: "zstd",
    headers: {
      authorization: "Bearer official-auth-canary",
      "chatgpt-account-id": "official-account-canary",
      "x-oai-attestation": "official-attestation-canary",
    },
  });
  assert.equal(officialResponse.status, 200);
  assert.equal(officialResponse.headers["x-codex-turn-state"], "official-state");
  assert.equal(officialRequests[0].url, "/backend-api/codex/responses?beta=1");
  assert.deepEqual(officialRequests[0].raw, officialRaw);
  assert.equal(officialRequests[0].headers.authorization, "Bearer official-auth-canary");
  assert.equal(officialRequests[0].headers["chatgpt-account-id"], "official-account-canary");
  assert.equal(officialRequests[0].headers["x-oai-attestation"], "official-attestation-canary");

  const externalResponse = await request({
    port: address.port,
    path: `${base}/responses`,
    body: {
      model: "grok-4.6",
      input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "external" }] }],
      service_tier: "priority",
      client_metadata: { secret: "thread-canary" },
      tools: [
        { type: "image_generation" },
        { type: "function", name: "local", parameters: { type: "object" } },
      ],
    },
    headers: { authorization: "Bearer must-not-reach-worker" },
  });
  assert.equal(externalResponse.status, 200);
  assert.match(externalResponse.body.toString(), /response.completed/);
  assert.equal(xai.requests[0].service_tier, undefined);
  assert.equal(xai.requests[0].client_metadata, undefined);
  assert.deepEqual(xai.requests[0].tools.map((tool) => tool.type), ["function"]);

  const openrouterResponse = await request({
    port: address.port,
    path: `${base}/responses`,
    body: {
      model: "stealth/ox-alpha",
      input: [{ type: "message", role: "user", content: "openrouter" }],
      tools: [],
    },
  });
  assert.equal(openrouterResponse.status, 400);
  assert.equal(JSON.parse(openrouterResponse.body).error.code, "MODEL_ROUTE_UNAVAILABLE");
  assert.equal(openrouter.requests.length, 0);

  const compactV2Response = await request({
    port: address.port,
    path: `${base}/responses`,
    body: {
      model: "gemini-3.8-flash-high",
      input: [
        { type: "message", role: "user", content: [{ type: "input_text", text: "history" }] },
        { type: "compaction_trigger" },
      ],
      tools: [],
      stream: true,
    },
  });
  assert.equal(compactV2Response.status, 200);
  const compactV2Events = compactV2Response.body.toString()
    .split(/\r?\n/)
    .filter((line) => line.startsWith("data: "))
    .map((line) => JSON.parse(line.slice(6)));
  const compactV2Items = compactV2Events
    .filter((event) => event.type === "response.output_item.done")
    .map((event) => event.item);
  assert.equal(compactV2Items.length, 1);
  assert.equal(compactV2Items[0].type, "compaction");
  assert.equal(compactV2Events.at(-1).type, "response.completed");
  assert.deepEqual(compactV2Events.at(-1).response.output, compactV2Items);
  assert.equal(agy.requests.at(-1).input.at(-1).type, "compaction_trigger");

  const compactResponse = await request({
    port: address.port,
    path: `${base}/responses/compact`,
    body: { model: "grok-4.6", input: [{ type: "message", role: "user", content: "compact me" }] },
  });
  const compact = JSON.parse(compactResponse.body);
  assert.equal(compact.object, "response.compaction");
  assert.equal(compact.model, "grok-4.6");
  assert.equal(typeof compact.created_at, "number");
  assert.match(compact.id, /^resp_compaction_switchboard_/);
  assert.equal(compact.output.length, 1);
  assert.equal(compact.output[0].type, "compaction");
  assert.equal(compact.output[0].created_by, "grok-4.6");
  assert.doesNotMatch(compact.output[0].encrypted_content, /private-opaque-canary/);

  const replacement = [{ type: "message", role: "user", id: "environment-source",
    content: [{ type: "input_text", text: "<environment_context><cwd>/fixture</cwd></environment_context>" }],
    internal_chat_message_metadata_passthrough: { content_item_kinds: ["environment_context"] } },
  { type: "message", role: "user", content: [{ type: "input_text", text: "exact upstream summary" }] }];
  const originalCompact = web.compact;
  web.compact = async () => ({ summary: "exact upstream summary", replacement_history: replacement });
  try {
    const unary = await request({ port: address.port, path: `${base}/responses/compact`,
      body: { model: "chatgpt-web/high", input: [] } });
    assert.equal(unary.status, 200);
    assert.deepEqual(JSON.parse(unary.body).output, replacement);
  } finally {
    web.compact = originalCompact;
  }
  await request({
    port: address.port,
    path: `${base}/responses`,
    body: { model: "gpt-5.6-sol", input: compact.output },
  });
  const rewritten = JSON.parse(decodeBody(officialRequests.at(-1).raw, "identity"));
  assert.equal(rewritten.input[0].type, "message");
  assert.match(rewritten.input[0].content[0].text, /portable summary/);
  assert.equal(Object.hasOwn(rewritten.input[0], "is_canonical_summary"), false);
  assert.doesNotMatch(JSON.stringify(rewritten), /private-opaque-canary/);
  await request({
    port: address.port, path: `${base}/responses`,
    body: { model: "gpt-5.6-sol", store: false, input: [webReasoning] },
  });
  const webToNative = JSON.parse(decodeBody(officialRequests.at(-1).raw, "identity"));
  assert.deepEqual(webToNative.input, [{ type: "reasoning", summary: webReasoning.summary }]);
  assert.equal(webToNative.store, false);

  const searchResponse = await request({
    port: address.port,
    path: `${base}/alpha/search`,
    body: { model: "grok-4.6", query: "test" },
  });
  assert.equal(searchResponse.status, 400);
  assert.equal(JSON.parse(searchResponse.body).error.code, "EXTERNAL_HOSTED_CAPABILITY_UNAVAILABLE");

  const ws = new WebSocket(`ws://127.0.0.1:${address.port}${base}/responses`, {
    headers: { session_id: "01a13501-d183-7833-97c8-a458039be661" },
  });
  await new Promise((resolve, reject) => { ws.once("open", resolve); ws.once("error", reject); });
  const sequence = [
    ["gpt-5.6-sol", "openai"],
    ["chatgpt-web/high", "web"],
    ["grok-4.6", "xai"],
    ["gemini-3.8-flash-high", "agy"],
    ["gpt-5.6-sol", "openai"],
  ];
  for (const [model, expected] of sequence) {
    const events = await terminalWsTurn(ws, {
      type: "response.create",
      model,
      input: [webReasoning, ...(expected === "openai" ? compact.output : []),
        { type: "message", role: "user", content: [{ type: "input_text", text: model }] }],
      tools: [],
      store: false,
    });
    assert.equal(events.at(-1).type, "response.completed", `${expected} route should complete`);
    if (expected === "openai") {
      const record = usageRecords.at(-1);
      assert.equal(record.transport, "ws");
      assert.equal(record.request_correlation.client_session_id, "01a13501-d183-7833-97c8-a458039be661");
      assert.equal(record.provider_response.response_id, events.at(-1).response.id);
      assert.equal(record.provider_response.model, model);
      assert.equal(record.provider_response.uncached_input_tokens, 16);
      assert.equal(record.provider_response.total_tokens, 90);
      assert.equal(record.provider_response.reasoning_tokens, null);
    }
  }
  const firstWeb = await terminalWsTurn(ws, {
    type: "response.create",
    model: "chatgpt-web/high",
    input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "first Web turn" }] }],
    tools: [],
    store: false,
  });
  const firstWebId = firstWeb.find((event) => event.type === "response.created")?.response?.id;
  await terminalWsTurn(ws, {
    type: "response.create",
    model: "chatgpt-web/high",
    previous_response_id: firstWebId,
    input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "continue Web turn" }] }],
    tools: [],
    store: false,
  });
  assert.equal(web.requests.at(-1).previous_response_id, firstWebId);
  const wsCompactV2 = await terminalWsTurn(ws, {
    type: "response.create",
    model: "gemini-3.8-flash-high",
    input: [
      { type: "message", role: "user", content: [{ type: "input_text", text: "ws history" }] },
      { type: "compaction_trigger" },
    ],
    tools: [],
    store: false,
  });
  const wsCompactV2Items = wsCompactV2
    .filter((event) => event.type === "response.output_item.done")
    .map((event) => event.item);
  assert.equal(wsCompactV2Items.length, 1);
  assert.equal(wsCompactV2Items[0].type, "compaction");
  assert.deepEqual(wsCompactV2.at(-1).response.output, wsCompactV2Items);
  const firstXai = await terminalWsTurn(ws, {
    type: "response.create",
    model: "grok-4.6",
    input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "first tool-loop turn" }] }],
    tools: [],
    store: false,
  });
  const firstXaiId = firstXai.find((event) => event.type === "response.created")?.response?.id;
  const secondXai = await terminalWsTurn(ws, {
    type: "response.create",
    model: "grok-4.6",
    previous_response_id: firstXaiId,
    input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "second tool-loop turn" }] }],
    tools: [],
    store: false,
  });
  assert.equal(secondXai.at(-1).type, "response.completed");
  assert.match(xai.requests.at(-1).previous_response_id, /^resp_xai_/);
  assert.deepEqual(xai.requests.at(-1).input.map((item) => item.type), ["message"]);
  const closed = new Promise(resolve => ws.once("close", code => resolve(code)));
  for (const upstream of officialWss.clients) upstream.close(1000, "idle upstream expiry");
  const timeout = setTimeout(() => ws.terminate(), 1500);
  const closeCode = await closed;
  clearTimeout(timeout);
  assert.equal(closeCode, 1012, "native client must observe upstream connection retirement");
  assert.deepEqual(officialWsModels, ["gpt-5.6-sol", "gpt-5.6-sol"]);
  assert.ok(xai.requests.some((body) => body.model === "grok-4.6"));
  assert.ok(agy.requests.some((body) => body.model === "gemini-3.8-flash-high"));
  assert.equal(web.requests.length, 3);
  assert.equal(openrouter.requests.length, 0);
  const rejectedWs = new WebSocket(`ws://127.0.0.1:${address.port}${base}/responses`);
  await new Promise((resolve, reject) => { rejectedWs.once("open", resolve); rejectedWs.once("error", reject); });
  const rejectionClosed = new Promise(resolve => rejectedWs.once("close", resolve));
  const rejectedEvents = await terminalWsTurn(rejectedWs, {
    type: "response.create", model: "gpt-5.6-sol", previous_response_id: "invalid_for_connection", input: [],
  });
  assert.equal(rejectedEvents.at(-1).type, "error");
  const rejectionTimeout = setTimeout(() => rejectedWs.terminate(), 1500);
  assert.equal(await rejectionClosed, 1012);
  clearTimeout(rejectionTimeout);
  assert.equal(officialWsModels.length, 3, "rejected request was forwarded exactly once, never replayed");
});

test("Web pre-submit and delivery-uncertain failures do not retry, substitute, or disable other providers", async (t) => {
  const officialRequests = [];
  const official = http.createServer(async (req, res) => {
    for await (const _chunk of req) {}
    officialRequests.push(req.url);
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ ok: true }));
  });
  const officialAddress = await listen(official);
  const web = new InjectedWebFailureWorker();
  const xai = new FakeWorker("xai");
  const agy = new FakeWorker("agy");
  const switchboard = await createSwitchboardServer({
    runtime: { installation_capability: "test-capability-abcdefghijklmnopqrstuvwxyz", host: "127.0.0.1", port: 0 },
    admission: {
      status: "ADMITTED",
      external_batch_admitted: true,
      authority_effect: EXTERNAL_EFFECT_SCOPE,
      protocol_compatibility: { external_model_orchestration: EXTERNAL_ORCHESTRATION_PROTOCOL },
      policy: { external_models: "allow", effect_scope: EXTERNAL_EFFECT_SCOPE },
      official_models: ["gpt-5.6-sol"],
      catalog_sha256: "catalog-sha",
      cli_sha256: "cli-sha",
    },
    xaiWorker: xai,
    agyWorker: agy,
    openrouterWorker: new FakeWorker("openrouter"),
    webWorker: web,
    skipFileGate: true,
    officialOrigin: `http://127.0.0.1:${officialAddress.port}`,
  });
  const address = await switchboard.listen({ host: "127.0.0.1", port: 0 });
  t.after(async () => {
    await switchboard.close();
    await closeServer(official);
  });
  const path = "/test-capability-abcdefghijklmnopqrstuvwxyz/backend-api/codex/v1/responses";
  const turn = (model, content, tools = []) => request({
    port: address.port,
    path,
    body: { model, input: [{ type: "message", role: "user", content }], tools },
  });

  const preSubmit = await turn("chatgpt-web/high", "pre-submit");
  assert.equal(preSubmit.status, 502);
  assert.equal(JSON.parse(preSubmit.body).error.code, "WEB_PROVIDER_UNAVAILABLE");
  assert.equal((await turn("gpt-5.6-sol", "native")).status, 200);

  const uncertain = await turn("chatgpt-web/pro", "post-submit");
  assert.equal(uncertain.status, 502);
  assert.equal(JSON.parse(uncertain.body).error.code, "WEB_DELIVERY_UNCERTAIN");
  assert.equal((await turn("grok-4.6", "grok")).status, 200);
  assert.equal((await turn("gpt-5.6-sol", "full-harness", [{ type: "function", name: "local" }])).status, 200);

  assert.deepEqual(web.outboundIdentities, ["web-outbound-1", "web-outbound-2"]);
  assert.equal(web.requests.length, 2);
  assert.equal(officialRequests.length, 2);
  assert.equal(xai.requests.length, 1);
  assert.equal(agy.requests.length, 0);
});

test("external WS response.cancel aborts only the active worker session", async (t) => {
  const runtime = { installation_capability: "test-capability-abcdefghijklmnopqrstuvwxyz", host: "127.0.0.1", port: 0 };
  const admission = {
    status: "ADMITTED",
    external_batch_admitted: true,
    authority_effect: EXTERNAL_EFFECT_SCOPE,
    protocol_compatibility: { external_model_orchestration: EXTERNAL_ORCHESTRATION_PROTOCOL },
    policy: { external_models: "allow", effect_scope: EXTERNAL_EFFECT_SCOPE },
    official_models: ["gpt-5.6-sol"],
    catalog_sha256: "catalog-sha",
    cli_sha256: "cli-sha",
  };
  const agy = new CancellableWorker("agy");
  const switchboard = await createSwitchboardServer({
    runtime,
    admission,
    xaiWorker: new FakeWorker("xai"),
    agyWorker: agy,
    openrouterWorker: new FakeWorker("openrouter"),
    skipFileGate: true,
  });
  const address = await switchboard.listen({ host: "127.0.0.1", port: 0 });
  const ws = new WebSocket(`ws://127.0.0.1:${address.port}/test-capability-abcdefghijklmnopqrstuvwxyz/backend-api/codex/v1/responses`);
  t.after(async () => {
    if (ws.readyState === WebSocket.OPEN) ws.close();
    await switchboard.close();
  });
  await new Promise((resolve, reject) => { ws.once("open", resolve); ws.once("error", reject); });

  const terminal = new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("WebSocket cancel timed out")), 5_000);
    ws.on("message", (data) => {
      const event = JSON.parse(data.toString());
      if (event.type === "response.created") ws.send(JSON.stringify({ type: "response.cancel" }));
      if (["response.completed", "response.failed", "response.incomplete", "error"].includes(event.type)) {
        clearTimeout(timer);
        resolve(event);
      }
    });
  });
  ws.send(JSON.stringify({
    type: "response.create",
    model: "gemini-3.8-flash-high",
    input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "cancel me" }] }],
    tools: [],
  }));

  const event = await terminal;
  assert.equal(event.type, "error");
  assert.equal(event.status, 499);
  assert.equal(event.error.code, "AGY_REQUEST_CANCELLED");
  assert.equal(agy.closedSessions.length, 1);
});

class KeyRecordingWorker extends FakeWorker {
  constructor(route) {
    super(route);
    this.keys = [];
    this.closedSessions = [];
  }
  async runResponses(body, options) {
    this.keys.push({ sessionKey: options.sessionKey, ownerKey: options.ownerKey });
    return super.runResponses(body, options);
  }
  async closeSession(sessionKey) {
    this.closedSessions.push(sessionKey);
  }
}

test("normal server shutdown closes idle WebSockets with a close handshake", async (t) => {
  const instance = await createSwitchboardServer({
    runtime: { installation_capability: "test-capability-abcdefghijklmnopqrstuvwxyz" },
    admission: { official_models: [] }, skipFileGate: true,
    xaiWorker: new FakeWorker("xai"), agyWorker: new FakeWorker("agy"),
    openrouterWorker: new FakeWorker("openrouter"), webWorker: new FakeWorker("web"),
  });
  const address = await instance.listen({ host: "127.0.0.1", port: 0 });
  const ws = new WebSocket(`ws://127.0.0.1:${address.port}/test-capability-abcdefghijklmnopqrstuvwxyz/backend-api/codex/v1/responses`);
  await new Promise((resolve, reject) => { ws.once("open", resolve); ws.once("error", reject); });
  t.after(async () => { ws.terminate(); await instance.close(); });
  const closed = new Promise((resolve) => ws.once("close", (code) => resolve(code)));
  let timer;
  try {
    await Promise.race([
      instance.close(),
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("IDLE_WEBSOCKET_BLOCKS_NORMAL_SHUTDOWN")), 1000); }),
    ]);
  } finally { clearTimeout(timer); }
  assert.equal(await closed, 1001);
});

for (const [name, code, contextError, terminalType] of [
  ["upstream incomplete", "AGY_UPSTREAM_INCOMPLETE", false],
  ["rejected protocol", "AGY_INVALID_STRUCTURED_OUTPUT", false],
  ["worker timeout", "EXTERNAL_WORKER_TIMEOUT", false],
  ["context overflow", "EXTERNAL_MANDATORY_CONTEXT_TOO_LARGE", true],
  ["worker ended without terminal", null, false],
  ["provider error event", "AGY_UPSTREAM_INCOMPLETE", false, "error"],
  ["terminal followed by worker error", "AGY_UPSTREAM_INCOMPLETE", false, "response.failed"],
]) for (const transport of ["HTTP", "WebSocket"]) {
  test(`external ${transport} ${name} ends with one native-readable failed response`, async (t) => {
    class FailureWorker extends FakeWorker {
      async runResponses(body, { onEvent }) {
        this.requests.push(structuredClone(body));
        onEvent({ type: "response.created", response: { id: "resp_external_failure" } });
        onEvent({ type: "response.in_progress", response: { id: "resp_external_failure", status: "in_progress" } });
        if (terminalType) {
          onEvent(terminalType === "error"
            ? { type: terminalType, error: { code, message: "Injected terminal provider failure." } }
            : { type: terminalType, response: {
                id: "resp_external_failure", status: "failed",
                error: { code: "server_error", message: `${code}: Injected terminal provider failure.` },
              } });
          onEvent({ type: "response.completed", response: { id: "resp_external_failure" } });
        }
        if (code) throw new SwitchboardError(code, "Injected external failure; no tool was accepted.", 502);
        return { status: 200 };
      }
    }
    const agy = new FailureWorker("agy");
    const instance = await createSwitchboardServer({
      runtime: { installation_capability: "test-capability-abcdefghijklmnopqrstuvwxyz" },
      admission: {
        status: "ADMITTED", external_batch_admitted: true,
        authority_effect: EXTERNAL_EFFECT_SCOPE,
        protocol_compatibility: { external_model_orchestration: EXTERNAL_ORCHESTRATION_PROTOCOL },
        policy: { external_models: "allow", effect_scope: EXTERNAL_EFFECT_SCOPE },
        official_models: ["gpt-5.6-sol"], catalog_sha256: "catalog-sha", cli_sha256: "cli-sha",
      },
      xaiWorker: new FakeWorker("xai"), agyWorker: agy,
      openrouterWorker: new FakeWorker("openrouter"), webWorker: new FakeWorker("web"),
      modelCatalog: { models: [{ slug: "gemini-3.8-flash-high" }] }, skipFileGate: true,
    });
    const address = await instance.listen({ host: "127.0.0.1", port: 0 });
    t.after(() => instance.close());
    const body = { model: "gemini-3.8-flash-high", input: [{ role: "user", content: "Read only." }], tools: [], stream: true };
    const path = "/test-capability-abcdefghijklmnopqrstuvwxyz/backend-api/codex/v1/responses";
    let events;
    if (transport === "HTTP") {
      const result = await request({ port: address.port, path, body });
      assert.equal(result.status, 200, "headers already started before the upstream failure");
      events = new SseDecoder().push(result.body);
    } else {
      const ws = new WebSocket(`ws://127.0.0.1:${address.port}${path}`);
      await new Promise((resolve, reject) => { ws.once("open", resolve); ws.once("error", reject); });
      events = [];
      ws.on("message", (data) => events.push(JSON.parse(data.toString())));
      try {
        await terminalWsTurn(ws, { type: "response.create", ...body });
      } finally {
        const closed = new Promise((resolve) => ws.once("close", resolve));
        ws.close();
        await closed;
      }
    }
    const terminals = events.filter((event) => ["response.completed", "response.failed", "response.incomplete", "error"].includes(event.type));
    assert.equal(terminals.length, 1);
    assert.equal(terminals[0].type, "response.failed", "a bare error followed by EOF is not a Responses failure terminal");
    assert.equal(terminals[0].response.id, events[0].response.id);
    assert.equal(terminals[0].response.status, "failed");
    assert.equal(terminals[0].response.error.code, contextError ? "context_length_exceeded" : "server_error");
    assert.match(terminals[0].response.error.message, new RegExp(code || "EXTERNAL_STREAM_INCOMPLETE"));
    assert.equal(terminals[0].response.usage, undefined, "no fabricated usage on failed inference");
    assert.equal(events.some((event) => event.item?.type === "function_call"), false);
    assert.equal(agy.requests.length, 1, "the adapter does not re-infer a failed request");
  });
}

test("Grok and AGY sessions follow the caller thread, while the connection or request owns the turn", async (t) => {
  const runtime = { installation_capability: "test-capability-abcdefghijklmnopqrstuvwxyz", host: "127.0.0.1", port: 0 };
  const admission = {
    status: "ADMITTED",
    external_batch_admitted: true,
    authority_effect: EXTERNAL_EFFECT_SCOPE,
    protocol_compatibility: { external_model_orchestration: EXTERNAL_ORCHESTRATION_PROTOCOL },
    policy: { external_models: "allow", effect_scope: EXTERNAL_EFFECT_SCOPE },
    official_models: ["gpt-5.6-sol"],
    catalog_sha256: "catalog-sha",
    cli_sha256: "cli-sha",
  };
  const xai = new KeyRecordingWorker("xai");
  const agy = new KeyRecordingWorker("agy");
  const switchboard = await createSwitchboardServer({
    runtime,
    admission,
    xaiWorker: xai,
    agyWorker: agy,
    openrouterWorker: new FakeWorker("openrouter"),
    skipFileGate: true,
  });
  const address = await switchboard.listen({ host: "127.0.0.1", port: 0 });
  const base = "/test-capability-abcdefghijklmnopqrstuvwxyz/backend-api/codex/v1";
  const open = async () => {
    const ws = new WebSocket(`ws://127.0.0.1:${address.port}${base}/responses`);
    await new Promise((resolve, reject) => { ws.once("open", resolve); ws.once("error", reject); });
    return ws;
  };
  const sockets = [];
  t.after(async () => {
    for (const ws of sockets) if (ws.readyState === WebSocket.OPEN) ws.close();
    await switchboard.close();
  });
  const frame = (model, metadata) => ({
    type: "response.create",
    model,
    input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "hello" }] }],
    tools: [],
    store: false,
    ...(metadata ? { client_metadata: metadata } : {}),
  });
  const threadA = "019e0000-aaaa-7000-8000-00000000000a";
  const threadB = "019e0000-bbbb-7000-8000-00000000000b";

  const first = await open();
  const second = await open();
  sockets.push(first, second);
  await terminalWsTurn(first, frame("grok-4.6", { thread_id: threadA }));
  await terminalWsTurn(second, frame("grok-4.6", {
    "x-codex-turn-metadata": JSON.stringify({ thread_id: threadA, turn_id: "turn-2" }),
  }));
  await terminalWsTurn(second, frame("grok-4.6", { thread_id: threadB }));
  await terminalWsTurn(second, frame("grok-4.6", {
    "x-codex-turn-metadata": JSON.stringify({ thread_id: threadA, context_window_id: "window-2" }),
  }));
  await terminalWsTurn(second, frame("gemini-3.8-flash-high", { thread_id: threadA }));
  await terminalWsTurn(second, frame("grok-4.6", null));

  const [aOnFirst, aOnSecond, bOnSecond, aNextWindow, noThread] = xai.keys;
  assert.match(aOnFirst.sessionKey, /^thread-[a-f0-9]{32}$/);
  assert.equal(aOnSecond.sessionKey, aOnFirst.sessionKey, "a reconnect reaches the same thread session");
  assert.notEqual(aOnSecond.ownerKey, aOnFirst.ownerKey, "each connection owns its own turns");
  assert.notEqual(bOnSecond.sessionKey, aOnFirst.sessionKey);
  assert.notEqual(aNextWindow.sessionKey, aOnFirst.sessionKey, "a new context window starts a new session");
  assert.equal(noThread.sessionKey, noThread.ownerKey, "a caller without a thread keeps a connection session");
  assert.match(noThread.ownerKey, /^ws-/);
  assert.equal(agy.keys[0].sessionKey, aOnFirst.sessionKey);
  assert.doesNotMatch(JSON.stringify([...xai.keys, ...agy.keys]), new RegExp(threadA));

  const closedBefore = xai.closedSessions.length;
  const firstClosed = new Promise((resolve) => first.once("close", resolve));
  first.close();
  await firstClosed;
  await new Promise((resolve) => setTimeout(resolve, 50));
  const closedNow = xai.closedSessions.slice(closedBefore);
  assert.deepEqual(closedNow, [aOnFirst.ownerKey], "closing a connection cancels its owner, not the thread session");

  const response = await request({
    port: address.port,
    path: `${base}/responses`,
    headers: { session_id: threadA },
    body: { model: "grok-4.6", input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "http" }] }], tools: [], stream: true },
  });
  assert.equal(response.status, 200);
  const httpKeys = xai.keys.at(-1);
  assert.equal(httpKeys.sessionKey, aOnFirst.sessionKey, "an HTTP request on the thread reaches the same session");
  assert.match(httpKeys.ownerKey, /^http-/);
  assert.equal(xai.closedSessions.at(-1), httpKeys.ownerKey, "the request releases its owner only");
});

test("external WS response.cancel on a thread session aborts the connection's turn", async (t) => {
  const runtime = { installation_capability: "test-capability-abcdefghijklmnopqrstuvwxyz", host: "127.0.0.1", port: 0 };
  const admission = {
    status: "ADMITTED",
    external_batch_admitted: true,
    authority_effect: EXTERNAL_EFFECT_SCOPE,
    protocol_compatibility: { external_model_orchestration: EXTERNAL_ORCHESTRATION_PROTOCOL },
    policy: { external_models: "allow", effect_scope: EXTERNAL_EFFECT_SCOPE },
    official_models: ["gpt-5.6-sol"],
    catalog_sha256: "catalog-sha",
    cli_sha256: "cli-sha",
  };
  const agy = new CancellableWorker("agy");
  const switchboard = await createSwitchboardServer({
    runtime,
    admission,
    xaiWorker: new FakeWorker("xai"),
    agyWorker: agy,
    openrouterWorker: new FakeWorker("openrouter"),
    skipFileGate: true,
  });
  const address = await switchboard.listen({ host: "127.0.0.1", port: 0 });
  const ws = new WebSocket(`ws://127.0.0.1:${address.port}/test-capability-abcdefghijklmnopqrstuvwxyz/backend-api/codex/v1/responses`);
  t.after(async () => {
    if (ws.readyState === WebSocket.OPEN) ws.close();
    await switchboard.close();
  });
  await new Promise((resolve, reject) => { ws.once("open", resolve); ws.once("error", reject); });
  const terminal = new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("WebSocket cancel timed out")), 5_000);
    ws.on("message", (data) => {
      const event = JSON.parse(data.toString());
      if (event.type === "response.created") ws.send(JSON.stringify({ type: "response.cancel" }));
      if (["response.completed", "response.failed", "response.incomplete", "error"].includes(event.type)) {
        clearTimeout(timer);
        resolve(event);
      }
    });
  });
  ws.send(JSON.stringify({
    type: "response.create",
    model: "gemini-3.8-flash-high",
    client_metadata: { thread_id: "019e0000-cccc-7000-8000-00000000000c" },
    input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "cancel me" }] }],
    tools: [],
  }));
  const event = await terminal;
  assert.equal(event.type, "error");
  assert.equal(event.error.code, "AGY_REQUEST_CANCELLED");
  assert.match(agy.closedSessions[0], /^ws-/, "cancel closes the connection owner, not the thread session");
});
