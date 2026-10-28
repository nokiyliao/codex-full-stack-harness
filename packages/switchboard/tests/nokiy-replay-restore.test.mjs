import assert from "node:assert/strict";
import { appendFileSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, describe, test } from "node:test";

import { projectNokiyDirect } from "../src/nokiy-direct-route.mjs";
import { nativeEventProjector } from "../src/nokiy-native-tool.mjs";
import { createReplayRestorer } from "../src/nokiy-replay-restore.mjs";

const catalog = { models: ["gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"].map((slug) => ({ slug,
  supported_reasoning_levels: ["low", "medium", "high", "max", "xhigh", "ultra"].map((effort) => ({ effort })),
  multi_agent_reasoning_effort: "xhigh" })) };
const profiles = [{ slug: "nokiy-direct", display_name: "Nokiy Ultra replay test", workflow: "nokiy-direct",
  coordinator: { model: "gpt-6-astra", reasoning_effort: "max" },
  worker: { model: "gpt-6.1-sol", reasoning_effort: "max", service_tier: "default" },
  worker_selector: { default_effort: "medium", models: { low: "gpt-6-luna", medium: "gpt-6.1-sol", high: "gpt-6-astra" } } }];
const host = { python: "/synthetic/nokiy/python", script: "/synthetic/nokiy/host.py",
  coordinator_mode: "dispatch_only_v1", parent_result_delivery: "accept_v1" };
const execDescription = ["declare const tools: { exec_command(args: {", "  cmd: string;", "}): Promise<{ output: string; }>; };",
  "declare const tools: { write_stdin(args: {", "  chars?: string;", "  session_id: number;", "}): Promise<{ output: string; }>; };"].join("\n");
const AT = { type: "additional_tools", id: "at_fixture", role: "developer",
  tools: [{ type: "custom", name: "exec", description: execDescription, format: { type: "text" } }] };
const BI = { type: "message", role: "developer", content: [{ type: "input_text", text: "BASE" }] };
const user = (text) => ({ type: "message", role: "user", content: [{ type: "input_text", text }] });
const common = { model: "nokiy-direct", tool_choice: "auto", parallel_tool_calls: false,
  reasoning: { effort: "medium", summary: "auto", context: "all_turns" }, store: false, stream: true };
const original = { type: "custom_tool_call", id: "ctc_fixture", status: "completed", call_id: "call_fixture", name: "nokiy_execute",
  input: JSON.stringify({ operation: "read_result", artifact_root: "/synthetic/artifacts", request_id: "tura_embedded_fixture" }) };

