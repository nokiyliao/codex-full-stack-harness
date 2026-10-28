import assert from "node:assert/strict";
import { test } from "node:test";
import { forgetChainsForTests, itemHashes, itemKinds, rememberChain, replayDivergence, templateDiff } from "../src/replay-diagnostic.mjs";

const thread = "019fd4f0-1079-77e2-8be7-cbf75ca28df5";
const items = [
  { type: "message", role: "developer", content: "rules" },
  { type: "message", role: "user", content: "task" },
  { type: "custom_tool_call", name: "nokiy_execute", call_id: "c1", input: "{}" },
  { type: "custom_tool_call_output", call_id: "c1", output: "done" },
];

test("a replay that matches the chain reports no divergence before its new items", () => {
  forgetChainsForTests();
  rememberChain(thread, { instructions: "a", tools: [1] }, itemHashes(items), itemKinds(items));
  const replay = [...items, { type: "message", role: "user", content: "next" }];
  const result = replayDivergence(thread, { instructions: "a", tools: [1] }, replay);
  assert.equal(result.first_diff, items.length);
  assert.equal(result.chain_kind, null);
  assert.equal(result.input_kind, "message:user");
  assert.deepEqual(result.template_keys_changed, []);
});

test("a projected tool call in the replay is located with both kinds and changed template keys", () => {
  forgetChainsForTests();
  rememberChain(thread, { instructions: "a", tools: [1] }, itemHashes(items), itemKinds(items));
  const replay = items.map((item) => item.name === "nokiy_execute" ? { ...item, name: "exec", input: "code" } : item);
  const result = replayDivergence(thread, { instructions: "a", tools: [2] }, replay);
  assert.equal(result.first_diff, 2);
  assert.equal(result.chain_kind, "custom_tool_call:nokiy_execute");
  assert.equal(result.input_kind, "custom_tool_call:exec");
  assert.deepEqual(result.template_keys_changed, ["tools"]);
});

test("unknown threads and the bounded store never report or throw", () => {
  forgetChainsForTests();
  assert.equal(replayDivergence(thread, {}, items), null);
  for (let index = 0; index < 70; index += 1) {
    rememberChain(`00000000-0000-4000-8000-${String(index).padStart(12, "0")}`, {}, [], []);
  }
  assert.equal(replayDivergence("00000000-0000-4000-8000-000000000000", {}, items), null);
  assert.notEqual(replayDivergence("00000000-0000-4000-8000-000000000069", {}, items), null);
  rememberChain(null, {}, [], []);
  rememberChain(thread, {}, "not an array", []);
  assert.equal(replayDivergence(thread, {}, items), null);
});

test("templateDiff reports prompt-shaping changes with sizes and ignores unrendered metadata", () => {
  const previous = { instructions: "base+contract", tools: [1, 2, 3], client_metadata: { a: 1 }, reasoning: { effort: "xhigh" } };
  assert.deepEqual(templateDiff(previous, { ...previous, client_metadata: { a: 2 }, access_programs: ["x"] }), []);
  const changed = templateDiff(previous, { ...previous, instructions: "", tools: [] });
  assert.deepEqual(changed.map((item) => item.key), ["instructions", "tools"]);
  assert.equal(changed[0].before_length, 13);
  assert.equal(changed[0].after_length, 0);
  assert.equal(changed[1].before_count, 3);
  assert.equal(changed[1].after_count, 0);
});
