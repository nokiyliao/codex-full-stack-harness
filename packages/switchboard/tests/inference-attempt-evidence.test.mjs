import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import http from "node:http";
import test from "node:test";
import { gzipSync } from "node:zlib";
import WebSocket, { WebSocketServer } from "ws";
import { createInferenceAttemptEvidence, summarizeRequestProvenance } from "../src/inference-attempt-evidence.mjs";
import { createSwitchboardServer } from "../src/server.mjs";

const sha = (value) => createHash("sha256").update(value).digest("hex");
test("attempt evidence retains cache-write counts without inventing missing values", () => {
  for (const written of [0, 50, undefined]) {
    const records = [];
    const run = createInferenceAttemptEvidence({ emit: (event) => records.push(event) })
      .start({ requestId: "cache-write-test", transport: "http" });
    run.dispatch("fixture");
    run.finish({ outcome: "success", observation: { input_tokens: 100, output_tokens: 10,
      cache_write_tokens: written, cache_write_30m_tokens: written } });
    const usage = records.find((event) => event.event_type === "attempt_terminal").usage;
    assert.equal(usage.cache_write_tokens, written ?? null);
    assert.equal(usage.cache_write_30m_tokens, written ?? null);
    assert.equal(usage.cache_write_12h_tokens, null);
  }
});

test("provider delta metrics are content-free, UTF-8 aware and reset per attempt", () => {
  const records = [];
  let clock = 100;
  const run = createInferenceAttemptEvidence({ now: () => clock, emit: (e) => records.push(e) })
    .start({ requestId: "delta-test", transport: "http" });
  const delta = { type: "response.function_call_arguments.delta", delta: "私密" };
  run.observeOutput(delta);
  run.dispatch("fixture");
  clock = 125;
  run.observeOutput({ type: "response.output_item.added", delta: "ignored" });
  run.observeOutput({ type: "response.output_text.delta", delta: "" });
  clock = 140;
  run.observeOutput(delta);
  run.observeOutput({ type: "response.output_text.delta", delta: "abc" });
  run.endAttempt({ outcome: "failure" });
  run.observeOutput(delta);
  run.dispatch("fixture");
  run.finish({ outcome: "success" });
  run.observeOutput(delta);
  const terminals = records.filter((e) => e.event_type === "attempt_terminal");
  assert.deepEqual(terminals[0].provider_output_deltas, {
    scope: "pre_projection_observed_deltas_not_token_usage",
    first_content_delta_ms: 40,
    categories: { function_arguments: { utf8_bytes: 6, events: 1 }, text: { utf8_bytes: 3, events: 1 } },
  });
  assert.equal(terminals[1].provider_output_deltas.categories, null);
  assert.equal(terminals[1].provider_output_deltas.first_content_delta_ms, null);
  assert.equal(JSON.stringify(records).includes("私密"), false);
});
const summary = (body) => summarizeRequestProvenance({ wire: JSON.stringify(body) });
const message = (role, text) => ({ type: "message", role, content: [{ type: "input_text", text }] });

