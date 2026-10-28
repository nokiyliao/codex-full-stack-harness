import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import http from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import WebSocket, { WebSocketServer } from "ws";
import { createSwitchboardServer } from "../src/server.mjs";

const capability = "offline-projection-error-abcdefghijklmnop";
const declaration = "declare const tools: { exec_command(args: {cmd: string;}): Promise<unknown>; };\n"
  + "declare const tools: { write_stdin(args: {session_id: number;chars?: string;}): Promise<unknown>; };";
const original = { type: "custom_tool_call", name: "exec", namespace: "functions", id: "original_item",
  call_id: "original_call", input: 'text(await tools.exec_command({"cmd":"NEVER_EXECUTED"}));' };
const input = [original, { type: "custom_tool_call_output", call_id: original.call_id,
  output: '{"session_id":42,"output":"running"}' }];
const requestBody = () => ({ model: "nokiy", stream: true, reasoning: { effort: "max" }, input,
  tools: [{ type: "namespace", name: "functions", tools: [{ type: "custom", name: "exec",
    description: declaration, format: { type: "text" } }] }] });

test("local SSE rejects correctable tool inputs as native results while policy failures stay terminal",
  { timeout: 10000 }, async (t) => {
    const root = await mkdtemp(join(tmpdir(), "nokiy-projection-error-"));
    t.after(() => rm(root, { recursive: true, force: true }));
    const script = join(root, "host.py"), bytes = "# Never executed: local transport fixture.\n";
    await writeFile(script, bytes);
    let current, requests = 0;
    const upstream = http.createServer(async (req, res) => {
      for await (const _chunk of req) { /* Consume only the synthetic fixture. */ }
      requests++;
      res.writeHead(200, { "content-type": "text/event-stream" });
      res.write('data: {"type":"response.created","response":{"id":"fixture"}}\n\n');
      await new Promise((resolve) => setImmediate(resolve));
      res.write(`data: ${JSON.stringify({ type: "response.output_item.done", item: current.item })}\n\n`);
      res.end(`data: ${JSON.stringify({ type: "response.completed", response: {
        id: "fixture", status: "completed", output: [current.item],
      } })}\n\n`);
    });
    const wss = new WebSocketServer({ server: upstream });
    wss.on("connection", socket => socket.on("message", () => {
      requests++;
      socket.send(JSON.stringify({ type: "response.output_item.done", item: current.item }));
      socket.send(JSON.stringify({ type: "response.completed", response: {
        id: "ws_fixture_" + requests, status: "completed", output: [current.item],
      } }));
    }));
    await new Promise((resolve) => upstream.listen(0, "127.0.0.1", resolve));
    const fakeWorker = { close() {}, async closeSession() {} };
    const board = await createSwitchboardServer({
      runtime: { installation_capability: capability },
      admission: { status: "ADMITTED", external_batch_admitted: true,
        official_models: ["gpt-6-astra", "gpt-6.1-sol"],
        protocol_compatibility: { external_model_orchestration: "responses-tool-loop-v2" },
        policy: { external_models: "allow", effect_scope: "local_inference_routing_only" },
        authority_effect: "local_inference_routing_only", local_nokiy_direct: { admitted: true,
          host: { python: process.execPath, script, coordinator_mode: "dispatch_only_v1",
            sha256: createHash("sha256").update(bytes).digest("hex") } } },
      workflowProfiles: [{ slug: "nokiy", display_name: "Offline fixture", workflow: "nokiy",
        coordinator: { model: "gpt-6-astra", reasoning_effort: "max" },
        worker: { model: "gpt-6.1-sol", reasoning_effort: "max", service_tier: "default" } },
      { slug: "nokiy-direct", display_name: "Offline Ultra", workflow: "nokiy-direct",
        coordinator: { model: "gpt-6-astra", reasoning_effort: "xhigh" },
        worker: { model: "gpt-6.1-sol", reasoning_effort: "xhigh", service_tier: "default" } }],
      modelCatalog: { models: ["gpt-6-astra", "gpt-6.1-sol"].map((slug) => ({ slug,
        supported_reasoning_levels: [{ effort: "max" }, { effort: "xhigh" }] })) },
      xaiWorker: fakeWorker, agyWorker: fakeWorker, webWorker: fakeWorker, openrouterWorker: fakeWorker,
      skipFileGate: true, officialOrigin: `http://127.0.0.1:${upstream.address().port}`,
    });
    const address = await board.listen({ host: "127.0.0.1", port: 0 });
    t.after(async () => {
      board.server.closeAllConnections();
      upstream.closeAllConnections();
      await board.close();
      for (const socket of wss.clients) socket.terminate();
      await new Promise(resolve => wss.close(resolve));
      await new Promise((resolve) => upstream.close(resolve));
    });
    const records = [], write = process.stdout.write;
    t.mock.method(process.stdout, "write", function (chunk, ...args) {
      if (typeof chunk === "string" && chunk.startsWith("{")) {
        try { records.push(JSON.parse(chunk)); } catch { /* Not a structured log. */ }
      }
      return write.call(this, chunk, ...args);
    });
    const cases = [
      { name: "worker intake", recoverable: true, code: "NOKIY_NATIVE_TOOL_INVALID", item: { type: "custom_tool_call",
        name: "nokiy_execute", id: "worker_item", call_id: "worker_call", input: "not json" } },
      { name: "observation", recoverable: true, code: "NOKIY_NATIVE_OBSERVATION_INVALID", diagnostic: "native_observation",
        reason: "ORIGINAL_PAIR_UNAVAILABLE", item: { type: "custom_tool_call", name: "nokiy_observe",
          id: "observe_item", call_id: "observe_call", input: JSON.stringify({ kind: "exec_session",
            session_id: 42, source_call_id: "missing_call" }) } },
      { name: "native arguments", recoverable: true, code: "NOKIY_NATIVE_TOOL_INVALID", item: { type: "custom_tool_call",
        name: "nokiy_native", id: "native_item", call_id: "native_call", input: '{"cmd":42}' } },
      { name: "coordinator policy", code: "NOKIY_COORDINATOR_TOOL_FORBIDDEN", diagnostic: "coordinator_rejection",
        reason: "call_not_advertised", item: { ...original, input: 'text("SECRET_COMMAND");' } },
    ];
    for (const fixture of cases) await t.test(fixture.name, async () => {
      current = fixture;
      const count = requests;
      const response = await fetch(`http://127.0.0.1:${address.port}/${capability}/backend-api/codex/v1/responses`,
        { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(requestBody()),
          signal: AbortSignal.timeout(2000) });
      assert.equal(response.status, 200, "SSE headers were already sent; application error belongs in a terminal frame");
      const text = await response.text();
      const events = text.split("\n\n").filter(Boolean).map((frame) => JSON.parse(frame.slice(6)));
      assert.equal(events[0].type, "response.created");
      if (fixture.recoverable) {
        assert.equal(events.at(-1).type, "response.completed");
        assert.equal(events.filter((event) => event.type === "error").length, 0);
        const item = events.find((event) => event.type === "response.output_item.done").item;
        assert.deepEqual(item, events.at(-1).response.output[0]);
        assert.equal(item.call_id, fixture.item.call_id);
        assert.equal(item.name, "exec");
        assert.equal(item.namespace, "functions");
        const result = JSON.parse(item.input.slice("text(".length, -2));
        assert.equal(result.status, "REJECTED_LOCAL_TOOL_CALL");
        assert.equal(result.execution_started, false);
        assert.equal(result.same_input_retry_allowed, false);
        assert.equal(result.safe_to_correct_input, true);
        assert.equal(result.error.code, fixture.code);
        if (fixture.reason) assert.equal(result.error.details[fixture.diagnostic].reason, fixture.reason);
      } else {
        assert.equal(events.at(-1).type, "error");
        assert.equal(events.at(-1).error.code, fixture.code);
        assert.equal(events.filter((event) => event.type === "error").length, 1);
        // Codex honors response.failed (invalid_prompt is not retried) and ignores a bare error event.
        assert.equal(events.at(-2).type, "response.failed");
        assert.equal(events.at(-2).response.error.code, "invalid_prompt");
        assert.ok(events.at(-2).response.error.message.startsWith(`${fixture.code}: `));
        assert.ok(!events.some((event) => event.type === "response.completed"));
        const logged = records.filter((record) => record.error_code === fixture.code).at(-1);
        assert.equal(logged.status, 422);
        assert.equal(logged.diagnostic.official_http.terminal_cause, "projection_error");
        assert.equal(logged.diagnostic[fixture.diagnostic].reason, fixture.reason);
      }
      assert.ok(!text.includes("SECRET_COMMAND") && !text.includes("NEVER_EXECUTED"));
      assert.equal(requests, count + 1, "No automatic provider retry or native execution");
    });
    await t.test("Ultra WebSocket survives intake rejection and accepts a corrected next call", async () => {
      const workspace = join(root, "workspace");
      await mkdir(workspace);
      await writeFile(join(workspace, "a.py"), "x = 1\n");
      const socket = new WebSocket(`ws://127.0.0.1:${address.port}/${capability}/backend-api/codex/v1/responses`);
      t.after(() => socket.terminate());
      await new Promise((resolve, reject) => { socket.once("open", resolve); socket.once("error", reject); });
      const exchange = body => new Promise((resolve, reject) => {
        const receive = bytes => {
          const event = JSON.parse(bytes.toString());
          if (event.type === "error" || event.type === "response.failed") {
            socket.off("message", receive); reject(Error(JSON.stringify(event)));
          } else if (event.type === "response.completed") {
            socket.off("message", receive); resolve(event.response);
          }
        };
        socket.on("message", receive);
        socket.send(JSON.stringify({ ...body, type: "response.create" }));
      });
      const batch = { operation: "run_batch", artifact_root: join(root, "receipts"),
        members: [{ intent: { workspace, task: "Read a.py only.", read: ["a.py"] } }],
        post_batch_verifier: { intent: { verifier: ["wrong type, no value logging"] } } };
      current = { item: { type: "custom_tool_call", name: "nokiy_execute", id: "ws_reject", call_id: "ws_reject",
        input: JSON.stringify(batch) } };
      const body = { ...requestBody(), model: "nokiy-direct", reasoning: { effort: "xhigh" }, input: [] };
      const first = await exchange(body);
      const rejected = first.output[0];
      const outputs = [];
      const { runInNewContext } = await import("node:vm");
      await runInNewContext(rejected.input, { text: value => outputs.push(value), tools: new Proxy({}, {
        get() { assert.fail("Rejected intake must not execute any tool"); },
      }) });
      assert.equal(outputs[0].status, "REJECTED_LOCAL_TOOL_CALL");
      assert.equal(outputs[0].execution_started, false);
      assert.equal(outputs[0].error.details.native_tool_validation.issues[0].field, "post_batch_verifier.intent.verifier");
      batch.post_batch_verifier.intent.verifier = { argv: ["/bin/true"] };
      current = { item: { type: "custom_tool_call", name: "nokiy_execute", id: "ws_corrected", call_id: "ws_corrected",
        input: JSON.stringify(batch) } };
      const second = await exchange({ ...body, previous_response_id: first.id, input: [{
        type: "custom_tool_call_output", call_id: rejected.call_id, output: JSON.stringify(outputs[0]),
      }] });
      assert.equal(socket.readyState, WebSocket.OPEN);
      assert.equal(second.output[0].call_id, "ws_corrected");
      assert.match(second.output[0].input, /'--payload'/, "Corrected input returns to unchanged host admission");
      assert.ok(!second.output[0].input.includes("REJECTED_LOCAL_TOOL_CALL"));
      socket.close();
    });
  });
