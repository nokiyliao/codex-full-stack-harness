import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import { nativeToolProjection, nativeToolItem, nativeEventProjector, nativeSseProjector } from "../src/nokiy-native-tool.mjs";
import { projectNokiyDirect } from "../src/nokiy-direct-route.mjs";
import { coordinatorContract } from "../src/nokiy-coordinator-policy.mjs";
import { errorPayload } from "../src/shared.mjs";
import { flattenTools, restoreToolItem } from "../src/tool-bridge.mjs";

const host = { python: "/not/executed/python", script: "/not/executed/host.py", coordinator_mode: "dispatch_only_v1" };
const worker = "gpt-6.1-sol";
const options = { host, worker, workerEffort: "max", workflow: "nokiy" };
const fn = (name, extra = {}) => ({ type: "function", name, parameters: { type: "object", properties: {} }, ...extra });
const payload = { operation: "read_result", artifact_root: "/not/read", request_id: "original_request" };

function body(type = "custom") {
  return { model: "nokiy", reasoning: { effort: "max" }, instructions: "Preserve native instructions.",
    tools: [{ type: "namespace", name: "functions", tools: [
      type === "custom" ? { type, name: "exec", format: { type: "text" } } : fn("exec_command"),
      fn("wait"), fn("write_stdin"), fn("request_user_input"), fn("update_goal"), fn("apply_patch"),
    ] }, { type: "web_search" }, fn("tool_search"),
    { type: "namespace", name: "remote_mcp", tools: [fn("wait"), fn("get_goal")] }],
    input: [{ type: "message", role: "user", content: "Delegate the bounded task." },
      { type: "additional_tools", tools: [fn("get_goal"), fn("exec_command"), fn("read_file")] },
      { type: "tool_search_output", call_id: "past_discovery", tools: [fn("request_user_input_async"), fn("network")] }],
  };
}
const names = (tools) => tools.flatMap((tool) => tool.type === "namespace" ? names(tool.tools) : [tool.name]);
function call(name, args = {}, namespace = "functions") {
  return { type: "function_call", name, namespace, id: "item_control", call_id: "call_control", arguments: JSON.stringify(args) };
}
const appNames = ["read_thread", "list_threads", "wait_threads", "send_message_to_thread", "handoff_thread"];
const app = (name = "read_thread", extra = {}) => fn(name, { description: `Advertised native App ${name}.`,
  defer_loading: true, strict: true, parameters: { type: "object", properties: { thread_id: { type: "string" } },
    required: ["thread_id"], additionalProperties: false }, ...extra });
const nativeSearch = { type: "tool_search", execution: "client", defer_loading: true, description: "Native client discovery." };
function advertised(tools, source = "tools") {
  const request = body();
  request.tools = [{ ...request.tools[0], tools: [request.tools[0].tools[0]] }];
  request.input = [request.input[0]];
  if (source === "tools") request.tools.push(...tools);
  else request.input.push({ type: source, call_id: "original_discovery", tools });
  return request;
}

for (const type of ["custom", "function"]) {
  test(`${type}: dispatch retains its hidden native executor but exposes no general execution tools`, () => {
    const request = body(type), original = structuredClone(request);
    const projection = nativeToolProjection(request, options);
    assert.deepEqual(names(projection.body.tools), ["wait", "write_stdin", "request_user_input", "update_goal", "tool_search", "nokiy_execute"]);
    assert.deepEqual(projection.body.input.slice(1).map((item) => names(item.tools)), [["get_goal"], ["request_user_input_async"]]);
    assert.deepEqual(projection.body.input[0], request.input[0]);
    assert.equal(projection.body.instructions, request.instructions);
    assert.equal(projection.target.name, type === "custom" ? "exec" : "exec_command");
    const item = { type: type === "custom" ? "custom_tool_call" : "function_call", name: "nokiy_execute",
      id: "dispatch", call_id: "dispatch_call", [type === "custom" ? "input" : "arguments"]: JSON.stringify(payload) };
    const projected = nativeToolItem(item, projection);
    assert.equal(projected.name, projection.target.name);
    assert.equal(projected.namespace, "functions");
    assert.equal(projected.call_id, item.call_id);
    const value = projected.input ?? JSON.parse(projected.arguments).cmd;
    const envelope = JSON.parse(Buffer.from(value.match(/'--payload' '([A-Za-z0-9_-]+)'/)[1], "base64url"));
    assert.equal(envelope.worker, worker);
    assert.equal(envelope.reasoning_effort, "max");
    assert.deepEqual(envelope.payload, payload);
    assert.deepEqual(request, original);
  });
}

test("mode is opt-in, invalid modes fail closed, forced raw tool choices fail before inference", () => {
  const request = body();
  assert.deepEqual(nativeToolProjection(request, { ...options, host: { ...host, coordinator_mode: undefined } }).body.tools.slice(0, -1), request.tools);
  assert.throws(() => nativeToolProjection(request, { ...options, host: { ...host, coordinator_mode: "typo" } }), { code: "NOKIY_COORDINATOR_MODE_INVALID" });
  for (const choice of ["exec", { type: "custom", name: "exec", namespace: "functions" },
    { type: "function", name: "wait", namespace: "remote_mcp" },
    { type: "allowed_tools", tools: [{ type: "function", name: "apply_patch", namespace: "functions" }] }]) {
    assert.throws(() => nativeToolProjection({ ...request, tool_choice: choice }, options), { code: "NOKIY_COORDINATOR_TOOL_FORBIDDEN" });
  }
  for (const choice of ["auto", "none", "required", { type: "custom", name: "nokiy_execute" },
    { type: "function", name: "wait", namespace: "functions" }]) {
    assert.deepEqual(nativeToolProjection({ ...request, tool_choice: choice }, options).body.tool_choice, choice);
  }
});