test("inbound and outgoing App catalogs remain distinguishable without content", () => {
  const records = [];
  const body = { tools: [{ type: "function", function: { name: "advertised", description: "envelope-secret",
    parameters: { type: "object", properties: { token: { default: "credential-secret" } } } } }],
    input: [{ type: "additional_tools", tools: [{ type: "function", name: "read_thread", namespace: "codex_app",
      defer_loading: true, parameters: { type: "object", description: "schema-secret" } },
      { type: "tool_search", execution: "client" }] },
    { type: "tool_search_output", tools: [{ type: "function", name: "discovered", defer_loading: false }] },
    message("user", "message-secret"), { type: "function_call", name: "read_thread", arguments: "argument-secret" }],
    headers: { authorization: "credential-secret" }, url: "https://url-secret.invalid" };
  const wire = JSON.stringify(body);
  const producer = createInferenceAttemptEvidence({ processInstanceId: "binding-process", emit: (e) => records.push(e) });
  const run = producer.start({ requestId: "binding", transport: "http", request: { wire } });
  const absent = producer.start({ requestId: "absent", transport: "ws", request: { wire: "{}" } });
  absent.dispatch("fixture", { wire: "{}" });
  run.dispatch("fixture", { wire: '{"tools":[]}' });
  const paired = records.filter((e) => e.request_id === "binding");
  assert.deepEqual(paired.map((e) => e.event_type), ["request_start", "attempt_dispatch"]);
  assert.equal(paired[0].process_instance_id, paired[1].process_instance_id);
  assert.equal(paired[1].attempt_id, "binding-process:binding:1");
  const entries = paired[0].request_provenance.tools.entries;
  assert.deepEqual(entries.map(({ name, namespace, execution, defer_loading, parameters_kind }) =>
    [name, namespace, execution, defer_loading, parameters_kind]), [
    [null, null, null, null, "absent"], ["read_thread", "codex_app", null, true, "object"],
    [null, null, "client", null, "absent"], [`sha256:${sha("discovered")}`, null, null, false, "absent"],
  ]);
  assert.equal(entries[0].function_envelope, true);
  assert.equal(entries[0].function_name, `sha256:${sha("advertised")}`);
  assert.equal(entries[1].parameters_type, "object");
  assert.deepEqual(entries.map((e) => e.declaration), ["tools", "additional_tools", "additional_tools", "tool_search_output"]);
  assert.equal(paired[1].request_provenance.tools.status, "OBSERVED");
  assert.equal(paired[1].request_provenance.tools.count, 0);
  for (const record of records.filter((e) => e.request_id === "absent")) {
    assert.equal(record.request_provenance.tools.status, "UNKNOWN");
    assert.equal(record.request_provenance.tools.count, 0);
    assert.deepEqual(record.request_provenance.tools.entries, []);
  }
  assert.equal(JSON.stringify(body), wire);
  run.finish({ outcome: "success" });
  absent.finish({ outcome: "success" });
  assert.doesNotMatch(JSON.stringify(records), /schema-secret|message-secret|argument-secret|envelope-secret|credential-secret|url-secret/);
});

test("URL and token labels stay private with stable ingress and dispatch identities", () => {
  const results = [];
  for (const value of ["https://url-secret.invalid", "sk_live_private_token_0123456789abcdef"]) {
    const body = { previous_response_id: value,
      tools: [{ type: "namespace", name: value, tools: [
        { type: value, name: value, namespace: value, function: { name: value } },
      ] }], tool_choice: { type: value, name: value } };
    const wire = JSON.stringify(body), records = [];
    const request = { wire, projection: { nativeTool: {
      target: { type: value, name: value, namespace: value },
    } } };
    const run = createInferenceAttemptEvidence({ emit: (event) => records.push(event) })
      .start({ requestId: "private-labels", transport: "http", request });
    run.dispatch("fixture", request);
    const result = records[0].request_provenance, digest = `sha256:${sha(value)}`;
    results.push(result);
    assert.deepEqual(records[1].request_provenance, result);
    assert.equal(result.tools.entries[0].name, digest);
    const entry = result.tools.entries[1];
    assert.deepEqual([entry.type, entry.name, entry.namespace, entry.function_name], Array(4).fill(digest));
    assert.deepEqual(entry.namespaces, [digest]);
    assert.equal(result.previous_response_id, digest);
    assert.deepEqual([result.tool_choice.type, result.tool_choice.name], [digest, digest]);
    assert.deepEqual([result.wrapping.type, result.wrapping.name, result.wrapping.namespace], Array(3).fill(digest));
    const declarations = JSON.stringify([{ source: "/tools", kind: "tools", tools: body.tools }]);
    assert.equal(result.tools.count, 2);
    assert.equal(result.tools.group_count, 1);
    assert.equal(result.tools.sha256, sha(declarations));
    assert.equal(result.tools.bytes, Buffer.byteLength(declarations));
    assert.equal(result.wire.sha256, sha(wire));
    assert.equal(result.tools.truncated, false);
    assert.equal(result.truncated, false);
    assert.equal(JSON.stringify(body), wire);
    run.finish({ outcome: "success" });
    assert.equal(JSON.stringify(records).includes(value), false);
  }
  assert.notEqual(results[0].tools.entries[1].namespace, results[1].tools.entries[1].namespace);
  assert.notEqual(results[0].tools.entries[1].function_name, results[1].tools.entries[1].function_name);
  const empty = summary({ previous_response_id: "", tools: [{ type: "", name: "", namespace: "",
    function: { name: "" } }], tool_choice: { type: "", name: "" } });
  const entry = empty.tools.entries[0];
  assert.deepEqual([entry.type, entry.name, entry.namespace, entry.function_name, empty.previous_response_id,
    empty.tool_choice.type, empty.tool_choice.name], Array(7).fill(null));
});

