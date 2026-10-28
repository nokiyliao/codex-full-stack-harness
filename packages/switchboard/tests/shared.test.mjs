import assert from "node:assert/strict";
import test from "node:test";

import {
  canonicalCompactionMarkerPrefix,
  createCanonicalCompaction,
  decodeCanonicalCompaction,
  normalizeCompactions,
  trustedProviderStateMarker,
} from "../src/history-neutral.mjs";
import {
  decodeBody,
  encodeJsonBody,
  endpointFromPath,
  externalRequestBody,
  externalThreadSessionKey,
  officialRequestHeaders,
  officialResponseObservation,
  officialRequestCorrelation,
  routeForModel,
  safeLog,
  WEB_MODELS,
} from "../src/shared.mjs";
import {
  DEFAULT_MAX_EXTERNAL_HISTORY_BYTES,
  boundExternalHistory,
  boundedExternalInstructions,
  flattenTools,
  normalizeHistory,
  prepareXaiBody,
  restoreEvent,
} from "../src/tool-bridge.mjs";
import { execFile } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

test("request correlation is bounded caller evidence, not authorization", () => {
  const id = "01a13501-d183-7833-97c8-a458039be661";
  assert.deepEqual(officialRequestCorrelation({ previous_response_id: "resp_parent",
    input: [{ type: "custom_tool_call_output", output: "secret" }] },
  { session_id: id, authorization: "secret" }), {
    source: "caller_request_not_authority", client_thread_id: null, client_session_id: id,
    previous_response_id: "resp_parent", last_input_type: "custom_tool_call_output",
  });
  const native = officialRequestCorrelation({}, { "thread-id": id, "session-id": id });
  assert.equal(native.client_thread_id, id);
  assert.equal(native.client_session_id, id);
  assert.equal(officialRequestCorrelation({}, { "session-id": id, session_id: "different" }).client_session_id, null);
  for (const value of [undefined, null, {}, [id], "private message", "x".repeat(1000)]) {
    const observed = officialRequestCorrelation({ previous_response_id: value,
      input: [{ type: value }] }, { session_id: value });
    assert.equal(observed.client_session_id, null);
    assert.equal(observed.previous_response_id, null);
    assert.equal(observed.last_input_type, null);
  }
});

test("cache writes preserve explicit values, absence and contradictions", () => {
  const observe = (details) => officialResponseObservation({ type: "response.completed",
    response: { usage: { input_tokens: 100, output_tokens: 20, input_tokens_details: details } } });
  const valid = observe({ cached_tokens: 40, cache_write_tokens: 50,
    cache_write_tokens_details: { ttl_30m_tokens: 30, ttl_12h_tokens: 20 } });
  assert.equal(valid.cache_write_tokens, 50);
  assert.equal(valid.cache_write_30m_tokens, 30);
  assert.equal(valid.cache_write_12h_tokens, 20);
  assert.equal(valid.uncached_input_tokens, 60);
  assert.equal(valid.usage_status, "REPORTED");
  assert.equal(observe({ cached_tokens: 40 }).cache_write_tokens, null);
  assert.equal(observe({ cached_tokens: 40, cache_write_tokens: 0 }).cache_write_tokens, 0);
  assert.equal(observe({ cached_tokens: 40, cache_write_tokens: 70 }).usage_status, "INCONSISTENT");
  assert.equal(observe({ cache_write_tokens: 20, cache_write_tokens_details:
    { ttl_30m_tokens: 15, ttl_12h_tokens: 10 } }).usage_status, "INCONSISTENT");
});

test("official usage observations preserve missing and inconsistent values", () => {
  const response = { id: "resp_canary", model: "gpt-6-astra", service_tier: "default",
    input: "secret", output: "secret", usage: { input_tokens: 100, output_tokens: 20,
      total_tokens: 120, input_tokens_details: { cached_tokens: 80 },
      output_tokens_details: { reasoning_tokens: 15 } } };
  for (const type of ["response.completed", "response.failed", "response.incomplete"]) {
    const observed = officialResponseObservation({ type, response });
    assert.equal(observed.usage_status, "REPORTED");
    assert.equal(observed.terminal_event, type);
    assert.equal(observed.uncached_input_tokens, 20);
    assert.equal(observed.total_tokens, 120);
    assert.equal(observed.reasoning_tokens, 15);
    assert.equal(JSON.stringify(observed).includes("secret"), false);
  }
  assert.equal(officialResponseObservation({ type: "response.created", response }), null);
  assert.equal(officialResponseObservation({ type: "error", response }), null);
  const missing = officialResponseObservation({ type: "response.completed", response: {} });
  assert.equal(missing.usage_status, "PARTIAL_OR_MISSING");
  assert.equal(missing.input_tokens, null);
  assert.equal(missing.cached_input_tokens, null);
  assert.equal(missing.model, null);
  for (const usage of [{ input_tokens: 100, output_tokens: 20, total_tokens: 135 },
    { input_tokens: 100, input_tokens_details: { cached_tokens: 101 } },
    { output_tokens: 20, output_tokens_details: { reasoning_tokens: 21 } }]) {
    assert.equal(officialResponseObservation({ type: "response.completed", response: { usage } }).usage_status, "INCONSISTENT");
  }
  for (const value of [-1, 0.5, "100", Infinity, Number.MAX_SAFE_INTEGER + 1]) {
    assert.equal(officialResponseObservation({ type: "response.completed",
      response: { usage: { input_tokens: value } } }).input_tokens, null);
  }
  assert.equal(officialResponseObservation({ type: "response.completed",
    response: { id: "private message with spaces" } }).response_id, null);
});