test("same-connection native deltas retain only confirmed coordination capabilities", () => {
  const first = nativeToolProjection(body(), options);
  const delta = { tools: [], input: [{ type: "message", role: "user", content: "Continue." }] };
  const base = { ...options, continuationTarget: first.target };
  const wait = { type: "response.output_item.done", item: call("wait", { cell_id: "original" }) };
  assert.throws(() => nativeEventProjector(nativeToolProjection(delta, base))(wait), { code: "NOKIY_COORDINATOR_TOOL_FORBIDDEN" });
  let bindings = first.coordinatorBindings;
  for (let i = 0; i < 3; i++) {
    const projection = nativeToolProjection(delta, { ...base, continuationCoordinatorBindings: bindings });
    assert.deepEqual(nativeEventProjector(projection)(wait), wait);
    assert.deepEqual(projection.body.input, delta.input, "do not reconstruct or duplicate omitted provider history");
    assert.equal(projection.coordinatorBindings.length, bindings.length);
    assert.throws(() => nativeEventProjector(projection)({ ...wait, item: call("exec_command", { cmd: "forbidden" }) }),
      { code: "NOKIY_COORDINATOR_TOOL_FORBIDDEN" });
    bindings = projection.coordinatorBindings;
  }
});

test("stream and terminal guards reject undeclared execution, hosted tools and spoofed controls", () => {
  const projection = nativeToolProjection(body(), options);
  const blocked = [
    call("exec_command", { cmd: "touch forbidden" }), call("apply_patch", { patch: "forbidden" }),
    call("wait", {}, "remote_mcp"), call("get_goal", {}, "functions"),
    { type: "custom_tool_call", name: "exec", namespace: "functions", input: "arbitrary code" },
    { type: "web_search_call", id: "hosted" }, { type: "mcp_call", name: "read_file" },
    { type: "local_shell_call", id: "shell" },
    { type: "custom_tool_call", name: "nokiy_execute", namespace: "remote_mcp", input: JSON.stringify(payload) },
  ];
  for (const item of blocked) {
    for (const event of [{ type: "response.output_item.added", item }, { type: "response.output_item.done", item },
      { type: "response.completed", response: { status: "completed", output: [item] } }]) {
      assert.throws(() => nativeEventProjector(projection)(event), { code: "NOKIY_COORDINATOR_TOOL_FORBIDDEN" });
      const parser = nativeSseProjector(nativeEventProjector(projection));
      assert.throws(() => parser.push(Buffer.from(`data: ${JSON.stringify(event)}\n\n`)), { code: "NOKIY_COORDINATOR_TOOL_FORBIDDEN" });
    }
  }
});

test("observation controls retain IDs and cancellation while stdin injection never reaches native execution", () => {
  const projection = nativeToolProjection(body(), options);
  assert.deepEqual(projection.body.tools[0].tools.find((tool) => tool.name === "write_stdin").parameters.properties.chars.enum, [""]);
  for (const item of [call("wait", { cell_id: "original", yield_time_ms: 10000 }),
    call("wait", { cell_id: "original", terminate: true }), call("write_stdin", { session_id: 123, chars: "" }),
    call("get_goal", {}, null), call("update_goal", { status: "complete" }),
    call("request_user_input", { questions: [] })]) {
    const projector = nativeEventProjector(projection);
    assert.equal(projector({ type: "response.output_item.added", item: { ...item, arguments: "" } }), null);
    assert.equal(projector({ type: "response.function_call_arguments.delta", item_id: item.id, delta: item.arguments }), null);
    const done = { type: "response.output_item.done", item };
    assert.deepEqual(projector(done), done);
  }
  for (const args of [{ session_id: 123, chars: "rm -rf anything\n" }, { chars: "\u0003" }, [], null]) {
    const item = call("write_stdin", args);
    const projector = nativeEventProjector(projection);
    assert.equal(projector({ type: "response.output_item.added", item }), null);
    assert.throws(() => projector({ type: "response.output_item.done", item }), { code: "NOKIY_COORDINATOR_TOOL_FORBIDDEN" });
  }
});

