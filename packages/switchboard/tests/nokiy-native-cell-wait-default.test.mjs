import assert from "node:assert/strict";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import { nativeToolProjection, nativeToolItem, continueNativeObservation,
  nativeEventProjector, nativeSseProjector } from "../src/nokiy-native-tool.mjs";

const options = { host: { python: "/never/python", script: "/never/host.py", coordinator_mode: "dispatch_only_v1" },
  worker: "gpt-6.1-sol", workerEffort: "max", workflow: "nokiy" };
const schema = { type: "object", properties: {
  cell_id: { type: "string" }, yield_time_ms: { type: "integer", minimum: 1 },
  max_tokens: { type: "integer", minimum: 0 }, terminate: { type: "boolean" },
}, required: ["cell_id"], additionalProperties: false };
const FLOOR = 300000;
const exec = { type: "custom", name: "exec", format: { type: "text" }, description:
  "declare const tools: { exec_command(args: {cmd: string;}): Promise<unknown>; };\n"
    + "declare const tools: { write_stdin(args: {session_id: number; chars?: string;}): Promise<unknown>; };" };
const waitTool = (parameters = schema) => ({ type: "function", name: "wait", parameters });
const body = (input = [], parameters = schema, extra = {}) => ({ tools: [
  { type: "namespace", name: "functions", tools: [exec, waitTool(parameters)] },
], input, ...extra });
const output = (call, value) => ({ type: call.type === "function_call" ? "function_call_output" : "custom_tool_call_output",
  call_id: call.call_id, output: value });
const yielded = "Script running with cell ID original_cell\nWall time 600 seconds\nOutput:\n";
const wait = (args = { cell_id: "original_cell" }, extra = {}) => ({ type: "function_call", name: "wait",
  namespace: "functions", id: "wait_item", call_id: "wait_call", arguments: JSON.stringify(args), ...extra });
const pending = (parameters = schema, extra = {}) => {
  const initial = nativeToolProjection(body([], parameters, extra), options);
  const source = nativeToolItem({ type: "custom_tool_call", name: "nokiy_native", call_id: "original",
    input: '{"cmd":"NOT_EXECUTED"}' }, initial);
  return continueNativeObservation(nativeToolProjection(body([source, output(source, yielded)], parameters, extra), options));
};
const unchanged = (item, projection) => assert.equal(nativeToolItem(item, projection), item);

test("Efficient preserves native waits so independent parent work can continue", () => {
  const opts = { ...options, workflow: "nokiy-direct", profile: { slug: "nokiy-efficient",
    workflow: "nokiy-direct", coordinator: {model:"gpt-6.1-sol",reasoning_effort:"max"} } };
  const efficientBody = input => body(input, schema, {model:"gpt-6.1-sol",reasoning:{effort:"max"}});
  const initial = nativeToolProjection(efficientBody([]), opts);
  const source = nativeToolItem({ type: "custom_tool_call", name: "nokiy_execute", call_id: "worker",
    input: JSON.stringify({ operation: "run", observe: "later", draft: { workspace: "/ws",
      artifact_root: "/artifacts/worker", prompt: "bounded work", authority_effect: "none" },
      action: { context_summary: "bounded" } }) }, initial);
  const projection = continueNativeObservation(nativeToolProjection(efficientBody([source, output(source, yielded)]), opts));
  assert.equal(projection.coordinatorBindings, null);
  assert.equal(projection.nativeObservationState.pendingCells.length, 1);
  for (const yield_time_ms of [1000, 60000, FLOOR]) {
    unchanged(wait({cell_id:"original_cell",yield_time_ms}), projection);
  }
  unchanged(wait(), projection);
  unchanged(wait({cell_id:"unknown_cell",yield_time_ms:1000}), projection);
  unchanged(wait({cell_id:"original_cell",terminate:true}), projection);
  unchanged(wait(), { ...projection, nativeObservationConflicts: ["worker"] });
  const project = nativeEventProjector(projection);
  assert.equal(project({type:"response.output_item.added",item:wait({}, {arguments:""})}), null);
  assert.equal(project({type:"response.function_call_arguments.delta",item_id:"wait_item",delta:"{}"}), null);
  const done = project({type:"response.output_item.done",item:wait()});
  assert.equal(JSON.parse(done.item.arguments).yield_time_ms, undefined);
  const completed = project({type:"response.completed",response:{output:[wait()]}});
  assert.equal(completed.response.output[0].arguments, done.item.arguments);
  const implicit = wait({cell_id:"original_cell",yield_time_ms:1000});
  delete implicit.namespace;
  assert.equal(JSON.parse(nativeToolItem(implicit, projection).arguments).yield_time_ms, 1000);
  const stream = nativeEventProjector(projection);
  assert.equal(stream({type:"response.output_item.added",item:{...implicit,arguments:""}}), null);
  assert.equal(JSON.parse(stream({type:"response.output_item.done",item:implicit}).item.arguments).yield_time_ms, 1000);
});

