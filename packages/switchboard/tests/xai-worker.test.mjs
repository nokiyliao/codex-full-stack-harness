import assert from "node:assert/strict";
import { chmod, mkdtemp, rm, writeFile } from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { WebSocketServer } from "ws";

import { IsolatedWorkerClient } from "../src/worker-client.mjs";
import {
  createExternalEventBridge,
  normalizeExternalItemsForOfficial,
  prepareXaiBody,
} from "../src/tool-bridge.mjs";

function listen(server) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolve(server.address()));
  });
}

test("xAI worker prewarms large instructions and keeps provider state private", async (t) => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "codex-xai-worker-test-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const tokenCommand = path.join(fixture, "token.sh");
  await writeFile(tokenCommand, "#!/bin/sh\nprintf test-token", { mode: 0o700 });
  await chmod(tokenCommand, 0o700);

  const frames = [];
  const compactBodies = [];
  const sockets = new Set();
  const server = http.createServer((request, response) => {
    if (request.method !== "POST" || request.url !== "/v1/responses/compact") {
      response.writeHead(404).end();
      return;
    }
    const chunks = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", () => {
      compactBodies.push(JSON.parse(Buffer.concat(chunks).toString()));
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({
        id: "cmp_private",
        object: "response.compaction",
        model: "grok-4.6",
        output: [{ type: "compaction", id: "cmp_private", encrypted_content: "opaque-private-state" }],
      }));
    });
  });
  const wss = new WebSocketServer({ noServer: true });
  server.on("upgrade", (request, socket, head) => {
    wss.handleUpgrade(request, socket, head, (ws) => wss.emit("connection", ws, request));
  });
  wss.on("connection", (ws, request) => {
    sockets.add(ws);
    ws.once("close", () => sockets.delete(ws));
    assert.equal(request.headers.authorization, "Bearer test-token");
    ws.on("message", (data) => {
      const frame = JSON.parse(data.toString());
      frames.push(frame);
      const id = frame.generate === false ? "resp_private_warmup" : "resp_public_turn";
      ws.send(JSON.stringify({ type: "response.created", response: { id } }));
      if (JSON.stringify(frame.input || []).includes("WAIT_FOR_CANCEL")) return;
      if (frame.generate !== false && frame.tools?.some((tool) => tool.name === "exec_command")) {
        ws.send(JSON.stringify({
          type: "response.output_item.done",
          output_index: 0,
          item: {
            type: "function_call",
            name: "exec_command",
            call_id: "call_exec_adapter",
            arguments: JSON.stringify({ cmd: "pwd", workdir: "/tmp" }),
          },
        }));
      } else if (frame.generate !== false) {
        ws.send(JSON.stringify({
          type: "response.output_item.done",
          output_index: 0,
          item: {
            type: "message",
            role: "assistant",
            id: "msg_1",
            content: [{ type: "output_text", text: "prewarm ok" }],
          },
        }));
      }
      ws.send(JSON.stringify({ type: "response.completed", response: { id } }));
    });
  });
  const address = await listen(server);
  t.after(() => new Promise((resolve) => {
    for (const socket of sockets) socket.terminate();
    wss.close();
    server.close(resolve);
  }));

  const worker = new IsolatedWorkerClient({
    script: new URL("../src/xai-worker.mjs", import.meta.url).pathname,
    label: "xAI-prewarm-test",
    env: {
      SWITCHBOARD_XAI_TOKEN_COMMAND: tokenCommand,
      SWITCHBOARD_XAI_WS_URL: `ws://127.0.0.1:${address.port}/v1/responses`,
      SWITCHBOARD_XAI_PREWARM_ENABLED: "1",
      SWITCHBOARD_XAI_PREWARM_BYTES: "1024",
    },
  });
  t.after(() => worker.close());
  const result = await worker.runResponses({
    model: "grok-4.6",
    reasoning: { effort: "xhigh" },
    store: false,
    instructions: `${"preserve authority ".repeat(240)}\n\n`.repeat(20),
    input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "hello" }] }],
    tools: [{ type: "function", name: "local_tool", parameters: { type: "object" } }],
  }, { sessionKey: "prewarm-test" });

  assert.equal(result.prewarmed, true);
  assert.equal(frames.length, 2);
  assert.equal(frames[0].generate, false);
  assert.ok(frames[0].instructions.length < 25 * 1024);
  assert.match(frames[0].instructions, /AUTHORITATIVE ROUTE IDENTITY/);
  assert.match(frames[0].instructions, /"catalog_model":"grok-4\.6"/);
  assert.match(frames[0].instructions, /"provider":"xai"/);
  assert.match(frames[0].instructions, /"requested_reasoning_effort":"xhigh"/);
  assert.match(frames[0].instructions, /Codex is the host harness, not the responding model identity/);
  assert.equal(frames[1].previous_response_id, "resp_private_warmup");
  assert.equal(frames[1].instructions, undefined);
  assert.equal(frames[1].tools, undefined);
  assert.equal(
    result.tool_projection.provider_prewarm_request_bytes,
    Buffer.byteLength(JSON.stringify(frames[0])),
  );
  assert.equal(
    result.tool_projection.provider_request_bytes,
    Buffer.byteLength(JSON.stringify(frames[1])),
  );
  assert.ok(result.tool_projection.provider_instruction_bytes > 0);
  assert.ok(result.tool_projection.provider_tools_bytes > 0);
  assert.equal(result.tool_projection.provider_reasoning_effort, "xhigh");
  assert.deepEqual(result.events.slice(0, 4).map((event) => event.type), [
    "response.created",
    "response.output_item.added",
    "response.reasoning_summary_part.added",
    "response.reasoning_summary_text.delta",
  ]);
  assert.equal(result.events[0].switchboard_public_status, undefined);
  assert.equal(result.events[1].switchboard_public_status, "xai_processing_v1");
  assert.equal(result.events[3].delta, "Grok processing");
  assert.equal(result.events.find((event) => event.item?.type === "message")?.output_index, 1);
  assert.equal(result.events.at(-1).response.id, "resp_public_turn");
  assert.doesNotMatch(JSON.stringify(result.events), /resp_private_warmup/);

  const cancelEvents = [];
  const cancelled = worker.runResponses({
    model: "grok-4.6",
    store: false,
    input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "WAIT_FOR_CANCEL" }] }],
    tools: [],
  }, { sessionKey: "cancel-test", onEvent: (event) => cancelEvents.push(event) });
  for (let attempt = 0; attempt < 20 && cancelEvents.length < 4; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  assert.equal(cancelEvents[3]?.delta, "Grok processing");
  await worker.closeSession("cancel-test");
  await assert.rejects(cancelled, (error) => error.code === "XAI_REQUEST_CANCELLED" && error.status === 499);

  const recovered = await worker.runResponses({
    model: "grok-4.6",
    store: false,
    input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "after cancel" }] }],
    tools: [],
  }, { sessionKey: "after-cancel" });
  assert.equal(recovered.events.at(-1).type, "response.completed");

  const continued = await worker.runResponses({
    model: "grok-4.6",
    store: false,
    previous_response_id: "resp_public_turn",
    input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "continue owned state" }] }],
    tools: [],
  }, { sessionKey: "after-cancel" });
  assert.equal(continued.events.at(-1).type, "response.completed");
  assert.equal(frames.at(-1).previous_response_id, "resp_public_turn");
  assert.equal(frames.at(-1).input.length, 1);
  assert.equal(frames.at(-1).instructions, undefined);
  assert.equal(frames.at(-1).tools, undefined);
  assert.equal(continued.tool_projection.xai_prefix_reused, true);
  assert.match(frames.at(-1).prompt_cache_key, /^[a-f0-9]{64}$/);

  const explicitPrewarm = await worker.runResponses({
    model: "grok-4.6",
    store: false,
    generate: false,
    instructions: "prewarm immutable instructions",
    input: [],
    tools: [],
  }, { sessionKey: "explicit-prewarm-test" });
  assert.equal(explicitPrewarm.events.at(-1).type, "response.completed");
  assert.equal(frames.at(-1).generate, false);
  assert.deepEqual(frames.at(-1).input, []);

  const defaultWorker = new IsolatedWorkerClient({
    script: new URL("../src/xai-worker.mjs", import.meta.url).pathname,
    label: "xAI-default-no-prewarm-test",
    env: {
      SWITCHBOARD_XAI_TOKEN_COMMAND: tokenCommand,
      SWITCHBOARD_XAI_WS_URL: `ws://127.0.0.1:${address.port}/v1/responses`,
      SWITCHBOARD_XAI_HTTP_BASE: `http://127.0.0.1:${address.port}/v1`,
      SWITCHBOARD_XAI_AUTO_COMPACT_BYTES: "1024",
      SWITCHBOARD_XAI_AUTO_COMPACT_MIN_RATIO: "0",
      SWITCHBOARD_XAI_PREWARM_BYTES: "1024",
    },
  });
  t.after(() => defaultWorker.close());
  const frameCountBeforeDefault = frames.length;
  const defaultResult = await defaultWorker.runResponses({
    model: "grok-4.6",
    store: false,
    instructions: `${"preserve authority ".repeat(240)}\n\n`.repeat(20),
    input: [
      { type: "message", role: "user", content: [{ type: "input_text", text: `old question ${"q".repeat(2048)}` }] },
      { type: "message", role: "assistant", content: [{ type: "output_text", text: `old answer ${"a".repeat(2048)}` }] },
      { type: "message", role: "user", content: [{ type: "input_text", text: "no hidden prewarm" }] },
    ],
    tools: [],
  }, { sessionKey: "default-no-prewarm-test" });
  assert.equal(defaultResult.prewarmed, false);
  assert.equal(defaultResult.compacted, true);
  assert.equal(compactBodies.length, 1);
  assert.equal(frames.length, frameCountBeforeDefault + 1);
  assert.notEqual(frames.at(-1).generate, false);
  assert.equal(frames.at(-1).input[0].type, "compaction");
  assert.equal(frames.at(-1).input.at(-1).content[0].text, "no hidden prewarm");
  assert.doesNotMatch(JSON.stringify(frames.at(-1).input), /old question|old answer/);

  // A message that mentions the current model is an ordinary turn: it keeps
  // its history, guidance and tools, and the route identity block answers it.
  const identityResult = await defaultWorker.runResponses({
    model: "grok-4.6",
    store: false,
    reasoning: { effort: "xhigh" },
    instructions: "IDENTITY_GUIDANCE_SENTINEL",
    input: [
      { type: "message", role: "user", content: [{ type: "input_text", text: "IDENTITY_HISTORY_SENTINEL" }] },
      { type: "message", role: "assistant", content: [{ type: "output_text", text: "old identity answer" }] },
      { type: "message", role: "user", content: [{ type: "input_text", text: "請說明目前的模型路由與推理等級，然後修好 local_tool 的設定" }] },
    ],
    tools: [{ type: "function", name: "local_tool", parameters: { type: "object" } }],
  }, { sessionKey: "route-identity-ordinary-turn-test" });
  const identityFrame = frames.at(-1);
  assert.ok(identityFrame.tools.some((tool) => /local_tool/.test(tool.name)));
  assert.match(JSON.stringify(identityFrame.input), /IDENTITY_HISTORY_SENTINEL/);
  assert.match(JSON.stringify(identityFrame.input), /然後修好 local_tool 的設定/);
  assert.match(identityFrame.instructions, /IDENTITY_GUIDANCE_SENTINEL/);
  assert.match(identityFrame.instructions, /"catalog_model":"grok-4\.6"/);
  assert.match(identityFrame.instructions, /"requested_reasoning_effort":"xhigh"/);
  assert.equal("xai_identity_fast_path" in identityResult.tool_projection, false);

  const compactedResponseId = defaultResult.events.at(-1).response.id;
  const continuedCompacted = await defaultWorker.runResponses({
    model: "grok-4.6",
    store: false,
    previous_response_id: compactedResponseId,
    input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "compact continuation delta" }] }],
    tools: [],
  }, { sessionKey: "default-no-prewarm-test" });
  assert.equal(continuedCompacted.events.at(-1).type, "response.completed");
  assert.equal(frames.at(-1).previous_response_id, undefined);
  assert.equal(frames.at(-1).input[0].type, "compaction");
  assert.equal(frames.at(-1).input.at(-1).content[0].text, "compact continuation delta");

  const desktopResult = await defaultWorker.runResponses({
    model: "grok-4.6",
    store: false,
    input: [
      {
        type: "message",
        role: "user",
        content: [{ type: "input_text", text: "Use structured exec_command to run pwd." }],
      },
      {
        type: "additional_tools",
        id: "tools_current_codex_contract",
        tools: [
          {
            type: "namespace",
            name: "codex",
            tools: [{ type: "custom", name: "exec", description: "Run Codex tools." }],
          },
          {
            type: "namespace",
            name: "devspace_utm_9013",
            tools: [{ type: "function", name: "bash", parameters: { type: "object" } }],
          },
          {
            type: "namespace",
            name: "devspace_local_lab",
            tools: [{ type: "function", name: "bash", parameters: { type: "object" } }],
          },
          { type: "function", name: "node_repl", parameters: { type: "object" } },
        ],
      },
    ],
    tools: [],
    tool_choice: { type: "custom", namespace: "codex", name: "exec" },
  }, { sessionKey: "desktop-exec-adapter-test" });
  const desktopFrame = frames.at(-1);
  const desktopToolNames = desktopFrame.tools.map((tool) => tool.name);
  assert.ok(desktopToolNames.includes("exec_command"));
  assert.ok(desktopToolNames.includes("write_stdin"));
  assert.equal(desktopToolNames.some((name) => /devspace|node_repl/i.test(name)), false);
  assert.equal(desktopFrame.input.some((item) => item.type === "additional_tools"), false);
  assert.deepEqual(desktopFrame.tool_choice, { type: "function", name: "exec_command" });
  assert.equal(desktopResult.tool_projection.additional_tool_count, 4);
  assert.equal(desktopResult.tool_projection.custom_exec_namespaced, true);
  assert.equal(desktopResult.tool_projection.exec_adapter_projected, true);
  const restoredExec = desktopResult.events.find((event) => event.item?.type === "custom_tool_call");
  assert.equal(restoredExec?.item?.name, "exec");
  assert.equal(restoredExec?.item?.namespace, "codex");
  assert.match(restoredExec.item.input, /tools\.exec_command/);
  assert.equal(desktopResult.events.at(-1).type, "response.completed");

  const stableTools = [
    { type: "function", name: "alpha_reader", description: "Read the alpha record", parameters: { type: "object" } },
    { type: "function", name: "beta_writer", description: "Write the beta record", parameters: { type: "object" } },
  ];
  const prefixStarted = frames.length;
  const prefixFirst = await worker.runResponses({
    model: "grok-4.6",
    store: false,
    instructions: "stable guidance",
    input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "PERSISTENCE_PREFIX_FIRST" }] }],
    tools: stableTools,
  }, { sessionKey: "prefix-stable" });
  const boundFrame = frames.slice(prefixStarted).find((frame) => Array.isArray(frame.tools) && frame.tools.length);
  assert.equal(boundFrame.tools.length, 2);
  assert.equal(prefixFirst.tool_projection.xai_prefix_reused, false);
  const prefixContinued = await worker.runResponses({
    model: "grok-4.6",
    store: false,
    instructions: "stable guidance",
    previous_response_id: "resp_public_turn",
    input: [
      { type: "message", role: "user", content: [{ type: "input_text", text: "PERSISTENCE_PREFIX_FIRST" }] },
      { type: "message", role: "assistant", content: [{ type: "output_text", text: "prewarm ok" }] },
      { type: "message", role: "user", content: [{ type: "input_text", text: "use beta_writer" }] },
    ],
    tools: [],
  }, { sessionKey: "prefix-stable" });
  assert.equal(prefixContinued.tool_projection.xai_prefix_reused, true);
  assert.equal(frames.at(-1).previous_response_id, "resp_public_turn");
  assert.equal(frames.at(-1).instructions, undefined);
  assert.equal(frames.at(-1).tools, undefined);
  assert.equal(frames.at(-1).prompt_cache_key, boundFrame.prompt_cache_key);
  const prefixChanged = await worker.runResponses({
    model: "grok-4.6",
    store: false,
    instructions: "stable guidance",
    previous_response_id: "resp_public_turn",
    input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "catalog changed" }] }],
    tools: [
      stableTools[0],
      { ...stableTools[1], description: "Write the beta record with a new contract" },
    ],
  }, { sessionKey: "prefix-stable" });
  assert.equal(prefixChanged.tool_projection.xai_prefix_reused, false);
  assert.equal(frames.at(-1).previous_response_id, "resp_public_turn");
  assert.equal(Array.isArray(frames.at(-1).tools), true);
  assert.equal(typeof frames.at(-1).instructions, "string");
});