test("request logs retain workflow alias without admitting request contents", (t) => {
  const lines = [];
  t.mock.method(process.stdout, "write", (chunk) => { lines.push(String(chunk)); return true; });
  for (const alias of ["nokiy", "nokiy-direct", "nokiy-efficient"]) {
    safeLog({ request_id: "bound-request", model: "gpt-6-astra", requested_alias: alias,
      prompt: "private prompt", authorization: "private credential", input: ["private input"] });
    const record = JSON.parse(lines.at(-1));
    assert.equal(record.requested_alias, alias);
    assert.equal(record.model, "gpt-6-astra");
    assert.equal(record.request_id, "bound-request");
    assert.deepEqual(Object.keys(record).sort(),
      ["model", "request_id", "requested_alias", "timestamp"]);
  }
  safeLog({ model: "gpt-6.1-sol" });
  assert.equal(Object.hasOwn(JSON.parse(lines.at(-1)), "requested_alias"), false);
});

import {
  _resetSpawnPayloadCacheForTests,
  _simulateNewProcessForTests,
  rememberSpawnPayloads,
} from "../src/spawn-payload-cache.mjs";

test("zstd inspection round-trips without changing the JSON", () => {
  const value = { model: "gpt-5.6-sol", input: [{ type: "message", role: "user", content: "hello" }] };
  const encoded = encodeJsonBody(value, "zstd");
  assert.deepEqual(JSON.parse(decodeBody(encoded, "zstd")), value);
  assert.notEqual(encoded.toString("hex"), Buffer.from(JSON.stringify(value)).toString("hex"));
});

test("official headers retain credentials and remove hop-by-hop fields", () => {
  const headers = officialRequestHeaders({
    authorization: "Bearer official-canary",
    "chatgpt-account-id": "account-canary",
    "x-oai-attestation": "attestation-canary",
    connection: "keep-alive, x-remove-me",
    "x-remove-me": "gone",
    host: "127.0.0.1:43130",
    "content-length": "999",
    "x-client-request-id": "request-canary",
  }, 42);
  assert.equal(headers.authorization, "Bearer official-canary");
  assert.equal(headers["chatgpt-account-id"], "account-canary");
  assert.equal(headers["x-oai-attestation"], "attestation-canary");
  assert.equal(headers["x-client-request-id"], "request-canary");
  assert.equal(headers["content-length"], "42");
  assert.equal(headers.host, undefined);
  assert.equal(headers.connection, undefined);
  assert.equal(headers["x-remove-me"], undefined);
});

test("external body strips hosted tools and OpenAI-only request metadata", () => {
  const clean = externalRequestBody({
    model: "grok-4.6",
    service_tier: "priority",
    prompt_cache_key: "secret-cache-key",
    safety_identifier: "secret-safety-id",
    client_metadata: { thread_id: "secret-thread" },
    tools: [
      { type: "image_generation" },
      { type: "web_search" },
      { type: "function", name: "local_tool", parameters: { type: "object" } },
    ],
  });
  assert.deepEqual(clean.tools.map((tool) => tool.type), ["function"]);
  assert.equal(clean.service_tier, undefined);
  assert.equal(clean.prompt_cache_key, undefined);
  assert.equal(clean.safety_identifier, undefined);
  assert.equal(clean.client_metadata, undefined);
});

test("capability path retains the backend-api and v1 compatibility markers", () => {
  assert.equal(
    endpointFromPath("/capability/backend-api/codex/v1/realtime/calls", "capability"),
    "/realtime/calls",
  );
  assert.throws(
    () => endpointFromPath("/wrong/backend-api/codex/v1/responses", "capability"),
    (error) => error.code === "SWITCHBOARD_NOT_READY",
  );
});

