import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import http from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import WebSocket, { WebSocketServer } from "ws";

// Read at module load: a short interval would expose any default keep-alive quickly.
process.env.SWITCHBOARD_CACHE_KEEPALIVE_MS = "30";
delete process.env.SWITCHBOARD_CACHE_KEEPALIVE_MAX;
const { EXTERNAL_EFFECT_SCOPE, EXTERNAL_ORCHESTRATION_PROTOCOL } = await import("../src/external-admission.mjs");
const { createSwitchboardServer } = await import("../src/server.mjs");

const CAPABILITY = "test-capability-abcdefghijklmnopqrstuvwxyz";
const PREFIX = "/" + CAPABILITY + "/backend-api/codex/v1";
const MODEL = "gpt-5.6-sol";
const user = (text) => ({ type: "message", role: "user", content: text });
const listen = (server) => new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server.address().port)));
const tools = [{ type: "function", name: "exec_command", parameters: { type: "object" } }];
const nokiyProfiles = ["nokiy", "nokiy-direct", "nokiy-efficient"].map((slug) => ({
  slug, display_name: slug, workflow: slug === "nokiy" ? "nokiy" : "nokiy-direct",
  coordinator: { model: slug === "nokiy-efficient" ? "gpt-6.1-sol" : "gpt-6-astra", reasoning_effort: "max" },
  worker: { model: "gpt-6.1-sol", reasoning_effort: "max", service_tier: "default" },
}));
const nativePrefix = { type: "additional_tools", role: "developer", tools: [
  { type: "namespace", name: "functions", tools: [
    { type: "custom", name: "exec", format: { type: "text" }, description: [
      "declare const tools: { exec_command(args: {cmd: string;}): Promise<unknown>; };",
      "declare const tools: { write_stdin(args: {session_id: number;chars?: string;}): Promise<unknown>; };",
      "declare const tools: { get_goal(args: {}): Promise<unknown>; };",
      "declare const tools: { update_goal(args: {status: string;}): Promise<unknown>; };",
    ].join("\n") },
    ...["wait", "request_user_input", "request_user_input_async"].map((name) => ({
      type: "function", name, parameters: { type: "object", properties: {} },
    })),
  ] },
] };
const liteInput = () => [structuredClone(nativePrefix),
  { type: "message", role: "developer", content: [{ type: "input_text", text: "fixture native base" }] }, user("task")];
const names = (items) => items.flatMap((item) => item.type === "namespace" ? names(item.tools) : [item.name]);

class IdleWorker {
  async runResponses() { throw new Error("external route not expected"); }
  async closeSession() {}
  close() {}
}