test("xAI worker compacts completed active-turn tools before the mandatory history gate", async (t) => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "codex-xai-active-turn-test-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const tokenCommand = path.join(fixture, "token.sh");
  await writeFile(tokenCommand, "#!/bin/sh\nprintf test-token", { mode: 0o700 });
  await chmod(tokenCommand, 0o700);

  const compactBodies = [];
  const frames = [];
  const sockets = new Set();
  const server = http.createServer((request, response) => {
    if (request.method !== "POST" || request.url !== "/v1/responses/compact") {
      response.writeHead(404).end();
      return;
    }
    const chunks = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", () => {
      compactBodies.push(JSON.parse(Buffer.concat(chunks).toString()));
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({
        output: [{ type: "compaction", id: "cmp_active", encrypted_content: "opaque-active-state" }],
      }));
    });
  });
  const wss = new WebSocketServer({ noServer: true });
  server.on("upgrade", (request, socket, head) => {
    wss.handleUpgrade(request, socket, head, (ws) => wss.emit("connection", ws, request));
  });
  wss.on("connection", (ws) => {
    sockets.add(ws);
    ws.once("close", () => sockets.delete(ws));
    ws.on("message", (data) => {
      const frame = JSON.parse(data.toString());
      frames.push(frame);
      ws.send(JSON.stringify({ type: "response.created", response: { id: "resp_active" } }));
      ws.send(JSON.stringify({
        type: "response.output_item.done",
        output_index: 0,
        item: {
          type: "message",
          role: "assistant",
          content: [{ type: "output_text", text: "active compaction ok" }],
        },
      }));
      ws.send(JSON.stringify({ type: "response.completed", response: { id: "resp_active" } }));
    });
  });
  const address = await listen(server);
  t.after(() => new Promise((resolve) => {
    for (const socket of sockets) socket.terminate();
    wss.close();
    server.close(resolve);
  }));

  const worker = new IsolatedWorkerClient({
    script: new URL("../src/xai-worker.mjs", import.meta.url).pathname,
    label: "xAI-active-turn-compaction-test",
    env: {
      SWITCHBOARD_XAI_TOKEN_COMMAND: tokenCommand,
      SWITCHBOARD_XAI_WS_URL: `ws://127.0.0.1:${address.port}/v1/responses`,
      SWITCHBOARD_XAI_HTTP_BASE: `http://127.0.0.1:${address.port}/v1`,
      SWITCHBOARD_MAX_EXTERNAL_HISTORY_BYTES: String(8 * 1024),
      SWITCHBOARD_XAI_PRECOMPACTION_HISTORY_BYTES: String(128 * 1024),
      SWITCHBOARD_XAI_ACTIVE_TAIL_BYTES: String(3 * 1024),
      SWITCHBOARD_XAI_AUTO_COMPACT_BYTES: "1024",
      SWITCHBOARD_XAI_AUTO_COMPACT_MIN_RATIO: "0",
    },
  });
  t.after(() => worker.close());

  const authorityText = "Never mutate live authority. Preserve exact tool evidence.";
  const currentRequest = "Complete ACTIVE_TURN_OBJECTIVE without losing the recent tool result.";
  const input = [
    { type: "message", role: "developer", content: [{ type: "input_text", text: authorityText }] },
    { type: "message", role: "user", content: [{ type: "input_text", text: currentRequest }] },
  ];
  for (let index = 0; index < 12; index += 1) {
    const recent = index === 11;
    input.push({
      type: "function_call",
      call_id: `call_${index}`,
      name: "exec_command",
      arguments: JSON.stringify({ cmd: `read-${index}` }),
    });
    input.push({
      type: "function_call_output",
      call_id: `call_${index}`,
      output: `${recent ? "RECENT_ATOMIC_RESULT" : `OLD_TOOL_RESULT_${index}`} ${"x".repeat(recent ? 1024 : 4096)}`,
    });
  }

  const result = await worker.runResponses({
    model: "grok-4.6",
    store: false,
    input,
    tools: [{ type: "function", name: "exec_command", parameters: { type: "object" } }],
  }, { sessionKey: "active-turn-compaction-test" });

  assert.equal(result.compacted, true);
  assert.equal(result.compaction_scope, "active_turn");
  assert.equal(compactBodies.length, 1);
  assert.equal(frames.length, 1);
  assert.ok(Buffer.byteLength(JSON.stringify(frames[0].input)) <= 8 * 1024);
  assert.equal(frames[0].input[0].type, "compaction");
  assert.match(JSON.stringify(frames[0].input), /Never mutate live authority/);
  assert.match(JSON.stringify(frames[0].input), /ACTIVE_TURN_OBJECTIVE/);
  assert.match(JSON.stringify(frames[0].input), /call_11/);
  assert.match(JSON.stringify(frames[0].input), /RECENT_ATOMIC_RESULT/);
  assert.doesNotMatch(JSON.stringify(frames[0].input), /OLD_TOOL_RESULT_0/);
  assert.match(JSON.stringify(compactBodies[0].input), /OLD_TOOL_RESULT_0/);
  assert.equal(result.events.at(-1).type, "response.completed");
});

