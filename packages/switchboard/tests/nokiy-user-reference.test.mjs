// Coordinator user-message references: exact expansion, no re-generation of task text.

import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { NOKIY_TOOL, nativeToolProjection, nativeToolItem, nativeEventProjector } from "../src/nokiy-native-tool.mjs";

let directory, previousCodexHome;
before(() => {
  directory = mkdtempSync(join(tmpdir(), "nokiy-user-reference-"));
  writeFileSync(join(directory, "config.toml"), "[agents]\nmax_threads = 4\n");
  previousCodexHome = process.env.CODEX_HOME;
  process.env.CODEX_HOME = directory;
});
after(() => {
  if (previousCodexHome === undefined) delete process.env.CODEX_HOME;
  else process.env.CODEX_HOME = previousCodexHome;
  rmSync(directory, { recursive: true, force: true });
});

const host = { python: "/stub/python3", script: "/stub/nokiy-native-host.py" };
const TASK = "COORDINATOR NOTE: dispatch only.\nWORKER TASK:\nRead 雪 files.\n1. First \"quoted\" question?\n";
const input = [
  { type: "message", role: "user", content: [{ type: "input_text", text: "<environment_context>cwd</environment_context>" }] },
  { type: "message", role: "user", content: [{ type: "input_text", text: "# AGENTS.md instructions for /x\n\nrules" }] },
  { type: "message", role: "user", content: [{ type: "input_text", text: TASK }] },
];

function projection(type = "function", workflow = "nokiy-direct", body = { input }) {
  const tool = type === "function"
    ? { type, name: "exec_command", parameters: { type: "object" } }
    : { type, name: "exec", format: { type: "text" } };
  return nativeToolProjection({ ...body, tools: [{ type: "namespace", name: "functions", tools: [tool] }] }, {
    host, worker: "gpt-6.1-sol", workerEffort: "max", workerServiceTier: "default", workflow,
  });
}
const call = (payload, type = "function") => ({ type: type === "function" ? "function_call" : "custom_tool_call",
  name: NOKIY_TOOL, id: "item_ref", call_id: "call_ref", status: "completed",
  [type === "function" ? "arguments" : "input"]: JSON.stringify(payload) });
const envelopeOf = (item) => {
  const text = item.arguments ? JSON.parse(item.arguments).cmd : item.input;
  const encoded = text.match(/'--payload' '([A-Za-z0-9_-]+)'/)?.[1];
  assert.ok(encoded, "host command expected");
  return JSON.parse(Buffer.from(encoded, "base64url").toString("utf8"));
};
const run = (prompt, summary = "bounded") => ({ operation: "run",
  draft: { workspace: "/w", artifact_root: "/a", prompt, authority_effect: "none" },
  action: { context_summary: summary } });

test("run expands the exact operator text after a marker and the whole latest message", () => {
  for (const type of ["custom", "function"]) {
    const projected = nativeToolItem(call(run("{{nokiy:user_message_after:WORKER TASK:}}\nOnly question 1.",
      "Context: {{nokiy:user_message}}"), type), projection(type));
    const payload = envelopeOf(projected).payload;
    assert.equal(payload.draft.prompt, "Read 雪 files.\n1. First \"quoted\" question?\n\nOnly question 1.");
    assert.equal(payload.action.context_summary, "Context: " + TASK);
  }
});

test("compact batch shared and delta prompts expand; other fields stay literal", () => {
  const payload = { operation: "run_batch", artifact_root: "/a",
    shared: { schema_version: "nokiy_batch_shared_v1", draft: { workspace: "/w",
      prompt: "{{nokiy:user_message_after:WORKER TASK:}}" }, action: { context_summary: "s" } },
    member_deltas: [{ draft: { artifact_root: "/a/one", prompt: "IDs 1-6" },
      action: { mission: { task_id: "{{nokiy:user_message}}" } } }] };
  const expanded = envelopeOf(nativeToolItem(call(payload), projection())).payload;
  assert.equal(expanded.shared.draft.prompt, "Read 雪 files.\n1. First \"quoted\" question?\n");
  assert.equal(expanded.member_deltas[0].action.mission.task_id, "{{nokiy:user_message}}");
});

test("payloads without references are unchanged", () => {
  const original = run("plain {{other}} text");
  assert.deepEqual(envelopeOf(nativeToolItem(call(original), projection())).payload, original);
});

test("batch guidance demonstrates the operator reference instead of copying task text", () => {
  const tool = projection().body.tools.find((item) => item.name === NOKIY_TOOL);
  assert.ok(tool, "nokiy tool expected");
  assert.match(tool.description, /prompt:"rules the operator text lacks\.\.\.\\n\\n\{\{nokiy:user_message\}\}\\n"/);
  assert.match(tool.description, /write \{\{nokiy:user_message\}\} \(or \{\{nokiy:user_message_after:EXACT MARKER TEXT\}\}\) in shared\.draft\.prompt instead of copying it/);
  assert.doesNotMatch(tool.description, /nested references are rejected; no templates are evaluated/);
});

test("an unresolved marker becomes a correctable local rejection, never a host command", () => {
  for (const type of ["custom", "function"]) {
    const projector = nativeEventProjector(projection(type));
    const event = projector({ type: "response.output_item.done",
      item: call(run("{{nokiy:user_message_after:MISSING MARKER}}"), type) });
    const text = type === "function" ? JSON.parse(event.item.arguments).cmd : event.item.input;
    assert.doesNotMatch(text, /--payload/);
    assert.match(text, /NOKIY_USER_REFERENCE_UNRESOLVED/);
    assert.match(text, /REJECTED_LOCAL_TOOL_CALL/);
    assert.equal(event.item.call_id, "call_ref");
  }
  assert.throws(() => nativeToolItem(call(run("{{nokiy:user_message}}")), projection("function", "nokiy-direct", { input: [] })),
    (error) => error.code === "NOKIY_USER_REFERENCE_UNRESOLVED");
});