test("provider selection isolates Web while Native, Full-Harness, and Grok retain their routes", () => {
  const official = new Set(["gpt-5.6-sol", "gpt-5.4"]);
  assert.equal(routeForModel("gpt-5.6-sol", official), "openai");
  assert.equal(routeForModel("gpt-5.4", official), "openai");
  for (const model of WEB_MODELS) assert.equal(routeForModel(model, official), "web");
  assert.throws(() => routeForModel("chatgpt-web/unknown", official), (error) => error.code === "MODEL_ROUTE_UNAVAILABLE");
  assert.equal(routeForModel("grok-4.6", official), "xai");
  assert.throws(() => routeForModel("stealth/ox-alpha", official), (error) => error.code === "MODEL_ROUTE_UNAVAILABLE");
  assert.equal(routeForModel("gemini-3.8-flash-high", official), "agy");
  assert.equal(routeForModel("grok-agy-gemini-3.8-flash-high", official), "agy");
  assert.equal(routeForModel("gemini-4-argon-high", official), "agy");
  assert.equal(routeForModel("grok-agy-gemini-4-argon-high", official), "agy");
  assert.throws(() => routeForModel("grok-4.6-typo", official), (error) => error.code === "MODEL_ROUTE_UNAVAILABLE");
});

test("canonical compaction is digest-checked and provider-neutral", () => {
  const { stateKey, item } = createCanonicalCompaction({
    model: "grok-4.6",
    route: "xai",
    summary: "Keep the official app-server and the same thread.",
  });
  assert.ok(item.encrypted_content.startsWith(canonicalCompactionMarkerPrefix));
  assert.equal(decodeCanonicalCompaction(item).state_key, stateKey);
  const providerItem = { type: "compaction", id: "cmp_xai_private", encrypted_content: "opaque-private" };
  const forXai = normalizeCompactions([item], "xai", new Map([[stateKey, { item: providerItem }]]));
  assert.deepEqual(forXai.input, [{ ...providerItem, [trustedProviderStateMarker]: true }]);
  const forOfficial = normalizeCompactions([item], "openai", new Map([[stateKey, { item: providerItem }]]));
  assert.equal(forOfficial.input[0].type, "message");
  assert.match(forOfficial.input[0].content[0].text, /same thread/);
  const tampered = structuredClone(item);
  tampered.encrypted_content += "x";
  assert.throws(() => decodeCanonicalCompaction(tampered), (error) => error.code === "INVALID_CANONICAL_COMPACTION");
});

test("namespace aliases are collision-safe and round-trip call identity", () => {
  const sourceTools = [
    {
      type: "namespace",
      name: "alpha.beta",
      tools: [{ type: "function", name: "read", parameters: { type: "object" } }],
    },
    {
      type: "namespace",
      name: "alpha_beta",
      tools: [{ type: "function", name: "read", parameters: { type: "object" } }],
    },
    { type: "tool_search" },
  ];
  const { bridge, tools } = flattenTools(sourceTools);
  assert.equal(new Set(tools.map((tool) => tool.name)).size, 3);
  const first = [...bridge.values()].find((item) => item.namespace === "alpha.beta");
  const history = normalizeHistory([{
    type: "function_call",
    namespace: "alpha.beta",
    name: "read",
    call_id: "call_1",
    arguments: "{}",
  }], bridge);
  assert.equal(history[0].name, first.alias);
  const restored = restoreEvent({
    type: "response.output_item.done",
    item: { type: "function_call", name: first.alias, call_id: "call_2", arguments: "{}" },
  }, bridge);
  assert.equal(restored.item.namespace, "alpha.beta");
  assert.equal(restored.item.name, "read");
});

test("caller tool definitions cannot be overridden by discovered history", () => {
  const declared = [{
    type: "function",
    name: "inspect",
    description: "current caller definition",
    parameters: {
      type: "object",
      properties: { path: { type: "string" } },
      required: ["path"],
      additionalProperties: false,
    },
  }];
  const discovered = [{
    type: "tool_search_output",
    tools: [{
      type: "function",
      name: "inspect",
      description: "stale discovered definition",
      parameters: {
        type: "object",
        properties: { path: { type: "number" } },
        required: ["path"],
        additionalProperties: false,
      },
    }],
  }];
  assert.throws(
    () => flattenTools(declared, discovered),
    (error) => error.code === "EXTERNAL_TOOL_IDENTITY_CONFLICT" && error.status === 409,
  );
});

test("Codex reasoning effort maps to the exact Grok 4.6 capability set", () => {
  const { clean } = prepareXaiBody({
    model: "grok-4.6",
    input: [{ type: "message", role: "user", content: "reason about this" }],
    tools: [],
    reasoning: { effort: "max", summary: "detailed", extra: "not-supported" },
  });
  assert.deepEqual(clean.reasoning, { effort: "xhigh" });
});

test("xAI projection admits only an explicit empty non-generating prewarm", () => {
  const { clean } = prepareXaiBody({
    model: "grok-4.6",
    generate: false,
    instructions: "prewarm immutable instructions",
    input: [],
    tools: [],
  });
  assert.equal(clean.generate, false);
  assert.deepEqual(clean.input, []);
  assert.throws(
    () => prepareXaiBody({ model: "grok-4.6", input: [], tools: [] }),
    (error) => error.code === "EXTERNAL_CURRENT_REQUEST_MISSING",
  );
});

