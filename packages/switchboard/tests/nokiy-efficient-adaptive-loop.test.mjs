import assert from "node:assert/strict";
import test from "node:test";
import { runInNewContext } from "node:vm";
import { coordinatorContract } from "../src/nokiy-coordinator-policy.mjs";
import { efficientNativeLoop, resolveWorkflowProfiles } from "../src/nokiy-workflow-profiles.mjs";
import { projectNokiyDirect } from "../src/nokiy-direct-route.mjs";
import { nativeToolProjection, nativeToolItem, nativeEventProjector, nativeSseProjector } from "../src/nokiy-native-tool.mjs";
import { createReplayRestorer } from "../src/nokiy-replay-restore.mjs";

const host = { python: "/not/executed/python", script: "/not/executed/host.py", coordinator_mode: "dispatch_only_v1" };
const catalog = { models: ["gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"].map((slug) => ({
  slug, supported_reasoning_levels: ["low", "medium", "high", "max", "xhigh"].map((effort) => ({ effort })),
  context_window: 500000, max_context_window: 500000, supports_parallel_tool_calls: true,
})) };
const profiles = resolveWorkflowProfiles(["nokiy", "nokiy-direct", "nokiy-efficient"].map((slug) => ({
  slug, display_name: slug, workflow: slug === "nokiy" ? "nokiy" : "nokiy-direct",
  coordinator: { model: slug === "nokiy-efficient" ? "gpt-6.1-sol" : "gpt-6-astra", reasoning_effort: "max" },
  worker: { model: "gpt-6.1-sol", reasoning_effort: "max", service_tier: "default" },
  worker_selector: { default_effort: "medium",
    models: { low: "gpt-6-luna", medium: "gpt-6.1-sol", high: "gpt-6-astra" },
    reasoning_efforts: { low: "max", medium: "max", high: "xhigh" } },
})), catalog);
const efficient = profiles.find((profile) => profile.slug === "nokiy-efficient");
const fn = { type: "function", name: "exec_command", parameters: { type: "object", properties: {
  cmd: { type: "string" }, yield_time_ms: { type: "integer", minimum: 250, maximum: 30000 },
  max_output_tokens: { type: "integer", minimum: 1 },
  sandbox_permissions: { type: "string", enum: ["use_default", "require_escalated"] },
}, required: ["cmd"], additionalProperties: false } };
const custom = { type: "custom", name: "exec", format: { type: "text" },
  description: "declare const tools: { exec_command(args: {cmd: string; workdir?: string; yield_time_ms?: number; max_output_tokens?: number;}): Promise<unknown>; };" };
const patch = { type: "custom", name: "apply_patch", format: { type: "text" }, description: "Existing native patch capability." };
const stdin = { type: "function", name: "write_stdin", parameters: { type: "object", properties: {
  session_id: { type: "integer" }, chars: { type: "string" },
}, required: ["session_id"], additionalProperties: false } };
const request = (tool = fn) => ({ model: "gpt-6.1-sol", reasoning: { effort: "max" },
  instructions: "Existing native permissions, owner/CAS and Goal instructions.",
  tools: [{ type: "namespace", name: "functions", tools: [structuredClone(tool), patch, stdin] }],
  input: [{ type: "message", role: "user", content: "Complete one coherent authorized branch." }],
});
const options = (profile = efficient) => ({ host, profile, workflow: profile.workflow,
  worker: profile.worker.model, workerEffort: profile.worker.reasoning_effort, workerServiceTier: profile.worker.service_tier });
const call = (type = "function_call", name = "exec_command") => ({ type, name, namespace: "functions",
  id: "native_item", call_id: "native_call", [type === "function_call" ? "arguments" : "input"]:
    type === "function_call" ? JSON.stringify({ cmd: "authorized task", sandbox_permissions: "require_escalated" }) : "native code" });
const flatten = (tools) => tools.flatMap((tool) => tool.type === "namespace" ? flatten(tool.tools) : [tool]);
const appNames = ["read_thread", "list_threads", "wait_threads", "send_message_to_thread", "handoff_thread"];
const appTools = { type: "namespace", name: "mcp__codex_app", description: "Native App owner.", tools: appNames.map((name) => ({
  type: "function", name, defer_loading: true, strict: true, parameters: { type: "object",
    properties: { thread_id: { type: "string" }, message: { type: "string" } }, required: ["thread_id"], additionalProperties: false },
})) };