test("known App identifiers remain readable without trusting arbitrary names in their namespace", () => {
  const secret = "read_thread_sk_live_private_token_0123456789abcdef";
  const result = summary({ tools: [{ type: "namespace", name: "mcp__codex_app", tools: [
    { type: "function", name: "read_thread", namespace: "mcp__codex_app", function: { name: "read_thread" } },
    { type: "function", name: secret, namespace: "mcp__codex_app", function: { name: secret } },
  ] }], tool_choice: { type: "function", name: "read_thread" } });
  const [parent, known, unknown] = result.tools.entries;
  assert.equal(parent.name, "mcp__codex_app");
  assert.deepEqual([known.type, known.name, known.namespace, known.function_name],
    ["function", "read_thread", "mcp__codex_app", "read_thread"]);
  assert.deepEqual(known.namespaces, ["mcp__codex_app"]);
  assert.equal(unknown.name, `sha256:${sha(secret)}`);
  assert.equal(unknown.function_name, `sha256:${sha(secret)}`);
  assert.equal(result.tool_choice.type, "function");
  assert.equal(result.tool_choice.name, "read_thread");
  assert.equal(JSON.stringify(result).includes(secret), false);
});

test("malformed tool fields expose only bounded allowlisted shape metadata", () => {
  const privateObject = { description: "nested-secret", arguments: "argument-secret" };
  const result = summary({ tools: [null, 7, privateObject, { type: "function", name: "x".repeat(201),
    namespace: privateObject, execution: "execution-secret", defer_loading: privateObject,
    parameters: { type: "schema-type-secret", ...privateObject }, function: { name: privateObject } }],
    input: [{ type: "additional_tools", tools: privateObject }, { type: "tool_search_output", tools: null }] });
  assert.equal(result.tools.count, 4);
  assert.deepEqual(result.tools.entries.at(-1), { source: "/tools/3", declaration: "tools", type: "function",
    name: null, namespaces: [], namespace: null, execution: null, defer_loading: null,
    parameters_kind: "object", parameters_type: null, function_envelope: true, function_name: null });
  assert.ok(Buffer.byteLength(JSON.stringify(result)) < 64 * 1024);
  assert.doesNotMatch(JSON.stringify(result), /nested-secret|argument-secret|execution-secret|schema-type-secret/);
  const records = [];
  const run = createInferenceAttemptEvidence({ emit: (e) => records.push(e) })
    .start({ requestId: "malformed", request: { wire: "invalid-private-json" } });
  assert.equal(records[0].request_provenance.status, "UNKNOWN");
  assert.ok(run.dispatch("fixture", { wire: "null" }));
  assert.equal(records[1].request_provenance.status, "UNKNOWN");
  run.finish({ outcome: "success" });
  assert.doesNotMatch(JSON.stringify(records), /invalid-private-json/);
});

test("actual declaration sources, namespaces and full schema hashes survive without descriptions", () => {
  const body = { tools: [{ type: "namespace", name: "functions", tools: [
    { type: "function", name: "exec_command", description: "declaration-secret", parameters: { type: "object" } },
  ] }], input: [{ type: "additional_tools", role: "developer", tools: [{ type: "custom", name: "exec" }] },
    { type: "tool_search_output", tools: [{ type: "function", name: "lookup" }] }] };
  const result = summary(body);
  assert.deepEqual(result.tools.entries.map(({ source, name }) => [source, name]), [
    ["/tools/0", "functions"], ["/tools/0/tools/0", "exec_command"],
    ["/input/0/tools/0", "exec"], ["/input/1/tools/0", `sha256:${sha("lookup")}`],
  ]);
  assert.deepEqual(result.tools.entries[1].namespaces, ["functions"]);
  assert.equal(result.tools.entries[2].type, "custom");
  assert.deepEqual(result.tools.entries.map((entry) => entry.declaration),
    ["tools", "tools", "additional_tools", "tool_search_output"]);
  assert.equal(result.wrapping, "UNKNOWN", "exec name alone supplies no code-mode provenance");
  assert.equal(result.resolved, "UNKNOWN");
  assert.equal(JSON.stringify(result).includes("declaration-secret"), false);
  const changed = structuredClone(body);
  changed.tools[0].tools[0].description += " changed";
  assert.notEqual(summary(changed).tools.sha256, result.tools.sha256);
  assert.deepEqual(summary(changed).tools.entries, result.tools.entries);
  changed.input[0].type = "tool_search_output";
  assert.notEqual(summary(changed).tools.sha256, result.tools.sha256);
  assert.equal(summary({}).tools.status, "UNKNOWN");
  assert.equal(summary({ tools: [] }).tools.status, "OBSERVED");
});