test("a uniquely bound original cell receives only the schema-accepted missing yield", () => {
  const projection = pending(), state = projection.nativeObservationState;
  assert.equal(state.pendingCells.length, 1);
  assert.deepEqual(state.cellWaitControl.schema, schema);
  assert.notEqual(state.cellWaitControl.schema, schema);
  assert.deepEqual(JSON.parse(JSON.stringify(state.cellWaitControl.schema)), schema);
  for (const args of [{ cell_id: "original_cell" }, { cell_id: "original_cell", max_tokens: 17, terminate: false }]) {
    const item = wait(args), original = structuredClone(item);
    const projected = nativeToolItem(item, projection);
    assert.deepEqual(projected, { ...item, arguments: JSON.stringify({ ...args, yield_time_ms: FLOOR }) });
    assert.deepEqual(item, original);
    assert.deepEqual(Object.keys(JSON.parse(projected.arguments)).sort(), [...Object.keys(args), "yield_time_ms"].sort());
  }
});

test("explicit polls below the floor are raised; long, malformed or terminating waits are untouched", () => {
  const projection = pending();
  for (const yield_time_ms of [0, 1, 1000, 10000, 60000, 120000, FLOOR - 1]) {
    const args = { cell_id: "original_cell", max_tokens: 12000, yield_time_ms };
    assert.deepEqual(JSON.parse(nativeToolItem(wait(args), projection).arguments), { ...args, yield_time_ms: FLOOR });
  }
  // Explicit waits at or above the operator's 300000 ms rule, including hour-long ones, are kept.
  for (const yield_time_ms of [FLOOR, FLOOR + 1, 600000, 3600000, null, "300000", -1, 1.5]) {
    unchanged(wait({ cell_id: "original_cell", yield_time_ms }), projection);
  }
  for (const yield_time_ms of [1000, 60000]) {
    unchanged(wait({ cell_id: "original_cell", yield_time_ms, terminate: true }), projection);
    // Without any visible origin the cell can only be an earlier coordinator cell (compacted or
    // from a lost connection): a coordinator cannot launch raw exec cells. Visible foreign origins
    // stay untouched (see the visible-origin test below).
    assert.equal(JSON.parse(nativeToolItem(wait({ cell_id: "unseen_cell", yield_time_ms }), projection).arguments).yield_time_ms, FLOOR);
  }
  for (const terminate of [true, null, 0, "false"]) unchanged(wait({ cell_id: "original_cell", terminate }), projection);
  for (const args of [null, [], {}, { cell_id: 42 }, { cell_id: "invalid cell" },
    { cell_id: "original_cell", unexpected: true }, { cell_id: "original_cell", max_tokens: "17" }]) unchanged(wait(args), projection);
  for (const arguments_ of ["not JSON", '{"cell_id":', " ", "x".repeat(4097)]) {
    unchanged(wait({}, { arguments: arguments_ }), projection);
  }
  unchanged(wait({}, { arguments: ' { "cell_id": "original_cell", "yield_time_ms": 3600000 } ' }), projection);
  for (const extra of [{ namespace: "remote" }, { namespace: null }, { type: "custom_tool_call" }, { name: "write_stdin" }]) {
    unchanged(wait({ cell_id: "original_cell" }, extra), projection);
  }
});