test("denials identify the exact binding and event without exposing arguments or arbitrary names", () => {
  const projection = nativeToolProjection(body(), options);
  const inputs = [
    [call("exec_command", { cmd: "SECRET_COMMAND" }), "call_not_advertised"],
    [call("wait", { cell_id: "SECRET_HANDLE" }, "remote_mcp"), "call_not_advertised"],
    [call("write_stdin", { session_id: 123, chars: "SECRET_INPUT" }), "stdin_not_observation"],
    [{ ...call("write_stdin"), arguments: "SECRET_BAD_JSON" }, "stdin_arguments_invalid"],
    [call("SECRET NAME\n"), "call_not_advertised"],
  ];
  for (const [item, reason] of inputs) {
    assert.throws(() => nativeEventProjector(projection)({ type: "response.output_item.done", item }), (error) => {
      const diagnostic = errorPayload(error, "request_fixture").error.details.coordinator_rejection;
      assert.equal(error.code, "NOKIY_COORDINATOR_TOOL_FORBIDDEN");
      assert.equal(diagnostic.reason, reason);
      assert.equal(diagnostic.stage, "response.output_item.done");
      assert.equal(diagnostic.call.name, item.name.startsWith("SECRET") ? "<invalid>" : item.name);
      assert.equal(diagnostic.call.namespace, item.namespace);
      assert.ok(diagnostic.advertised.some((entry) => entry.name === "wait" && entry.namespace === "functions"));
      assert.doesNotMatch(JSON.stringify(diagnostic), /SECRET|"(?:arguments|cell_id|session_id|cmd)":/);
      assert.ok(JSON.stringify(diagnostic).length < 2048);
      return true;
    });
  }
});

test("official unqualified calls resolve only to a unique advertised native control", () => {
  const projection = nativeToolProjection(body(), options);
  const qualified = call("wait", { cell_id: "1", yield_time_ms: 10000 });
  const unqualified = { ...qualified };
  delete unqualified.namespace;
  const before = structuredClone(unqualified);
  const project = nativeEventProjector(projection);
  assert.equal(project({ type: "response.output_item.added", item: { ...unqualified, arguments: "", status: "in_progress" } }), null);
  assert.equal(project({ type: "response.function_call_arguments.delta", item_id: unqualified.id, delta: unqualified.arguments }), null);
  assert.deepEqual(project({ type: "response.output_item.done", item: unqualified }).item, qualified);
  assert.deepEqual(project({ type: "response.completed", response: { output: [unqualified] } }).response.output, [qualified]);
  assert.deepEqual(unqualified, before, "do not mutate provider evidence");
  assert.deepEqual(project({ type: "response.output_item.done", item: { ...unqualified, namespace: null } }).item, qualified);

  const ambiguousBody = body();
  ambiguousBody.tools.push(fn("wait"));
  const ambiguous = nativeToolProjection(ambiguousBody, options);
  assert.throws(() => nativeEventProjector(ambiguous)({ type: "response.output_item.done", item: unqualified }), (error) => {
    assert.equal(error.details.coordinator_rejection.reason, "call_ambiguous");
    return true;
  });
  for (const item of [{ ...unqualified, namespace: "remote_mcp" }, { ...unqualified, name: "exec_command" },
    { ...unqualified, type: "custom_tool_call", input: "not a function call" },
    { ...unqualified, name: "write_stdin", arguments: '{"session_id":123,"chars":"forbidden"}' }]) {
    assert.throws(() => nativeEventProjector(projection)({ type: "response.output_item.done", item }),
      { code: "NOKIY_COORDINATOR_TOOL_FORBIDDEN" });
  }
});

test("App definitions retain flat and immediate native namespace schemas across declared and deferred sources", () => {
  const shapes = [[null, false, "tools"], ["functions", false, "additional_tools"],
    ["functions", true, "tool_search_output"], ["codex_app", false, "tools"],
    ["codex_app", true, "additional_tools"], ["mcp__codex_app", false, "tool_search_output"],
    ["mcp__codex_app", true, "tools"]];
  for (const [namespace, wrapped, source] of shapes) {
    const members = appNames.map((name) => app(name, !wrapped && namespace ? { namespace } : {}));
    const definitions = wrapped ? [{ type: "namespace", name: namespace, description: "Native owner.",
      defer_loading: true, tools: members }] : members;
    const request = advertised(definitions, source), before = structuredClone(request);
    const projection = nativeToolProjection(request, options);
    assert.deepEqual(source === "tools" ? projection.body.tools.slice(0, -1) : projection.body.input.at(-1).tools, definitions);
    for (const name of appNames) {
      assert.ok(projection.coordinatorBindings.some((binding) => binding.name === name
        && binding.namespace === namespace && binding.type === "function_call"));
      const item = call(name, { thread_id: "authorized_thread" }, namespace);
      assert.deepEqual(nativeEventProjector(projection)({ type: "response.output_item.done", item }).item, item);
    }
    const dispatch = nativeToolItem({ type: "custom_tool_call", name: "nokiy_execute", call_id: "worker_call",
      input: JSON.stringify(payload) }, projection);
    const envelope = JSON.parse(Buffer.from(dispatch.input.match(/'--payload' '([A-Za-z0-9_-]+)'/)[1], "base64url"));
    assert.deepEqual(envelope.payload, payload);
    assert.doesNotMatch(JSON.stringify(envelope), /read_thread|list_threads|wait_threads|send_message_to_thread|handoff_thread|"tools"/);
    assert.deepEqual(request, before);
  }
});