test("owned xAI continuation sends only authority, current scope, and omission evidence", () => {
  const { clean } = prepareXaiBody({
    model: "grok-4.6",
    previous_response_id: "resp_owned",
    input: [
      { type: "message", role: "developer", content: "preserve authority boundary" },
      { type: "message", role: "user", content: `old optional request ${"x".repeat(32 * 1024)}` },
      { type: "message", role: "assistant", content: "old optional answer" },
      { type: "message", role: "user", content: "current exact request" },
    ],
    tools: [],
  }, { allowProviderContinuation: true });
  const projected = JSON.stringify(clean.input);
  assert.match(projected, /preserve authority boundary/);
  assert.match(projected, /current exact request/);
  assert.match(projected, /external_context_omission_v1/);
  assert.doesNotMatch(projected, /old optional request|old optional answer/);
});

test("xAI projection isolates foreign compaction and preserves only trusted provider state", () => {
  const { clean } = prepareXaiBody({
    model: "grok-4.6",
    input: [
      { type: "compaction", id: "cmp_openai", encrypted_content: "foreign-openai-secret" },
      {
        type: "compaction",
        id: "cmp_xai",
        encrypted_content: "trusted-xai-state",
        [trustedProviderStateMarker]: true,
      },
      { type: "message", role: "user", content: [{ type: "input_text", text: "continue" }] },
    ],
    tools: [],
  });
  assert.equal(clean.input.length, 2);
  assert.equal(clean.input[0].type, "compaction");
  assert.equal(clean.input[0].encrypted_content, "trusted-xai-state");
  assert.equal(clean.input[0][trustedProviderStateMarker], undefined);
  assert.doesNotMatch(JSON.stringify(clean), /foreign-openai-secret/);
});

test("external model projection bounds caller guidance and keeps tool search", () => {
  const repeatedGuidance = `${"preserve this authority constraint ".repeat(220)}\n\n`;
  const { clean } = prepareXaiBody({
    model: "grok-4.6",
    instructions: repeatedGuidance.repeat(12),
    input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "inspect the current test" }] }],
    tools: [
      ...Array.from({ length: 40 }, (_, index) => ({
        type: "function",
        name: `tool_${index}`,
        description: index === 39 ? "inspect current test" : "unrelated action",
        parameters: { type: "object" },
      })),
      { type: "tool_search" },
    ],
  });
  assert.ok(clean.instructions.length < 25 * 1024);
  // Every caller tool is kept, in the caller's order, whatever the message says.
  assert.equal(clean.tools.length, 41);
  assert.ok(clean.tools.some((tool) => tool.name.startsWith("tool_search__")));
  const names = clean.tools.map((tool) => tool.name);
  assert.ok(names.findIndex((name) => name.startsWith("tool_0__")) < names.findIndex((name) => name.startsWith("tool_39__")));
});

test("unique oversized caller guidance uses whole-block deterministic omission", () => {
  const guidance = [
    `ordinary ${"x".repeat(30 * 1024)}`,
    "Never expose credentials or expand trading authority.",
    "Complete the exact current task.",
  ].join("\n\n");
  const first = boundedExternalInstructions(guidance);
  const second = boundedExternalInstructions(guidance);
  assert.equal(first, second);
  assert.ok(first.length < 25 * 1024);
  assert.match(first, /Never expose credentials or expand trading authority/);
  assert.match(first, /external_instruction_omission_v1/);
  assert.doesNotMatch(first, /ordinary x{100}/);
});

test("external tool quarantine removes a broken handler without corrupting namespace aliases", () => {
  const { clean, bridge } = prepareXaiBody({
    model: "grok-4.6",
    input: [{ type: "message", role: "user", content: "run bash or read a file" }],
    tools: [{
      type: "namespace",
      name: "mcp__codex_apps__devspace_utm_9013_stable",
      tools: [
        { type: "function", name: "_bash", description: "run bash", parameters: { type: "object" } },
        { type: "function", name: "_read", description: "read a file", parameters: { type: "object" } },
      ],
    }],
  }, {
    toolQuarantine: [{ namespace: "mcp__codex_apps__devspace_utm_9013_stable", name: "_bash" }],
  });

  assert.equal([...bridge.values()].some((definition) => definition.responseName === "_bash"), false);
  assert.equal([...bridge.values()].some((definition) => definition.responseName === "_read"), true);
  assert.equal(clean.tools.some((tool) => tool.name.includes("_bash")), false);
  assert.equal(clean.tools.some((tool) => tool.name.includes("_read")), true);
});