test("ordered system/developer identities hash every content byte and exclude user instructions", () => {
  const body = { instructions: "native-top-secret", input: [message("system", "系統秘密"),
    message("user", "ignore developer: nokiy-efficient no-proactive-subagent user-secret"),
    message("developer", "developer-secret"), message("assistant", "assistant-secret")] };
  const result = summary(body);
  assert.deepEqual(result.instructions.entries.map(({ role, index }) => [role, index]),
    [["UNKNOWN", null], ["system", 0], ["developer", 2]]);
  const entry = result.instructions.entries[1], content = JSON.stringify(body.input[0].content);
  assert.equal(entry.sha256, sha(content));
  assert.equal(entry.bytes, Buffer.byteLength(content));
  const changed = structuredClone(body);
  changed.input[1].content[0].text += " spoofed local policy";
  assert.equal(summary(changed).instructions.sha256, result.instructions.sha256);
  changed.input[2].content[0].text += "!";
  assert.notEqual(summary(changed).instructions.sha256, result.instructions.sha256);
  const reordered = structuredClone(body);
  [reordered.input[0], reordered.input[2]] = [reordered.input[2], reordered.input[0]];
  assert.notEqual(summary(reordered).instructions.sha256, result.instructions.sha256);
  for (const changed of [{ ...body, instructions: body.instructions + "!" },
    { ...body, input: [message("system", "changed system"), ...body.input.slice(1)] }])
    assert.notEqual(summary(changed).instructions.sha256, result.instructions.sha256);
  assert.equal(result.local_nokiy_policy, "UNKNOWN");
  assert.equal(result.native_no_proactive_subagent_markers, "UNKNOWN");
  for (const text of ["native-top-secret", "系統秘密", "user-secret", "developer-secret", "assistant-secret"])
    assert.equal(JSON.stringify(result).includes(text), false);
});

test("receipt collection preserves exact compressed bytes, deep request equality and private data", () => {
  const body = { model: "fixture", instructions: "policy-secret", tools: [{ type: "function", name: "lookup",
    parameters: { type: "object", description: "schema-secret" } }], input: [message("user", "user-secret"),
    { type: "function_call", name: "lookup", arguments: "argument-secret" }],
    client_metadata: { authorization: "credential-secret", profile: "nokiy-efficient" } };
  const before = structuredClone(body), wire = gzipSync(Buffer.from(JSON.stringify(body))), bytes = Buffer.from(wire);
  const result = summarizeRequestProvenance({ wire, encoding: "gzip" });
  assert.deepEqual(body, before);
  assert.deepEqual(wire, bytes);
  assert.equal(result.wire.sha256, sha(bytes));
  assert.equal(result.wire.bytes, bytes.length);
  assert.equal(result.resolved, "UNKNOWN");
  for (const text of ["policy-secret", "schema-secret", "user-secret", "argument-secret", "credential-secret"])
    assert.equal(JSON.stringify(result).includes(text), false);
});

