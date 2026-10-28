import assert from "node:assert/strict";
import http from "node:http";
import { createHash } from "node:crypto";
import { resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import test from "node:test";
import { gzipSync } from "node:zlib";

const sourceRoot = process.env.SWITCHBOARD_TEST_SOURCE_ROOT
  || fileURLToPath(new URL("..", import.meta.url));
const { createSwitchboardServer } = await import(pathToFileURL(resolve(sourceRoot, "src/server.mjs")));
const sha = (value) => createHash("sha256").update(value).digest("hex");
const frame = (event) => Buffer.from(`data: ${JSON.stringify(event)}\n\n`);
const message = "UPSTREAM_MESSAGE_SECRET: temporary provider failure, preserve every byte.";
const upstreamError = { code: "server_error", type: "server_error", message };
const partial = frame({ type: "response.output_text.delta", delta: "partial fixture reply" });
const failed = frame({ type: "response.failed", response: {
  id: "resp_failure_fixture", status: "failed", output: [], error: upstreamError,
} });
const topError = frame({ type: "error", error: upstreamError });
const worker = { close() {}, async closeSession() {},
  async runResponses() { throw new Error("External worker must not run in this fixture."); } };

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

async function bounded(promise, label, milliseconds = 1200) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_resolve, reject) => {
      timer = setTimeout(() => reject(new Error(label)), milliseconds);
    })]);
  } finally { clearTimeout(timer); }
}

function listen(server) {
  return new Promise((done, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => done(server.address()));
  });
}