function crowdedDesktopCatalog({ userText, toolChoice }) {
  const mcpTools = [
    { type: "function", name: "node_repl", description: "Run JavaScript in a REPL.", parameters: { type: "object" } },
    ...Array.from({ length: 8 }, (_, index) => ({
      type: "function",
      name: `recent_tool_${index + 1}`,
      description: `Recent tool ${index + 1}`,
      parameters: { type: "object" },
    })),
  ];
  return {
    model: "grok-4.6",
    input: [
      {
        type: "tool_search_output",
        tools: [
          { type: "custom", name: "exec", description: "Run nested Codex tools." },
          ...mcpTools,
        ],
      },
      { type: "function_call", name: "node_repl", call_id: "call_repl", arguments: "{\"code\":\"1+1\"}" },
      { type: "function_call_output", call_id: "call_repl", output: "2" },
      { type: "message", role: "user", content: userText },
    ],
    tools: [
      { type: "tool_search", description: "Discover tools." },
      { type: "custom", name: "exec", description: "Run JavaScript code to orchestrate tool calls." },
      { type: "namespace", name: "mcp__codex_apps__devspace", tools: mcpTools },
      {
        type: "namespace",
        name: "devspace_utm_9013",
        tools: [{ type: "function", name: "bash", description: "Run a shell command.", parameters: { type: "object" } }],
      },
      {
        type: "namespace",
        name: "devspace_local_lab",
        tools: [{ type: "function", name: "bash", description: "Run a shell command.", parameters: { type: "object" } }],
      },
    ],
    tool_choice: toolChoice,
  };
}

function assertExecAdapters(clean, bridge, expectedNamespace = null) {
  const names = clean.tools.map((tool) => tool.name);
  assert.ok(names.includes("exec_command"));
  assert.ok(names.includes("write_stdin"));
  assert.equal(names.some((name) => /^exec__/.test(name)), false);
  assert.equal(names.some((name) => /node_repl/i.test(name)), false);
  assert.equal(names.some((name) => /devspace/i.test(name)), false);
  assert.equal(new Set(names).size, names.length);
  const restored = restoreEvent({
    type: "response.output_item.done",
    item: {
      type: "function_call",
      name: "exec_command",
      call_id: "call_exec_adapter",
      arguments: JSON.stringify({ cmd: "pwd", workdir: "/tmp" }),
    },
  }, bridge);
  assert.equal(restored.item.type, "custom_tool_call");
  assert.equal(restored.item.name, "exec");
  assert.equal(restored.item.namespace, expectedNamespace || undefined);
  assert.match(restored.item.input, /tools\.exec_command/);
  assert.match(restored.item.input, /"cmd":"pwd"/);
  assert.match(restored.item.input, /"workdir":"\/tmp"/);
}

test("custom exec projects structured exec_command and write_stdin adapters", () => {
  const { clean, bridge } = prepareXaiBody({
    model: "grok-4.6",
    input: [{
      type: "message",
      role: "user",
      content: "Run exec_command in the worktree and use write_stdin if it returns a session ID.",
    }],
    tools: [{
      type: "custom",
      name: "exec",
      description: "Run JavaScript code to orchestrate tool calls.",
    }],
  });

  const execDefinition = [...bridge.values()]
    .find((definition) => definition.responseName === "exec_command");
  const waitDefinition = [...bridge.values()]
    .find((definition) => definition.responseName === "write_stdin");
  assert.ok(execDefinition);
  assert.ok(waitDefinition);
  assert.equal(execDefinition.alias, "exec_command");
  assert.equal(waitDefinition.alias, "write_stdin");
  assert.ok(clean.tools.some((tool) => tool.name === execDefinition.alias));
  assert.ok(clean.tools.some((tool) => tool.name === waitDefinition.alias));
  assertExecAdapters(clean, bridge);
});

test("crowded Desktop catalog keeps exec adapters and drops competing shells", () => {
  const { clean, bridge } = prepareXaiBody(crowdedDesktopCatalog({
    userText: "Run pwd in the worktree.",
    toolChoice: "required",
  }));
  assertExecAdapters(clean, bridge);
  assert.equal(clean.tool_choice, "required");
});

test("namespaced-only Desktop exec projects adapters and restores its namespace", () => {
  const desktop = crowdedDesktopCatalog({
    userText: "Use structured exec_command to run pwd in the worktree.",
    toolChoice: { type: "custom", namespace: "codex", name: "exec" },
  });
  desktop.tools = desktop.tools.filter((tool) => tool.type !== "custom");
  desktop.input[0].tools = desktop.input[0].tools.filter((tool) => tool.type !== "custom");
  desktop.tools.push({
    type: "namespace",
    name: "codex",
    tools: [{
      type: "custom",
      name: "exec",
      description: "Run JavaScript code to orchestrate Codex tools.",
    }],
  });

  const { clean, bridge } = prepareXaiBody(desktop);
  assertExecAdapters(clean, bridge, "codex");
  assert.deepEqual(clean.tool_choice, { type: "function", name: "exec_command" });
});