test("xAI worker clips an oversized active tool result and completes the turn", async (t) => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "codex-xai-shrink-test-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const tokenCommand = path.join(fixture, "token.sh");
  await writeFile(tokenCommand, "#!/bin/sh\nprintf test-token", { mode: 0o700 });
  await chmod(tokenCommand, 0o700);

  const frames = [];
  const sockets = new Set();
  const server = http.createServer((request, response) => {
    if (request.method !== "POST" || request.url !== "/v1/responses/compact") {
      response.writeHead(404).end();
      return;
    }
    const chunks = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", () => {
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({
        id: "cmp_shrink",
        object: "response.compaction",
        model: "grok-4.6",
        output: [{ type: "compaction", id: "cmp_shrink", encrypted_content: "opaque-shrink-state" }],
      }));
    });
  });
  const wss = new WebSocketServer({ noServer: true });
  server.on("upgrade", (request, socket, head) => {
    wss.handleUpgrade(request, socket, head, (ws) => wss.emit("connection", ws, request));
  });
  wss.on("connection", (ws) => {
    sockets.add(ws);
    ws.once("close", () => sockets.delete(ws));
    ws.on("message", (data) => {
      const frame = JSON.parse(data.toString());
      frames.push(frame);
      ws.send(JSON.stringify({ type: "response.created", response: { id: "resp_shrink" } }));
      ws.send(JSON.stringify({
        type: "response.output_item.done",
        output_index: 0,
        item: {
          type: "message",
          role: "assistant",
          content: [{ type: "output_text", text: "shrunk turn ok" }],
        },
      }));
      ws.send(JSON.stringify({ type: "response.completed", response: { id: "resp_shrink" } }));
    });
  });
  const address = await listen(server);
  t.after(() => new Promise((resolve) => {
    for (const socket of sockets) socket.terminate();
    wss.close();
    server.close(resolve);
  }));

  const worker = new IsolatedWorkerClient({
    script: new URL("../src/xai-worker.mjs", import.meta.url).pathname,
    label: "xAI-shrink-test",
    env: {
      SWITCHBOARD_XAI_TOKEN_COMMAND: tokenCommand,
      SWITCHBOARD_XAI_WS_URL: `ws://127.0.0.1:${address.port}/v1/responses`,
      SWITCHBOARD_XAI_HTTP_BASE: `http://127.0.0.1:${address.port}/v1`,
      SWITCHBOARD_MAX_EXTERNAL_HISTORY_BYTES: String(8 * 1024),
      SWITCHBOARD_XAI_PRECOMPACTION_HISTORY_BYTES: String(128 * 1024),
      SWITCHBOARD_XAI_ACTIVE_TAIL_BYTES: String(64 * 1024),
      SWITCHBOARD_XAI_AUTO_COMPACT_BYTES: "1024",
      SWITCHBOARD_XAI_AUTO_COMPACT_MIN_RATIO: "0",
    },
  });
  t.after(() => worker.close());

  const result = await worker.runResponses({
    model: "grok-4.6",
    store: false,
    input: [
      { type: "message", role: "developer", content: [{ type: "input_text", text: "Never mutate live authority." }] },
      { type: "message", role: "user", content: [{ type: "input_text", text: "Read HUGE_TOOL_TARGET." }] },
      { type: "function_call", call_id: "call_old", name: "exec_command", arguments: "{\"cmd\":\"old\"}" },
      { type: "function_call_output", call_id: "call_old", output: `OLD_TOOL_RESULT ${"y".repeat(4096)}` },
      { type: "function_call", call_id: "call_huge", name: "exec_command", arguments: "{\"cmd\":\"huge\"}" },
      { type: "function_call_output", call_id: "call_huge", output: `HUGE_TOOL_HEAD ${"z".repeat(20 * 1024)} HUGE_TOOL_TAIL` },
    ],
  }, { sessionKey: "shrink-oversized-tool" });

  assert.equal(result.events.at(-1).type, "response.completed");
  assert.equal(frames.length, 1);
  const encoded = JSON.stringify(frames[0].input);
  assert.ok(Buffer.byteLength(encoded) <= 8 * 1024);
  assert.match(encoded, /HUGE_TOOL_TARGET/);
  assert.match(encoded, /call_huge/);
  assert.match(encoded, /switchboard_truncated/);
  assert.match(encoded, /Never mutate live authority/);
});

