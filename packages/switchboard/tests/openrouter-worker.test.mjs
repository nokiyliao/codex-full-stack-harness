import assert from "node:assert/strict";
import { chmod, mkdtemp, rm, writeFile } from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { IsolatedWorkerClient } from "../src/worker-client.mjs";
import { parseStructuredAction, structuredActionEvents } from "../src/openrouter-worker.mjs";

function listen(server) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolve(server.address()));
  });
}

test("OpenRouter structured action parser admits exact actions and rejects plain prose", () => {
  const actions = [{
    name: "exec_command",
    parameters: {
      type: "object",
      properties: { cmd: { type: "string" } },
      required: ["cmd"],
      additionalProperties: false,
    },
  }];
  assert.deepEqual(
    parseStructuredAction(
      '[caller action requested call_id=call_two] exec_command {"cmd":"printf ok"}',
      actions,
    ),
    { kind: "tool_call", toolName: "exec_command", arguments: { cmd: "printf ok" } },
  );
  assert.deepEqual(
    parseStructuredAction(
      '執行第一步唯讀驗證。\n[caller action requested call_id=call_three] exec_command {"cmd":"printf live"}',
      actions,
    ),
    { kind: "tool_call", toolName: "exec_command", arguments: { cmd: "printf live" } },
  );
  assert.deepEqual(
    parseStructuredAction(
      '[caller action requested```html call_id=call_four] exec_command {"cmd":"printf fenced"}',
      actions,
    ),
    { kind: "tool_call", toolName: "exec_command", arguments: { cmd: "printf fenced" } },
  );
  assert.deepEqual(
    parseStructuredAction(
      '{"kind":"tool_call","tool_name":"exec_command","arguments_json":"{\\"cmd\\":\\"printf schema\\"}","text":null}',
      actions,
    ),
    { kind: "tool_call", toolName: "exec_command", arguments: { cmd: "printf schema" } },
  );
  assert.deepEqual(
    parseStructuredAction('{"cmd":"printf inferred"}', actions),
    { kind: "tool_call", toolName: "exec_command", arguments: { cmd: "printf inferred" } },
  );
  assert.throws(
    () => parseStructuredAction("final answer", actions),
    (error) => error.code === "OPENROUTER_ACTION_JSON_INVALID" && error.status === 422,
  );
  assert.deepEqual(
    parseStructuredAction("final answer", actions, { allowPlainMessage: true }),
    { kind: "message", text: "final answer" },
  );
  assert.throws(
    () => parseStructuredAction("執行第二步", actions, { allowPlainMessage: true }),
    (error) => error.code === "OPENROUTER_ACTION_JSON_INVALID" && error.status === 422,
  );
  assert.throws(
    () => parseStructuredAction('{"cmd":"printf truncated', actions, { allowPlainMessage: true }),
    (error) => error.code === "OPENROUTER_ACTION_JSON_INVALID" && error.status === 422,
  );
});

test("OpenRouter structured actions use unique response-bound item identities", () => {
  const action = { kind: "tool_call", toolName: "exec_command", arguments: { cmd: "true" } };
  const first = structuredActionEvents(action, {
    response: { id: "gen-first" },
  }).find((event) => event.item)?.item;
  const second = structuredActionEvents(action, {
    response: { id: "gen-second" },
  }).find((event) => event.item)?.item;
  assert.notEqual(first.id, second.id);
  assert.notEqual(first.call_id, second.call_id);

  const missingFirst = structuredActionEvents(action, { response: {} })
    .find((event) => event.item)?.item;
  const missingSecond = structuredActionEvents(action, { response: {} })
    .find((event) => event.item)?.item;
  assert.notEqual(missingFirst.id, missingSecond.id);
  assert.notEqual(missingFirst.call_id, missingSecond.call_id);
});

test("OpenRouter rejects arguments outside the selected action schema", () => {
  const actions = [{
    name: "read_file",
    parameters: {
      type: "object",
      properties: { path: { type: "string" } },
      required: ["path"],
      additionalProperties: false,
    },
  }];
  assert.throws(
    () => parseStructuredAction(
      '{"kind":"tool_call","tool_name":"read_file","arguments":{"path":42}}',
      actions,
    ),
    (error) => error.code === "OPENROUTER_ACTION_SCHEMA_INVALID" && error.status === 422,
  );
});