test("Desktop custom exec namespace field uses the same adapter path", () => {
  const desktop = crowdedDesktopCatalog({
    userText: "Use structured exec_command to run pwd in the worktree.",
    toolChoice: { type: "custom", namespace: "codex", name: "exec" },
  });
  desktop.tools = desktop.tools.filter((tool) => tool.type !== "custom");
  desktop.input[0].tools = desktop.input[0].tools.filter((tool) => tool.type !== "custom");
  desktop.tools.push({
    type: "custom",
    namespace: "codex",
    name: "exec",
    description: "Run JavaScript code to orchestrate Codex tools.",
  });

  const { clean, bridge, diagnostics } = prepareXaiBody(desktop);
  assertExecAdapters(clean, bridge, "codex");
  assert.deepEqual(clean.tool_choice, { type: "function", name: "exec_command" });
  assert.equal(diagnostics.custom_exec_top_level, false);
  assert.equal(diagnostics.custom_exec_namespaced, true);
  assert.equal(diagnostics.exec_adapter_projected, true);
});

test("exec_command wording keeps the stable tool list, suppresses REPL, and rewrites custom tool_choice", () => {
  const { clean, bridge } = prepareXaiBody(crowdedDesktopCatalog({
    userText: "Use structured exec_command.",
    toolChoice: { type: "custom", name: "exec" },
  }));
  assertExecAdapters(clean, bridge);
  assert.ok(clean.tools.some((tool) => /tool_search/i.test(tool.name)));
  assert.deepEqual(clean.tool_choice, { type: "function", name: "exec_command" });
  // The tool list, part of the cached prompt prefix, does not depend on the message.
  const other = prepareXaiBody(crowdedDesktopCatalog({
    userText: "Summarize what changed without running anything.",
    toolChoice: { type: "custom", name: "exec" },
  }));
  assert.deepEqual(other.clean.tools, clean.tools);

  const rewritten = prepareXaiBody(crowdedDesktopCatalog({
    userText: "Use structured exec_command.",
    toolChoice: { type: "function", name: "exec" },
  }));
  assert.deepEqual(rewritten.clean.tool_choice, { type: "function", name: "exec_command" });
});

test("opaque official spawn ciphertext is not forwarded and can be recovered from spawn_agent", () => {
  _resetSpawnPayloadCacheForTests();
  const ciphertext = `gAAAAABqfqhg${"A".repeat(80)}`;
  const envelope = {
    type: "agent_message",
    recipient: "/root/grok_gateway_lifecycle",
    content: [
      { type: "input_text", text: "Message Type: NEW_TASK\nTask name: /root/grok_gateway_lifecycle\nPayload:\n" },
      { type: "encrypted_content", encrypted_content: ciphertext },
    ],
  };

  const leaked = prepareXaiBody({
    model: "grok-4.6",
    input: [
      { type: "message", role: "user", content: [{ type: "input_text", text: "<codex_delegation>parent poison</codex_delegation>" }] },
      envelope,
    ],
    tools: [],
  });
  assert.equal(leaked.clean.input.length, 1);
  assert.doesNotMatch(JSON.stringify(leaked.clean), new RegExp(ciphertext));
  assert.match(leaked.clean.input[0].content[0].text, /encrypted and unreadable/);

  rememberSpawnPayloads({
    type: "function_call",
    name: "spawn_agent",
    arguments: JSON.stringify({
      task_name: "/root/grok_gateway_lifecycle",
      message: "Fix Tura gateway lifecycle redundancy.",
    }),
  });
  const recovered = prepareXaiBody({
    model: "grok-4.6",
    input: [envelope],
    tools: [],
  });
  assert.match(recovered.clean.input[0].content[0].text, /Fix Tura gateway lifecycle redundancy/);
  assert.doesNotMatch(JSON.stringify(recovered.clean), new RegExp(ciphertext));
});

test("spawn payload cache is shared across isolated worker processes via disk", async () => {
  const cacheFile = path.join(mkdtempSync(path.join(tmpdir(), "switchboard-spawn-")), "cache.json");
  _resetSpawnPayloadCacheForTests({ persist: true, path: cacheFile });
  rememberSpawnPayloads({
    type: "response.output_item.done",
    item: {
      type: "function_call",
      name: "spawn_agent",
      arguments: JSON.stringify({
        task_name: "/root/gemini_dispatch",
        message: "Inspect Gemini dispatch without inventing a payload.",
      }),
    },
  });
  rememberSpawnPayloads({
    type: "custom_tool_call",
    name: "followup_task",
    input: JSON.stringify({
      target: "/root/gemini_dispatch",
      message: "Also return the recovered follow-up.",
    }),
  });
  _simulateNewProcessForTests();
  const ciphertext = `gAAAAABqfqhg${"B".repeat(80)}`;
  const recovered = prepareXaiBody({
    model: "grok-4.6",
    input: [
      {
        type: "agent_message",
        recipient: "/root/gemini_dispatch",
        content: [
          { type: "input_text", text: "Message Type: NEW_TASK\nTask name: /root/gemini_dispatch\nPayload:\n" },
          { type: "encrypted_content", encrypted_content: ciphertext },
        ],
      },
      {
        type: "agent_message",
        recipient: "/root/gemini_dispatch",
        content: [
          { type: "input_text", text: "Message Type: MESSAGE\nTask name: /root/gemini_dispatch\nPayload:\n" },
          { type: "encrypted_content", encrypted_content: `${ciphertext}C` },
        ],
      },
    ],
    tools: [],
  });
  assert.match(recovered.clean.input[0].content[0].text, /Inspect Gemini dispatch without inventing a payload/);
  assert.match(recovered.clean.input[1].content[0].text, /Also return the recovered follow-up/);
  assert.doesNotMatch(JSON.stringify(recovered.clean), /gAAAAA/);

  const workerScript = fileURLToPath(new URL("../src/spawn-payload-cache.mjs", import.meta.url));
  const { stdout } = await execFileAsync(process.execPath, [
    "--input-type=module",
    "-e",
    `process.env.SWITCHBOARD_SPAWN_PAYLOAD_CACHE_PATH = ${JSON.stringify(cacheFile)};
     const { lookupSpawnPayload } = await import(${JSON.stringify(workerScript)});
     process.stdout.write(lookupSpawnPayload("/root/gemini_dispatch") || "");`,
  ]);
  assert.match(stdout, /Inspect Gemini dispatch without inventing a payload/);
  _resetSpawnPayloadCacheForTests();
});