test("old callers, retries, prewarm and invalid diagnostics retain lifecycle identity and fail open", () => {
  const records = [];
  const producer = createInferenceAttemptEvidence({ processInstanceId: "fixture-process", emit: (e) => records.push(e) });
  const legacy = producer.start({ requestId: "legacy", modelRequested: "fixture", transport: "ws" });
  assert.equal(Object.hasOwn(records.at(-1), "request_provenance"), false);
  const one = legacy.dispatch("fixture");
  assert.equal(records.at(-1).request_provenance.status, "UNKNOWN");
  legacy.endAttempt({ outcome: "failure" });
  const two = legacy.dispatch("fixture", { wire: "invalid-secret-json" });
  assert.equal(records.at(-1).request_provenance.status, "UNKNOWN");
  assert.equal(one, "fixture-process:legacy:1");
  assert.equal(two, "fixture-process:legacy:2");
  legacy.finish({ outcome: "success" });
  assert.equal(records.at(-1).attempt_count, 2);
  const prewarm = producer.start({ requestId: "prewarm", modelRequested: "fixture", transport: "ws" });
  prewarm.finish({ outcome: "success", syntheticRequest: { wire: '{"generate":false,"tools":[]}' } });
  assert.equal(records.at(-1).attempt_count, 0);
  assert.equal(records.at(-1).attempt_id, null);
  assert.equal(records.at(-1).upstream_dispatch_started, false);
  assert.equal(records.at(-1).request_provenance.request_kind, "SYNTHETIC_PREWARM");
  const real = producer.start({ requestId: "real", modelRequested: "fixture", transport: "http" });
  real.dispatch("fixture", { wire: '{"generate":false,"tools":[]}' });
  assert.equal(records.at(-1).request_provenance.request_kind, "PREWARM");
  assert.equal(records.at(-1).request_id, "real");
  real.finish({ outcome: "success" });
  const checkpoint = producer.checkpoint();
  assert.equal(checkpoint.active_requests, 0);
  assert.equal(checkpoint.emission_error_count, 0);
  assert.equal(JSON.stringify(records).includes("invalid-secret-json"), false);
  for (const request of [undefined, null, {}, { wire: "{" }, { wire: "null" }])
    assert.equal(summarizeRequestProvenance(request).status, "UNKNOWN");
});

test("large metadata is bounded with full counts/hashes and never refuses dispatch", () => {
  const body = { tools: Array.from({ length: 1800 }, (_, i) => ({ type: "function", name: `tool_${i}` })),
    input: Array.from({ length: 500 }, (_, i) => message("developer", `private instruction ${i}`)) };
  const first = summary(body);
  assert.equal(first.truncated, true);
  assert.equal(first.tools.truncated, true);
  assert.equal(first.instructions.truncated, true);
  assert.equal(first.tools.count, 1800);
  assert.equal(first.instructions.count, 500);
  assert.ok(first.full_metadata.bytes > 64 * 1024);
  assert.ok(Buffer.byteLength(JSON.stringify(first)) < 64 * 1024);
  assert.match(first.full_metadata.sha256, /^[a-f0-9]{64}$/);
  body.tools.at(-1).parameters = { description: "last-schema-secret" };
  body.input.at(-1).content[0].text += " changed";
  const last = summary(body);
  assert.notEqual(last.tools.sha256, first.tools.sha256);
  assert.notEqual(last.instructions.sha256, first.instructions.sha256);
  assert.notEqual(last.full_metadata.sha256, first.full_metadata.sha256);
  const records = [];
  const attempt = createInferenceAttemptEvidence({ emit: (e) => records.push(e) })
    .start({ requestId: "large", request: { wire: JSON.stringify(body) } });
  assert.ok(attempt.dispatch("fixture", { wire: JSON.stringify(body) }));
  assert.equal(records.at(-1).upstream_dispatch_started, true);
  for (const record of records) {
    assert.equal(record.request_id, "large");
    assert.equal(record.request_provenance.tools.count, 1800);
    assert.equal(record.request_provenance.truncated, true);
    assert.equal(record.request_provenance.tools.truncated, true);
    assert.ok(Buffer.byteLength(JSON.stringify(record.request_provenance)) < 64 * 1024);
    assert.doesNotMatch(JSON.stringify(record), /last-schema-secret|private instruction/);
  }
  attempt.finish({ outcome: "success" });
});

