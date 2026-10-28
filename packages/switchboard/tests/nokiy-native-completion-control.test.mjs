import assert from "node:assert/strict";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import { nativeToolProjection, nativeToolItem, nativeEventProjector, continueNativeObservation } from "../src/nokiy-native-tool.mjs";

const host = { python: "/never/executed/python", script: "/never/executed/host.py", coordinator_mode: "dispatch_only_v1" };
const options = { host, worker: "gpt-6.1-sol", workerEffort: "max", workflow: "nokiy" };
const custom = { type: "custom", name: "exec", format: { type: "text" }, description:
  'declare const tools: { exec_command(args: {cmd: string; workdir?: string; shell?: string; login?: boolean; tty?: boolean; max_output_tokens?: number; yield_time_ms?: number;}): Promise<unknown>; };\n'
  + 'declare const tools: { write_stdin(args: {session_id: number; chars?: string;}): Promise<unknown>; };' };
const body = (input = []) => ({ tools: [{ type: "namespace", name: "functions", tools: [structuredClone(custom)] }], input });
const call = (args) => ({ type: "custom_tool_call", name: "nokiy_native", id: "native_item", call_id: "native_call",
  status: "completed", input: JSON.stringify(args) });
const args = { cmd: "authorized exact command", workdir: "/exact/worktree", shell: "/bin/zsh", login: false,
  tty: true, yield_time_ms: 1000, max_output_tokens: 2000 };
const normalize = (value) => JSON.parse(JSON.stringify(value));
const clipMarker = "\n[Native output truncated; initial receipt and tail retained]\n";

async function observe(results, { nativeArgs = args, waitAvailable = true } = {}) {
  const item = nativeToolItem(call(nativeArgs), nativeToolProjection(body(), options));
  const launches = [], waits = [], printed = [];
  let index = 0;
  const take = () => {
    assert.ok(index < results.length, "no extra launch or wait after the supplied outcome");
    const result = results[index++];
    if (result instanceof Error) throw result;
    return result;
  };
  const tools = { async exec_command(value) { launches.push(normalize(value)); return take(); } };
  if (waitAvailable) tools.write_stdin = async (value) => {
    assert.deepEqual(Object.keys(value).sort(), ["chars", "session_id"], "only advertised native wait arguments");
    waits.push(normalize(value)); return take();
  };
  await runInNewContext(`(async () => { ${item.input}\n })()`, { tools, text: (value) => printed.push(value) }, { timeout: 1000 });
  assert.deepEqual(launches, [nativeArgs], "one exact launch preserves all native arguments");
  assert.equal(index, results.length);
  assert.equal(printed.length, 1);
  for (const wait of waits) assert.deepEqual(wait, { session_id: results[0].session_id, chars: "" });
  return { item, result: printed[0], waits };
}

function assertUncertainRecovery(item, result) {
  const serialized = JSON.stringify(result);
  assert.ok(Buffer.byteLength(serialized, "utf8") <= 240 * 1024, "complete uncertain JSON stays within the recovery budget");
  assert.equal(result.terminal, false);
  assert.equal(result.exit_code, null);
  assert.equal(result.session_id, 42);
  assert.equal(result.recovery_session_id, 42);
  const input = [item, { type: "custom_tool_call_output", call_id: item.call_id, output: serialized }];
  const projection = nativeToolProjection(body(input), { ...options, host: { ...host, parent_result_delivery: "accept_v1" } });
  assert.equal(projection.resultDelivery, null, "native deterministic completion is not worker business acceptance");
  const observer = { type: "custom_tool_call", name: "nokiy_observe", call_id: "observe_original", input: JSON.stringify({
    kind: "exec_session", source_call_id: item.call_id, session_id: 42,
  }) };
  assert.equal(nativeToolItem(observer, projection).input, 'text(await tools.write_stdin({"session_id":42,"chars":""}));');
  const retained = continueNativeObservation(projection);
  assert.equal(retained.nativeObservationState.references.length, 1);
  assert.equal(retained.nativeObservationState.references[0].source_call_id, item.call_id);
  const resumed = continueNativeObservation(nativeToolProjection(body(), options), { state: retained.nativeObservationState });
  assert.equal(nativeToolItem(observer, resumed).input, 'text(await tools.write_stdin({"session_id":42,"chars":""}));');
  return { input, projection, observer };
}

