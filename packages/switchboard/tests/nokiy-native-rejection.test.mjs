import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import { nativeToolProjection, nativeToolItem, nativeEventProjector, nativeSseProjector } from "../src/nokiy-native-tool.mjs";

const host = { python: "/never/executed/python", script: "/never/executed/host.py", coordinator_mode: "dispatch_only_v1" };
const options = { host, worker: "gpt-6.1-sol", workerEffort: "max", workflow: "nokiy" };
const custom = { type: "custom", name: "exec", format: { type: "text" }, description:
  'declare const tools: { exec_command(args: {cmd: string; workdir?: string;}): Promise<unknown>; };\n'
  + 'declare const tools: { write_stdin(args: {session_id: number; chars?: string;}): Promise<unknown>; };' };
const command = { type: "function", name: "exec_command", parameters: { type: "object", properties: {
  cmd: { type: "string" }, workdir: { type: "string" },
}, required: ["cmd"], additionalProperties: false } };
const stdin = { type: "function", name: "write_stdin", parameters: { type: "object", properties: {
  session_id: { type: "integer" }, chars: { type: "string" },
}, required: ["session_id"], additionalProperties: false } };
const body = (type = "custom") => ({ tools: [{ type: "namespace", name: "functions",
  tools: type === "custom" ? [structuredClone(custom)] : [structuredClone(command), structuredClone(stdin)] }], input: [] });
const call = (name, args, type = "custom") => ({ type: type === "custom" ? "custom_tool_call" : "function_call",
  name, id: "original_item", call_id: "original_call", status: "completed",
  [type === "custom" ? "input" : "arguments"]: typeof args === "string" ? args : JSON.stringify(args) });
const observe = { kind: "exec_session", source_call_id: "SECRET_UNVERIFIED_SOURCE", session_id: 42 };
const invalidCommand = { cmd: "SECRET_PROPOSAL; never execute this", native_thread_id: "SECRET_FOREIGN_THREAD" };
const done = (item) => ({ type: "response.output_item.done", item });

function rejection(result, code) {
  assert.equal(result.status, "REJECTED_LOCAL_TOOL_CALL");
  assert.equal(result.execution_started, false);
  assert.equal(result.same_input_retry_allowed, false);
  assert.equal(result.safe_to_correct_input, true);
  assert.equal(result.error.code, code);
  assert.equal(typeof result.error.message, "string");
  assert.doesNotMatch(JSON.stringify(result), /SECRET|PROPOSAL|native_thread_id/);
}