test("xAI worker reports schema rejection location without input values", async (t) => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "codex-xai-rejection-test-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const tokenCommand = path.join(fixture, "token.sh");
  await writeFile(tokenCommand, "#!/bin/sh\nprintf test-token", { mode: 0o700 });
  await chmod(tokenCommand, 0o700);

  const server = http.createServer((request, response) => {
    const chunks = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", () => {
      const raw = Buffer.concat(chunks).toString();
      const functionCallIndex = raw.indexOf('"type":"function_call"');
      const column = Buffer.byteLength(raw.slice(0, functionCallIndex)) + 10;
      response.writeHead(422, { "content-type": "text/plain" });
      response.end(`Failed to deserialize the JSON body into the target type: at line 1 column ${column}`);
    });
  });
  const address = await listen(server);
  t.after(() => new Promise((resolve) => server.close(resolve)));

  const worker = new IsolatedWorkerClient({
    script: new URL("../src/xai-worker.mjs", import.meta.url).pathname,
    label: "xAI-rejection-test",
    env: {
      SWITCHBOARD_XAI_TOKEN_COMMAND: tokenCommand,
      SWITCHBOARD_XAI_HTTP_BASE: `http://127.0.0.1:${address.port}/v1`,
      SWITCHBOARD_XAI_AUTO_COMPACT_BYTES: "1",
    },
  });
  t.after(() => worker.close());

  await assert.rejects(worker.runResponses({
    model: "grok-4.6",
    input: [
      { type: "message", role: "user", content: [{ type: "input_text", text: "中文 private-old-value" }] },
      { type: "function_call", call_id: "call_1", name: "tool", arguments: "{}" },
      { type: "function_call_output", call_id: "call_1", output: "private-tool-value" },
      { type: "message", role: "user", content: [{ type: "input_text", text: "current" }] },
    ],
    tools: [],
  }, { sessionKey: "rejection-test" }), (error) => {
    assert.equal(error.code, "XAI_INPUT_SCHEMA_REJECTED_I1_FUNCTION_CALL");
    assert.match(error.message, /input\[1\] type=function_call/);
    assert.doesNotMatch(error.message, /private-old-value|private-tool-value/);
    return true;
  });
});

