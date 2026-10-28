import assert from "node:assert/strict";
import test from "node:test";
import { nativeToolProjection } from "../src/nokiy-native-tool.mjs";

test("Efficient exposes bounded observation guidance without rewriting native tools", () => {
  const body = { model: "gpt-6.1-sol", reasoning: { effort: "max" }, instructions: "Original native permissions.",
    tools: [{ type: "namespace", name: "functions", tools: [{ type: "custom", name: "exec",
      format: { type: "text" },
      description: "declare const tools: { exec_command(args: { cmd: string; }): Promise<unknown>; };" }] }],
    input: [{ type: "message", role: "user", content: "Run the existing acceptance once." }] };
  const before = structuredClone(body);
  const projection = nativeToolProjection(body, {
    host: { python: "/not/executed/python", script: "/not/executed/host.py", coordinator_mode: "dispatch_only_v1" },
    workflow: "nokiy-direct", worker: "gpt-6.1-sol", workerEffort: "xhigh", workerServiceTier: "default",
    profile: { slug: "nokiy-efficient", workflow: "nokiy-direct",
      coordinator: { model: "gpt-6.1-sol", reasoning_effort: "max" } },
  });
  const wire = JSON.stringify(projection.body);
  assert.equal(projection.adaptiveNative, true);
  assert.equal(projection.coordinatorBindings, null);
  assert.deepEqual(body, before);
  assert.equal(wire.match(/Observation economy:/g)?.length, 1);
  assert.match(wire, /outer yield_time_ms=330000/);
  assert.match(wire, /wait yield_time_ms=300000/);
  assert.match(wire, /finish independent work before blocking/);
  assert.match(wire, /interactive input, server startup, or explicit operator timing/);
  assert.match(wire, /A yield never authorizes restarting the command/);
});