test("HTTP receipt is derived from the exact final compressed mock-upstream payload", async (t) => {
  const receipts = [], write = process.stdout.write;
  t.mock.method(process.stdout, "write", function (chunk, ...args) {
    if (typeof chunk === "string" && chunk.startsWith("{")) {
      const record = JSON.parse(chunk);
      if (record.attempt_lifecycle) receipts.push(record.attempt_lifecycle);
      return true;
    }
    return write.call(this, chunk, ...args);
  });
  let received;
  const official = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    received = Buffer.concat(chunks);
    assert.equal(req.headers.authorization, "Bearer http-credential-secret");
    res.writeHead(200, { "content-type": "text/event-stream" });
    res.write(`data: ${JSON.stringify({ type: "response.output_text.delta", delta: "private-output" })}\n\n`);
    res.end(`data: ${JSON.stringify({ type: "response.completed", response: { id: "resp_fixture", status: "completed", output: [] } })}\n\n`);
  });
  await new Promise((resolve) => official.listen(0, "127.0.0.1", resolve));
  const switchboard = await createSwitchboardServer({
    runtime: { installation_capability: "test-capability-abcdefghijklmnopqrstuvwxyz", host: "127.0.0.1", port: 0 },
    admission: { status: "ADMITTED", official_models: ["gpt-6.1-sol"] }, skipFileGate: true,
    officialOrigin: `http://127.0.0.1:${official.address().port}`,
  });
  t.after(async () => { await switchboard.close(); official.closeAllConnections();
    await new Promise((resolve) => official.close(resolve)); });
  const address = await switchboard.listen({ host: "127.0.0.1", port: 0 });
  const body = { model: "gpt-6.1-sol", tools: [{ type: "function", name: "lookup", description: "http-schema-secret" }],
    input: [message("system", "http-policy-secret"), message("user", "http-user-secret")] };
  const wire = gzipSync(Buffer.from(JSON.stringify(body)));
  await new Promise((resolve, reject) => {
    const request = http.request({ host: "127.0.0.1", port: address.port, method: "POST",
      path: "/test-capability-abcdefghijklmnopqrstuvwxyz/backend-api/codex/v1/responses",
      headers: { authorization: "Bearer http-credential-secret", "content-type": "application/json",
        "content-encoding": "gzip", "content-length": wire.length } }, (res) => {
      res.resume(); res.once("end", resolve);
    });
    request.once("error", reject); request.end(wire);
  });
  assert.deepEqual(received, wire);
  const dispatch = receipts.find((e) => e.event_type === "attempt_dispatch");
  const start = receipts.find((e) => e.event_type === "request_start");
  assert.equal(start.request_id, dispatch.request_id);
  assert.equal(start.transport, "http");
  assert.equal(start.request_provenance.wire.sha256, sha(wire));
  assert.equal(start.request_provenance.tools.entries[0].name, `sha256:${sha("lookup")}`);
  assert.equal(dispatch.request_provenance.wire.sha256, sha(received));
  assert.equal(dispatch.request_provenance.tools.entries[0].name, `sha256:${sha("lookup")}`);
  assert.equal(dispatch.request_provenance.instructions.entries[0].role, "system");
  assert.equal(receipts.find((e) => e.event_type === "attempt_terminal").attempt_id, dispatch.attempt_id);
  assert.deepEqual(receipts.find((e) => e.event_type === "attempt_terminal").provider_output_deltas.categories,
    { text: { utf8_bytes: 14, events: 1 } });
  assert.equal(JSON.stringify(receipts).includes("private-output"), false);
  for (const secret of ["http-schema-secret", "http-policy-secret", "http-user-secret", "http-credential-secret"])
    assert.equal(JSON.stringify(receipts).includes(secret), false);
});