test("xAI worker rejects provider response continuation not owned by its session", async (t) => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "codex-xai-continuation-test-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const tokenCommand = path.join(fixture, "token.sh");
  await writeFile(tokenCommand, "#!/bin/sh\nprintf test-token", { mode: 0o700 });
  await chmod(tokenCommand, 0o700);
  const worker = new IsolatedWorkerClient({
    script: new URL("../src/xai-worker.mjs", import.meta.url).pathname,
    label: "xAI-continuation-test",
    env: { SWITCHBOARD_XAI_TOKEN_COMMAND: tokenCommand },
  });
  t.after(() => worker.close());

  await assert.rejects(worker.runResponses({
    model: "grok-4.6",
    previous_response_id: "resp_provider_private",
    input: [{ type: "function_call_output", call_id: "call_1", output: "result" }],
    tools: [],
  }, { sessionKey: "continuation-test" }), (error) => {
    assert.equal(error.code, "previous_response_not_found");
    assert.equal(error.status, 409);
    return true;
  });
});

test("external event bridge hides provider state and drops private reasoning", () => {
  const bridge = createExternalEventBridge("xai");
  const created = bridge.transform({
    type: "response.created",
    response: { id: "resp_provider_private", previous_response_id: "resp_parent_private" },
  });
  const reasoning = bridge.transform({
    type: "response.output_item.done",
    response_id: "resp_provider_private",
    item: { type: "reasoning", id: "rs_provider_private", encrypted_content: "secret" },
  });
  const message = bridge.transform({
    type: "response.output_item.done",
    response_id: "resp_provider_private",
    item: {
      type: "message",
      id: "msg_provider_private",
      role: "assistant",
      content: [{ type: "output_text", text: "portable" }],
    },
  });
  const completed = bridge.transform({
    type: "response.completed",
    response: {
      id: "resp_provider_private",
      output: [
        { type: "reasoning", id: "rs_provider_private", encrypted_content: "secret" },
        message.item,
      ],
    },
  });

  assert.equal(reasoning, null);
  assert.equal(bridge.providerResponseId, "resp_provider_private");
  assert.equal(created.response.id, bridge.publicResponseId);
  assert.match(message.item.id, /^msg_ext_/);
  assert.deepEqual(completed.response.output.map((item) => item.type), ["message"]);
  assert.doesNotMatch(JSON.stringify([created, message, completed]), /provider_private|secret|previous_response_id/);

  const official = normalizeExternalItemsForOfficial([message.item]);
  assert.equal(official.changed, true);
  assert.equal(official.input[0].id, undefined);
  assert.equal(official.input[0].content[0].text, "portable");
});