describe("Nokiy replay restoration", { concurrency: false }, () => {
  let dir, originalHome;
  before(() => {
    originalHome = process.env.CODEX_HOME;
    dir = mkdtempSync(join(tmpdir(), "nokiy-replay-restore-"));
    writeFileSync(join(dir, "config.toml"), "[agents]\nmax_threads = 2\n");
    process.env.CODEX_HOME = dir;
  });
  after(() => {
    if (originalHome === undefined) delete process.env.CODEX_HOME;
    else process.env.CODEX_HOME = originalHome;
    rmSync(dir, { recursive: true, force: true });
  });

  test("a projected coordinator call is restored only for the exact bytes Codex received", () => {
    const first = projectNokiyDirect({ ...common, input: [AT, BI, user("task")] }, { catalog, profiles, host });
    const seen = [];
    const project = nativeEventProjector(first.nativeTool, { onProjected: (raw, projected) => seen.push([raw, projected]) });
    const event = project({ type: "response.output_item.done", output_index: 0, item: original });
    const projected = event.item;
    assert.equal(projected.name, "exec");
    assert.notEqual(projected.input, original.input);
    assert.deepEqual(seen, [[original, projected]]);

    const store = createReplayRestorer({ path: join(dir, "store.jsonl") });
    for (const [raw, shown] of seen) store.record(raw, shown);
    const output = { type: "custom_tool_call_output", call_id: "call_fixture", output: "result" };
    const replay = [AT, BI, user("task"), { ...projected, id: undefined }, output];
    const restored = store.restore(replay);
    assert.equal(restored.restored, 1);
    assert.equal(restored.input[3].name, "nokiy_execute");
    assert.equal(restored.input[3].input, original.input);
    assert.equal(restored.input[3].call_id, "call_fixture");
    assert.deepEqual(restored.input[4], output);
    // An edited or unknown call stays exactly as Codex sent it.
    const edited = store.restore([{ ...projected, input: projected.input + " " }]);
    assert.equal(edited.restored, 0);
    assert.equal(store.restore([{ ...projected, call_id: "call_other" }]).restored, 0);

    // The binding survives a router restart through the append-only store.
    const reloaded = createReplayRestorer({ path: join(dir, "store.jsonl") });
    assert.equal(reloaded.restore(replay).restored, 1);
    assert.equal(reloaded.originalCall(projected).input, original.input);
    assert.equal(reloaded.originalCall({ ...projected, input: projected.input + " " }), null);
    assert.equal(reloaded.originalCall({ ...projected, call_id: "call_other" }), null);
    assert.equal(reloaded.originalCall(output), null);

    // A full Nokiy request restores before projection; a chained delta is untouched.
    const full = projectNokiyDirect({ ...common, input: replay }, { catalog, profiles, host, replayRestore: reloaded.restore });
    assert.equal(full.replayRestored, 1);
    assert.ok(full.body.input.some((item) => item.call_id === "call_fixture" && item.name === "nokiy_execute" && item.input === original.input));
    const chained = projectNokiyDirect({ ...common, previous_response_id: "resp_1", input: [output] },
      { catalog, profiles, host, replayRestore: reloaded.restore, continuationTarget: first.nativeTool.target,
        continuationCoordinatorBindings: first.nativeTool.coordinatorBindings });
    assert.equal(chained.replayRestored, 0);
  });

  test("the store ignores torn lines and stays within its byte bound", () => {
    const path = join(dir, "bounded.jsonl");
    const store = createReplayRestorer({ path, maxBytes: 600 });
    for (let index = 0; index < 8; index += 1) {
      store.record({ type: "function_call", call_id: `call_${index}`, name: "nokiy_native", arguments: "{\"cmd\":\"true\"}" },
        { type: "function_call", call_id: `call_${index}`, name: "exec_command", arguments: `{"cmd":"wrapped ${index}"}` });
    }
    assert.ok(store.stats().bytes <= 600);
    assert.equal(store.restore([{ type: "function_call", call_id: "call_0", name: "exec_command", arguments: '{"cmd":"wrapped 0"}' }]).restored, 0);
    assert.equal(store.restore([{ type: "function_call", call_id: "call_7", name: "exec_command", arguments: '{"cmd":"wrapped 7"}' }]).restored, 1);
    // Wait for the asynchronous appends, then add a torn trailing line.
    return new Promise((resolve) => setTimeout(resolve, 50)).then(() => {
      appendFileSync(path, '{"c":"call_torn","f":');
      const reloaded = createReplayRestorer({ path, maxBytes: 600 });
      assert.equal(reloaded.restore([{ type: "function_call", call_id: "call_7", name: "exec_command", arguments: '{"cmd":"wrapped 7"}' }]).restored, 1);
      assert.ok(readFileSync(path, "utf8").length > 0);
    });
  });

  test("delivery records restore the compact acceptance and re-insert the transport turn", () => {
    const path = join(dir, "delivery.jsonl");
    const store = createReplayRestorer({ path });
    const output = { type: "custom_tool_call_output", call_id: "call_delivery", output: "worker result" };
    const transport = { type: "message", role: "developer", content: [{ type: "input_text", text: "transport" }] };
    store.recordInsertion(output, [transport]);
    const compact = { type: "message", id: "msg_1", role: "assistant", status: "completed",
      content: [{ type: "output_text", text: '{"nokiy_delivery":"accept_v1","reference":"r","edits":[]}', annotations: [] }] };
    const expanded = { ...compact, content: [{ ...compact.content[0], text: "the complete worker answer" }] };
    store.record(compact, expanded);
    const replayed = { type: "message", role: "assistant", content: [{ type: "output_text", text: "the complete worker answer", annotations: [] }], phase: "final_answer" };
    const next = user("next");
    const result = store.restore([output, replayed, next]);
    assert.equal(result.inserted, 1);
    assert.equal(result.restored, 1);
    assert.deepEqual(result.input[1], transport);
    assert.equal(result.input[2].content[0].text, compact.content[0].text);
    assert.equal(result.input[2].phase, "final_answer", "Codex's own envelope fields are kept");
    assert.deepEqual(result.input[3], next);
    // Already present, or the newest item: nothing is inserted.
    assert.equal(store.restore([output, transport, replayed]).inserted, 0);
    assert.equal(store.restore([user("x"), output]).inserted, 0);
    // Version 1 store lines keep restoring after the upgrade.
    const legacyPath = join(dir, "legacy.jsonl");
    const legacy = createReplayRestorer({ path: legacyPath });
    const shown = { type: "custom_tool_call", call_id: "call_v1", name: "exec", input: "wrapper" };
    legacy.record({ type: "custom_tool_call", call_id: "call_v1", name: "nokiy_execute", input: "{}" }, shown);
    const line = JSON.parse(readFileSync(legacyPath, "utf8").trim());
    writeFileSync(legacyPath, JSON.stringify({ c: "call_v1", f: line.k.slice("call:".length), o: line.v.call }) + "\n");
    assert.equal(createReplayRestorer({ path: legacyPath }).restore([shown]).input[0].name, "nokiy_execute");
  });

  test("a composer prediction keeps the native prefix and a recent tail starting at a user message", async () => {
    const { trimPredictionHistory } = await import("../src/nokiy-direct-route.mjs");
    const big = "x".repeat(4000);
    const turn = (n) => [user(`question ${n} ${big}`), { type: "reasoning", summary: [], encrypted_content: big },
      { type: "custom_tool_call", call_id: `c${n}`, name: "exec", input: big }, { type: "custom_tool_call_output", call_id: `c${n}`, output: big },
      { type: "message", role: "assistant", content: [{ type: "output_text", text: `answer ${n}` }] }];
    const history = Array.from({ length: 20 }, (_, n) => turn(n)).flat();
    const ask = user("predict the next message");
    const input = [AT, BI, ...history, ask];
    const result = trimPredictionHistory(input, 40_000);
    assert.deepEqual(result.input.slice(0, 2), [AT, BI]);
    assert.equal(result.input[2].role, "user", "the tail starts at a user message");
    assert.deepEqual(result.input.at(-1), ask);
    assert.ok(result.stats.after_bytes <= 40_000 + JSON.stringify(AT).length + JSON.stringify(BI).length + 30_000);
    assert.ok(result.stats.after_items < result.stats.before_items);
    // Every kept tool output still has its call.
    const calls = new Set(result.input.filter((item) => item.type === "custom_tool_call").map((item) => item.call_id));
    assert.ok(result.input.filter((item) => item.type === "custom_tool_call_output").every((item) => calls.has(item.call_id)));
    // Small histories and a disabled budget are untouched.
    assert.equal(trimPredictionHistory([AT, BI, user("one"), ask], 40_000), null);
    assert.equal(trimPredictionHistory(input, 0), null);
    // Through the route, only prediction requests are trimmed.
    const prediction = projectNokiyDirect({ ...common, client_metadata: { prediction_source_thread_id: "parent" }, input },
      { catalog, profiles, host });
    assert.ok(prediction.predictionTrim && prediction.predictionTrim.after_items < input.length);
    // Codex carries app-server metadata inside the turn metadata blob.
    const blob = JSON.stringify({ thread_source: "composer_predictions", prediction_source_thread_id: "parent", prediction_trigger: "live" });
    const nested = projectNokiyDirect({ ...common, client_metadata: { "x-codex-turn-metadata": blob }, input },
      { catalog, profiles, host });
    assert.ok(nested.predictionTrim && nested.predictionTrim.after_items < input.length);
    const other = projectNokiyDirect({ ...common, client_metadata: { "x-codex-turn-metadata": JSON.stringify({ thread_source: "user" }) }, input },
      { catalog, profiles, host });
    assert.equal(other.predictionTrim, null);
    const ordinary = projectNokiyDirect({ ...common, input }, { catalog, profiles, host });
    assert.equal(ordinary.predictionTrim, null);
  });
});