test("function and built-in client discovery retain their supplied definitions and distinct call types", () => {
  const search = fn("tool_search", { defer_loading: true, parameters: { type: "object",
    properties: { query: { type: "string" } }, required: ["query"], additionalProperties: false } });
  for (const [definitions, source, namespace, builtin] of [
    [[search], "tools", null, false],
    [[{ type: "namespace", name: "functions", tools: [search] }], "additional_tools", "functions", false],
    [[{ ...search, namespace: "functions" }], "tool_search_output", "functions", false],
    [[nativeSearch], "tools", null, true], [[nativeSearch], "tool_search_output", null, true],
  ]) {
    const projection = nativeToolProjection(advertised(definitions, source), options);
    assert.deepEqual(source === "tools" ? projection.body.tools.slice(0, -1) : projection.body.input.at(-1).tools, definitions);
    const item = builtin ? { type: "tool_search_call", call_id: "native_discovery", execution: "client",
      arguments: { query: "native App", limit: 3 }, status: "completed" } : call("tool_search", { query: "native App" }, namespace);
    const project = nativeEventProjector(projection);
    assert.deepEqual(project({ type: "response.output_item.done", item }).item, item);
    assert.throws(() => project({ type: "response.output_item.done", item: builtin
      ? call("tool_search", {}, null) : { type: "tool_search_call", execution: "client", arguments: { query: "native App" } } }),
    { code: "NOKIY_COORDINATOR_TOOL_FORBIDDEN" });
  }
});

test("tool choices require exactly the advertised App or discovery identity", () => {
  const request = advertised([app("read_thread", { namespace: "codex_app" }), fn("tool_search"), nativeSearch]);
  for (const choice of [{ type: "function", name: "read_thread", namespace: "codex_app" },
    { type: "function", name: "read_thread" }, { type: "function", name: "tool_search" }, { type: "tool_search" },
    { type: "allowed_tools", mode: "required", tools: [{ type: "tool_search" },
      { type: "function", name: "read_thread", namespace: "codex_app" }] }]) {
    assert.deepEqual(nativeToolProjection({ ...request, tool_choice: choice }, options).body.tool_choice, choice);
  }
  for (const choice of [{ type: "function", name: "send_message_to_thread" },
    { type: "function", name: "read_thread", namespace: "remote_mcp" }, { type: "tool_search", execution: "server" },
    { type: "tool_search", name: "tool_search" },
    { type: "allowed_tools", tools: [{ type: "function", name: "read_file" }] }]) {
    assert.throws(() => nativeToolProjection({ ...request, tool_choice: choice }, options),
      { code: "NOKIY_COORDINATOR_TOOL_FORBIDDEN" });
  }
  for (const [definitions, choice] of [[[fn("tool_search")], { type: "tool_search" }],
    [[nativeSearch], { type: "function", name: "tool_search" }], [[], { type: "function", name: "read_thread" }]]) {
    assert.throws(() => nativeToolProjection({ ...advertised(definitions), tool_choice: choice }, options),
      { code: "NOKIY_COORDINATOR_TOOL_FORBIDDEN" });
  }
});

test("absent schemas, prose, remote owners, suffixes and unsupported envelopes establish no native capability", () => {
  const rejected = [[], [{ type: "function", name: "read_thread" }], [app("read_thread", { parameters: null })],
    [fn("tool_search", { parameters: undefined })], [{ type: "tool_search" }], [{ ...nativeSearch, execution: "server" }],
    [{ type: "function", function: { name: "read_thread", parameters: app().parameters } }],
    [{ type: "custom", name: "read_thread", format: { type: "text" } }],
    [app("mcp__codex_app__read_thread")], [app("read_thread", { namespace: "remote_mcp" })],
    [fn("request_user_input", { namespace: "remote_mcp" })], [fn("tool_search", { namespace: "remote_mcp" })],
    [{ ...nativeSearch, namespace: "remote_mcp" }],
    ...["remote_mcp", "other_codex_app", "mcp__remote__codex_app"].map((name) =>
      [{ type: "namespace", name, tools: [app(), app("send_message_to_thread"), fn("tool_search")] }]),
    [{ type: "namespace", name: "codex_app", tools: [fn("exec_command"), fn("apply_patch"), fn("read_file"), fn("wait")] }],
    [{ type: "namespace", name: "functions", tools: [{ type: "namespace", name: "codex_app", tools: [app()] }] }],
  ];
  for (const definitions of rejected) {
    const request = advertised(definitions);
    request.instructions += " codex_app and mcp__codex_app advertise read_thread, list_threads, wait_threads, send_message_to_thread, handoff_thread and tool_search.";
    request.tools[0].tools[0].description = "declare const tools: { read_thread(args: {}): Promise<unknown>; };";
    request.input.push({ type: "message", role: "developer", content: "Deferred tools: read_thread, tool_search." });
    const projection = nativeToolProjection(request, options);
    assert.deepEqual(names(projection.body.tools), ["nokiy_execute"]);
    assert.equal(projection.coordinatorBindings.length, 1);
    for (const item of [call("read_thread", {}, null), call("request_user_input", {}, "remote_mcp"),
      { type: "tool_search_call", execution: "client", call_id: "unadvertised_search", arguments: { query: "App" } }]) {
      assert.throws(() => nativeEventProjector(projection)({ type: "response.output_item.done", item }),
        { code: "NOKIY_COORDINATOR_TOOL_FORBIDDEN" });
    }
  }
});