async function fixture(t, nokiy = false) {
  const receipts = [], wires = [], diagnostics = [];
  const write = process.stdout.write;
  t.mock.method(process.stdout, "write", function (chunk, ...args) {
    if (typeof chunk === "string" && chunk.startsWith("{")) {
      const record = JSON.parse(chunk);
      if (record.attempt_lifecycle) receipts.push(record.attempt_lifecycle);
      if (record.route === "nokiy-native-tool-missing") diagnostics.push(record.diagnostic);
    }
    return write.call(this, chunk, ...args);
  });
  const official = http.createServer(async (req, res) => {
    let raw = "";
    for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw);
    requests.push(body);
    res.writeHead(200, { "content-type": "text/event-stream" });
    res.end("data: " + JSON.stringify({ type: "response.completed", response: {
      id: "resp_http_" + requests.length, output: [{ type: "message", role: "assistant",
        content: [{ type: "output_text", text: "done" }] }],
      usage: { input_tokens: 100, output_tokens: 1 },
    } }) + "\n\n");
  });
  const officialWss = new WebSocketServer({ server: official });
  const requests = [];
  officialWss.on("connection", (ws) => {
    ws.on("message", (raw) => {
      wires.push(Buffer.from(raw));
      const body = JSON.parse(raw.toString());
      requests.push(body);
      const id = "resp_official_" + requests.length;
      const last = Array.isArray(body.input) ? body.input.at(-1) : null;
      const output = body.generate === false ? []
        : last?.content === "read without exec"
          ? [{ type: "function_call", call_id: "read_1", name: "read_file", arguments: "{}" }]
        : last?.content === "call tool" && body.max_output_tokens === undefined
          ? [{ type: "function_call", call_id: "call_1", name: "exec_command", arguments: "{}" }]
          : [{ type: "message", role: "assistant", content: [{ type: "output_text", text: "done" }] }];
      for (const item of output) ws.send(JSON.stringify({ type: "response.output_item.done", item }));
      ws.send(JSON.stringify({ type: "response.completed", response: { id, output,
        usage: { input_tokens: 100, input_tokens_details: { cached_tokens: 0 }, output_tokens: output.length } } }));
    });
  });
  const officialPort = await listen(official);
  const idle = new IdleWorker();
  let host;
  if (nokiy) {
    const root = await mkdtemp(join(tmpdir(), "nokiy-synthetic-prewarm-"));
    t.after(() => rm(root, { recursive: true, force: true }));
    const script = join(root, "host.py"), source = "# Never executed: projection fixture.\n";
    await writeFile(script, source);
    host = { python: process.execPath, script, coordinator_mode: "dispatch_only_v1",
      sha256: createHash("sha256").update(source).digest("hex") };
  }
  const switchboard = await createSwitchboardServer({
    runtime: { installation_capability: CAPABILITY, host: "127.0.0.1", port: 0 },
    admission: {
      status: "ADMITTED", external_batch_admitted: true,
      authority_effect: EXTERNAL_EFFECT_SCOPE,
      protocol_compatibility: { external_model_orchestration: EXTERNAL_ORCHESTRATION_PROTOCOL },
      policy: { external_models: "allow", effect_scope: EXTERNAL_EFFECT_SCOPE },
      official_models: [MODEL, "gpt-6-astra", "gpt-6.1-sol"],
      ...(nokiy ? { local_nokiy_direct: { admitted: true, host } } : {}),
    },
    ...(nokiy ? { workflowProfiles: nokiyProfiles,
      modelCatalog: { models: ["gpt-6-astra", "gpt-6.1-sol"].map((slug) => ({
        slug, supported_reasoning_levels: [{ effort: "max" }],
      })) } } : {}),
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
    await new Promise((resolve) => official.close(resolve));
  });
  const connect = async () => {
    const ws = new WebSocket("ws://127.0.0.1:" + address.port + PREFIX + "/responses", { headers: { session_id: "prewarm-session" } });
    clients.push(ws);
    await new Promise((resolve, reject) => { ws.once("open", resolve); ws.once("error", reject); });
    return ws;
  };
  return { requests, connect, receipts, wires, diagnostics,
    url: "http://127.0.0.1:" + address.port + PREFIX + "/responses" };
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

test("exec-less coordinator calls can return tool results on their own bound chain", async (t) => {
  const state = await fixture(t, true), ws = await state.connect();
  const request = { model: "nokiy-efficient", reasoning: { effort: "max" } };
  const first = await turn(ws, { ...request, instructions: "Read the supplied evidence.",
    tools: [{ type: "function", name: "read_file", parameters: { type: "object" } }],
    input: [user("read without exec")] });
  assert.equal(first.terminal.type, "response.completed");
  assert.equal(first.terminal.response.output[0].name, "read_file");
  const next = await turn(ws, { ...request, previous_response_id: first.terminal.response.id,
    input: [{ type: "function_call_output", call_id: "read_1", output: "verified evidence" }] });
  assert.equal(next.terminal.type, "response.completed");
  assert.equal(state.requests.length, 2);
  assert.equal(state.requests[1].model, "gpt-6.1-sol");
  assert.equal(state.requests[1].previous_response_id, first.terminal.response.id);
  assert.equal(state.requests[1].tools, undefined);
  assert.equal(state.requests[1].instructions, undefined);
  assert.equal(state.requests[1].input.length, 1);
  const third = await turn(ws, { ...request, previous_response_id: next.terminal.response.id,
    tools: [{ type: "function", name: "read_file", parameters: { type: "object" } }], input: [user("continue")] });
  assert.equal(third.terminal.type, "response.completed");
  assert.notEqual(state.requests[2].tool_choice, "none");
  assert.equal(state.diagnostics.at(-1).native_tool_missing.continuation, true);
});