test("missing, old, conflicting or foreign ownership never supplies defaults", () => {
  const projection = pending(), state = projection.nativeObservationState;
  const old = { ...state.cellWaitControl };
  delete old.schema;
  for (const changed of [undefined, { ...state, owner: "foreign" }, { ...state, pendingCells: [] },
    { ...state, pendingCells: [...state.pendingCells, state.pendingCells[0]] },
    { ...state, cellWaitControl: null }, { ...state, cellWaitControl: old },
    { ...state, cellWaitControl: { ...state.cellWaitControl, namespace: "remote" } }]) {
    unchanged(wait(), { ...projection, nativeObservationState: changed });
  }
  unchanged(wait(), { ...projection, nativeObservationConflicts: ["original"] });
  unchanged(wait(), { ...projection, coordinatorBindings: null });
  unchanged(wait(), nativeToolProjection(body(), options));
  const source = nativeToolItem({ type: "custom_tool_call", name: "nokiy_native", call_id: "original",
    input: '{"cmd":"NOT_EXECUTED"}' }, nativeToolProjection(body(), options));
  const input = [source, output(source, yielded)];
  for (const tools of [[{ type: "namespace", name: "functions", tools: [exec] }],
    [{ type: "namespace", name: "functions", tools: [exec, waitTool(), waitTool()] }],
    [...body().tools, { type: "namespace", name: "remote", tools: [waitTool()] }]]) {
    unchanged(wait(), continueNativeObservation(nativeToolProjection({ tools, input }, options)));
  }
});

test("the complete native schema, not a local hardcoded bound, admits the default", () => {
  const restricted = [
    { ...schema, properties: { ...schema.properties, yield_time_ms: { type: "integer", minimum: FLOOR + 1 } } },
    { ...schema, properties: { ...schema.properties, yield_time_ms: { type: "integer", enum: [10000, 600000] } } },
    { ...schema, properties: { ...schema.properties, yield_time_ms: { type: "string" } } },
    { ...schema, required: ["cell_id", "max_tokens"] },
    { ...schema, not: { properties: { yield_time_ms: { const: FLOOR } }, required: ["yield_time_ms"] } },
    { ...schema, properties: { ...schema.properties, yield_time_ms: { $ref: "missing-schema" } } },
  ];
  const absent = structuredClone(schema);
  delete absent.properties.yield_time_ms;
  restricted.push(absent);
  for (const parameters of restricted) unchanged(wait(), pending(parameters));
  const defaults = { ...schema, properties: { ...schema.properties,
    yield_time_ms: { type: "integer", enum: [10000, FLOOR], default: 10000 },
    max_tokens: { type: "integer", default: 999 },
  } };
  assert.deepEqual(JSON.parse(nativeToolItem(wait(), pending(defaults)).arguments), { cell_id: "original_cell", yield_time_ms: FLOOR });
  assert.deepEqual(defaults.properties.max_tokens, { type: "integer", default: 999 }, "AJV must not insert unrelated defaults");
});

test("a native schema maximum below the floor bounds the raised wait instead of dropping it", () => {
  const bounded = { ...schema, properties: { ...schema.properties, yield_time_ms: { type: "integer", minimum: 1, maximum: 120000 } } };
  const projection = pending(bounded);
  assert.equal(JSON.parse(nativeToolItem(wait(), projection).arguments).yield_time_ms, 120000);
  assert.equal(JSON.parse(nativeToolItem(wait({ cell_id: "original_cell", yield_time_ms: 1000 }), projection).arguments).yield_time_ms, 120000);
  for (const yield_time_ms of [120000, 119999 + 1]) unchanged(wait({ cell_id: "original_cell", yield_time_ms }), projection);
  const exclusive = { ...schema, properties: { ...schema.properties, yield_time_ms: { type: "integer", exclusiveMaximum: 120000 } } };
  unchanged(wait(), pending(exclusive));
});

test("authenticated continuation retains the full schema without accepting old state as schema", () => {
  const projection = pending(), call = nativeToolItem(wait(), projection);
  const carry = { state: projection.nativeObservationState, call };
  const next = continueNativeObservation(nativeToolProjection({ tools: [], previous_response_id: "same_chain",
    input: [output(call, yielded)] }, { ...options, continuationTarget: projection.target,
    continuationCoordinatorBindings: projection.coordinatorBindings }), carry);
  assert.deepEqual(next.nativeObservationState.cellWaitControl.schema, schema);
  assert.equal(JSON.parse(nativeToolItem(wait(), next).arguments).yield_time_ms, FLOOR);
  assert.equal(next.nativeObservationState.pendingCells[0].source_call_id, "original");
  const foreign = continueNativeObservation(nativeToolProjection({ tools: [], previous_response_id: "same_chain",
    input: [output(call, yielded)] }, { ...options, continuationTarget: projection.target,
    continuationCoordinatorBindings: projection.coordinatorBindings }), { ...carry, state: { ...carry.state, owner: "foreign" } });
  unchanged(wait(), foreign);
});