test("encrypted spawn_agent still creates the disk cache file", () => {
  const cacheFile = path.join(mkdtempSync(path.join(tmpdir(), "switchboard-spawn-")), "cache.json");
  _resetSpawnPayloadCacheForTests({ persist: true, path: cacheFile });
  rememberSpawnPayloads({
    type: "function_call",
    name: "spawn_agent",
    arguments: JSON.stringify({
      task_name: "/root/encrypted_only",
      message: `gAAAAABqfqhg${"D".repeat(80)}`,
    }),
  });
  assert.equal(existsSync(cacheFile), true);
  const parsed = JSON.parse(readFileSync(cacheFile, "utf8"));
  assert.equal(parsed.v, 1);
  assert.equal(parsed.entries["/root/encrypted_only"], undefined);
  _resetSpawnPayloadCacheForTests();
});

test("empty history still projects exec adapters for top-level custom exec", () => {
  const { clean, bridge, diagnostics } = prepareXaiBody({
    model: "grok-4.6",
    input: [{ type: "message", role: "user", content: "pwd" }],
    tools: [{ type: "custom", name: "exec", description: "Run Codex tools." }],
  });
  assert.ok(clean.tools.some((tool) => tool.name === "exec_command"));
  assert.ok(clean.tools.some((tool) => tool.name === "write_stdin"));
  assert.equal(diagnostics.exec_adapter_projected, true);
  const restored = restoreEvent({
    type: "response.output_item.done",
    item: {
      type: "function_call",
      name: "exec_command",
      call_id: "call_fresh",
      arguments: JSON.stringify({ cmd: "pwd" }),
    },
  }, bridge);
  assert.equal(restored.item.type, "custom_tool_call");
  assert.equal(restored.item.name, "exec");
});

test("empty history projects function-type exec as exec_command", () => {
  const { clean, diagnostics } = prepareXaiBody({
    model: "grok-4.6",
    input: [{ type: "message", role: "user", content: "pwd" }],
    tools: [{ type: "function", name: "exec", description: "Run a command." }],
  });
  assert.ok(clean.tools.some((tool) => tool.name === "exec_command"));
  assert.equal(diagnostics.exec_adapter_projected, true);
});

test("large history without NEW_TASK is hard-capped below the byte budget", () => {
  const poison = `gAAAAABqfqhg${"E".repeat(80)}`;
  const bloated = Array.from({ length: 80 }, (_, index) => ({
    type: "message",
    role: "user",
    content: [{ type: "input_text", text: `parent blob ${index} ${"x".repeat(8000)}` }],
  }));
  bloated.push({
    type: "message",
    role: "user",
    content: [{ type: "input_text", text: "current turn only" }],
  });
  const { clean } = prepareXaiBody({
    model: "grok-4.6",
    input: bloated,
    tools: [],
  });
  const bytes = Buffer.byteLength(JSON.stringify(clean.input));
  assert.ok(bytes <= DEFAULT_MAX_EXTERNAL_HISTORY_BYTES, bytes);
  assert.match(JSON.stringify(clean.input), /current turn only/);
  assert.match(JSON.stringify(clean.input), /external_context_omission_v1/);
  assert.doesNotMatch(JSON.stringify(clean.input), new RegExp(poison));
  const bounded = boundExternalHistory(bloated);
  assert.ok(Buffer.byteLength(JSON.stringify(bounded)) <= DEFAULT_MAX_EXTERNAL_HISTORY_BYTES);
});