for (const type of ["custom", "function"]) {
  for (const [name, args, code] of [["nokiy_observe", observe, "NOKIY_NATIVE_OBSERVATION_INVALID"],
    ["nokiy_native", invalidCommand, "NOKIY_NATIVE_TOOL_INVALID"]]) {
    test(`${type} ${name}: pure validation stays fatal but the declared event call returns a static rejection`, async () => {
      const projection = nativeToolProjection(body(type), options);
      const original = call(name, args, type), before = structuredClone(original);
      assert.throws(() => nativeToolItem(original, projection), { code });
      const mapped = nativeEventProjector(projection)(done(original)).item;
      assert.equal(mapped.id, original.id);
      assert.equal(mapped.call_id, original.call_id);
      assert.equal(mapped.type, original.type);
      assert.equal(mapped.namespace, "functions");
      assert.equal(mapped.status, original.status);
      assert.doesNotMatch(JSON.stringify(mapped), /SECRET|PROPOSAL|--payload|never\/executed/);
      let result;
      if (type === "custom") {
        assert.equal(mapped.name, "exec");
        const outputs = [];
        await runInNewContext(`(async () => { ${mapped.input} })()`, {
          text: (value) => outputs.push(JSON.parse(JSON.stringify(value))),
          tools: new Proxy({}, { get: () => assert.fail("A rejected proposal must not invoke native tools or an original session") }),
        });
        assert.equal(outputs.length, 1);
        result = outputs[0];
      } else {
        assert.equal(mapped.name, "exec_command");
        const nativeArgs = JSON.parse(mapped.arguments);
        assert.equal(projection.nativeExecution.validate(nativeArgs), true);
        assert.deepEqual(Object.keys(nativeArgs), ["cmd"]);
        assert.match(nativeArgs.cmd, /^\/usr\/bin\/printf '%s\\n' '/);
        result = JSON.parse(execFileSync("/bin/sh", ["-c", nativeArgs.cmd], { encoding: "utf8" }));
      }
      rejection(result, code);
      if (name === "nokiy_observe") {
        assert.equal(result.error.details.native_observation.reason, "ORIGINAL_PAIR_UNAVAILABLE");
      }
      assert.deepEqual(original, before);
    });
  }
}

test("SSE and provider completion expose the same rejected call without synthesizing provider success", () => {
  const projection = nativeToolProjection(body(), options), original = call("nokiy_observe", observe);
  const created = { type: "response.created", response: { id: "provider_response", status: "in_progress" } };
  const completed = { type: "response.completed", sequence_number: 8,
    response: { id: "provider_response", status: "completed", output: [original] } };
  const project = nativeEventProjector(projection), parser = nativeSseProjector(project);
  const frames = [created, { type: "response.output_item.added", item: { ...original, input: "" } },
    { type: "response.custom_tool_call_input.delta", item_id: original.id, delta: original.input }, done(original), completed];
  const result = parser.push(Buffer.from(frames.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(""))) + parser.end();
  assert.doesNotMatch(result, /SECRET|response\.failed|turn\.completed|assistant/);
  const events = result.trim().split("\n\n").map((frame) => JSON.parse(frame.slice(6)));
  assert.deepEqual(events[0], created);
  assert.equal(events.length, 3);
  assert.deepEqual(events[1].item, events[2].response.output[0]);
  assert.equal(events[2].response.id, completed.response.id);
  assert.equal(events[2].response.status, completed.response.status);
  assert.equal(events[2].sequence_number, completed.sequence_number);
  assert.equal(project.resultDeliveryObservation().eligibility, "disabled");
  assert.equal(project.resultDeliveryObservation().outcome, "unobserved");
});

test("malformed native arguments are a local rejection, never a corrected or replayed command", () => {
  const projection = nativeToolProjection(body(), options);
  for (const args of ["not JSON SECRET", "", null, [], { cmd: 42 }, { cmd: "SECRET".repeat(20000) }]) {
    const mapped = nativeEventProjector(projection)(done(call("nokiy_native", args))).item;
    assert.match(mapped.input, /^text\(\{"status":"REJECTED_LOCAL_TOOL_CALL"/);
    assert.doesNotMatch(mapped.input, /SECRET|tools\.|--payload/);
  }
});

test("missing or incompatible native executors cannot manufacture a rejection tool result", () => {
  const requests = [body("function"), body("function"), body("function")];
  requests[0].tools[0].tools[0].parameters.required.push("workdir");
  requests[1].tools[0].tools[0].parameters.properties.cmd.maxLength = 10;
  requests[2].tools[0].tools[0].parameters.properties.cmd.pattern = "^admitted_project_command$";
  for (const request of requests) {
    const projection = nativeToolProjection(request, options);
    assert.throws(() => nativeEventProjector(projection)(done(call("nokiy_observe", observe, "function"))),
      { code: "NOKIY_NATIVE_OBSERVATION_INVALID" });
  }
  const initial = nativeToolProjection(body(), options);
  const continuation = nativeToolProjection({ tools: [], input: [] }, { ...options,
    continuationTarget: { type: "custom", name: "exec", namespace: "functions" },
    continuationCoordinatorBindings: initial.coordinatorBindings });
  assert.throws(() => nativeEventProjector(continuation)(done(call("nokiy_native", invalidCommand))),
    { code: "NOKIY_NATIVE_TOOL_INVALID" });
});

test("foreign, ambiguous and undeclared calls retain their fatal boundaries", () => {
  const native = { type: "function_call", name: "exec_command", namespace: "remote_mcp",
    call_id: "source", arguments: '{"cmd":"SECRET_FOREIGN"}' };
  const output = { type: "function_call_output", call_id: "source", output: '{"session_id":42,"output":""}' };
  const request = { ...body(), input: [native, output] };
  const projection = nativeToolProjection(request, options);
  assert.throws(() => nativeEventProjector(projection)(done(call("nokiy_observe", { ...observe, source_call_id: "source" }))),
    (error) => error.details?.native_observation?.reason === "ORIGINAL_TOOL_NOT_NATIVE");
  for (const original of [call("unknown", {}), { ...call("nokiy_observe", observe), namespace: "remote_mcp" },
    call("nokiy_native", invalidCommand, "function"), call("exec", "SECRET"), call("nokiy_goal", {})]) {
    assert.throws(() => nativeEventProjector(projection)(done(original)), { code: "NOKIY_COORDINATOR_TOOL_FORBIDDEN" });
  }
  const invalidWorker = call("nokiy_execute", "not JSON SECRET");
  assert.throws(() => nativeToolItem(invalidWorker, projection), { code: "NOKIY_NATIVE_TOOL_INVALID" });
  const correctedIntake = nativeEventProjector(projection)(done(invalidWorker));
  rejection(JSON.parse(correctedIntake.item.input.slice(5, -2)), "NOKIY_NATIVE_TOOL_INVALID");
  const ambiguous = body();
  ambiguous.tools.push({ type: "namespace", name: "remote_mcp", tools: [structuredClone(command)] });
  ambiguous.input = [{ ...native, namespace: undefined }, output];
  assert.throws(() => nativeEventProjector(nativeToolProjection(ambiguous, options))(
    done(call("nokiy_observe", { ...observe, source_call_id: "source" }))),
  (error) => error.details?.native_observation?.reason === "ORIGINAL_TOOL_AMBIGUOUS");
});

test("malformed SSE stays fatal and actual upstream failed events remain unchanged", () => {
  const projection = nativeToolProjection(body(), options);
  const parser = nativeSseProjector(nativeEventProjector(projection));
  assert.throws(() => parser.push(Buffer.from('data: {"type":invalid}\n\n')), { code: "NOKIY_NATIVE_TOOL_INVALID" });
  const incomplete = nativeSseProjector(nativeEventProjector(projection));
  incomplete.push(Buffer.from('data: {"type":"response.completed"}'));
  assert.throws(() => incomplete.end(), { code: "NOKIY_NATIVE_TOOL_INVALID" });
  const failed = { type: "response.failed", response: { id: "actual_provider_failure", status: "failed", error: { code: "actual" } } };
  assert.deepEqual(nativeEventProjector(projection)(failed), failed);
});