// 2026-10-19 00:04 Taipei: the date changed while a worker cell was running, so Codex sent the
// yielded output followed by an <environment_context> message and the cell was never tracked.
test("a continuation output followed by context messages still binds the yielded cell", () => {
  const initial = nativeToolProjection(body(), options);
  const source = nativeToolItem({ type: "custom_tool_call", name: "nokiy_native", call_id: "original",
    input: '{"cmd":"NOT_EXECUTED"}' }, initial);
  const carry = { state: continueNativeObservation(initial).nativeObservationState, call: source };
  const context = { type: "message", role: "user",
    content: [{ type: "input_text", text: "<environment_context><current_date>2026-10-19</current_date></environment_context>" }] };
  const chained = (input) => continueNativeObservation(nativeToolProjection({ tools: [], previous_response_id: "same_chain", input },
    { ...options, continuationTarget: initial.target, continuationCoordinatorBindings: initial.coordinatorBindings }), carry);
  for (const trailing of [[], [context], [context, { ...context, content: [{ type: "input_text", text: "user steer" }] }]]) {
    const next = chained([output(source, yielded), ...trailing]);
    assert.equal(next.nativeObservationState.pendingCells.length, 1, String(trailing.length));
    assert.equal(JSON.parse(nativeToolItem(wait({ cell_id: "original_cell", yield_time_ms: 1000 }), next).arguments).yield_time_ms, FLOOR);
  }
  for (const input of [[context, output(source, yielded)], [output(source, yielded), output({ ...source, call_id: "other" }, "x")]]) {
    assert.equal(chained(input).nativeObservationState.pendingCells.length, 0);
  }
});

test("streamed waits expose no partial arguments and done/completed projections agree", () => {
  const projection = pending(), project = nativeEventProjector(projection), item = wait();
  assert.equal(project({ type: "response.output_item.added", item: { ...item, arguments: "" } }), null);
  for (const type of ["response.function_call_arguments.delta", "response.function_call_arguments.done"]) {
    assert.equal(project({ type, item_id: item.id, delta: item.arguments, arguments: item.arguments }), null);
  }
  const done = project({ type: "response.output_item.done", item }).item;
  const completed = project({ type: "response.completed", response: { output: [item] } }).response.output[0];
  assert.deepEqual(done, completed);
  assert.equal(done.id, item.id);
  assert.equal(done.call_id, item.call_id);
  assert.equal(done.type, item.type);
  assert.deepEqual(JSON.parse(done.arguments), { cell_id: "original_cell", yield_time_ms: FLOOR });
  const normalize = nativeEventProjector(projection)({ type: "response.output_item.done", item: { ...item, namespace: undefined } }).item;
  assert.deepEqual(normalize, done, "only the existing unique native binding normalizes an omitted namespace");
  const parser = nativeSseProjector(nativeEventProjector(projection));
  const frame = (event) => Buffer.from(`data: ${JSON.stringify(event)}\n\n`);
  assert.equal(parser.push(frame({ type: "response.output_item.added", item: { ...item, arguments: "" } })), "");
  assert.equal(parser.push(frame({ type: "response.function_call_arguments.delta", item_id: item.id, delta: item.arguments })), "");
  assert.deepEqual(JSON.parse(parser.push(frame({ type: "response.output_item.done", item })).slice(6)).item, done);
  assert.equal(parser.end(), "");
});