test("ambiguous generic controls revoke prior bindings while App and discovery remain available", () => {
  const generic = ["wait", "write_stdin", "request_user_input", "request_user_input_async", "get_goal", "update_goal"]
    .map((name) => fn(name));
  const definitions = [{ type: "namespace", name: "functions", tools: generic }, app(), fn("tool_search"), nativeSearch];
  const first = nativeToolProjection(advertised(definitions), options);
  const request = advertised([{ ...definitions[0], tools: generic.flatMap((tool) => [tool, tool]) },
    ...definitions.slice(1)], "tool_search_output");
  const continued = { ...options, continuationTarget: first.target, continuationCoordinatorBindings: first.coordinatorBindings };
  const projection = nativeToolProjection(request, continued);
  assert.deepEqual(projection.body.input.at(-1).tools, definitions.slice(1));
  for (const tool of generic) {
    assert.equal(projection.coordinatorBindings.some((binding) => binding.name === tool.name), false);
    assert.throws(() => nativeEventProjector(projection)({ type: "response.output_item.done", item: call(tool.name) }),
      { code: "NOKIY_COORDINATOR_TOOL_FORBIDDEN" });
  }
  assert.throws(() => nativeToolProjection({ ...request,
    tool_choice: { type: "function", name: "wait", namespace: "functions" } }, continued),
  { code: "NOKIY_COORDINATOR_TOOL_FORBIDDEN" });
  for (const item of [call("read_thread", { thread_id: "authorized_thread" }, null), call("tool_search", {}, null),
    { type: "tool_search_call", execution: "client", call_id: "native_discovery", arguments: { query: "App" } }]) {
    assert.deepEqual(nativeEventProjector(projection)({ type: "response.output_item.done", item }).item, item);
  }
});

test("App and discovery conflicting explicit owners and duplicate current exact identities fail closed", () => {
  for (const definitions of [[{ type: "namespace", name: "functions", tools: [app("read_thread", { namespace: "remote_mcp" })] }],
    [{ type: "namespace", name: "codex_app", tools: [app("read_thread", { namespace: "mcp__codex_app" })] }],
    [{ type: "namespace", name: "codex_app", namespace: "remote_mcp", tools: [app()] }],
    [app(), app()], [app(), app("read_thread", { parameters: { type: "object", properties: {} } })],
    [app("read_thread", { namespace: "codex_app" }), { type: "namespace", name: "codex_app", tools: [app()] }],
    [nativeSearch, nativeSearch], [fn("tool_search"), fn("tool_search")],
    [{ type: "namespace", name: "functions", tools: [fn("tool_search", { namespace: "remote_mcp" })] }]]) {
    assert.throws(() => nativeToolProjection(advertised(definitions), options), { code: "NOKIY_COORDINATOR_TOOL_FORBIDDEN" });
  }
});

test("App namespace omission resolves only one owner and never substitutes another recipient or owner", () => {
  const definitions = ["codex_app", "mcp__codex_app"].map((name) => ({ type: "namespace", name, tools: [app()] }));
  const request = advertised(definitions), projection = nativeToolProjection(request, options);
  for (const namespace of ["codex_app", "mcp__codex_app"]) {
    const item = call("read_thread", { thread_id: "authorized_thread" }, namespace);
    assert.deepEqual(nativeEventProjector(projection)({ type: "response.output_item.done", item }).item, item);
  }
  const unqualified = call("read_thread", { thread_id: "authorized_thread" });
  delete unqualified.namespace;
  assert.throws(() => nativeEventProjector(projection)({ type: "response.output_item.done", item: unqualified }), (error) =>
    error.code === "NOKIY_COORDINATOR_TOOL_FORBIDDEN" && error.details.coordinator_rejection.reason === "call_ambiguous");
  assert.throws(() => nativeToolProjection({ ...request, tool_choice: { type: "function", name: "read_thread" } }, options),
    { code: "NOKIY_COORDINATOR_TOOL_FORBIDDEN" });
  const unique = nativeToolProjection(advertised([definitions[0]]), options);
  assert.deepEqual(nativeEventProjector(unique)({ type: "response.output_item.done", item: unqualified }).item,
    { ...unqualified, namespace: "codex_app" });
  assert.throws(() => nativeEventProjector(unique)({ type: "response.output_item.done", item: { ...unqualified, namespace: "mcp__codex_app" } }),
    { code: "NOKIY_COORDINATOR_TOOL_FORBIDDEN" });
});

test("current App schemas supersede history while repeated history is one binding and latest conflicts reject", () => {
  const old = app("read_thread", { namespace: "codex_app", description: "Old schema." });
  const latest = { ...old, description: "Latest native schema.", parameters: { type: "object", properties: { cursor: { type: "string" } } } };
  const request = advertised([latest, nativeSearch]);
  request.input.push({ type: "additional_tools", tools: [old, old, nativeSearch] },
    { type: "tool_search_output", tools: [old, nativeSearch] });
  const before = structuredClone(request), projection = nativeToolProjection(request, options);
  assert.deepEqual(projection.body.tools.slice(0, -1), [latest, nativeSearch]);
  assert.deepEqual(projection.body.input.slice(1).map((item) => item.tools), [[], []]);
  assert.equal(projection.coordinatorBindings.filter((binding) => binding.name === "read_thread").length, 1);
  assert.deepEqual(request, before);
  const historical = advertised([], "additional_tools");
  historical.input.push({ type: "additional_tools", tools: [old, nativeSearch] },
    { type: "tool_search_output", tools: [latest, nativeSearch] });
  const updated = nativeToolProjection(historical, options);
  assert.deepEqual(updated.body.input.at(-2).tools, []);
  assert.deepEqual(updated.body.input.at(-1).tools, [latest, nativeSearch]);
  historical.input.push({ type: "tool_search_output", tools: [latest, latest] });
  assert.throws(() => nativeToolProjection(historical, options), { code: "NOKIY_COORDINATOR_TOOL_FORBIDDEN" });
});

