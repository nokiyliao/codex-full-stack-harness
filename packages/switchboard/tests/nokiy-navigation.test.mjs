import assert from "node:assert/strict";
import { test } from "node:test";
import { nativeToolProjection, nativeToolItem } from "../src/nokiy-native-tool.mjs";

const projection = () => nativeToolProjection({ tools: [{ type: "function", name: "exec_command", parameters: { type: "object" } }], input: [] },
  { worker: "gpt-6.1-sol", workerEffort: "xhigh", workflow: "nokiy-direct",
    host: { python: "/offline/python", script: "/offline/host.py", coordinator_mode: "dispatch_only_v1" } });
const call = (payload) => ({ type: "function_call", name: "nokiy_execute", call_id: "map-call", id: "map-item", arguments: JSON.stringify(payload) });
const payload = { operation: "navigate", workspace: "/workspace", read_paths: ["src", "tests"], query: "prepare" };

test("coordinator receives automatic portable DCF navigation via the native executor", () => {
  const p = projection();
  const tool = p.body.tools.find(t => t.name === "nokiy_execute");
  assert.ok(tool.parameters.properties.operation.enum.includes("navigate"));
  assert.match(tool.description, /automatically builds\/extends a Python DCF task map/);
  assert.match(tool.description, /expected_source_snapshot_sha256 at offset zero/);
  assert.match(tool.description, /source snapshot alone is not a cursor/);
  assert.equal(tool.parameters.properties.expected_source_snapshot_sha256.pattern, "^[0-9a-f]{64}$");
  const item = nativeToolItem(call(payload), p);
  assert.equal(item.name, "exec_command");
  assert.equal(item.call_id, "map-call");
  const match = item.arguments.match(/'--payload' '([A-Za-z0-9_-]+)'/);
  assert.deepEqual(JSON.parse(Buffer.from(match[1], "base64url")).payload, payload);
  assert.equal(p.pendingResultDelivery ?? null, null);
  assert.equal(p.resultDelivery ?? null, null);
});

test("navigation forwards batched literal lookups unchanged for caller normalization", () => {
  const { query, ...scope } = payload;
  for (const fields of [
    { queries: ["Zed", "alpha.*", "Zed", "a".repeat(256), "é".repeat(128), "😀".repeat(64)], projection: "locations",
      offset: 1, expected_map_sha256: "a".repeat(64) },
    { queries: Array(32).fill("same"), query: "", projection: "full" },
    { queries: ["one"] },
    { queries: ["changed"], projection: "full", expected_source_snapshot_sha256: "b".repeat(64) },
    { queries: ["changed"], projection: "full", offset: 1,
      expected_map_sha256: "a".repeat(64), expected_source_snapshot_sha256: "b".repeat(64) },
  ]) {
    const value = { ...scope, ...fields };
    const item = nativeToolItem(call(value), projection());
    const { cmd } = JSON.parse(item.arguments);
    const match = cmd.match(/'--payload' '([A-Za-z0-9_-]+)'/);
    assert.ok(match);
    assert.deepEqual(JSON.parse(Buffer.from(match[1], "base64url").toString("utf8")).payload, value);
  }
});

test("navigation rejects invalid batches, UTF-8 bounds, mixed queries and projections before execution", () => {
  const invalid = [
    ...[null, "name", {}, [], Array(33).fill("name"), [""], [null], [1],
      ["a".repeat(257)], ["é".repeat(129)], ["😀".repeat(65)], ["\ud800"]].map(queries => ({ queries })),
    ...[null, 1, [], "a".repeat(513)].map(query => ({ query })),
    { query: "name", queries: ["other"] },
    ...[null, 1, {}, "compact", ""].map(projection => ({ projection })),
  ];
  for (const fields of invalid) {
    assert.throws(() => nativeToolItem(call({ ...payload, query: "", ...fields }), projection()),
      { code: "NOKIY_NATIVE_TOOL_INVALID" });
  }
});

test("navigation refuses scope expansion and invalid continuation before execution", () => {
  for (const bad of [{ read_paths: ["../other"] }, { read_paths: [".git"] }, { read_paths: ["src/**"] },
    { read_paths: ["src", "src"] }, { read_paths: [] }, { offset: 1 }, { offset: -1 },
    { expected_map_sha256: "wrong" }, { action: {} }, { read_paths: ["/outside"] }]) {
    assert.throws(() => nativeToolItem(call({ ...payload, ...bad }), projection()));
  }
  assert.ok(nativeToolItem(call({ ...payload, offset: 1, expected_map_sha256: "a".repeat(64) }), projection()));
  for (const bad of [
    { offset: 1, expected_source_snapshot_sha256: "b".repeat(64) },
    ...["wrong", "A".repeat(64), [], ["b".repeat(64)], 1].map(expected_source_snapshot_sha256 => ({ expected_source_snapshot_sha256 })),
  ]) assert.throws(() => nativeToolItem(call({ ...payload, ...bad }), projection()));
});