test("Web event bridge preserves the downstream response identity and payload", () => {
  const bridge = createExternalEventBridge("web");
  const providerEvent = {
    type: "response.completed",
    response: {
      id: "resp_web_owned",
      output: [{ type: "message", id: "msg_web_owned", role: "assistant", content: [] }],
    },
  };

  assert.deepEqual(bridge.transform(providerEvent), providerEvent);
  assert.equal(bridge.providerResponseId, "resp_web_owned");
  assert.equal(bridge.publicResponseId, null);
});

test("AGY bridge admits only the adapter-owned public status lifecycle", () => {
  const bridge = createExternalEventBridge("agy");
  const privateReasoning = bridge.transform({
    type: "response.output_item.added",
    item: { type: "reasoning", id: "rs_private", summary: [{ type: "summary_text", text: "secret" }] },
  });
  const publicStatus = bridge.transform({
    switchboard_public_status: "agy_processing_v1",
    type: "response.output_item.added",
    item: { type: "reasoning", id: "rs_status", summary: [], encrypted_content: "must-drop" },
  });
  const publicDelta = bridge.transform({
    switchboard_public_status: "agy_processing_v1",
    type: "response.reasoning_summary_text.delta",
    item_id: "rs_status",
    summary_index: 0,
    delta: "AGY processing",
  });

  assert.equal(privateReasoning, null);
  assert.equal(publicStatus.switchboard_public_status, undefined);
  assert.match(publicStatus.item.id, /^item_ext_/);
  assert.equal(publicStatus.item.encrypted_content, null);
  assert.equal(publicDelta.item_id, publicStatus.item.id);
  assert.doesNotMatch(JSON.stringify([publicStatus, publicDelta]), /must-drop|rs_status/);
});

test("xAI bridge admits only the adapter-owned public status lifecycle", () => {
  const bridge = createExternalEventBridge("xai");
  const privateReasoning = bridge.transform({
    type: "response.output_item.added",
    item: { type: "reasoning", id: "rs_private", summary: [{ type: "summary_text", text: "secret" }] },
  });
  const wrongMarker = bridge.transform({
    switchboard_public_status: "agy_processing_v1",
    type: "response.reasoning_summary_text.delta",
    item_id: "rs_wrong",
    summary_index: 0,
    delta: "must not pass",
  });
  const publicStatus = bridge.transform({
    switchboard_public_status: "xai_processing_v1",
    type: "response.output_item.added",
    item: { type: "reasoning", id: "rs_status", summary: [], encrypted_content: "must-drop" },
  });
  const publicDelta = bridge.transform({
    switchboard_public_status: "xai_processing_v1",
    type: "response.reasoning_summary_text.delta",
    item_id: "rs_status",
    summary_index: 0,
    delta: "Grok processing",
  });

  assert.equal(privateReasoning, null);
  assert.equal(wrongMarker, null);
  assert.equal(publicStatus.switchboard_public_status, undefined);
  assert.match(publicStatus.item.id, /^item_ext_/);
  assert.equal(publicStatus.item.encrypted_content, null);
  assert.equal(publicDelta.item_id, publicStatus.item.id);
  assert.doesNotMatch(JSON.stringify([publicStatus, publicDelta]), /must-drop|rs_status/);
});

test("official collaboration envelopes stay native while external models receive text", () => {
  const envelope = {
    type: "agent_message",
    id: "amsg_internal",
    author: "/root",
    recipient: "/root/worker",
    content: [
      { type: "input_text", text: "Message Type: NEW_TASK\nPayload:\n" },
      { type: "encrypted_content", encrypted_content: "reply WORKER_OK" },
    ],
  };

  const official = normalizeExternalItemsForOfficial([envelope]);
  assert.equal(official.changed, false);
  assert.deepEqual(official.input, [envelope]);

  const { clean } = prepareXaiBody({ model: "grok-4.6", input: [envelope], tools: [] });
  assert.deepEqual(clean.input, [{
    type: "message",
    role: "user",
    content: [{ type: "input_text", text: "Message Type: NEW_TASK\nPayload:\nreply WORKER_OK" }],
  }]);

  assert.doesNotMatch(JSON.stringify(clean), /encrypted_content|amsg_internal/);
});