test("bound continuation preserves native App/discovery without synthesizing definitions and filters new discoveries", () => {
  const send = app("send_message_to_thread", { namespace: "codex_app" });
  const initial = advertised([send, nativeSearch], "additional_tools");
  initial.input.at(-1).tools.unshift(...initial.tools);
  initial.tools = [];
  const first = nativeToolProjection(initial, options);
  const unknown = { type: "function_call_output", call_id: "original_send", output: '{"status":"UNKNOWN","send_id":"original_send"}' };
  const delta = { tools: [], input: [unknown] };
  const base = { ...options, continuationTarget: first.target };
  const bound = { ...base, continuationCoordinatorBindings: first.coordinatorBindings };
  const items = [{ ...call("send_message_to_thread", { thread_id: "authorized_thread" }, "codex_app"), call_id: "original_send" },
    { type: "tool_search_call", execution: "client", call_id: "original_search", arguments: { query: "App recovery" } }];
  const continued = nativeToolProjection(delta, bound);
  assert.deepEqual(names(continued.body.tools), ["nokiy_execute"]);
  assert.deepEqual(continued.body.input, [unknown]);
  for (const item of items) {
    assert.deepEqual(nativeEventProjector(continued)({ type: "response.output_item.done", item }).item, item);
    assert.throws(() => nativeEventProjector(nativeToolProjection(delta, base))({ type: "response.output_item.done", item }),
      { code: "NOKIY_COORDINATOR_TOOL_FORBIDDEN" });
  }
  const update = { ...delta, input: [unknown, { type: "tool_search_output", call_id: "original_search", tools: [
    app("wait_threads", { namespace: "codex_app" }), fn("exec_command"), fn("apply_patch"),
    { type: "namespace", name: "remote_mcp", tools: [app()] },
    { ...send, parameters: null }, { ...nativeSearch, execution: "server" },
  ] }] };
  const filtered = nativeToolProjection(update, bound);
  assert.deepEqual(filtered.body.input.at(-1).tools, [app("wait_threads", { namespace: "codex_app" })]);
  const recover = call("wait_threads", { send_id: "original_send", thread_id: "authorized_thread" }, "codex_app");
  assert.deepEqual(nativeEventProjector(filtered)({ type: "response.output_item.done", item: recover }).item, recover);
  for (const item of [...items, call("exec_command"), call("read_thread", {}, "remote_mcp")]) {
    assert.throws(() => nativeEventProjector(filtered)({ type: "response.output_item.done", item }),
      { code: "NOKIY_COORDINATOR_TOOL_FORBIDDEN" });
  }
});

test("send stream, repeated terminal representations and SSE preserve the exact native recipient, arguments and IDs", () => {
  const projection = nativeToolProjection(advertised([{ type: "namespace", name: "mcp__codex_app",
    tools: [app("send_message_to_thread")] }]), options);
  const item = { ...call("send_message_to_thread", {}, "mcp__codex_app"), id: "original_send_item", call_id: "original_send",
    arguments: ' { "thread_id": "authorized_thread", "message": "authorized message", "send_id": "original_send" } ', status: "completed" };
  const before = structuredClone(item), project = nativeEventProjector(projection);
  assert.deepEqual(nativeToolItem(item, projection), item);
  const added = { type: "response.output_item.added", item: { ...item, arguments: "", status: "in_progress" } };
  const delta = { type: "response.function_call_arguments.delta", item_id: item.id, delta: item.arguments };
  assert.equal(project(added), null);
  assert.equal(project(delta), null);
  const done = { type: "response.output_item.done", item };
  const terminal = { type: "response.completed", response: { status: "completed", output: [item] } };
  assert.deepEqual(project(done), done);
  assert.deepEqual(project(terminal), terminal);
  assert.deepEqual(project(terminal), terminal, "a repeated representation retains the original send, never a fresh call");
  const parser = nativeSseProjector(nativeEventProjector(projection));
  const wire = [added, delta, done, terminal].map((event) => `data: ${JSON.stringify(event)}\n\n`).join("");
  const output = parser.push(Buffer.from(wire.slice(0, 31))) + parser.push(Buffer.from(wire.slice(31))) + parser.end();
  assert.deepEqual(output.trim().split("\n\n").map((frame) => JSON.parse(frame.slice(6))), [done, terminal]);
  assert.deepEqual(item, before);
});

test("the existing bridge's nameless native discovery survives streamed, final and SSE projection unchanged", () => {
  const projection = nativeToolProjection(advertised([nativeSearch], "additional_tools"), options);
  const { bridge, tools } = flattenTools([nativeSearch]);
  const item = restoreToolItem({ type: "function_call", name: tools[0].name, id: "discovery_item", call_id: "original_discovery",
    arguments: '{"query":"native App recovery","limit":4}', status: "completed" }, bridge);
  assert.equal(Object.hasOwn(item, "name"), false);
  assert.equal(item.type, "tool_search_call");
  const events = [{ type: "response.output_item.added", item: { ...item, status: "in_progress" } },
    { type: "response.output_item.done", item }, { type: "response.completed", response: { output: [item] } }];
  const project = nativeEventProjector(projection), parser = nativeSseProjector(nativeEventProjector(projection));
  for (const event of events) {
    assert.deepEqual(project(event), event);
    assert.deepEqual(JSON.parse(parser.push(Buffer.from(`data: ${JSON.stringify(event)}\n\n`)).slice(6)), event);
  }
  for (const invalid of [{ ...item, name: "tool_search" }, { ...item, namespace: "remote_mcp" },
    { ...item, execution: "server" }, { ...item, execution: undefined }]) {
    assert.throws(() => project({ type: "response.output_item.done", item: invalid }), { code: "NOKIY_COORDINATOR_TOOL_FORBIDDEN" });
  }
  assert.equal(Object.hasOwn(item, "name"), false);
});