test("HTTP exec-less full and unbound chained requests answer and name advertised tools", async (t) => {
  const state = await fixture(t, true);
  for (const previous_response_id of [undefined, "resp_unbound_http"]) {
    const response = await fetch(state.url, { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ model: "nokiy-efficient", reasoning: { effort: "max" }, stream: true,
        tools: [{ type: "function", name: "read_file", parameters: { type: "object" } }],
        previous_response_id, input: [user("answer from the supplied evidence")] }) });
    assert.equal(response.status, 200);
    assert.match(await response.text(), /response.completed/);
    const sent = state.requests.at(-1);
    assert.equal(sent.model, "gpt-6.1-sol");
    assert.equal(sent.tool_choice, previous_response_id ? "none" : undefined);
    assert.deepEqual(state.diagnostics.at(-1).native_tool_missing.advertised_tools, ["function:read_file"]);
  }
});

test("official generate:false prewarm is answered locally; its successor carries the full chain", async (t) => {
  delete process.env.SWITCHBOARD_SYNTHETIC_OFFICIAL_PREWARM;
  const state = await fixture(t);
  const ws = await state.connect();
  const prewarm = await turn(ws, { model: MODEL, instructions: "fixture", tools, input: [user("hello")], generate: false });
  assert.equal(prewarm.terminal.type, "response.completed");
  assert.match(prewarm.terminal.response.id, /^resp_openai_prewarm_/);
  assert.deepEqual(prewarm.terminal.response.usage, { input_tokens: 0, input_tokens_details: { cached_tokens: 0 },
    output_tokens: 0, output_tokens_details: { reasoning_tokens: 0 }, total_tokens: 0 });
  assert.equal(state.requests.length, 0, "the prewarm never reaches the provider");
  const next = await turn(ws, { model: MODEL, instructions: "fixture", tools,
    previous_response_id: prewarm.terminal.response.id, input: [user("next")] });
  assert.equal(next.terminal.type, "response.completed");
  assert.equal(ws.readyState, WebSocket.OPEN);
  assert.equal(state.requests.length, 1);
  assert.equal(state.requests[0].previous_response_id, undefined, "the synthetic id is never sent upstream");
  assert.equal(state.requests[0].generate, undefined);
  assert.deepEqual(state.requests[0].input, [user("hello"), user("next")]);
  assert.equal(state.requests[0].instructions, "fixture");
  const synthetic = state.receipts.find((e) => e.request_provenance?.request_kind === "SYNTHETIC_PREWARM");
  const actual = state.receipts.find((e) => e.event_type === "attempt_dispatch");
  assert.equal(synthetic.attempt_count, 0);
  assert.equal(synthetic.upstream_dispatch_started, false);
  assert.notEqual(synthetic.request_id, actual.request_id);
  assert.equal(actual.request_provenance.request_kind, "REQUEST");
  assert.equal(actual.attempt_ordinal, 1);
  assert.ok(actual.attempt_id.includes(actual.request_id));
  assert.equal(actual.request_provenance.wire.sha256, createHash("sha256").update(state.wires[0]).digest("hex"));
  assert.equal(actual.request_provenance.wire.bytes, state.wires[0].length);
  assert.equal(actual.request_provenance.previous_response_id, null);
  // Later turns chain normally from the real response.
  await turn(ws, { model: MODEL, instructions: "fixture", tools,
    previous_response_id: next.terminal.response.id, input: [user("third")] });
  assert.equal(state.requests[1].previous_response_id, next.terminal.response.id);
  assert.deepEqual(state.requests[1].input, [user("third")]);
});

test("a prewarm successor without previous_response_id is forwarded unchanged", async (t) => {
  delete process.env.SWITCHBOARD_SYNTHETIC_OFFICIAL_PREWARM;
  const state = await fixture(t);
  const ws = await state.connect();
  await turn(ws, { model: MODEL, instructions: "fixture", tools, input: [user("hello")], generate: false });
  await turn(ws, { model: MODEL, instructions: "fixture", tools, input: [user("hello"), user("full")] });
  assert.equal(state.requests.length, 1);
  assert.deepEqual(state.requests[0].input, [user("hello"), user("full")]);
});