test("native command stays with the original numeric handle until terminal without replay or a worker", async () => {
  const initial = { session_id: 42, output: "receipt\n", chunk_id: "initial" };
  const terminal = { exit_code: 0, output: "done\n", chunk_id: "terminal", cancellation: { requested: false } };
  const { item, result, waits } = await observe([initial, { session_id: 42, output: "progress\n" }, terminal]);
  assert.equal(item.id, "native_item");
  assert.equal(item.call_id, "native_call");
  assert.equal(item.status, "completed");
  assert.equal(item.name, "exec");
  assert.equal(item.namespace, "functions");
  assert.equal(waits.length, 2);
  assert.equal(result.output, "receipt\nprogress\ndone\n");
  assert.equal(result.terminal, true);
  assert.equal(result.exit_code, 0);
  assert.equal(result.recovery_session_id, 42);
  assert.equal(result.chunk_id, "terminal");
  assert.equal(result.cancellation, terminal.cancellation);
  assert.doesNotMatch(item.input, /--payload|spawn|prepare|HOME|CODEX_HOME/);
});

test("observation yielding is not a task lifetime or retry limit", async () => {
  const { waits, result } = await observe([
    ...Array.from({ length: 129 }, () => ({ session_id: 0, output: "" })),
    { exit_code: 0, output: "terminal after all observation yields" },
  ], { nativeArgs: { cmd: "long authorized command", yield_time_ms: 250 } });
  assert.equal(waits.length, 129);
  assert.equal(result.terminal, true);
  assert.equal(result.recovery_session_id, 0);
});

for (const exit_code of [0, 1, 130]) {
  test(`short immediate terminal ${exit_code} retains the exact native output object`, async () => {
    const terminal = { exit_code, output: "original output", chunk_id: "original", wall_time_seconds: 0.2,
      cancellation: { requested: exit_code === 130 } };
    const { result, waits } = await observe([terminal]);
    assert.equal(result, terminal);
    assert.equal(waits.length, 0);
    assert.deepEqual(Object.keys(result), Object.keys(terminal));
  });
}

test("terminal failure and cancellation metadata survive observation without cancellation commands", async () => {
  const terminal = { exit_code: 130, output: "cancelled\n", status: "CANCELLED", cancellation: { requested: true },
    cleanup: { status: "complete" }, authority_effect: "none" };
  const { result, waits } = await observe([{ session_id: 42, output: "receipt\n" }, terminal]);
  assert.equal(waits.length, 1);
  assert.equal(result.terminal, true);
  assert.equal(result.exit_code, 130);
  for (const key of ["status", "cancellation", "cleanup", "authority_effect"]) assert.equal(result[key], terminal[key]);
});

for (const clipping of [{ truncated: true }, { output_truncated: true }, {
  output: "Warning: truncated output (original token count: 7063)\nclipped fragment\n",
}]) {
  test(`host clipping stays sticky for ${JSON.stringify(clipping)}`, async () => {
    const fragment = { session_id: 42, output: "clipped fragment\n", ...clipping };
    const { result } = await observe([{ session_id: 42, output: "receipt\n" }, fragment,
      { exit_code: 0, output: "tail\n", truncated: false, output_truncated: false }]);
    assert.equal(result.output, "receipt\n" + fragment.output + "tail\n" + clipMarker);
    assert.equal(result.truncated, true);
    assert.equal(result.output_truncated, true);
    assert.equal(result.recovery_session_id, 42);
  });
}

test("bounded native output preserves receipt head and terminal failure tail", async () => {
  const limit = 64 * 1024, receipt = "original receipt\n", middle = "x".repeat(3 * limit), tail = "failure detail\n";
  const { result } = await observe([{ session_id: 42, output: receipt }, { session_id: 42, output: middle },
    { exit_code: 1, output: tail }]);
  const complete = receipt + middle + tail;
  assert.equal(result.output, complete.slice(0, limit) + clipMarker + complete.slice(-limit));
  assert.equal(result.truncated, true);
  assert.equal(result.exit_code, 1);
  assert.equal(result.recovery_session_id, 42);
});