test("parent-completion dispatch matching tries deferred form before failing or using an exact record", () => {
  const packet = { operation: "run_batch", observe: "later", artifact_root: "/task/batch", members: [
    { draft: { workspace: "/workspace", artifact_root: "/task/batch/worker", prompt: "bounded" },
      action: { context_summary: "scope" } }],
    post_batch_verifier: {
      draft: { workspace: "/workspace", artifact_root: "/task/final", prompt: "combined gate", authority_effect: "none" },
      action: { operations: ["read", "command"], read_scopes: ["parent.py"], write_scopes: [], target_paths: [],
        verifier_commands: [{ argv: ["/stub/python", "-m", "unittest"] }] },
      parent_source_targets: ["parent.py"] } };
  for (const payload of [packet, { operation: "run_batch_ref", observe: "later", parent_completion: true,
    input_path: "/task/batch.json", input_sha256: "b".repeat(64) }]) {
    const old = { ...options(), host: { ...host, parent_result_delivery: "accept_v1", batch_reference: "direct_v1" } };
    const original = { type: "custom_tool_call", name: "nokiy_execute", call_id: "parent_binding", input: JSON.stringify(payload) };
    const dispatched = nativeToolItem(original, nativeToolProjection(request(custom), old));
    const output = { type: "custom_tool_call_output", call_id: original.call_id,
      output: "Script running with cell ID 7\nWall time 0.0 seconds\nOutput:\n" };
    const body = { ...request(custom), input: [dispatched, output] };
    assert.ok(nativeToolProjection(body, old).pendingResultDelivery);
    const next = { ...old, host: { ...old.host, script: "/next/physical/host.py" } };
    assert.equal(nativeToolProjection(body, next).pendingResultDelivery, null);
    const retained = createReplayRestorer();
    retained.record(original, dispatched);
    assert.ok(nativeToolProjection(body, { ...next, recordedNativeCall: retained.originalCall }).pendingResultDelivery);
  }
});
const nativeSearch = { type: "tool_search", execution: "client", defer_loading: true, description: "Native client discovery." };

test("navigation advertises reference discovery and existing worker context handoff", () => {
  const p = nativeToolProjection(request(fn), options()).body.tools.at(-1).parameters.properties;
  assert.match(p.queries.description, /ref:CaptureError/);
  assert.match(p.queries.description, /locations and source SHA/);
  assert.match(p.queries.description, /do not repeat discovery/);
  assert.match(p.queries.description, /not complete dynamic behavior evidence/);
});

test("verifier catalogs advertise a reference-only shape on every complete action", () => {
  const p = nativeToolProjection(request(fn), options()).body.tools.at(-1).parameters.properties;
  for (const action of [p.action, p.members.items.properties.action, p.post_batch_verifier.properties.action]) {
    assert.equal(action.properties.source_queries.maxItems, 16);
    assert.equal(action.properties.source_queries.items.maxLength, 256);
    const [reference, inline] = action.properties.verifier_commands.items.anyOf;
    assert.equal(reference.additionalProperties, false);
    assert.deepEqual(Object.keys(reference.properties), ["catalog_ref"]);
    assert.equal(reference.properties.catalog_ref.additionalProperties, false);
    assert.deepEqual(reference.properties.catalog_ref.required, ["path", "command"]);
    assert.deepEqual(inline.required, ["argv"]);
    assert.deepEqual(inline.not, { required: ["catalog_ref"] });
    assert.equal(inline.properties.argv.minItems, 1);
  }
});