test("coordinator contract removes parent-work fallback, preserves model settings and auxiliary native behavior", () => {
  const catalog = { models: ["gpt-6-astra", worker].map((slug) => ({ slug, supported_reasoning_levels: [{ effort: "max" }] })) };
  const profiles = [{ slug: "nokiy", display_name: "Nokiy", workflow: "nokiy",
    coordinator: { model: "gpt-6-astra", reasoning_effort: "max" }, worker: { model: worker, reasoning_effort: "max", service_tier: "default" } }];
  const request = body();
  const routed = projectNokiyDirect(request, { host, catalog, profiles });
  assert.equal(routed.body.model, "gpt-6-astra");
  assert.deepEqual(routed.body.reasoning, request.reasoning);
  assert.ok(routed.body.instructions.startsWith(request.instructions));
  assert.match(routed.body.instructions, /No fallback to parent shell\/file\/MCP execution/);
  assert.match(routed.body.instructions, /Delegate navigation, implementation, tests, source review/);
  assert.match(routed.body.instructions, /scope\/owner\/CAS/);
  assert.doesNotMatch(routed.body.instructions, /parent native execution may continue|still inspect task-specific quality/);
  const compact = projectNokiyDirect(request, { host, catalog, profiles, endpoint: "/responses/compact" });
  assert.equal(compact.nativeTool, null);
  assert.equal(compact.body.instructions, request.instructions);
  assert.equal(projectNokiyDirect({ ...request, model: "gpt-6-astra" }, { host, catalog, profiles }), null);
});

for (const [display_name, workflow, model, reasoning_effort] of [
  ["Nokiy", "nokiy", "gpt-6-astra", "xhigh"],
  ["Nokiy Ultra", "nokiy-direct", "gpt-6-astra", "max"],
  ["Nokiy Efficient", "nokiy-direct", "gpt-6.1-sol", "max"],
]) {
  test(`${display_name}: same-gap correction includes verification and adoptable delivery without forcing success`, () => {
    const profile = { display_name, workflow, coordinator: { model, reasoning_effort },
      worker: { model: worker, reasoning_effort: "max" } };
    const before = structuredClone(profile);
    const text = coordinatorContract(profile);
    assert.match(text, /same unfinished already-authorized gap/);
    assert.match(text, /complete the necessary verification and return the corrected deliverable in the original required format/);
    assert.match(text, /preserving explicit unknowns and the original acceptance criteria/);
    assert.match(text, /when both can be completed coherently at equal quality within that scope/);
    assert.match(text, /genuine dependencies, required independent verification, new authorization or uncertain effects still require staged work or original-ID recovery/);
    assert.match(text, /Reconcile the worker result against the assigned scope and acceptance criteria using its evidence/);
    assert.match(text, /without rewriting it/);
    assert.deepEqual(profile, before);
  });
}

const policyProfiles = JSON.parse(readFileSync(new URL("../config/nokiy-workflows.json", import.meta.url), "utf8")).profiles;
const efficientProfile = policyProfiles.find((profile) => profile.slug === "nokiy-efficient");
const discoveryPolicy = coordinatorContract(efficientProfile);

test("Efficient: routed native policy prefers batched Python locations with unchanged execution settings", () => {
  const slugs = new Set(["gpt-6-astra", "gpt-6-luna", worker, efficientProfile.coordinator.model,
    efficientProfile.worker.model, ...Object.values(efficientProfile.worker_selector?.models ?? {})]);
  const catalog = { models: [...slugs].map((slug) => ({ slug,
    context_window: 500000, supported_reasoning_levels: [{ effort: "max" }, { effort: "xhigh" }] })) };
  const request = { ...body("function"), model: efficientProfile.slug };
  request.tools[0].tools[0].parameters.properties = { cmd: { type: "string" }, workdir: { type: "string" } };
  request.tools[0].tools[0].parameters.required = ["cmd"];
  const before = structuredClone({ request, profiles: policyProfiles });
  const routed = projectNokiyDirect(request, { host, catalog, profiles: policyProfiles });
  assert.match(routed.body.instructions, /native-owned branches whose Python implementation or dependencies are unknown, prefer navigate projection=locations with batched known queries before broad searches/);
  assert.match(routed.body.instructions, /reuse valid source-SHA-bound locations rather than rediscovering them/);
  assert.equal(routed.body.model, efficientProfile.coordinator.model);
  assert.equal(routed.body.reasoning.effort, efficientProfile.coordinator.reasoning_effort);
  assert.ok(routed.body.instructions.includes(`Worker ${efficientProfile.worker.model}/${efficientProfile.worker.reasoning_effort}`));
  assert.deepEqual(routed.body.tools.slice(0, -1), request.tools);
  assert.match(routed.body.instructions, /within configured capacity/);
  assert.match(routed.body.instructions, /Native permissions, session\/Goal, owner\/CAS and deployment authority remain controlling/);
  assert.deepEqual({ request, profiles: policyProfiles }, before);
});