const malformed = [
  ["missing exit", { output: "unknown" }, /no valid terminal exit status/],
  ["fractional exit", { output: "unknown", exit_code: 0.5 }, /no valid terminal exit status/],
  ["both running and exit", { output: "unknown", session_id: 42, exit_code: 0 }, /both a running session and an exit status/],
  ["changed handle", { output: "unknown", session_id: 99 }, /session changed/],
  ["primitive", "unstructured output", /Invalid native result/],
  ["array", [], /Invalid native result/],
];
for (const [name, unexpected, message] of malformed) {
  test(`uncertain ${name} preserves original recovery and never follows another handle`, async () => {
    const { result, waits } = await observe([{ session_id: 42, output: "receipt\n" }, unexpected]);
    assert.equal(waits.length, 1);
    assert.equal(result.terminal, false);
    assert.equal(result.exit_code, null);
    assert.equal(result.session_id, 42);
    assert.equal(result.recovery_session_id, 42);
    assert.equal(result.observed_session_id, unexpected?.session_id ?? null);
    assert.equal(result.observed_exit_code, unexpected?.exit_code ?? null);
    assert.match(result.wait_error, message);
    assert.match(result.wait_error, /Do not replay/);
  });
}

for (const session_id of ["42", "original-session", -1, 0.5, Number.MAX_SAFE_INTEGER + 1, {}, ""]) {
  test(`invalid native original handle ${JSON.stringify(session_id)} is not coerced or observed`, async () => {
    const { result, waits } = await observe([{ session_id, output: "receipt\n" }]);
    assert.equal(waits.length, 0);
    assert.equal(result.terminal, false);
    assert.equal(result.recovery_session_id, session_id);
    assert.match(result.wait_error, /Invalid original native session/);
  });
}

test("missing wait capability returns explicit uncertainty with the original handle", async () => {
  const { result, waits } = await observe([{ session_id: 42, output: "receipt\n" }], { waitAvailable: false });
  assert.equal(waits.length, 0);
  assert.equal(result.terminal, false);
  assert.equal(result.session_id, 42);
  assert.equal(result.recovery_session_id, 42);
  assert.match(result.wait_error, /Native wait unavailable/);
});

for (const name of ["Error", "AbortError"]) {
  test(`${name} during observation retains cancellation, clipping and original recovery`, async () => {
    const error = new Error("original observation interrupted");
    error.name = name;
    const metadata = { cancellation: { requested: true }, cleanup: { status: "pending" }, authority_effect: "none" };
    const { result, waits } = await observe([{ session_id: 42, output: "receipt\n", truncated: true, ...metadata }, error]);
    assert.equal(waits.length, 1);
    assert.equal(result.terminal, false);
    assert.equal(result.exit_code, null);
    assert.equal(result.session_id, 42);
    assert.equal(result.recovery_session_id, 42);
    assert.equal(result.truncated, true);
    assert.equal(result.output, "receipt\n" + clipMarker);
    assert.match(result.wait_error, /Native wait failed/);
    for (const key of Object.keys(metadata)) assert.equal(result[key], metadata[key]);
  });
}

test("native launch denial propagates unchanged, with one launch and no fallback", async () => {
  const item = nativeToolItem(call(args), nativeToolProjection(body(), options));
  const denial = new Error("native permission denied");
  let launches = 0;
  await assert.rejects(runInNewContext(`(async () => { ${item.input}\n })()`, {
    tools: { exec_command: async () => { launches++; throw denial; }, write_stdin: () => assert.fail("No handle was launched") },
    text: () => assert.fail("No fabricated terminal output"),
  }), (error) => error === denial);
  assert.equal(launches, 1);
});

test("uncertain native wrapper keeps exact source-call recovery without admitting worker terminal delivery", async () => {
  const { item, result } = await observe([{ session_id: 42, output: "receipt\n" }], { waitAvailable: false,
    nativeArgs: { cmd: 'printf "%s" "data with \u2028 separator"' } });
  const { input, observer } = assertUncertainRecovery(item, result);
  for (const forged of [item.input.replace("chars: \"\"", 'chars: "injected"'), item.input + "\ntext('extra');"]) {
    const request = body([{ ...item, input: forged }, input[1]]);
    assert.throws(() => nativeToolItem(observer, nativeToolProjection(request, options)),
      (error) => error.details?.native_observation?.reason === "ORIGINAL_TOOL_NOT_NATIVE");
  }
});