test("official collaboration retains opaque task payload and identity beside external history", () => {
  const envelope = {
    type: "agent_message",
    id: "amsg_native_payload",
    author: "/root",
    recipient: "/root/opaque_body_canary",
    content: [
      { type: "input_text", text: "Message Type: NEW_TASK\nTask name: /root/opaque_body_canary\nSender: /root\nPayload:\n" },
      { type: "encrypted_content", encrypted_content: `gAAAAABqfqhg${"Z".repeat(100)}` },
    ],
  };
  const preimage = structuredClone(envelope);
  const native = normalizeExternalItemsForOfficial([envelope]);
  assert.equal(native.changed, false);
  assert.deepEqual(native.input, [preimage]);

  const mixed = normalizeExternalItemsForOfficial([
    { type: "reasoning", id: "rs_ext_previous", summary: [] },
    { type: "message", id: "msg_ext_previous", role: "assistant", content: [] },
    envelope,
  ]);
  assert.equal(mixed.changed, true);
  assert.deepEqual(mixed.input, [
    { type: "message", role: "assistant", content: [] },
    preimage,
  ]);
  assert.deepEqual(envelope, preimage);
});

test("xAI worker compact cleans internal Codex events and succeeds without schema rejection", async (t) => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "codex-xai-compact-test-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const tokenCommand = path.join(fixture, "token.sh");
  await writeFile(tokenCommand, "#!/bin/sh\nprintf test-token", { mode: 0o700 });
  await chmod(tokenCommand, 0o700);

  const receivedCompactBodies = [];
  const receivedResponsesBodies = [];
  const server = http.createServer((request, response) => {
    const chunks = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", () => {
      const body = JSON.parse(Buffer.concat(chunks).toString());
      if (request.url === "/v1/responses/compact") {
        receivedCompactBodies.push(body);
        for (const item of body.input || []) {
          assert.equal(["turn_aborted", "compaction_trigger", "turn_context"].includes(item.type), false);
        }
        response.writeHead(200, { "content-type": "application/json" });
        response.end(JSON.stringify({
          id: "cmp_test",
          object: "response.compaction",
          model: "grok-4.6",
          output: [{ type: "compaction", id: "cmp_test", encrypted_content: "compact-token" }],
          usage: { input_tokens: 100, output_tokens: 20 },
        }));
      } else if (request.url === "/v1/responses") {
        receivedResponsesBodies.push(body);
        response.writeHead(200, { "content-type": "application/json" });
        response.end(JSON.stringify({
          id: "resp_summary",
          output: [{ type: "message", role: "assistant", content: [{ type: "output_text", text: "canonical summary text" }] }],
          usage: { input_tokens: 100, output_tokens: 20 },
        }));
      } else {
        response.writeHead(404).end();
      }
    });
  });
  const address = await listen(server);
  t.after(() => new Promise((resolve) => server.close(resolve)));

  const worker = new IsolatedWorkerClient({
    script: new URL("../src/xai-worker.mjs", import.meta.url).pathname,
    label: "xAI-compact-harness-test",
    env: {
      SWITCHBOARD_XAI_TOKEN_COMMAND: tokenCommand,
      SWITCHBOARD_XAI_HTTP_BASE: `http://127.0.0.1:${address.port}/v1`,
    },
  });
  t.after(() => worker.close());

  const result = await worker.compact({
    model: "grok-4.6",
    input: [
      { type: "turn_context", payload: { cwd: "/test" } },
      { type: "message", role: "user", content: [{ type: "input_text", text: "hello" }] },
      { type: "turn_aborted", turn_id: "turn-1", reason: "interrupted" },
      { type: "compaction_trigger" },
      { type: "message", role: "user", content: [{ type: "input_text", text: "current prompt" }] },
    ],
  });

  assert.equal(result.summary, "canonical summary text");
  assert.equal(result.provider_item.id, "cmp_test");
  assert.equal(receivedCompactBodies.length, 1);
  const sentTypes = (receivedCompactBodies[0].input || []).map((item) => item.type);
  assert.deepEqual(sentTypes, ["message", "message"]);
});