test("OpenRouter worker keeps credentials local and streams provider-neutral tools", async (t) => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "codex-openrouter-worker-test-"));
  const keyPath = path.join(fixture, "openrouter-api-key.secret");
  await writeFile(keyPath, "test-openrouter-key\n", { mode: 0o600 });
  await chmod(keyPath, 0o600);
  t.after(() => rm(fixture, { recursive: true, force: true }));

  const requests = [];
  const server = http.createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks).toString());
    requests.push({ authorization: request.headers.authorization, body });
    if (request.url === "/chat/completions") {
      const actionSurfaceLine = String(body.messages?.[0]?.content || "")
        .split("\n")
        .find((line) => line.startsWith("AVAILABLE_ACTIONS="));
      const actionSurface = JSON.parse(actionSurfaceLine.slice("AVAILABLE_ACTIONS=".length));
      const repairFixture = JSON.stringify(body.messages).includes("repair truncated action");
      const repairRequest = String(body.messages?.[0]?.content || "")
        .includes("OPENROUTER_STRUCTURED_ACTION_REPAIR_V1");
      const content = repairFixture && !repairRequest
        ? '{"cmd":"printf truncated'
        : JSON.stringify({
            kind: "tool_call",
            tool_name: actionSurface[0].name,
            arguments: {},
          });
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({
        id: "chat_openrouter_private",
        choices: [{ message: { content } }],
        usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
      }));
      return;
    }
    if (body.stream === false) {
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({
        id: "resp_compact_private",
        output_text: "portable OpenRouter summary",
        usage: { input_tokens: 3, output_tokens: 4, total_tokens: 7 },
      }));
      return;
    }
    const structuredAction = String(body.instructions || "").includes("OPENROUTER_STRUCTURED_ACTION_V1");
    const actionSurfaceLine = String(body.instructions || "")
      .split("\n")
      .find((line) => line.startsWith("AVAILABLE_ACTIONS="));
    const actionSurface = structuredAction
      ? JSON.parse(actionSurfaceLine.slice("AVAILABLE_ACTIONS=".length))
      : [];
    const repair = String(body.instructions || "").includes("OPENROUTER_STRUCTURED_ACTION_REPAIR_V1");
    const structuredText = repair
      ? JSON.stringify({
          kind: "tool_call",
          tool_name: actionSurface[0]?.name,
          arguments: {},
        })
      : "executing now";
    const events = [
      { type: "response.created", response: { id: "resp_openrouter_private" } },
      ...(structuredAction && !repair ? [{
        type: "response.output_text.delta",
        output_index: 0,
        item_id: "msg_structured_action",
        content_index: 0,
        delta: structuredText,
      }] : []),
      {
        type: "response.output_item.done",
        output_index: 0,
        item: structuredAction ? {
          type: "message",
          id: "msg_structured_action",
          role: "assistant",
          content: [{ type: "output_text", text: structuredText }],
        } : {
          type: "function_call",
          id: "fc_openrouter_private",
          call_id: "call_openrouter",
          name: body.tools[0].name,
          arguments: "{}",
        },
      },
      {
        type: "response.completed",
        response: {
          id: "resp_openrouter_private",
          usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 },
        },
      },
    ];
    response.writeHead(200, { "content-type": "text/event-stream" });
    for (const event of events) response.write(`data: ${JSON.stringify(event)}\n\n`);
    response.end();
  });
  const address = await listen(server);
  t.after(() => new Promise((resolve) => server.close(resolve)));

  const worker = new IsolatedWorkerClient({
    script: new URL("../src/openrouter-worker.mjs", import.meta.url).pathname,
    label: "OpenRouter-test",
    env: {
      SWITCHBOARD_OPENROUTER_KEY_PATH: keyPath,
      SWITCHBOARD_OPENROUTER_RESPONSES_URL: `http://127.0.0.1:${address.port}/responses`,
      SWITCHBOARD_OPENROUTER_CHAT_URL: `http://127.0.0.1:${address.port}/chat/completions`,
    },
  });
  t.after(() => worker.close());

  const result = await worker.runResponses({
    model: "stealth/ox-alpha",
    input: [
      {
        type: "message",
        role: "developer",
        content: `historical-authority-${"z".repeat(400_000)}`,
      },
      ...Array.from({ length: 120 }, (_, index) => ({
        type: "message",
        role: index % 2 ? "assistant" : "user",
        content: `historical-${index}-${"x".repeat(900)}`,
      })),
      { type: "message", role: "user", content: "use the tool" },
    ],
    instructions: "critical top-level authority remains binding",
    tools: [{
      type: "namespace",
      name: "local",
      tools: [{
        type: "function",
        name: "inspect",
        description: "Inspect local state.",
        parameters: { type: "object", properties: {} },
      }],
    }],
    reasoning: { effort: "max", summary: "detailed" },
  }, { sessionKey: "openrouter-test" });

  assert.equal(requests[0].authorization, "Bearer test-openrouter-key");
  assert.equal(requests[0].body.model, "stealth/ox-alpha");
  assert.equal(requests[0].body.stream, false);
  assert.equal(requests[0].body.reasoning.effort, "high");
  assert.deepEqual(requests[0].body.response_format, { type: "json_object" });
  assert.deepEqual(requests[0].body.plugins, [{ id: "response-healing" }]);
  assert.match(requests[0].body.messages[0].content, /OPENROUTER_STRUCTURED_ACTION_V1/);
  assert.match(requests[0].body.messages[0].content, /critical top-level authority remains binding/);
  assert.doesNotMatch(JSON.stringify(requests[0].body.messages), /historical-authority-/);
  assert.ok(Buffer.byteLength(JSON.stringify(requests[0].body.messages)) < 70 * 1024);
  assert.match(JSON.stringify(requests[0].body.messages), /use the tool/);
  assert.doesNotMatch(JSON.stringify(requests[0].body.messages), /historical-119-/);
  const call = result.events.find((event) => event.item?.type === "function_call");
  assert.equal(call.item.namespace, "local");
  assert.equal(call.item.name, "inspect");
  assert.equal(result.events.at(-1).type, "response.completed");
  assert.deepEqual(result.events.at(-1).response.usage, {
    input_tokens: 1,
    input_tokens_details: null,
    output_tokens: 1,
    output_tokens_details: null,
    total_tokens: 2,
  });
  assert.equal(result.tool_projection.namespace_count, 1);
  assert.equal(result.tool_projection.structured_action_repair_count, 0);
  assert.doesNotMatch(JSON.stringify(requests[0].body), /test-openrouter-key/);

  const repaired = await worker.runResponses({
    model: "stealth/ox-alpha",
    input: [
      { type: "message", role: "user", content: "repair truncated action" },
      {
        type: "function_call",
        name: "inspect",
        call_id: "call_previous",
        arguments: "{}",
      },
      { type: "function_call_output", call_id: "call_previous", output: "previous result" },
    ],
    instructions: "repair fixture authority",
    tools: [{
      type: "function",
      name: "inspect",
      description: "Inspect local state.",
      parameters: { type: "object", properties: {} },
    }],
  }, { sessionKey: "openrouter-repair-test" });
  assert.equal(repaired.tool_projection.structured_action_repair_count, 1);
  assert.equal(repaired.events.find((event) => event.item?.type === "function_call")?.item.name, "inspect");
  assert.match(requests[2].body.messages[0].content, /OPENROUTER_STRUCTURED_ACTION_REPAIR_V1/);

  const compacted = await worker.compact({
    model: "stealth/ox-alpha",
    input: [{ type: "message", role: "user", content: "retain this" }],
  });
  assert.equal(compacted.summary, "portable OpenRouter summary");
  assert.equal(requests[3].body.stream, false);
  assert.deepEqual(requests[3].body.tools, []);

  const currentContract = await worker.runResponses({
    model: "stealth/ox-alpha",
    input: [
      { type: "message", role: "developer", content: "current input authority remains binding" },
      { type: "message", role: "user", content: "inspect current state" },
      {
        type: "additional_tools",
        id: "tools_current_codex_contract",
        tools: [{
          type: "function",
          name: "inspect",
          description: "Inspect local state.",
          parameters: { type: "object", properties: {} },
        }],
      },
    ],
    tools: [],
  }, { sessionKey: "openrouter-current-contract-test" });
  assert.match(requests[4].body.messages[0].content, /current input authority remains binding/);
  assert.doesNotMatch(JSON.stringify(requests[4].body.messages), /additional_tools/);
  assert.equal(currentContract.tool_projection.additional_tool_count, 1);
  assert.equal(currentContract.events.at(-1).type, "response.completed");

  await chmod(keyPath, 0o644);
  await assert.rejects(worker.runResponses({
    model: "stealth/ox-alpha",
    input: [{ type: "message", role: "user", content: "must fail closed" }],
    tools: [],
  }, { sessionKey: "openrouter-insecure-key" }), (error) => {
    assert.equal(error.code, "OPENROUTER_CREDENTIAL_PERMISSIONS");
    assert.equal(error.status, 503);
    assert.doesNotMatch(error.message, /test-openrouter-key/);
    return true;
  });
});