test("Efficient: known unchanged locations bypass discovery while necessary reads remain", () => {
  assert.match(discoveryPolicy, /Known exact unchanged locations go directly to bounded behavior reads/);
  assert.match(discoveryPolicy, /Impose no one-call\/minimum-term quota/);
  assert.match(discoveryPolicy, /preserve useful dependency reads, freshness and rereads after drift\/truncation/);
});

test("Efficient: reuse the navigate cache without extra probing and allow authorized missing-map builds", () => {
  assert.match(discoveryPolicy, /Reuse the existing cache through the navigate API without raw-cache inspection, a separate freshness-probe round or forced rebuild/);
  assert.match(discoveryPolicy, /a missing map may build automatically within authorized scope/);
});

test("Efficient: incomplete discovery stays explicit and retains scoped fallback", () => {
  assert.match(discoveryPolicy, /Keep unsupported languages, incomplete\/failed\/partial\/parse-error results and pagination explicit/);
  assert.match(discoveryPolicy, /use necessary existing authorized scoped fallback/);
  assert.match(discoveryPolicy, /never infer absence or authority from these states or retry blindly/);
});

test("serial and Ultra retain delegation policy without Efficient native discovery guidance", () => {
  for (const profile of policyProfiles.filter((item) => item.slug !== efficientProfile.slug)) {
    const before = structuredClone(profile);
    const text = coordinatorContract(profile);
    assert.match(text, /is a coordination-only parent/);
    assert.match(text, /Delegate navigation, implementation, tests, source review/);
    assert.doesNotMatch(text, /prefer navigate projection=locations|Known exact unchanged locations|Reuse the existing cache through the navigate API|one-call\/minimum-term quota/);
    if (profile.workflow === "nokiy") assert.match(text, /Execute one bounded action at a time; no batch or fan-out/);
    else assert.match(text, /Choose the smallest useful run_batch within configured capacity/);
    assert.deepEqual(profile, before);
  }
});

test("code-mode Goal bridge uses advertised native methods without exposing arbitrary code or creating a Goal", async () => {
  const request = body();
  request.tools[0].tools[0].description = 'declare const tools: { get_goal(args: {}): Promise<unknown>; };\ndeclare const tools: { update_goal(args: {status: "complete" | "paused" | "blocked"}): Promise<unknown>; };';
  const projection = nativeToolProjection(request, options);
  assert.ok(projection.body.tools.some((tool) => tool.name === "nokiy_goal"));
  const emitted = [], calls = [];
  for (const args of [{ operation: "get" }, { operation: "update", status: "complete" },
    { operation: "update", status: "blocked" }, { operation: "update", status: "paused" }]) {
    const original = { type: "custom_tool_call", name: "nokiy_goal", id: "goal_item", call_id: "goal_call", input: JSON.stringify(args) };
    const item = nativeEventProjector(projection)({ type: "response.output_item.done", item: original }).item;
    assert.equal(item.name, "exec");
    assert.equal(item.namespace, "functions");
    assert.equal(item.call_id, original.call_id);
    await runInNewContext(`(async () => { ${item.input} })()`, { text: (x) => emitted.push(x), tools: {
      get_goal: async (x) => { calls.push(["get", JSON.parse(JSON.stringify(x))]); return { goal: null }; },
      update_goal: async (x) => { calls.push(["update", JSON.parse(JSON.stringify(x))]); return { status: x.status }; },
    } });
  }
  assert.deepEqual(calls, [["get", {}], ["update", { status: "complete" }], ["update", { status: "blocked" }], ["update", { status: "paused" }]]);
  assert.equal(emitted.length, 4);
  for (const args of [{ operation: "create", objective: "other goal" }, { operation: "update", status: "active" },
    { operation: "get", thread_id: "foreign" }, { operation: "update", status: "complete", code: "arbitrary" }]) {
    assert.throws(() => nativeToolItem({ type: "custom_tool_call", name: "nokiy_goal", input: JSON.stringify(args) }, projection),
      { code: "NOKIY_COORDINATOR_TOOL_FORBIDDEN" });
  }
  const delta = nativeToolProjection({ tools: [], input: [] }, { ...options, continuationTarget: projection.target,
    continuationCoordinatorBindings: projection.coordinatorBindings });
  assert.ok(delta.body.tools.some((tool) => tool.name === "nokiy_goal"));
});

test("coordinators locate code with nokiy-nav line ranges", () => {
  assert.match(discoveryPolicy, /~\/\.local\/bin\/nokiy-nav NAME \[NAME \.\.\.\] \[--in DIR\] \[--refs\]/);
  assert.match(discoveryPolicy, /read only those ranges and keep rg for literal text that is not a symbol/);
  for (const profile of policyProfiles.filter((item) => item.slug !== efficientProfile.slug)) {
    assert.match(coordinatorContract(profile), /get exact definition line ranges with `~\/\.local\/bin\/nokiy-nav NAME \[--in DIR\]`/);
  }
});