test("exact native wrapper recovery accepts command parentheses, quotes, newlines and U2028 as JSON data", async () => {
  for (const cmd of ["printf '%s' ')('", "node -e '(()=>1)()'",
    'printf "%s" "quoted \\"value\\""\nprintf "%s" "next"', 'printf "%s" "line\u2028separator"']) {
    const { item, result } = await observe([{ session_id: 42, output: "receipt\n" }, new Error("observation unavailable")],
      { nativeArgs: { cmd } });
    assertUncertainRecovery(item, result);
  }
});

for (const [name, fragment] of [["multibyte", "\u4e2d\u6587\u6e2c\u8a66"], ["JSON quotes and slashes", '\\"'],
  ["control characters", "\u0000\u0001\n\r\t"]]) {
  test(`oversized uncertain ${name} stays recoverable within the complete JSON UTF8 budget`, async () => {
    const metadata = { cancellation: { requested: true }, cleanup: { status: "pending" }, authority_effect: "none" };
    const receipt = "original receipt\n", tail = "original failure tail\n";
    const { item, result } = await observe([
      { session_id: 42, output: receipt + fragment.repeat(100000) + tail, ...metadata },
      new Error("original observation failure"),
    ]);
    assertUncertainRecovery(item, result);
    assert.equal(result.truncated, true);
    assert.equal(result.output_truncated, true);
    assert.equal(result.observed_session_id, 42);
    assert.equal(result.observed_exit_code, null);
    assert.ok(result.output.startsWith(receipt));
    assert.ok(result.output.endsWith(tail));
    assert.match(result.wait_error, /original observation failure/);
    for (const key of Object.keys(metadata)) assert.deepEqual(result[key], metadata[key]);
    assert.notEqual(result.native_metadata_truncated, true);
  });
}

test("long multibyte and escaped wait error cannot hide the original source-call recovery", async () => {
  const metadata = { cancellation: { requested: false }, cleanup: { status: "pending" }, authority_effect: "none" };
  const error = new Error("original wait failure: " + '\u4e2d\u6587\u0000\n\\"'.repeat(100000));
  const { item, result } = await observe([{ session_id: 42, output: "receipt\n", ...metadata }, error]);
  assertUncertainRecovery(item, result);
  assert.match(result.wait_error, /original wait failure:/);
  assert.equal(result.observed_session_id, 42);
  assert.equal(result.observed_exit_code, null);
  for (const key of Object.keys(metadata)) assert.deepEqual(result[key], metadata[key]);
  assert.notEqual(result.native_metadata_truncated, true);
});

test("oversized metadata uses explicit omission without fabricating cancellation or losing recovery fields", async () => {
  const { item, result } = await observe([{
    session_id: 42, output: "original receipt\n", diagnostic: "\u4e2d\u6587metadata".repeat(100000),
    cancellation: { requested: true, detail: "original evidence".repeat(100000) },
  }, new Error("original metadata observation failure")]);
  assertUncertainRecovery(item, result);
  assert.equal(result.native_metadata_truncated, true);
  assert.equal(result.truncated, true);
  assert.equal(result.output_truncated, true);
  assert.equal(result.observed_session_id, 42);
  assert.equal(result.observed_exit_code, null);
  assert.equal(result.cancellation, undefined, "omitted cancellation evidence must not become a fabricated status");
  assert.equal(result.diagnostic, undefined);
  assert.match(result.wait_error, /original metadata observation failure/);
  assert.match(result.wait_error, /metadata.*omitted/i);
});

test("SSE completion emits one completion-owned script and preserves original call pairing", () => {
  const projection = nativeToolProjection(body(), options), item = call(args);
  const event = nativeEventProjector(projection)({ type: "response.output_item.done", item });
  assert.equal(event.item.input, nativeToolItem(item, projection).input);
  for (const key of ["id", "call_id", "type", "status"]) assert.equal(event.item[key], item[key]);
});