test("zero-model virtual waits reduce control calls, not worker lifetime or claimed inference time", async (t) => {
  const projection = pending(), state = projection.nativeObservationState;
  const oldControl = { ...state.cellWaitControl };
  delete oldControl.schema;
  const baseline = { ...projection, nativeObservationState: { ...state, cellWaitControl: oldControl } };
  const simulate = async (selected) => {
    let remaining = 350000, calls = 0, elapsed = 0;
    return runInNewContext(`(async () => {
      for (;;) {
        const args = project();
        const result = await wait(args);
        if (result.terminal) return result;
      }
    })()`, {
      project: () => JSON.parse(nativeToolItem(wait(), selected).arguments),
      wait: async (args) => {
        assert.equal(args.cell_id, "original_cell");
        assert.deepEqual(Object.keys(args).sort(), args.yield_time_ms === undefined ? ["cell_id"] : ["cell_id", "yield_time_ms"]);
        const observed = Math.min(args.yield_time_ms ?? 10000, remaining);
        remaining -= observed;
        elapsed += observed;
        calls += 1;
        return { terminal: remaining === 0, calls, elapsed_ms: elapsed, output: remaining === 0 ? "same terminal fixture" : "" };
      },
    });
  };
  const before = await simulate(baseline), after = await simulate(projection);
  assert.equal(before.calls, 35);
  // A 350 s run: one five-minute wait plus the remainder, instead of 35 ten-second polls.
  assert.equal(after.calls, 2);
  assert.equal(before.elapsed_ms, after.elapsed_ms);
  assert.equal(before.output, after.output);
  assert.equal(before.terminal, after.terminal);
  t.diagnostic(JSON.stringify({ scope: "VIRTUAL_CONTROL_CALLS_ONLY_NOT_PROVIDER_OR_RUNTIME_SPEEDUP",
    model_calls: 0, worker_launches: 0, before_wait_calls: before.calls, after_wait_calls: after.calls,
    both_virtual_elapsed_ms: after.elapsed_ms, same_terminal_output: true }));
});

// Observed coordinator pattern: peek at 1 s, then poll every 60 s until the worker finishes.
// Since 2026-10-26 the floor is the operator's five-minute wait, so a long run still reports.
test("a 1 s peek and 60 s polls over a 20-minute worker collapse to five-minute waits", () => {
  const projection = pending();
  const run = (raise) => {
    let remaining = 1_200_000, calls = 0;
    for (const requested of [1000, ...Array(40).fill(60000)]) {
      const item = wait({ cell_id: "original_cell", yield_time_ms: requested });
      const args = JSON.parse((raise ? nativeToolItem(item, projection) : item).arguments);
      remaining -= Math.min(args.yield_time_ms, remaining);
      calls += 1;
      if (remaining === 0) return calls;
    }
    return calls;
  };
  assert.equal(run(false), 21);
  assert.equal(run(true), 4);
});

// 2026-10-19 11:28Z live: compaction replaced the dispatch and its yielded output with a summary
// while the worker kept running, so the coordinator's next waits on that cell polled at 1 s.
test("after compaction removes a cell's origin, the coordinator wait still gets the floor", () => {
  const compacted = continueNativeObservation(nativeToolProjection(body([
    { type: "message", role: "user", content: [{ type: "input_text", text: "summary of earlier work" }] },
    { type: "message", role: "user", content: [{ type: "input_text", text: "operator reply" }] },
  ]), options));
  assert.equal(compacted.nativeObservationState.pendingCells.length, 0);
  for (const yield_time_ms of [undefined, 1000, 10000]) {
    const args = { cell_id: "139", max_tokens: 7000, ...(yield_time_ms ? { yield_time_ms } : {}) };
    assert.equal(JSON.parse(nativeToolItem(wait(args), compacted).arguments).yield_time_ms, FLOOR);
  }
  // Earlier waits that yielded the same cell are observations, not an origin.
  const firstWait = wait({ cell_id: "139", yield_time_ms: 1000 }, { call_id: "wait_1" });
  const later = continueNativeObservation(nativeToolProjection(body([firstWait,
    output(firstWait, "Script running with cell ID 139\nWall time 1.0 seconds\nOutput:\n")]), options));
  assert.equal(JSON.parse(nativeToolItem(wait({ cell_id: "139", yield_time_ms: 1000 }), later).arguments).yield_time_ms, FLOOR);
});

test("a visible origin keeps the tracker's verdict: foreign or released cells get no default", () => {
  // A raw exec cell from before the coordinator mode is visible and not a Nokiy projection.
  const raw = { type: "custom_tool_call", name: "exec", namespace: "functions", call_id: "raw_exec", input: "text(1)" };
  const foreign = continueNativeObservation(nativeToolProjection(body([raw,
    output(raw, "Script running with cell ID 77\nWall time 1.0 seconds\nOutput:\n")]), options));
  unchanged(wait({ cell_id: "77", yield_time_ms: 1000 }), foreign);
  // A Nokiy origin that the tracker no longer holds as pending (for example a finished cell).
  const projection = pending();
  unchanged(wait({ cell_id: "original_cell", yield_time_ms: 1000 }),
    { ...projection, nativeObservationState: { ...projection.nativeObservationState, pendingCells: [] } });
});