async function fixture(t, { wire, eof = false, fragmented = false, encoding = null,
  backpressure = false, rejectTerminalWrite = false, lateReset = false, splitPrefix = false,
  delayedWriteCallback = false, upstreamResetBeforeCallback = false } = {}) {
  const records = [], clientChunks = [];
  const received = deferred(), chunkReceived = deferred(), ended = deferred(), logged = deferred();
  const upstreamClosed = deferred();
  const prefixReceived = deferred();
  // Resolved once the switchboard's own upstream response has closed: an "upstream reset before
  // the delayed callback" scenario waits for it instead of racing two timers under load.
  const upstreamSeenClosed = deferred();
  let client, downstream, owned, upstreamResponse, requestCount = 0;
  let destroyCalls = 0, pauseObserved = false, resetEmitted = false;
  const stdoutWrite = process.stdout.write;
  t.mock.method(process.stdout, "write", function (chunk, ...args) {
    if (typeof chunk === "string" && chunk.startsWith("{")) {
      try {
        const record = JSON.parse(chunk);
        if (record.endpoint === "/responses") {
          records.push(record); logged.resolve(record);
        }
      } catch { /* Non-JSON test runner output is not fixture evidence. */ }
    }
    return stdoutWrite.call(this, chunk, ...args);
  });
  const official = http.createServer(async (req, res) => {
    for await (const _chunk of req) { /* Consume only the synthetic request. */ }
    requestCount += 1;
    res.once("close", () => upstreamClosed.resolve());
    res.writeHead(200, { "content-type": "text/event-stream",
      ...(encoding ? { "content-encoding": encoding } : {}) });
    received.resolve();
    if (splitPrefix) {
      res.write(wire.subarray(0, partial.length));
      await prefixReceived.promise;
      if (!res.destroyed) res.write(wire.subarray(partial.length));
    } else if (fragmented) {
      for (const byte of wire) {
        if (res.destroyed) break;
        res.write(Buffer.from([byte]));
        await new Promise((done) => setImmediate(done));
      }
    } else res.write(wire);
    if (eof && !res.destroyed) res.end();
    else if (upstreamResetBeforeCallback) setTimeout(() => res.destroy(), 10);
  });
  const officialAddress = await listen(official);
  const capability = "fixture-capability-no-installed-identity";
  const switchboard = await createSwitchboardServer({
    runtime: { installation_capability: capability, host: "127.0.0.1", port: 0 },
    admission: { official_models: ["gpt-6-astra"] }, skipFileGate: true,
    officialOrigin: `http://127.0.0.1:${officialAddress.port}`,
    xaiWorker: worker, agyWorker: worker, openrouterWorker: worker, webWorker: worker,
  });
  const address = await switchboard.listen({ host: "127.0.0.1", port: 0 });
  t.after(async () => {
    prefixReceived.resolve();
    client?.destroy();
    switchboard.server.closeAllConnections();
    official.closeAllConnections();
    await switchboard.close();
    await new Promise((done) => official.close(done));
  });
  switchboard.server.on("request", (_req, res) => {
    downstream = res;
    if (!backpressure && !rejectTerminalWrite && !delayedWriteCallback) return;
    const originalWrite = res.write;
    t.mock.method(res, "write", function (chunk, callback) {
      const ready = originalWrite.call(this, chunk, (error) => {
        const finish = () => callback?.(
          rejectTerminalWrite ? new Error("Injected terminal local write failure") : error);
        if (delayedWriteCallback) {
          if (upstreamResetBeforeCallback) return upstreamSeenClosed.promise.then(() => setTimeout(finish, 5));
          return setTimeout(finish, 40);
        }
        if (!backpressure) return finish();
        setImmediate(() => {
          pauseObserved ||= Boolean(upstreamResponse?.isPaused());
          res.emit("drain");
          finish();
        });
      });
      if (!backpressure) return ready;
      return false;
    });
  });
  const originalRequest = http.request;
  t.mock.method(http, "request", function (...args) {
    const outgoing = originalRequest.apply(this, args);
    if (!(args[0] instanceof URL) || args[0].port !== String(officialAddress.port)) return outgoing;
    owned = outgoing;
    outgoing.once("response", (response) => {
      upstreamResponse = response;
      response.once("close", () => upstreamSeenClosed.resolve());
    });
    outgoing.once("close", () => upstreamSeenClosed.resolve());
    const originalDestroy = outgoing.destroy;
    t.mock.method(outgoing, "destroy", function (...destroyArgs) {
      destroyCalls += 1;
      if (lateReset) {
        resetEmitted = true;
        return originalDestroy.call(this, Object.assign(new Error("LATE_RESET_SECRET"), { code: "ECONNRESET" }));
      }
      return originalDestroy.apply(this, destroyArgs);
    });
    return outgoing;
  });
  const raw = Buffer.from(JSON.stringify({ model: "gpt-6-astra", stream: true,
    input: [{ type: "message", role: "user", content: "SYNTHETIC_REQUEST_SECRET" }] }));
  client = http.request({ host: "127.0.0.1", port: address.port, method: "POST",
    path: `/${capability}/backend-api/codex/v1/responses`,
    headers: { "content-type": "application/json", "content-length": raw.length },
  }, (response) => {
    response.on("data", (chunk) => {
      clientChunks.push(chunk);
      if (Buffer.concat(clientChunks).length >= partial.length) prefixReceived.resolve();
      if (Buffer.concat(clientChunks).length >= wire.length) chunkReceived.resolve();
    });
    response.on("error", () => {});
    response.on("end", () => ended.resolve({ status: response.statusCode,
      body: Buffer.concat(clientChunks) }));
  });
  client.on("error", () => {});
  client.end(raw);
  await bounded(received.promise, "LOOPBACK_UPSTREAM_NOT_REACHED");
  return { records, raw, client, received, chunkReceived, ended, logged, upstreamClosed,
    get clientBody() { return Buffer.concat(clientChunks); },
    get requestCount() { return requestCount; }, get downstream() { return downstream; },
    get owned() { return owned; }, get destroyCalls() { return destroyCalls; },
    get pauseObserved() { return pauseObserved; }, get resetEmitted() { return resetEmitted; } };
}