test("the run contract advertises references for both workflows", () => {
  for (const workflow of ["nokiy", "nokiy-direct"]) {
    const description = projection("custom", workflow).body.tools.at(-1).description;
    assert.match(description, /\{\{nokiy:user_message_after:EXACT MARKER TEXT\}\}/);
    assert.match(description, /unresolved reference is rejected without starting a worker/);
  }
});


test("WebSocket delta frames resolve references from the connection's earlier operator text", { timeout: 10000 }, async (t) => {
  const { createHash } = await import("node:crypto");
  const { mkdtemp, readFile, rm, writeFile } = await import("node:fs/promises");
  const http = (await import("node:http")).default;
  const { WebSocket, WebSocketServer } = await import("ws");
  const { createSwitchboardServer } = await import("../src/server.mjs");
  const hash = (value) => createHash("sha256").update(value).digest("hex");
  const root = await mkdtemp(join(tmpdir(), "nokiy-reference-ws-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const script = join(root, "host.py");
  await writeFile(script, "# loopback only\n");
  let calls = 0;
  const reply = () => {
    calls += 1;
    const item = { type: "custom_tool_call", name: NOKIY_TOOL, id: "item_" + calls, call_id: "call_" + calls, status: "completed",
      input: JSON.stringify(run("{{nokiy:user_message_after:WORKER TASK:}}")) };
    return [{ type: "response.output_item.done", item },
      { type: "response.completed", response: { id: "resp_" + calls, status: "completed", output: [item] } }];
  };
  const upstream = http.createServer((req, res) => { res.writeHead(500); res.end(); });
  const wss = new WebSocketServer({ server: upstream });
  wss.on("connection", (socket) => socket.on("message", () => { for (const event of reply()) socket.send(JSON.stringify(event)); }));
  await new Promise((resolve) => upstream.listen(0, "127.0.0.1", resolve));
  t.after(async () => {
    for (const client of wss.clients) client.terminate();
    await new Promise((resolve) => wss.close(resolve));
    await new Promise((resolve) => upstream.close(resolve));
  });
  const fakeWorker = { close() {}, async closeSession() {} };
  const board = await createSwitchboardServer({
    runtime: { installation_capability: "offline-user-reference-abcdefghijklmnop" },
    admission: { status: "ADMITTED", external_batch_admitted: true, official_models: ["gpt-6-astra", "gpt-6.1-sol"],
      protocol_compatibility: { external_model_orchestration: "responses-tool-loop-v2" },
      policy: { external_models: "allow", effect_scope: "local_inference_routing_only" },
      authority_effect: "local_inference_routing_only",
      local_nokiy_direct: { admitted: true, host: { python: process.execPath, python_sha256: hash(await readFile(process.execPath)),
        script, sha256: hash("# loopback only\n") } } },
    workflowProfiles: { schema_version: "nokiy_workflow_profiles_v1", profiles: [{ slug: "nokiy-direct-test", display_name: "Test",
      workflow: "nokiy-direct", coordinator: { model: "gpt-6-astra", reasoning_effort: "ultra" },
      worker: { model: "gpt-6.1-sol", reasoning_effort: "max", service_tier: "default" } }] },
    modelCatalog: { models: ["gpt-6-astra", "gpt-6.1-sol"].map((slug) => ({ slug,
      supported_reasoning_levels: [{ effort: "max" }, { effort: "ultra" }] })) },
    xaiWorker: fakeWorker, agyWorker: fakeWorker, webWorker: fakeWorker, openrouterWorker: fakeWorker,
    skipFileGate: true, officialOrigin: "http://127.0.0.1:" + upstream.address().port,
  });
  const address = await board.listen({ host: "127.0.0.1", port: 0 });
  t.after(() => board.close());
  const socket = new WebSocket("ws://127.0.0.1:" + address.port + "/offline-user-reference-abcdefghijklmnop/backend-api/codex/v1/responses");
  t.after(() => socket.terminate());
  await new Promise((resolve, reject) => { socket.once("open", resolve); socket.once("error", reject); });
  const exchange = (body) => new Promise((resolve, reject) => {
    const receive = (bytes) => {
      const event = JSON.parse(bytes.toString());
      if (event.type === "error") { socket.off("message", receive); reject(new Error(JSON.stringify(event))); }
      if (event.type === "response.completed") { socket.off("message", receive); resolve(event.response); }
    };
    socket.on("message", receive);
    socket.send(JSON.stringify({ ...body, type: "response.create" }));
  });
  const body = { model: "nokiy-direct-test", reasoning: { effort: "max" }, input,
    tools: [{ type: "namespace", name: "functions", tools: [{ type: "custom", name: "exec", format: { type: "text" } }] }] };
  const prompts = [];
  const first = await exchange(body);
  prompts.push(envelopeOf(first.output[0]).payload.draft.prompt);
  const second = await exchange({ ...body, previous_response_id: first.id, input: [{ type: "custom_tool_call_output",
    call_id: first.output[0].call_id, output: "done" }] });
  prompts.push(envelopeOf(second.output[0]).payload.draft.prompt);
  assert.deepEqual(prompts, ["Read 雪 files.\n1. First \"quoted\" question?\n", "Read 雪 files.\n1. First \"quoted\" question?\n"]);
  assert.equal(calls, 2);
});