for (const profile of nokiyProfiles) test(`${profile.slug}: an empty synthetic prewarm successor keeps the full Lite catalog together`, async (t) => {
  delete process.env.SWITCHBOARD_SYNTHETIC_OFFICIAL_PREWARM;
  const state = await fixture(t, true), ws = await state.connect();
  const request = { model: profile.slug, reasoning: { effort: "max" } };
  const prewarm = await turn(ws, { ...request, input: [], generate: false });
  assert.match(prewarm.terminal.response.id, /^resp_openai_prewarm_/);
  assert.equal(state.requests.length, 0);
  const next = await turn(ws, { ...request, previous_response_id: prewarm.terminal.response.id, input: liteInput() });
  assert.equal(next.terminal.type, "response.completed");
  assert.equal(state.requests.length, 1);
  const sent = state.requests[0];
  assert.equal(sent.previous_response_id, undefined);
  assert.equal(sent.tools, undefined, "all adapters must join the current native catalog, not a separate top-level catalog");
  assert.equal(sent.instructions, undefined);
  assert.equal(sent.input.filter((item) => item.type === "additional_tools").length, 1);
  const advertised = names(sent.input[0].tools);
  assert.ok(advertised.includes("nokiy_execute"));
  assert.ok(advertised.includes("wait"));
  assert.ok(advertised.includes(profile.slug === "nokiy-efficient" ? "exec" : "nokiy_goal"));
  assert.deepEqual(sent.input.at(-1), user("task"));
});

test("an exec-less synthetic successor can answer and a later full request restores execution", async (t) => {
  const state = await fixture(t, true), ws = await state.connect();
  const request = { model: "nokiy-direct", reasoning: { effort: "max" } };
  const prewarm = await turn(ws, { ...request, input: [], generate: false });
  const previous_response_id = prewarm.terminal.response.id;
  const answered = await turn(ws, { ...request, previous_response_id, input: [user("no executor")] });
  assert.equal(answered.terminal.type, "response.completed");
  assert.equal(state.requests.length, 1);
  assert.equal(state.requests[0].previous_response_id, undefined);
  const next = await turn(ws, { ...request, input: liteInput() });
  assert.equal(next.terminal.type, "response.completed");
  assert.equal(state.requests[1].tools, undefined);
});

test("ordinary unbound deltas and nonempty synthetic-successor overrides keep their original catalog paths", async (t) => {
  const state = await fixture(t, true), ws = await state.connect();
  const request = { model: "nokiy-direct", reasoning: { effort: "max" } };
  await turn(ws, { ...request, previous_response_id: "resp_unbound_real", input: liteInput() });
  assert.equal(state.requests[0].previous_response_id, "resp_unbound_real");
  assert.ok(names(state.requests[0].tools).includes("nokiy_execute"));
  const prewarm = await turn(ws, { ...request, input: [], generate: false });
  const override = [{ type: "function", name: "exec_command", parameters: {
    type: "object", properties: { cmd: { type: "string" } }, required: ["cmd"],
  } }];
  await turn(ws, { ...request, previous_response_id: prewarm.terminal.response.id,
    instructions: "PRESERVE EXPLICIT OVERRIDE", tools: override, input: liteInput() });
  assert.ok(names(state.requests[1].tools).includes("nokiy_execute"));
  assert.ok(state.requests[1].instructions.startsWith("PRESERVE EXPLICIT OVERRIDE\n\n"));
});

test("SWITCHBOARD_SYNTHETIC_OFFICIAL_PREWARM=0 forwards the prewarm", async (t) => {
  process.env.SWITCHBOARD_SYNTHETIC_OFFICIAL_PREWARM = "0";
  t.after(() => { delete process.env.SWITCHBOARD_SYNTHETIC_OFFICIAL_PREWARM; });
  const state = await fixture(t);
  const ws = await state.connect();
  const prewarm = await turn(ws, { model: MODEL, instructions: "fixture", tools, input: [user("hello")], generate: false });
  assert.equal(prewarm.terminal.response.id, "resp_official_1");
  assert.equal(state.requests.length, 1);
  assert.equal(state.requests[0].generate, false);
  await turn(ws, { model: MODEL, instructions: "fixture", tools,
    previous_response_id: prewarm.terminal.response.id, input: [user("next")] });
  assert.equal(state.requests[1].previous_response_id, "resp_official_1");
});

test("cache keep-alive refreshes are off by default during a tool wait", async (t) => {
  const state = await fixture(t);
  const ws = await state.connect();
  const first = await turn(ws, { model: MODEL, instructions: "fixture", tools, input: [user("call tool")] });
  assert.equal(first.terminal.response.output[0].type, "function_call");
  await new Promise((resolve) => setTimeout(resolve, 200));
  assert.equal(state.requests.filter((body) => body.max_output_tokens !== undefined).length, 0);
  assert.equal(state.requests.length, 1);
});