test("WS ingress and dispatch catalogs share the actual turn ID without inheriting absent tools", async (t) => {
  const receipts = [], received = [], write = process.stdout.write;
  t.mock.method(process.stdout, "write", function (chunk, ...args) {
    if (typeof chunk === "string" && chunk.startsWith("{")) {
      const record = JSON.parse(chunk);
      if (record.attempt_lifecycle) receipts.push(record.attempt_lifecycle);
      return true;
    }
    return write.call(this, chunk, ...args);
  });
  const official = http.createServer();
  const wss = new WebSocketServer({ noServer: true });
  official.on("upgrade", (req, socket, head) => {
    wss.handleUpgrade(req, socket, head, (ws) => wss.emit("connection", ws));
  });
  wss.on("connection", (ws) => ws.on("message", (data) => {
    received.push(data.toString());
    const id = `resp_binding_${received.length}`;
    ws.send(JSON.stringify({ type: "response.created", response: { id } }));
    ws.send(JSON.stringify({ type: "response.function_call_arguments.delta", delta: "private-arguments" }));
    ws.send(JSON.stringify({ type: "response.completed", response: { id, status: "completed", output: [] } }));
  }));
  await new Promise((resolve) => official.listen(0, "127.0.0.1", resolve));
  const switchboard = await createSwitchboardServer({
    runtime: { installation_capability: "test-capability-abcdefghijklmnopqrstuvwxyz", host: "127.0.0.1", port: 0 },
    admission: { status: "ADMITTED", official_models: ["gpt-6.1-sol"] }, skipFileGate: true,
    officialOrigin: `http://127.0.0.1:${official.address().port}`,
  });
  let ws;
  t.after(async () => {
    ws?.terminate();
    await switchboard.close();
    for (const client of wss.clients) client.terminate();
    await new Promise((resolve) => wss.close(resolve));
    official.closeAllConnections();
    await new Promise((resolve) => official.close(resolve));
  });
  const address = await switchboard.listen({ host: "127.0.0.1", port: 0 });
  ws = new WebSocket(`ws://127.0.0.1:${address.port}/test-capability-abcdefghijklmnopqrstuvwxyz/backend-api/codex/v1/responses`,
    { headers: { authorization: "Bearer ws-credential-secret" } });
  await new Promise((resolve, reject) => { ws.once("open", resolve); ws.once("error", reject); });
  const bodies = [
    { type: "response.create", model: "gpt-6.1-sol", tools: [{ type: "tool_search", execution: "client" }],
      input: [{ type: "additional_tools", tools: [{ type: "function", name: "read_thread", namespace: "codex_app",
        defer_loading: true, parameters: { type: "object", description: "ws-schema-secret" } }] },
      message("user", "ws-message-secret")] },
    { type: "response.create", model: "gpt-6.1-sol", input: [message("user", "ws-second-secret")] },
  ];
  for (const body of bodies) {
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => { ws.off("message", onMessage); reject(new Error("WS evidence turn timed out")); }, 5_000);
      const onMessage = (data) => {
        const event = JSON.parse(data.toString());
        if (["response.completed", "response.failed", "error"].includes(event.type)) {
          clearTimeout(timer); ws.off("message", onMessage);
          if (event.type === "response.completed") resolve();
          else reject(new Error("WS evidence fixture failed"));
        }
      };
      ws.on("message", onMessage);
      ws.send(JSON.stringify(body));
    });
  }
  const starts = receipts.filter((e) => e.event_type === "request_start");
  assert.equal(starts.length, 2);
  assert.notEqual(starts[0].request_id, starts[1].request_id);
  assert.equal(receipts.filter((e) => e.event_type === "attempt_dispatch").length, 2);
  for (const [index, start] of starts.entries()) {
    const dispatch = receipts.find((e) => e.event_type === "attempt_dispatch" && e.request_id === start.request_id);
    assert.equal(start.transport, "ws");
    assert.equal(dispatch.process_instance_id, start.process_instance_id);
    assert.equal(start.request_provenance.wire.sha256, sha(JSON.stringify(bodies[index])));
    assert.equal(dispatch.request_provenance.wire.sha256, sha(received[index]));
    assert.deepEqual(start.request_provenance.tools, dispatch.request_provenance.tools);
    const terminal = receipts.find((e) => e.event_type === "attempt_terminal" && e.request_id === start.request_id);
    assert.deepEqual(terminal.provider_output_deltas.categories,
      { function_arguments: { utf8_bytes: 17, events: 1 } });
    assert.ok(terminal.provider_output_deltas.first_content_delta_ms >= 0);
  }
  assert.equal(JSON.stringify(receipts).includes("private-arguments"), false);
  assert.equal(starts[0].request_provenance.tools.entries[1].namespace, "codex_app");
  assert.equal(starts[0].request_provenance.tools.entries[1].defer_loading, true);
  assert.equal(starts[1].request_provenance.tools.status, "UNKNOWN");
  assert.equal(starts[1].request_provenance.tools.count, 0);
  assert.deepEqual(starts[1].request_provenance.tools.entries, []);
  assert.doesNotMatch(JSON.stringify(receipts), /ws-schema-secret|ws-message-secret|ws-second-secret|ws-credential-secret/);
});