test("bounded history preserves a deictic request and its exact preceding exchange", () => {
  const referent = "UNRESOLVED_REFERENT_7c4d31";
  const history = [
    ...Array.from({ length: 30 }, (_, index) => ({
      type: "message",
      role: "user",
      content: `old blob ${index} ${"x".repeat(900)}`,
    })),
    { type: "message", role: "user", content: "Review the unresolved files." },
    { type: "message", role: "assistant", content: `The unresolved set is ${referent}.` },
    { type: "message", role: "user", content: "Fix these unresolved changes now." },
  ];
  const bounded = boundExternalHistory(history, 4 * 1024);
  const encoded = JSON.stringify(bounded);
  assert.match(encoded, new RegExp(referent));
  assert.match(encoded, /Fix these unresolved changes now/);
  assert.match(encoded, /external_context_omission_v1/);
  assert.ok(Buffer.byteLength(encoded) <= 4 * 1024);
});

test("continuation keeps the previous objective and selection is deterministic", () => {
  const history = [
    { type: "message", role: "developer", content: "Never mutate live authority." },
    ...Array.from({ length: 20 }, (_, index) => ({
      type: "message", role: "user", content: `noise ${index} ${"n".repeat(600)}`,
    })),
    { type: "message", role: "user", content: "Implement objective EXACT_OBJECTIVE_91." },
    { type: "message", role: "assistant", content: "I prepared EXACT_OBJECTIVE_91." },
    { type: "message", role: "user", content: "Continue the previous task." },
  ];
  const first = boundExternalHistory(history, 5 * 1024);
  const second = boundExternalHistory(history, 5 * 1024);
  assert.deepEqual(first, second);
  assert.match(JSON.stringify(first), /EXACT_OBJECTIVE_91/);
  assert.match(JSON.stringify(first), /Never mutate live authority/);
});

test("tool calls and outputs are retained atomically for the active turn", () => {
  const history = [
    { type: "message", role: "user", content: "Inspect the fixture." },
    { type: "message", role: "assistant", content: "I will inspect it." },
    { type: "function_call", call_id: "call_fixture", name: "read", arguments: "{}" },
    { type: "function_call_output", call_id: "call_fixture", output: "fixture result" },
  ];
  const bounded = boundExternalHistory(history, 4 * 1024);
  const types = bounded.filter((item) => item.call_id === "call_fixture").map((item) => item.type);
  assert.deepEqual(types, ["function_call", "function_call_output"]);
});

test("missing deictic referent and oversized exact request fail closed", () => {
  assert.throws(
    () => boundExternalHistory([
      { type: "message", role: "user", content: "Continue the previous task." },
    ], 2048),
    (error) => error.code === "EXTERNAL_REFERENT_CONTEXT_MISSING",
  );
  assert.throws(
    () => boundExternalHistory([
      { type: "message", role: "user", content: `exact ${"z".repeat(4096)}` },
    ], 1024),
    (error) => error.code === "EXTERNAL_MANDATORY_CONTEXT_TOO_LARGE",
  );
});

test("external history strips private fields and rejects mandatory credentials", () => {
  const bounded = boundExternalHistory([
    { type: "message", role: "user", content: "Inspect public state.", client_metadata: "private" },
  ], 2048);
  assert.doesNotMatch(JSON.stringify(bounded), /client_metadata|private/);
  assert.throws(
    () => boundExternalHistory([
      { type: "message", role: "user", content: "api_key=abcdefghijklmnop123456" },
    ], 2048),
    (error) => error.code === "EXTERNAL_SENSITIVE_CONTEXT_DETECTED",
  );
});

test("external thread session key follows the caller thread and context window, as a digest only", () => {
  const thread = "019e0000-aaaa-7000-8000-00000000000a";
  const key = externalThreadSessionKey({ client_metadata: { thread_id: thread } });
  assert.match(key, /^thread-[a-f0-9]{32}$/);
  assert.equal(key.includes(thread), false);
  const turn = (value) => ({ client_metadata: { "x-codex-turn-metadata": JSON.stringify(value) } });
  assert.equal(externalThreadSessionKey(turn({ thread_id: thread, turn_id: "turn-1" })), key);
  assert.equal(externalThreadSessionKey({}, { "thread-id": thread }), key);
  assert.equal(externalThreadSessionKey({}, { session_id: thread }), key);
  assert.equal(externalThreadSessionKey({ prompt_cache_key: thread }), key);
  assert.equal(externalThreadSessionKey({}, { "x-codex-turn-metadata": JSON.stringify({ thread_id: thread }) }), key);
  // The per-turn thread wins over a connection header naming another one.
  assert.equal(externalThreadSessionKey({ client_metadata: { thread_id: thread } }, { "thread-id": "other-thread-id" }), key);
  assert.notEqual(externalThreadSessionKey(turn({ thread_id: thread, context_window_id: "window-2" })), key);
  for (const value of [undefined, null, "", "short", "x".repeat(129), "has space here", "../escape", 42, {}]) {
    assert.equal(externalThreadSessionKey({ client_metadata: { thread_id: value } }), null);
  }
  assert.equal(externalThreadSessionKey({ client_metadata: { "x-codex-turn-metadata": "{not json" } }), null);
});