test("xAI thread session outlives its owners: closing an owner cancels only that owner's turn", async (t) => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "codex-xai-owner-test-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const tokenCommand = path.join(fixture, "token.sh");
  await writeFile(tokenCommand, "#!/bin/sh\nprintf test-token", { mode: 0o700 });
  await chmod(tokenCommand, 0o700);
  const frames = [];
  const sockets = new Set();
  const server = http.createServer((_request, response) => response.writeHead(404).end());
  const wss = new WebSocketServer({ noServer: true });
  server.on("upgrade", (request, socket, head) => {
    wss.handleUpgrade(request, socket, head, (ws) => wss.emit("connection", ws, request));
  });
  wss.on("connection", (ws) => {
    sockets.add(ws);
    ws.once("close", () => sockets.delete(ws));
    ws.on("message", (data) => {
      const frame = JSON.parse(data.toString());
      if (frame.type !== "response.create") return;
      frames.push(frame);
      const id = `resp_turn_${frames.length}`;
      ws.send(JSON.stringify({ type: "response.created", response: { id } }));
      if (JSON.stringify(frame.input || []).includes("WAIT_FOR_CANCEL")) return;
      ws.send(JSON.stringify({
        type: "response.output_item.done",
        output_index: 0,
        item: { type: "message", role: "assistant", id: `msg_${frames.length}`, content: [{ type: "output_text", text: "ok" }] },
      }));
      ws.send(JSON.stringify({ type: "response.completed", response: { id } }));
    });
  });
  const address = await listen(server);
  t.after(() => new Promise((resolve) => {
    for (const socket of sockets) socket.terminate();
    wss.close();
    server.close(resolve);
  }));
  const worker = new IsolatedWorkerClient({
    script: new URL("../src/xai-worker.mjs", import.meta.url).pathname,
    label: "xAI-owner-test",
    env: {
      SWITCHBOARD_XAI_TOKEN_COMMAND: tokenCommand,
      SWITCHBOARD_XAI_WS_URL: `ws://127.0.0.1:${address.port}/v1/responses`,
      SWITCHBOARD_XAI_AUTO_COMPACT_BYTES: "0",
    },
  });
  t.after(() => worker.close());
  const turn = (text, extra = {}) => ({
    model: "grok-4.6",
    store: false,
    input: [{ type: "message", role: "user", content: [{ type: "input_text", text }] }],
    tools: [],
    ...extra,
  });
  const sessionKey = "thread-0123456789abcdef0123456789abcdef";

  await worker.runResponses(turn("first"), { sessionKey, ownerKey: "ws-owner-1" });
  await worker.closeSession("ws-owner-1");
  const second = await worker.runResponses(turn("second", { previous_response_id: "resp_turn_1" }),
    { sessionKey, ownerKey: "ws-owner-2" });
  assert.equal(second.events.at(-1).type, "response.completed");
  assert.equal(frames[1].previous_response_id, "resp_turn_1", "the thread session kept its continuation");
  assert.equal(frames[1].prompt_cache_key, frames[0].prompt_cache_key, "the provider cache key follows the thread");

  const waiting = worker.runResponses(turn("WAIT_FOR_CANCEL"), { sessionKey, ownerKey: "ws-owner-3" });
  while (frames.length < 3) await new Promise((resolve) => setTimeout(resolve, 10));
  await worker.closeSession("ws-owner-3");
  await assert.rejects(waiting, (error) => error.code === "XAI_REQUEST_CANCELLED" && error.status === 499);

  await worker.closeSession(sessionKey);
  await assert.rejects(worker.runResponses(turn("after close", { previous_response_id: "resp_turn_2" }),
    { sessionKey, ownerKey: "ws-owner-4" }), (error) => error.code === "previous_response_not_found");
});

test("xAI thread session persists owned response IDs and frozen prefix across worker restarts", async (t) => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "codex-xai-persist-test-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const tokenCommand = path.join(fixture, "token.sh");
  const sessionDir = path.join(fixture, "xai-sessions");
  await writeFile(tokenCommand, "#!/bin/sh\nprintf test-token", { mode: 0o700 });
  await chmod(tokenCommand, 0o700);
  const frames = [];
  const sockets = new Set();
  const server = http.createServer((_request, response) => response.writeHead(404).end());
  const wss = new WebSocketServer({ noServer: true });
  server.on("upgrade", (request, socket, head) => {
    wss.handleUpgrade(request, socket, head, (ws) => wss.emit("connection", ws, request));
  });
  wss.on("connection", (ws) => {
    sockets.add(ws);
    ws.once("close", () => sockets.delete(ws));
    ws.on("message", (data) => {
      const frame = JSON.parse(data.toString());
      if (frame.type !== "response.create") return;
      frames.push(frame);
      const id = `resp_persisted_${frames.length}`;
      ws.send(JSON.stringify({ type: "response.created", response: { id } }));
      ws.send(JSON.stringify({
        type: "response.output_item.done",
        output_index: 0,
        item: { type: "message", role: "assistant", id: `msg_${frames.length}`, content: [{ type: "output_text", text: "ok" }] },
      }));
      ws.send(JSON.stringify({ type: "response.completed", response: { id } }));
    });
  });
  const address = await listen(server);
  t.after(() => new Promise((resolve) => {
    for (const socket of sockets) socket.terminate();
    wss.close();
    server.close(resolve);
  }));
  const env = {
    SWITCHBOARD_XAI_TOKEN_COMMAND: tokenCommand,
    SWITCHBOARD_XAI_WS_URL: `ws://127.0.0.1:${address.port}/v1/responses`,
    SWITCHBOARD_XAI_AUTO_COMPACT_BYTES: "0",
    SWITCHBOARD_XAI_SESSION_DIR: sessionDir,
  };
  const worker1 = new IsolatedWorkerClient({
    script: new URL("../src/xai-worker.mjs", import.meta.url).pathname,
    label: "xAI-persist-worker-1",
    env,
  });
  t.after(() => worker1.close());
  const tools = [
    { type: "function", name: "alpha_reader", description: "Read alpha", parameters: { type: "object" } },
  ];
  const sessionKey = "thread-fedcba9876543210fedcba9876543210";
  await worker1.runResponses({
    model: "grok-4.6",
    store: false,
    instructions: "stable thread instructions",
    input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "turn 1" }] }],
    tools,
  }, { sessionKey, ownerKey: "http-1" });
  await worker1.close();

  const worker2 = new IsolatedWorkerClient({
    script: new URL("../src/xai-worker.mjs", import.meta.url).pathname,
    label: "xAI-persist-worker-2",
    env,
  });
  t.after(() => worker2.close());
  const second = await worker2.runResponses({
    model: "grok-4.6",
    store: false,
    instructions: "stable thread instructions",
    previous_response_id: "resp_persisted_1",
    input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "turn 2 after restart" }] }],
    tools: [],
  }, { sessionKey, ownerKey: "http-2" });
  assert.equal(second.events.at(-1).type, "response.completed");
  assert.equal(second.tool_projection.xai_prefix_reused, true);
  assert.equal(frames[1].previous_response_id, "resp_persisted_1");
  assert.equal(frames[1].instructions, undefined);
  assert.equal(frames[1].tools, undefined);
  assert.equal(frames[1].prompt_cache_key, frames[0].prompt_cache_key);
});