for (const scenario of [
  { name: "response.failed", terminal: failed, splitPrefix: true },
  { name: "error", terminal: topError, fragmented: false },
  { name: "fragmented response.failed", terminal: failed, fragmented: true },
  { name: "backpressured response.failed", terminal: failed, backpressure: true },
  { name: "response.failed with queued reset", terminal: failed, lateReset: true },
]) {
  test(`held-open ${scenario.name} autonomously ends with original bytes and partial output`,
    { timeout: 1500 }, async (t) => {
      const wire = Buffer.concat([partial, scenario.terminal]);
      const state = await fixture(t, { ...scenario, wire });
      const response = await bounded(state.ended.promise,
        "HELD_OPEN_FAILURE_DID_NOT_END_WITHOUT_CLIENT_CANCELLATION");
      assert.equal(response.status, 200);
      assert.deepEqual(response.body, wire);
      assert.equal(state.client.aborted, false, "Client must not cancel before proxy-owned EOF.");
      assert.equal(state.downstream.writableEnded, true);
      assert.equal(response.body.includes(Buffer.from("response.completed")), false);
      const record = await bounded(state.logged.promise, "TERMINAL_FAILURE_DIAGNOSTIC_MISSING");
      await bounded(state.upstreamClosed.promise, "OWNED_UPSTREAM_NOT_CANCELLED");
      const diagnostic = record.diagnostic.official_http;
      assert.equal(diagnostic.terminal_cause, "upstream_terminal_failure");
      assert.equal(diagnostic.terminal_parsed, true);
      assert.equal(diagnostic.sse_terminal_forwarded, scenario.name === "error" ? "error" : "response.failed");
      assert.equal(diagnostic.terminal_write_succeeded, true);
      assert.equal(diagnostic.upstream_eof_observed, false);
      assert.equal(diagnostic.upstream_error.code, "server_error");
      assert.equal(diagnostic.upstream_error.message_sha256, sha(Buffer.from(message)));
      assert.equal(diagnostic.upstream_error.message_bytes, Buffer.byteLength(message));
      assert.equal(diagnostic.request_raw_sha256, sha(state.raw));
      assert.equal(diagnostic.request_outgoing_sha256, sha(state.raw));
      assert.equal(state.requestCount, 1);
      assert.equal(state.records.length, 1);
      assert.ok(state.destroyCalls > 0);
      if (scenario.backpressure) assert.equal(state.pauseObserved, true);
      if (scenario.lateReset) assert.equal(state.resetEmitted, true);
      const logs = JSON.stringify(state.records);
      for (const secret of [message, "SYNTHETIC_REQUEST_SECRET", "LATE_RESET_SECRET"])
        assert.equal(logs.includes(secret), false);
    });
}

test("response.failed followed by upstream EOF preserves exactly one failure and partial output",
  { timeout: 1500 }, async (t) => {
    const wire = Buffer.concat([partial, failed]);
    const state = await fixture(t, { wire, eof: true });
    const response = await bounded(state.ended.promise, "UPSTREAM_EOF_FAILURE_DID_NOT_END");
    assert.deepEqual(response.body, wire);
    assert.equal(response.body.includes(Buffer.from("response.completed")), false);
    await bounded(state.logged.promise, "EOF_FAILURE_DIAGNOSTIC_MISSING");
    assert.equal(state.records.length, 1);
  });

for (const terminal of [failed, topError]) {
  test(`${terminal === failed ? "response.failed" : "error"} EOF before delayed local write keeps the failure`,
    { timeout: 1500 }, async (t) => {
      const wire = Buffer.concat([partial, terminal]);
      const state = await fixture(t, { wire, eof: true, delayedWriteCallback: true });
      const response = await bounded(state.ended.promise, "EOF_PREEMPTED_PENDING_TERMINAL_WRITE");
      assert.deepEqual(response.body, wire);
      const record = await bounded(state.logged.promise, "DELAYED_EOF_DIAGNOSTIC_MISSING");
      assert.equal(record.diagnostic.official_http.terminal_cause, "upstream_terminal_failure");
      assert.equal(record.diagnostic.official_http.upstream_eof_observed, true);
      assert.equal(record.diagnostic.official_http.upstream_transport_while_terminal_pending, "response_close");
      assert.equal(record.diagnostic.official_http.terminal_write_succeeded, true);
      assert.equal(state.records.length, 1);
      assert.equal(state.requestCount, 1);
    });
}

for (const terminal of [failed, topError]) for (const backpressure of [false, true]) {
  test(`${terminal === failed ? "response.failed" : "error"} upstream reset before delayed write${backpressure ? " with backpressure" : ""} keeps original failure`,
    { timeout: 1500 }, async (t) => {
      const wire = Buffer.concat([partial, terminal]);
      const state = await fixture(t, { wire, delayedWriteCallback: true,
        upstreamResetBeforeCallback: true, backpressure });
      const response = await bounded(state.ended.promise, "UPSTREAM_RESET_PREEMPTED_TERMINAL_WRITE");
      assert.deepEqual(response.body, wire);
      const record = await bounded(state.logged.promise, "DELAYED_RESET_DIAGNOSTIC_MISSING");
      assert.equal(record.diagnostic.official_http.terminal_cause, "upstream_terminal_failure");
      assert.equal(record.diagnostic.official_http.terminal_write_succeeded, true);
      assert.equal(record.diagnostic.official_http.upstream_eof_observed, false);
      assert.equal(record.diagnostic.official_http.upstream_transport_while_terminal_pending, "response_error");
      assert.equal(state.records.length, 1);
      assert.equal(state.requestCount, 1);
    });
}

