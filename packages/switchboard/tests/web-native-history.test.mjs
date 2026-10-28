import assert from "node:assert/strict";
import test from "node:test";
import { normalizeExternalItemsForOfficial } from "../src/tool-bridge.mjs";

test("Web local reasoning and inline items cross back to official without lookup ids", () => {
  const input = [
    { type: "reasoning", id: "rs_67606512f5c64226b4747d86b4f5872c", summary: [{ type: "summary_text", text: "Pro 思考" }], content: null, encrypted_content: null },
    { type: "message", id: `msg_${"a".repeat(32)}`, role: "assistant", content: [{ type: "output_text", text: "result" }] },
    { type: "function_call", id: `fc_${"b".repeat(32)}`, call_id: "call_keep", name: "pwd", arguments: "{}" },
    { type: "function_call_output", call_id: "call_keep", output: "/workspace" },
    { type: "reasoning", id: "rs_native", encrypted_content: "native-opaque", summary: [] },
    { type: "agent_message", id: "amsg_native", content: [{ type: "encrypted_content", encrypted_content: "native-task" }] },
  ];
  const before = structuredClone(input);
  const result = normalizeExternalItemsForOfficial(input);
  assert.equal(result.changed, true);
  const expected = structuredClone(input);
  for (const item of expected.slice(0, 3)) delete item.id;
  delete expected[0].encrypted_content;
  assert.deepEqual(result.input, expected);
  assert.deepEqual(input, before);
  assert.equal(normalizeExternalItemsForOfficial(result.input).changed, false);
});

test("native encrypted reasoning and standalone references are untouched", () => {
  const input = [
    { type: "reasoning", id: `rs_${"a".repeat(32)}`, encrypted_content: "native-opaque", summary: [] },
    { type: "item_reference", id: `msg_${"b".repeat(32)}` },
  ];
  assert.deepEqual(normalizeExternalItemsForOfficial(input), { input, changed: false });
});

test("canonical summary marker stays local when history crosses to an official model", () => {
  const input = [{
    type: "message",
    role: "user",
    is_canonical_summary: true,
    content: [{ type: "input_text", text: "[Canonical summary of prior context]\nKeep this context" }],
  }];
  const result = normalizeExternalItemsForOfficial(input);
  assert.equal(result.changed, true);
  assert.deepEqual(result.input, [{
    type: "message",
    role: "user",
    content: input[0].content,
  }]);
  assert.equal(input[0].is_canonical_summary, true);
  assert.equal(normalizeExternalItemsForOfficial(result.input).changed, false);
});