test("worker assignment separates exact writes from authorized dependency discovery", () => {
  for (const profile of profiles) {
    const text = coordinatorContract(profile);
    assert.match(text, /smallest relevant read_directories/);
    assert.match(text, /Do not grant the repository root by default/);
    assert.match(text, /parent is already authorized/);
    assert.match(text, /excludes every other member's writable paths/);
  }
});

test("Ultra dispatch guidance distinguishes reference identity from inline digest filling", () => {
  const text = coordinatorContract(profiles.find(profile => profile.slug === "nokiy-direct"));
  assert.match(text, /an omitted catalog_ref sha256 are pinned from current bytes before preparation/);
  assert.match(text, /Select JSON fields instead of dumping complete manifests/);
  assert.match(text, /do not create a duplicate mission file unless required by the task/);
});

test("Efficient advertises the parent completion field accepted by its executor", () => {
  const projected = nativeToolProjection(request(fn), options());
  const schema = projected.body.tools.at(-1).parameters.properties.post_batch_verifier;
  assert.equal(schema.additionalProperties, false);
  assert.deepEqual(schema.properties.parent_source_targets.items, { type: "string", minLength: 1 });
  assert.equal(schema.properties.parent_source_targets.uniqueItems, true);
  assert.equal(schema.properties.parent_source_targets.maxItems, 128);
  const ultra = profiles.find(profile => profile.slug === "nokiy-direct");
  const other = nativeToolProjection(request(fn), options(ultra));
  assert.equal(Object.hasOwn(other.body.tools.find(tool => tool.name === "nokiy_execute").parameters.properties.post_batch_verifier.properties,
    "parent_source_targets"), false);
});

test("Efficient parent barrier requires nonblocking observation and announces original plan before terminal", async () => {
  const payload = { operation: "run_batch", observe: "later", artifact_root: "/task/batch", members: [
    { draft: { workspace: "/workspace", artifact_root: "/task/batch/worker", prompt: "bounded" },
      action: { context_summary: "scope" } }],
    post_batch_verifier: {
      draft: { workspace: "/workspace", artifact_root: "/task/final", prompt: "combined gate", authority_effect: "none" },
      action: { operations: ["read", "command"], read_scopes: ["parent.py"], write_scopes: [], target_paths: [],
        verifier_commands: [{ argv: ["/stub/python", "-m", "unittest"] }] },
      parent_source_targets: ["parent.py"] } };
  const projection = nativeToolProjection(request(custom), options());
  const item = value => ({ type: "custom_tool_call", name: "nokiy_execute", call_id: "barrier", input: JSON.stringify(value) });
  assert.throws(() => nativeToolItem(item({ ...payload, observe: undefined }), projection), /observe:later/);
  assert.throws(() => nativeToolItem(item(payload), { ...projection, adaptiveNative: false }), /Efficient/);
  assert.match(projection.body.tools.at(-1).description, /complete-batch-parent/);
  assert.match(projection.body.tools.at(-1).description, /Efficient dispatch decision/);
  assert.match(projection.body.tools.at(-1).description, /existing read grants cover exact parent targets disjoint from worker writes/);
  assert.match(projection.body.tools.at(-1).description, /retain ordinary parent-owned final validation; do not force this shape/);
  const rendered = nativeToolItem(item(payload), projection);
  const plan = { plan_path: "/task/batch/plan.json", plan_sha256: "a".repeat(64) };
  const printed = [];
  const waits = [];
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const running = runInNewContext(`(async () => { ${rendered.input}\n })()`, {
    text: value => printed.push(JSON.parse(JSON.stringify(value))),
    tools: { exec_command: async () => ({ session_id: 42, output: "" }),
      write_stdin: async args => {
        waits.push(args);
        if (waits.length === 1) return { session_id: 42, output: JSON.stringify(plan) };
        if (waits.length === 2) return { session_id: 42, output: "\n" };
        return pending;
      } },
  });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(printed, [{ status: "PLAN_AVAILABLE_EXECUTION_PENDING", ...plan, parent_completion_required: true }]);
  // V417: explicit parent overlap keeps every observation short so the parent's own edits never queue behind it.
  assert.deepEqual(waits.map(value => value.yield_time_ms), [1000, 1000, 1000]);
  assert.ok(waits.every(value => value.session_id === 42 && value.chars === ""));
  release({ exit_code: 0, output: '{"status":"RESULT_AVAILABLE"}\n' });
  await running;
  assert.equal(printed.length, 2);
  assert.equal(printed[1].terminal, true);
  assert.equal(printed[1].recovery_session_id, 42);
});

test("only the validated Efficient Sol6.1/max profile selects native fusion", () => {
  const before = structuredClone(profiles);
  assert.equal(efficientNativeLoop(efficient), true);
  for (const profile of [null, {}, ...profiles.filter((profile) => profile !== efficient),
    { ...efficient, workflow: "nokiy" }, { ...efficient, slug: "nokiy-direct-sol" },
    { ...efficient, coordinator: { ...efficient.coordinator, model: "gpt-6-astra" } },
    { ...efficient, coordinator: { ...efficient.coordinator, reasoning_effort: "high" } }]) {
    assert.equal(efficientNativeLoop(profile), false);
  }
  assert.deepEqual(profiles, before);
});

for (const tool of [fn, custom]) {
  test(`${tool.type}: a coherent native branch retains actual schemas, authority fields and call pairing`, () => {
    const body = request(tool);
    body.tools.push(appTools, nativeSearch);
    body.input.push({ type: "additional_tools", tools: [{ type: "function", name: "tool_search", namespace: "functions",
      defer_loading: true, parameters: { type: "object", properties: { query: { type: "string" } }, required: ["query"] } }] },
    { type: "tool_search_output", call_id: "original_discovery", tools: [{ ...appTools.tools[0], namespace: "codex_app" }] });
    body.tool_choice = { type: "allowed_tools", mode: "auto", tools: [{ type: "tool_search" },
      { type: "function", name: "send_message_to_thread", namespace: "mcp__codex_app" }] };
    const before = structuredClone(body);
    const projected = nativeToolProjection(body, options());
    assert.deepEqual(projected.body.tools.slice(0, -1), body.tools);
    assert.equal(projected.body.tools.at(-1).name, "nokiy_execute");
    assert.equal(projected.coordinatorBindings, null);
    assert.equal(projected.nativeExecution, null, "no synthetic native command alias is needed");
    assert.equal(projected.body.instructions, body.instructions);
    assert.deepEqual(projected.body.input, body.input);
    assert.deepEqual(projected.body.tool_choice, body.tool_choice);
    const item = call(tool.type === "function" ? "function_call" : "custom_tool_call", tool.name);
    assert.deepEqual(nativeToolItem(item, projected), item);
    const event = { type: "response.output_item.done", item };
    assert.deepEqual(nativeEventProjector(projected)(event), event);
    const complete = { type: "response.completed", response: { status: "completed", output: [item] } };
    assert.deepEqual(nativeEventProjector(projected)(complete), complete);
    const parser = nativeSseProjector(nativeEventProjector(projected));
    assert.deepEqual(JSON.parse(parser.push(Buffer.from(`data: ${JSON.stringify(event)}\n\n`)).slice(6)), event);
    for (const native of [...appNames.map((name) => ({ type: "function_call", name, namespace: "mcp__codex_app",
      id: `app_${name}`, call_id: `original_${name}`, arguments: '{ "thread_id": "authorized_thread", "message": "unchanged" }' })),
      { type: "function_call", name: "read_thread", namespace: "codex_app", call_id: "flat_app", arguments: '{"thread_id":"authorized_thread"}' },
      { type: "function_call", name: "tool_search", namespace: "functions", call_id: "function_search", arguments: '{"query":"App"}' },
      { type: "tool_search_call", execution: "client", id: "search_item", call_id: "native_search",
        status: "completed", arguments: { query: "App recovery", limit: 4 } }]) {
      assert.deepEqual(nativeToolItem(native, projected), native);
      for (const nativeEvent of [{ type: "response.output_item.added", item: native }, { type: "response.output_item.done", item: native },
        { type: "response.completed", response: { output: [native] } }]) {
        assert.deepEqual(nativeEventProjector(projected)(nativeEvent), nativeEvent);
        assert.deepEqual(JSON.parse(parser.push(Buffer.from(`data: ${JSON.stringify(nativeEvent)}\n\n`)).slice(6)), nativeEvent);
      }
    }
    assert.deepEqual(body, before);
    assert.match(projected.body.tools.at(-1).description, /Fused native execution is not independent Nokiy runtime acceleration/);
    assert.match(projected.body.tools.at(-1).description, /a coherent native branch needs no worker preparation or artifact root/);
  });
}

test("Efficient preserves omission of App and discovery schemas despite guidance naming them", () => {
  const body = request();
  body.instructions += " Native Apps codex_app and mcp__codex_app: read_thread, list_threads, wait_threads, send_message_to_thread, handoff_thread; discover with tool_search.";
  const before = structuredClone(body), projected = nativeToolProjection(body, options());
  assert.deepEqual(projected.body.tools.slice(0, -1), body.tools);
  assert.deepEqual(projected.body.input, body.input);
  assert.equal(projected.coordinatorBindings, null);
  assert.equal(flatten(projected.body.tools).some((tool) => appNames.includes(tool.name)
    || tool.name === "tool_search" || tool.type === "tool_search"), false);
  assert.deepEqual(body, before);
});

test("client model, profile fields, instructions and metadata cannot enable native fusion", () => {
  const body = { ...request(), profile: efficient, metadata: { profile: efficient, native_loop: true },
    instructions: coordinatorContract(efficient) };
  const { profile: _profile, ...unbound } = options();
  for (const requestBody of [body, { ...body, model: "nokiy-efficient" }]) {
    const projected = nativeToolProjection(requestBody, unbound);
    assert.ok(projected.coordinatorBindings);
    assert.ok(!flatten(projected.body.tools).some((tool) => tool.name === "apply_patch"));
    assert.throws(() => nativeEventProjector(projected)({ type: "response.output_item.done", item: call() }),
      { code: "NOKIY_COORDINATOR_TOOL_FORBIDDEN" });
  }
  for (const changed of [{ ...request(), model: "gpt-6-astra" }, { ...request(), reasoning: { effort: "high" } }]) {
    assert.throws(() => nativeToolProjection(changed, options()), { code: "NOKIY_NATIVE_TOOL_INVALID" });
  }
  assert.throws(() => nativeToolProjection(request(), { ...options(), workflow: "nokiy" }), { code: "NOKIY_NATIVE_TOOL_INVALID" });
});

test("unsupported, remote or ambiguous native bindings fail explicitly without fabricated tools", () => {
  for (const body of [request({ ...custom, description: "No native declaration" }),
    request({ ...fn, parameters: { ...fn.parameters, required: [] } }),
    { ...request(), tools: [{ type: "namespace", name: "remote_mcp", tools: [fn] }] },
    { ...request(), tools: [{ type: "namespace", name: "functions", tools: [fn, fn] }] }]) {
    assert.throws(() => nativeToolProjection(body, options()), {
      code: "NOKIY_NATIVE_TOOL_INVALID", message: /unique supported advertised native/,
    });
  }
  assert.throws(() => nativeToolProjection({ ...request(), tools: [] }, options()), { code: "NOKIY_NATIVE_TOOL_INVALID" });
  assert.throws(() => nativeToolProjection(request(), { ...options(), host: { ...host, coordinator_mode: "invented" } }),
    { code: "NOKIY_COORDINATOR_MODE_INVALID" });
});

test("same-connection native continuation carries only the confirmed executor binding", () => {
  const initial = nativeToolProjection(request(), options());
  const delta = { ...request(), tools: [], input: [{ type: "message", role: "user", content: "Continue this branch." }] };
  const projected = nativeToolProjection(delta, { ...options(), continuationTarget: initial.target });
  assert.deepEqual(projected.body.input, delta.input);
  assert.deepEqual(nativeToolItem(call(), projected), call());
  assert.throws(() => nativeToolProjection(delta, { ...options(),
    continuationTarget: { name: "exec_command", type: "function", namespace: "functions" } }),
  { code: "NOKIY_NATIVE_TOOL_INVALID" });
});

test("historical tool updates replace a binding without changing the current native schema", () => {
  for (const type of ["additional_tools", "tool_search_output"]) {
    const old = { ...custom, description: "An older, no longer supported declaration" };
    const body = { ...request(custom), tools: [], input: [
      { type: "additional_tools", role: "developer", tools: [{ type: "namespace", name: "functions", tools: [old] }] },
      { type, tools: request(custom).tools },
      { type, tools: [{ type: "function", name: "unrelated", parameters: {} }] },
    ] };
    const snapshot = structuredClone(body);
    const expected = nativeToolProjection(request(custom), options());
    const projected = nativeToolProjection(body, options());
    assert.deepEqual(projected.target.executionSchema, expected.target.executionSchema);
    assert.deepEqual(projected.body.input, body.input);
    assert.deepEqual(body, snapshot);
    assert.deepEqual(nativeToolItem(call("custom_tool_call", "exec"), projected), call("custom_tool_call", "exec"));
  }
});

test("current top-level declaration is authoritative over historical schemas", () => {
  const older = structuredClone(fn);
  older.parameters.properties.historical_only = { type: "string" };
  const body = { ...request(), input: [{ type: "additional_tools", tools: request(older).tools }] };
  const projected = nativeToolProjection(body, options());
  assert.deepEqual(projected.target.executionSchema, fn.parameters);
  assert.equal(projected.target.executionSchema.properties.historical_only, undefined);
});

test("latest invalid or same-batch ambiguous declaration cannot fall back to history or continuation", () => {
  const initial = nativeToolProjection(request(custom), options());
  for (const latest of [[{ ...custom, description: "Unsupported current declaration" }], [custom, custom]]) {
    const body = { ...request(custom), tools: [], input: [
      { type: "additional_tools", tools: request(custom).tools },
      { type: "additional_tools", tools: [{ type: "namespace", name: "functions", tools: latest }] },
    ] };
    assert.throws(() => nativeToolProjection(body, { ...options(), continuationTarget: initial.target }),
      { code: "NOKIY_NATIVE_TOOL_INVALID" });
  }
});

test("Efficient guidance chooses useful branches, honors explicit runtimes and has no shared parent-only mandate", () => {
  const text = coordinatorContract(efficient);
  assert.match(text, /SAME native Codex loop/);
  assert.match(text, /genuinely independent useful branches/);
  assert.match(text, /honor explicit independent-runtime or selected-worker requests even for one branch/);
  assert.match(text, /independently verifiable branches with nonoverlapping write scopes/);
  assert.match(text, /Keep producer-consumer chains ordered and coherent/);
  assert.match(text, /within configured capacity/);
  assert.match(text, /Do not fill capacity automatically/);
  assert.match(text, /Wait for the original batch terminal before creating another batch/);
  assert.match(text, /Selecting this workflow grants no new authority/);
  assert.match(text, /native permission denial, owner conflict or CAS failure/);
  assert.match(text, /Fused native execution is not independent Nokiy runtime acceleration/);
  assert.match(text, /no universal performance claim/);
  assert.match(text, /let that worker inspect its implementation and tests rather than first solving or reading the whole branch yourself/);
  assert.match(text, /If dependencies or scope are unclear, inspect the necessary source before dispatch; this is not a read limit/);
  assert.match(text, /review the delegated diff and integrated checks on return/);
  assert.doesNotMatch(text, /is a coordination-only parent|Delegate navigation, implementation|Do not execute that work yourself|otherwise choose run\.|No fallback to parent shell/);
});

test("Efficient assesses independent outputs before offering native execution", () => {
  const text = coordinatorContract(efficient);
  const assessment = text.indexOf("proactively check for at least two useful independent authorized");
  const batch = text.indexOf("Two or more independently implementable and verifiable branches with nonoverlapping write scopes belong to workers");
  const fallback = text.indexOf("Keep native implementation within one coherent dependent branch");
  const nativeRole = text.indexOf("is the native-capable coordinator for this Efficient task");
  const nativeGuidance = text.indexOf("one coherent branch in the SAME native Codex loop");
  assert.ok(assessment >= 0 && assessment < batch && batch < fallback
    && fallback < nativeRole && nativeRole < nativeGuidance,
    "assess outputs and choose dispatch before presenting the native-loop permission");
  assert.equal(text.split("proactively check for at least two useful independent authorized").length - 1, 1);
});

test("Efficient keeps independent long branches useful and its native fallback evidence-based", () => {
  const text = coordinatorContract(efficient);
  assert.match(text, /independently verifiable branches with nonoverlapping write scopes/);
  assert.match(text, /Judge dependencies between proposed outputs, not shared inputs/);
  assert.match(text, /each can be completed and verified from the authorized starting evidence without another's produced result or changed artifact/);
  assert.match(text, /A common final report is a downstream consumer, not a dependency between those outputs/);
  assert.match(text, /dispatch every such branch in one run_batch instead of implementing one yourself/);
  assert.match(text, /retain a parent implementation branch beside workers only when it needs a worker's result/);
  assert.match(text, /Dispatch-first: when the task already names two or more independent implementation branches and their files/);
  assert.match(text, /dispatch them all as member intents in one run_batch after at most one preparatory call/);
  assert.match(text, /chain independent reads in one command or script, never one call per question/);
  assert.match(text, /Codex already loaded the applicable AGENTS\.md chain into your context; do not search for instruction files again/);
  assert.match(text, /Do not read delegated source, compute digests or compose a detailed handoff first/);
  assert.match(text, /J-Space enforces each member's write scope and the batch summary returns every changed file with its diff, so do not snapshot workspace hashes or read the acceptance harness before dispatch/);
  assert.doesNotMatch(text, /keep a substantial branch for this native loop/);
  assert.match(text, /total execution\/input\/coordination cost at equal quality/);
  assert.match(text, /Do not split one coherent dependent branch merely for concurrency or file count/);
  assert.match(text, /Retain run for one coherent delegated deliverable with an existing valid artifact root/);
  assert.match(text, /Keep native implementation within one coherent dependent branch/);
  assert.doesNotMatch(text, /complete the task in the same native loop, including multiple independent branches/);
  assert.match(text, /including worker context reconstruction, dispatch, integration and final acceptance/);
  assert.doesNotMatch(text, /Select the same native loop only|changed line .* every|under about 100 changed lines/);
  assert.match(text, /honor explicit independent-runtime or selected-worker requests even for one branch/);
  assert.doesNotMatch(text, /otherwise complete one coherent branch in the same native loop/);
});

test("Efficient's small-task exception never merges independent branches", () => {
  const text = coordinatorContract(efficient);
  assert.match(text, /Two or more independently implementable and verifiable branches with nonoverlapping write scopes belong to workers even when small or in the same worktree/);
  assert.doesNotMatch(text, /one coherent branch or a tiny task|only when it is tiny|Do not split tiny tasks/);
  assert.match(text, /Keep producer-consumer chains ordered and coherent/);
  assert.match(text, /Honor explicit independent-runtime or selected-worker requests even when native execution would otherwise be chosen/);
  const tools = flatten(nativeToolProjection(request(), options()).body.tools);
  for (const name of ["exec_command", "apply_patch", "nokiy_execute"]) {
    assert.ok(tools.some((tool) => tool.name === name), `${name} remains available`);
  }
});

test("branch-first ordering is specific to the validated Efficient mode", () => {
  for (const profile of profiles.filter((profile) => profile !== efficient)) {
    const before = structuredClone(profile);
    const text = coordinatorContract(profile);
    assert.ok(text.startsWith(`${profile.display_name}:`));
    assert.match(text, /is a coordination-only parent/);
    assert.doesNotMatch(text, /For two or more useful independent branches|observe":"later" so the call|Select the same native loop only/);
    if (profile.workflow === "nokiy") {
      assert.match(text, /Execute one bounded action at a time; no batch or fan-out/);
      assert.doesNotMatch(text, /proactively check for at least two/);
    } else {
      assert.ok(text.indexOf("proactively check for at least two")
        > text.indexOf("Selecting this workflow grants no new authority"));
      assert.match(text, /otherwise choose run\./);
    }
    assert.deepEqual(profile, before);
  }
});

test("Serial and Ultra keep restricted tools and observation-only stdin even with a Sol worker", () => {
  for (const profile of [...profiles.filter((profile) => profile !== efficient),
    ...["luna", "sol", "astra"].map((family) => ({ ...profiles[1], slug: `nokiy-direct-${family}` }))]) {
    const projected = nativeToolProjection(request(), options(profile));
    const tools = flatten(projected.body.tools);
    assert.ok(projected.coordinatorBindings);
    assert.ok(!tools.some((tool) => ["apply_patch", "exec", "exec_command"].includes(tool.name)));
    assert.deepEqual(tools.find((tool) => tool.name === "write_stdin").parameters.properties.chars.enum, [""]);
    for (const item of [call(), { ...call(), name: "apply_patch" },
      { ...call(), name: "write_stdin", arguments: JSON.stringify({ session_id: 42, chars: "input" }) }]) {
      assert.throws(() => nativeEventProjector(projected)({ type: "response.output_item.done", item }),
        { code: "NOKIY_COORDINATOR_TOOL_FORBIDDEN" });
    }
    assert.match(coordinatorContract(profile), /is a coordination-only parent/);
    assert.match(coordinatorContract(profile), /Delegate navigation, implementation/);
    assert.doesNotMatch(coordinatorContract(profile), /Fused native execution|SAME native Codex loop/);
  }
});

test("installed workers retain batch concurrency, exact envelopes and original-ID recovery", () => {
  const adaptive = nativeToolProjection(request(), options());
  const legacy = nativeToolProjection(request(), options(profiles[1]));
  const schema = (projection) => projection.body.tools.find((tool) => tool.name === "nokiy_execute").parameters;
  const adaptiveShared = structuredClone(schema(adaptive));
  delete adaptiveShared.properties.post_batch_verifier.properties.parent_source_targets;
  delete adaptiveShared.properties.post_batch_verifier.properties.intent.properties.parent_source_targets;
  assert.deepEqual(adaptiveShared, schema(legacy));
  assert.deepEqual(adaptive.operations, legacy.operations);
  const member = (id) => ({ draft: { workspace: "/not/read/workspace", artifact_root: `/not/read/batch/${id}`,
    prompt: `Independent useful branch ${id}`, authority_effect: "none", timeout_seconds: null },
    action: { mission: { mission_id: "fixture", task_id: id, mode: "DELIVERY", objective: "One output", current_predicate: "Missing output" },
      operations: ["read", "command"], read_scopes: ["input.txt"], write_scopes: [], target_paths: [],
      source_read: true, command_templates: [], forbidden_effects: ["deployment"] } });
  for (const payload of [{ operation: "run_batch", artifact_root: "/not/read/batch", max_parallel_workers: 1,
    members: [member("a"), member("b")] },
  { operation: "run_batch", artifact_root: "/not/read/batch", members: [member("requested-independent-runtime")] },
  { operation: "read_batch", plan_path: "/not/read/original-plan.json", plan_sha256: "a".repeat(64) },
  { operation: "read_result", artifact_root: "/not/read/original", request_id: "original_request" }]) {
    const item = { type: "function_call", name: "nokiy_execute", call_id: "worker_call", arguments: JSON.stringify(payload) };
    assert.deepEqual(nativeToolItem(item, adaptive), nativeToolItem(item, legacy));
    if (payload.operation === "run_batch") {
      const waiting = adaptive;
      assert.equal(JSON.parse(nativeToolItem(item, waiting).arguments).yield_time_ms, 30000);
      const early = { ...item, arguments: JSON.stringify({ ...payload, observe: "later" }) };
      assert.equal(JSON.parse(nativeToolItem(early, waiting).arguments).yield_time_ms, 1000);
      const bounded = structuredClone(waiting);
      bounded.target.executionSchema.properties.yield_time_ms.maximum = 5000;
      assert.equal(JSON.parse(nativeToolItem(item, bounded).arguments).yield_time_ms, 5000);
    }
    const encoded = JSON.parse(nativeToolItem(item, adaptive).arguments).cmd.match(/ '--payload' '([A-Za-z0-9_-]+)'$/)?.[1];
    assert.ok(encoded);
    assert.deepEqual(JSON.parse(Buffer.from(encoded, "base64url").toString("utf8")), {
      schema_version: "nokiy_native_tool_v2", worker: efficient.worker.model,
      reasoning_effort: efficient.worker.reasoning_effort, service_tier: efficient.worker.service_tier, payload,
    });
  }
});

// This integration gate requires the router to pass its validated profile into
// nativeToolProjection; text or the projected Sol model must never substitute.
test("the actual Efficient alias binds native fusion while preserving worker picker identities", () => {
  for (const routeHost of [host, { ...host, coordinator_mode: undefined }])
  for (const tool of [fn, custom]) for (const effort of ["low", "medium", "high", "max"]) {
    const body = { ...request(tool), model: "nokiy-efficient", reasoning: { effort } }, before = structuredClone(body);
    const routed = projectNokiyDirect(body, { catalog, profiles, host: routeHost });
    assert.equal(routed.body.model, "gpt-6.1-sol");
    assert.equal(routed.body.reasoning.effort, "max");
    assert.deepEqual(routed.body.tools.slice(0, -1), body.tools);
    assert.equal(routed.nativeTool.coordinatorBindings, null);
    const selected = effort === "max" ? "medium" : effort;
    assert.equal(routed.nativeTool.worker, efficient.worker_selector.models[selected]);
    assert.equal(routed.nativeTool.workerEffort, efficient.worker_selector.reasoning_efforts[selected]);
    assert.equal(routed.nativeTool.serviceTier, "default");
    assert.match(routed.body.instructions, /SAME native Codex loop/);
    assert.deepEqual(body, before);
  }
});