for (const eof of [false, true]) {
  test(`failed delayed local write after upstream ${eof ? "EOF" : "reset"} is not delivery`,
    { timeout: 1500 }, async (t) => {
      const state = await fixture(t, { wire: failed, eof, delayedWriteCallback: true,
        rejectTerminalWrite: true, upstreamResetBeforeCallback: !eof });
      const record = await bounded(state.logged.promise, "PENDING_WRITE_FAILURE_DID_NOT_SETTLE");
      assert.equal(record.diagnostic.official_http.terminal_cause, "downstream_error");
      assert.equal(record.diagnostic.official_http.terminal_parsed, true);
      assert.equal(record.diagnostic.official_http.terminal_write_succeeded, false);
      assert.equal(record.diagnostic.official_http.sse_terminal_forwarded, null);
      assert.equal(state.records.length, 1);
      assert.equal(state.requestCount, 1);
    });

  test(`client cancellation while terminal write awaits callback after upstream ${eof ? "EOF" : "reset"} is not delivery`,
    { timeout: 1500 }, async (t) => {
      const state = await fixture(t, { wire: failed, eof, delayedWriteCallback: true,
        upstreamResetBeforeCallback: !eof });
      await bounded(state.chunkReceived.promise, "CANCELLATION_TERMINAL_NOT_RECEIVED");
      state.client.destroy();
      const record = await bounded(state.logged.promise, "PENDING_CANCELLATION_DID_NOT_SETTLE");
      assert.equal(record.diagnostic.official_http.terminal_cause, "downstream_close");
      assert.equal(record.diagnostic.official_http.terminal_write_succeeded, false);
      assert.equal(record.diagnostic.official_http.sse_terminal_forwarded, null);
      assert.equal(state.records.length, 1);
      assert.equal(state.requestCount, 1);
    });
}

for (const type of ["response.completed", "response.incomplete"]) {
  test(`${type} still waits for upstream EOF or client cancellation`, { timeout: 1500 }, async (t) => {
    const wire = frame({ type, response: { id: "resp_nonfailure", status: type.slice(9) } });
    const state = await fixture(t, { wire });
    await bounded(state.chunkReceived.promise, "NONFAILURE_TERMINAL_NOT_FORWARDED");
    await new Promise((done) => setTimeout(done, 30));
    assert.equal(state.downstream.writableEnded, false);
    assert.deepEqual(state.clientBody, wire);
    state.client.destroy();
    const record = await bounded(state.logged.promise, "CLIENT_CANCELLATION_NOT_SETTLED");
    assert.equal(record.diagnostic.official_http.terminal_cause,
      type === "response.completed" ? "client_closed_after_completed" : "client_closed_after_terminal");
  });
}

for (const scenario of [
  { name: "compressed failure remains opaque", wire: gzipSync(failed), encoding: "gzip" },
  { name: "malformed frame remains byte preserving", wire: Buffer.from("data: {not-json}\n\n") },
  { name: "unterminated failure is not a delivered terminal", wire: failed.subarray(0, failed.length - 2) },
  { name: "failed local write is not terminal delivery", wire: failed, rejectTerminalWrite: true },
]) {
  test(scenario.name, { timeout: 1500 }, async (t) => {
    const state = await fixture(t, scenario);
    await bounded(state.chunkReceived.promise, "OPAQUE_OR_INCOMPLETE_BYTES_NOT_FORWARDED");
    await new Promise((done) => setTimeout(done, 30));
    assert.equal(state.downstream.writableEnded, false);
    assert.deepEqual(state.clientBody, scenario.wire);
    state.client.destroy();
    const record = await bounded(state.logged.promise, "OPAQUE_CANCELLATION_NOT_SETTLED");
    assert.equal(record.diagnostic.official_http.terminal_cause,
      scenario.rejectTerminalWrite ? "downstream_error" : "downstream_close");
    assert.equal(record.diagnostic.official_http.sse_terminal_forwarded, null);
    if (scenario.rejectTerminalWrite)
      assert.equal(record.diagnostic.official_http.terminal_write_succeeded, false);
  });
}
