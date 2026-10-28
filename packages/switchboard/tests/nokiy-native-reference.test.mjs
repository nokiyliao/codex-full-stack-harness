// Focused native reference projection regression tests.

import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { runInNewContext } from "node:vm";
import { fileURLToPath } from "node:url";
import {
  NOKIY_TOOL, nativeToolProjection, nativeToolItem, nativeEventProjector, nativeSseProjector,
} from "../src/nokiy-native-tool.mjs";

let directory, previousCodexHome;
before(() => {
  directory = mkdtempSync(join(tmpdir(), "nokiy-reference-node-"));
  writeFileSync(join(directory, "config.toml"), "[agents]\nmax_threads = 1\n");
  previousCodexHome = process.env.CODEX_HOME;
  process.env.CODEX_HOME = directory;
});
after(() => {
  if (previousCodexHome === undefined) delete process.env.CODEX_HOME;
  else process.env.CODEX_HOME = previousCodexHome;
  rmSync(directory, { recursive: true, force: true });
});

const host = { python: "/stub/python'3", script: "/stub/雪'nokiy-native-host.py" };
const worker = "gpt-6-sol";
const effort = "max";
const tier = "priority";
const filePayload = { operation: "run", draft: { workspace: "/workspace", artifact_root: "/artifacts",
  prompt: 'Exact 雪 é 😀 " \\ \n', authority_effect: "none" }, action: { context_summary: "bounded" } };
const exactBytes = Buffer.from(`\r\n${JSON.stringify(filePayload, null, 2)}\r\n\t`, "utf8");
const digest = createHash("sha256").update(exactBytes).digest("hex");

function reference() {
  return { operation: "run_ref", input_path: join(directory, "unopened-雪'\"\\.json"), input_sha256: digest };
}

function projection(type = "function", workflow = "nokiy-direct", admittedHost = host) {
  const tool = type === "function"
    ? { type, name: "exec_command", parameters: { type: "object" } }
    : { type, name: "exec", format: { type: "text" } };
  return nativeToolProjection({ tools: [{ type: "namespace", name: "functions", tools: [tool] }] }, {
    host: admittedHost, worker, workerEffort: effort, workerServiceTier: tier, workflow,
  });
}

function item(payload, type = "function") {
  return { type: type === "function" ? "function_call" : "custom_tool_call", name: NOKIY_TOOL,
    id: "native_item_雪", call_id: "native_call_123", status: "completed",
    [type === "function" ? "arguments" : "input"]: JSON.stringify(payload) };
}

function envelope(args) {
  const encoded = args.cmd.match(/'([A-Za-z0-9_-]+)'$/)?.[1];
  assert.ok(encoded, "host command must end with a quoted base64url envelope");
  return JSON.parse(Buffer.from(encoded, "base64url").toString("utf8"));
}

function expectedEnvelope(payload, workflow) {
  return { schema_version: workflow === "nokiy" ? "nokiy_native_tool_v3" : "nokiy_native_tool_v2",
    ...(workflow === "nokiy" ? { workflow: "nokiy" } : {}), worker,
    reasoning_effort: effort, service_tier: tier, payload };
}

for (const workflow of ["nokiy", "nokiy-direct"]) {
  for (const type of ["custom", "function"]) {
    test(`${workflow}/${type} advertises contract-derived inclusive draft bounds`, () => {
      const draftContract = JSON.parse(readFileSync(
        new URL("../src/nokiy-native-contract.json", import.meta.url), "utf8")).draft;
      const tool = projection(type, workflow).body.tools.at(-1);
      assert.equal(tool.type, type);
      const advertised = tool.description.match(
        /Draft bounds \(inclusive; null only where listed\): (.*?)\.(?: |$)/)?.[1].split("; ");
      const expected = Object.entries(draftContract.properties).flatMap(([name, spec]) => {
        const types = [spec.type].flat();
        if (types.includes("integer")) return [
          `${name}: integer ${spec.minimum}..${spec.maximum}${types.includes("null") ? " or null" : ""}`,
        ];
        return Object.hasOwn(spec, "x-maxUtf8Bytes")
          ? [`${name}: <=${spec["x-maxUtf8Bytes"]} UTF-8 bytes`] : [];
      });
      assert.deepEqual(advertised, expected);
      assert.deepEqual(advertised.filter((bound) => bound.endsWith(" or null"))
        .map((bound) => bound.split(":")[0]).sort(), ["max_trajectory_bytes", "timeout_seconds"]);
      assert.doesNotMatch(tool.description, /An explicit integer from \d+ through \d+ seconds/);
      if (type === "function") {
        assert.deepEqual(tool.parameters.properties.draft, draftContract);
        if (workflow === "nokiy-direct") {
          assert.deepEqual(tool.parameters.properties.members.items.properties.draft, draftContract);
        }
      }
    });
  }
}

test("custom description and function schemas advertise bounded run_ref in both workflows", () => {
  for (const workflow of ["nokiy", "nokiy-direct"]) {
    for (const type of ["custom", "function"]) {
      const tool = projection(type, workflow).body.tools.at(-1);
      assert.match(tool.description, /"operation":"run_ref"/);
      assert.match(tool.description, /98304 bytes once/);
      assert.match(tool.description, /not an envelope, reference, batch or recovery/);
      assert.match(tool.description, /digest do not change action identity/);
      if (type === "function") {
        assert.deepEqual(tool.parameters.properties.operation.enum, workflow === "nokiy"
          ? ["run", "run_ref", "read_result", "read_preparation", "read_http", "query_surfaces", "navigate"] : ["run", "run_ref", "read_result", "read_preparation", "read_http", "query_surfaces", "navigate", "run_batch", "read_batch"]);
        assert.equal(tool.parameters.properties.input_path.type, "string");
        assert.deepEqual(tool.parameters.properties.input_sha256,
          { type: "string", minLength: 64, maxLength: 64, pattern: "^[0-9a-f]{64}$" });
        assert.equal(tool.parameters.additionalProperties, false);
      }
    }
  }
});

test("batch reference is advertised only by a supporting Ultra host", () => {
  const enabled = { ...host, batch_reference: "direct_v1" };
  for (const type of ["function", "custom"]) {
    const admitted = projection(type, "nokiy-direct", enabled);
    const tool = admitted.body.tools.at(-1);
    assert.ok(admitted.operations.includes("run_batch_ref"));
    assert.match(tool.description, /"operation":"run_batch_ref"/);
    if (type === "function") assert.ok(tool.parameters.properties.operation.enum.includes("run_batch_ref"));
    for (const blocked of [projection(type), projection(type, "nokiy", enabled)]) {
      assert.ok(!blocked.operations.includes("run_batch_ref"));
      assert.doesNotMatch(blocked.body.tools.at(-1).description, /"operation":"run_batch_ref"/);
      assert.throws(() => nativeToolItem(item({ ...reference(), operation: "run_batch_ref" }, type), blocked));
    }
  }
});

test("batch reference projection retains exact identity without opening the input", () => {
  const payload = { ...reference(), operation: "run_batch_ref" };
  const admitted = projection("function", "nokiy-direct", { ...host, batch_reference: "direct_v1" });
  const call = item(payload);
  const projected = nativeToolItem(call, admitted);
  assert.equal(projected.id, call.id);
  assert.equal(projected.call_id, call.call_id);
  assert.deepEqual(envelope(JSON.parse(projected.arguments)), expectedEnvelope(payload, "nokiy-direct"));
  assert.equal(existsSync(payload.input_path), false);
  for (const bad of [{ ...payload, input_sha256: "bad" }, { ...payload, members: [] },
    { operation: "run_batch_ref", input_path: payload.input_path }]) {
    assert.throws(() => nativeToolItem(item(bad), admitted));
  }
});

test("action format is advertised consistently for serial, batch and custom intake", () => {
  const action = JSON.parse(readFileSync(
    new URL("../src/nokiy-native-contract.json", import.meta.url), "utf8")).action;
  assert.equal(action.properties.source_read.type, "boolean");
  assert.deepEqual(action.properties.operations.items.enum, ["read", "create", "modify", "delete", "command"]);
  assert.deepEqual(action.properties.allowed_operations.items.enum, action.properties.operations.items.enum);
  assert.match(action.properties.source_read.description, /requires read and command/);
  assert.deepEqual(action.properties.command_templates.items.type, ["object", "string"]);
  for (const workflow of ["nokiy", "nokiy-direct"]) {
    for (const type of ["custom", "function"]) {
      const tool = projection(type, workflow).body.tools.at(-1);
      assert.match(tool.description, /source_read:false becomes omission without adding a grant/);
      assert.match(tool.description, /exact scoped cat\/pwd strings become typed templates automatically/);
      if (type === "function") {
        // The advertised action is the intake contract plus the adapter-owned verifier and source-query fields.
        const advertised = [tool.parameters.properties.action,
          ...(workflow === "nokiy-direct" ? [tool.parameters.properties.members.items.properties.action] : [])];
        for (const schema of advertised) {
          const extensions = Object.keys(schema.properties).filter((key) => !Object.hasOwn(action.properties, key));
          assert.deepEqual(extensions.sort(), ["source_queries", "verifier_commands"]);
          assert.deepEqual({ ...schema, properties: Object.fromEntries(Object.entries(schema.properties)
            .filter(([key]) => !extensions.includes(key))) }, action);
        }
      }
    }
  }
});

test("HTTP evidence uses the existing native dispatch in both workflows without a model", () => {
  const payload = { operation: 'read_http', artifact_root: '/existing/task',
    urls: ['https://example.invalid/api/strategies'], observation_id: 'current-strategies-v1' };
  for (const workflow of ['nokiy', 'nokiy-direct']) {
    const projected = projection('function', workflow);
    const tool = projected.body.tools.at(-1);
    assert.ok(tool.parameters.properties.operation.enum.includes('read_http'));
    assert.equal(tool.parameters.properties.urls.maxItems, 4);
    assert.match(tool.description, /without a model, headers, credentials, cookies or automatic redirects/);
    const dispatched = nativeToolItem(item(payload), projected);
    assert.deepEqual(envelope(JSON.parse(dispatched.arguments)), expectedEnvelope(payload, workflow));
  }
});

test("scalar and batched navigation preserve the existing native route in both workflows", () => {
  const scope = { operation: "navigate", workspace: "/workspace", read_paths: ["src", "tests"] };
  for (const workflow of ["nokiy", "nokiy-direct"]) {
    const projected = projection("function", workflow);
    for (const payload of [{ ...scope, query: "legacy" },
      { ...scope, queries: ["Zed", "a.*", "Zed", "雪"], query: "", projection: "locations" }]) {
      const original = item(payload);
      const dispatched = nativeToolItem(original, projected);
      assert.equal(dispatched.name, "exec_command");
      assert.equal(dispatched.namespace, "functions");
      assert.equal(dispatched.call_id, original.call_id);
      assert.deepEqual(envelope(JSON.parse(dispatched.arguments)), expectedEnvelope(payload, workflow));
    }
  }
});

test("batch startup advertises direct-child materialization without a setup tool round", () => {
  for (const type of ["custom", "function"]) {
    const tool = projection(type, "nokiy-direct").body.tools.at(-1);
    for (const description of [tool.description,
      ...(type === "function" ? [tool.parameters.properties.members.description] : [])]) {
      assert.match(description, /canonical absolute plan directory and may be their common parent; if that one leaf is missing but its parent exists, the host creates it/);
      assert.match(description, /Missing direct member directories are created by the host after whole-batch input validation/);
      assert.match(description, /without a separate setup tool call/);
      assert.match(description, /no parent or workspace directory is created/);
      assert.match(description, /Original-plan recovery never creates directories or dispatches missing members/);
      assert.match(description, /Ordinary members may use different draft\.workspace directories when no artifact_root lies inside any member workspace/);
      assert.doesNotMatch(description, /create these canonical absolute directories before dispatch/);
    }
  }
});

test("batch artifact layout is explicit in custom and function intake", () => {
  for (const type of ["custom", "function"]) {
    const tool = projection(type).body.tools.at(-1);
    for (const text of [tool.description, ...(type === "function"
      ? [tool.parameters.properties.members.description] : [])]) {
      assert.equal(typeof text, "string");
      assert.match(text, /pairwise disjoint/);
      assert.match(text, /neither identical nor ancestors/);
      assert.match(text, /batch artifact_root is a canonical absolute plan directory and may be their common parent/);
      assert.match(text, /Missing direct member directories are created by the host/);
      assert.match(text, /\/task\/batch\/a/);
      assert.match(text, /\/task\/batch\/b/);
    }
  }
  assert.doesNotMatch(projection("custom", "nokiy").body.tools.at(-1).description,
    /batch artifact_root is a canonical absolute plan directory/);
});

test("function routing preserves reference path/hash, IDs and host model binding without opening the file", () => {
  for (const workflow of ["nokiy", "nokiy-direct"]) {
    const payload = reference();
    assert.equal(existsSync(payload.input_path), false);
    const original = item(payload);
    const environment = { HOME: process.env.HOME, CODEX_HOME: process.env.CODEX_HOME,
      CODEX_THREAD_ID: process.env.CODEX_THREAD_ID };
    const projected = nativeToolItem(original, projection("function", workflow));
    assert.equal(projected.type, original.type);
    assert.equal(projected.id, original.id);
    assert.equal(projected.call_id, original.call_id);
    assert.equal(projected.status, original.status);
    assert.equal(projected.name, "exec_command");
    assert.equal(projected.namespace, "functions");
    const args = JSON.parse(projected.arguments);
    assert.deepEqual(envelope(args), expectedEnvelope(payload, workflow));
    assert.ok(args.cmd.startsWith("'/stub/python'\\''3' '/stub/雪'\\''nokiy-native-host.py' '--payload' "));
    assert.equal(args.yield_time_ms, 1000);
    assert.equal(args.max_output_tokens, 131072);
    assert.deepEqual(environment, { HOME: process.env.HOME, CODEX_HOME: process.env.CODEX_HOME,
      CODEX_THREAD_ID: process.env.CODEX_THREAD_ID });
    assert.deepEqual(original, item(payload));
  }
});

test("inline and reference routing differ only in payload, not host envelope or call identity", () => {
  const inline = filePayload;
  for (const workflow of ["nokiy", "nokiy-direct"]) {
    const admitted = projection("function", workflow);
    const outputs = [inline, reference()].map((payload) => nativeToolItem(item(payload), admitted));
    const envelopes = outputs.map((output) => envelope(JSON.parse(output.arguments)));
    assert.deepEqual({ ...envelopes[0], payload: null }, { ...envelopes[1], payload: null });
    assert.deepEqual(envelopes[0].payload, inline);
    assert.deepEqual(envelopes[1].payload, reference());
    assert.equal(outputs[0].call_id, outputs[1].call_id);
    assert.equal(outputs[0].id, outputs[1].id);
  }
});

test("custom code mode invokes only a stubbed native boundary and preserves terminal identity", async () => {
  const payload = reference();
  const projected = nativeToolItem(item(payload, "custom"), projection("custom", "nokiy"));
  assert.equal(projected.type, "custom_tool_call");
  assert.equal(projected.name, "exec");
  assert.equal(projected.call_id, "native_call_123");
  assert.equal(projected.id, "native_item_雪");
  const calls = [], printed = [];
  await runInNewContext(`(async () => { ${projected.input}\n })()`, {
    tools: {
      async exec_command(args) { calls.push(args); return { session_id: 42, output: "initial\n" }; },
      async write_stdin(args) {
        assert.equal(args.session_id, 42);
        assert.equal(args.chars, "");
        return { exit_code: 0, output: "terminal 雪\n", marker: "original-result" };
      },
    },
    text(value) { printed.push(value); },
  }, { timeout: 1000 });
  assert.equal(calls.length, 1);
  assert.deepEqual(envelope(calls[0]), expectedEnvelope(payload, "nokiy"));
  assert.equal(printed.length, 1);
  assert.equal(printed[0].terminal, true);
  assert.equal(printed[0].recovery_session_id, 42);
  assert.equal(printed[0].marker, "original-result");
  assert.equal(printed[0].output, "initial\nterminal 雪\n");
});

async function nativeTransport(results, workflow = "nokiy-direct") {
  const projected = nativeToolItem(item(reference(), "custom"), projection("custom", workflow));
  const outer = JSON.parse(projected.input.match(/^\/\/ @exec: (.+)$/m)[1]);
  assert.deepEqual(outer, { yield_time_ms: 600000, max_output_tokens: 262144 });
  let index = 0, dispatches = 0;
  const printed = [];
  const tools = {
    async exec_command(args) {
      dispatches++;
      assert.equal(args.yield_time_ms, 1000);
      assert.equal(args.max_output_tokens, 131072);
      assert.equal("timeout_ms" in args, false);
      assert.equal(index, 0, "never replay the original dispatch");
      return results[index++];
    },
  };
  if (results.length > 1) tools.write_stdin = async (args) => {
    assert.equal(args.session_id, results[0].session_id);
    assert.equal(args.chars, "");
    assert.equal(args.yield_time_ms, 300000);
    assert.equal(args.max_output_tokens, 131072);
    assert.equal("timeout_ms" in args, false);
    assert.ok(index < results.length, "only observe the supplied original session");
    const result = results[index++];
    if (result instanceof Error) throw result;
    return result;
  };
  await runInNewContext(`(async () => { ${projected.input}\n })()`, {
    tools, text(value) { printed.push(value); },
  }, { timeout: 1000 });
  assert.equal(dispatches, 1);
  assert.equal(index, results.length);
  assert.equal(printed.length, 1);
  assert.ok(Buffer.byteLength(JSON.stringify(printed[0])) < outer.max_output_tokens,
    "outer budget also covers serialization of retained evidence");
  return printed[0];
}

const batchReceipt = JSON.stringify({ plan_path: "/artifacts/original-plan.json", plan_sha256: "a".repeat(64) }) + "\n";
const clipMarker = "\n[Native output truncated; initial receipt and tail retained]\n";

for (const workflow of ["nokiy", "nokiy-direct"]) {
  for (const waited of [false, true]) {
    test(`${workflow} embeds a JSON terminal as an object ${waited ? "after waits" : "immediately"}`, async () => {
      const summary = { schema_version: "nokiy_terminal_summary_v1", status: "RESULT_AVAILABLE",
        result_text: 'Exact 雪 "quoted" \\ answer', nested: { values: [1, 2] } };
      const terminal = { exit_code: 0, output: JSON.stringify(summary) + "\n", chunk_id: "c1",
        wall_time_seconds: 1.5, original_token_count: 99, cancellation: { requested: false } };
      const result = await nativeTransport(waited
        ? [{ session_id: 42, output: "" }, terminal] : [terminal], workflow);
      // The object is parsed inside the code-mode realm; compare its JSON form.
      assert.deepEqual(JSON.parse(JSON.stringify(result.output)), summary);
      assert.equal(result.terminal, true);
      assert.equal(result.exit_code, 0);
      assert.equal(result.recovery_session_id, waited ? 42 : null);
      assert.deepEqual(JSON.parse(JSON.stringify(result.cancellation)), { requested: false });
      for (const key of ["chunk_id", "wall_time_seconds", "original_token_count", "truncated", "output_truncated"]) {
        assert.equal(key in result, false, key);
      }
      // No nested JSON string: quotes inside the terminal are escaped once, not twice.
      assert.equal(JSON.stringify(result).includes('\\\\"'), false);
    });
  }

  test(`${workflow} keeps non-object or clipped output as text`, async () => {
    for (const output of ["[1,2]\n", batchReceipt + JSON.stringify({ status: "x" }) + "\n", "plain log\n"]) {
      const result = await nativeTransport([{ exit_code: 0, output, chunk_id: "c" }], workflow);
      assert.equal(result.output, output);
      assert.equal(result.chunk_id, "c");
    }
    const clipped = await nativeTransport([{ exit_code: 0, ...hostClippedResult }], workflow);
    assert.equal(typeof clipped.output, "string");
    assert.equal(clipped.output_truncated, true);
  });
}

const hostClippedResult = {
  output: "Warning: truncated output (original token count: 7063)\nTotal output lines: 1\n...",
  original_token_count: 7063,
};

for (const workflow of ["nokiy", "nokiy-direct"]) {
  for (const waited of [false, true]) {
    test(`${workflow} recognizes text-only host clipping ${waited ? "across waits" : "immediately"}`, async () => {
      const terminal = { exit_code: 0, output: "ordinary terminal\n", truncated: false, output_truncated: false };
      const result = await nativeTransport(waited ? [
        { session_id: 42, output: batchReceipt },
        { session_id: 42, ...hostClippedResult }, terminal,
      ] : [{ exit_code: 0, ...hostClippedResult }], workflow);
      const complete = (waited ? batchReceipt : "") + hostClippedResult.output + (waited ? terminal.output : "");
      assert.equal(result.output, complete + clipMarker);
      assert.equal(result.truncated, true);
      assert.equal(result.output_truncated, true);
      assert.equal(result.terminal, true);
      assert.equal(result.exit_code, 0);
      assert.equal(result.recovery_session_id, waited ? 42 : null);
      if (!waited) assert.equal(result.original_token_count, 7063);
    });
  }

  test(`${workflow} does not infer clipping from ordinary token metadata`, async () => {
    const output = "ordinary output\n";
    for (const original_token_count of [7063, 131073]) {
      const result = await nativeTransport([{ exit_code: 0, output, original_token_count }], workflow);
      assert.equal(result.output, output);
      assert.equal(result.truncated, false);
      assert.equal(result.output_truncated, false);
      assert.equal(result.terminal, true);
      assert.equal(result.exit_code, 0);
      assert.equal(result.original_token_count, original_token_count);
    }
  });

  test(`${workflow} retains text-only host clipping on an uncertain wait failure`, async () => {
    const result = await nativeTransport([
      { session_id: 42, output: batchReceipt },
      { session_id: 42, ...hostClippedResult },
      new Error("observation unavailable"),
    ], workflow);
    assert.equal(result.output, batchReceipt + hostClippedResult.output + clipMarker);
    assert.equal(result.truncated, true);
    assert.equal(result.output_truncated, true);
    assert.equal(result.terminal, false);
    assert.equal(result.exit_code, null);
    assert.equal(result.session_id, 42);
    assert.equal(result.recovery_session_id, 42);
    assert.equal(result.original_token_count, 7063);
    assert.match(result.wait_error, /Recover the original session; do not replay/);
  });
}

for (const workflow of ["nokiy", "nokiy-direct"]) {
  for (const bytes of [27180, 64 * 1024]) {
    for (const waited of [false, true]) {
      test(`${workflow} retains ${bytes}-byte inspection evidence ${waited ? "across waits" : "immediately"}`, async () => {
        const summary = "s".repeat(bytes);
        assert.equal(Buffer.byteLength(summary), bytes);
        const output = JSON.stringify({ inspection_summary: summary,
          postimage_diff: "--- a/source\n+++ b/source\n@@\n-old\n+new 雪\n",
          failure_evidence: { first_typed_blocker: null, same_input_retry_allowed: false },
          authority_effect: "none" }) + "\n";
        const metadata = { request_id: "tura_embedded_original", cleanup: { status: "complete" },
          cancellation: { requested: false }, authority_effect: "none",
          ...(bytes === 27180 ? { original_token_count: 7063 } : {}) };
        const terminal = { exit_code: 0, output, ...metadata };
        const result = await nativeTransport(waited ? [
          { session_id: 42, output: batchReceipt },
          { session_id: 42, output: "observing\n" }, terminal,
        ] : [terminal], workflow);
        assert.equal(result.terminal, true);
        assert.equal(result.exit_code, 0);
        assert.equal(result.recovery_session_id, waited ? 42 : null);
        if (waited) {
          assert.equal(result.output, batchReceipt + "observing\n" + output);
          assert.equal(result.truncated, false);
          assert.equal(result.output_truncated, false);
        } else {
          // A single JSON terminal is retained complete, embedded as an object.
          assert.deepEqual(JSON.parse(JSON.stringify(result.output)), JSON.parse(output));
          assert.equal("truncated" in result || "output_truncated" in result, false);
        }
        for (const [name, value] of Object.entries(metadata)) {
          if (!waited && name === "original_token_count") assert.equal(name in result, false);
          else assert.deepEqual(result[name], value);
        }
      });
    }
  }
}

for (const waited of [false, true]) {
  test(`bounded overflow retains original batch identity and failure tail ${waited ? "across waits" : "immediately"}`, async () => {
    const limit = 64 * 1024;
    const middle = "x".repeat(3 * limit);
    const metadata = { status: waited ? "CANCELLED" : "FAILED", cleanup: { status: "complete" },
      cancellation: { requested: waited }, failure_evidence: { detail: "original terminal failure" },
      authority_effect: "none", same_input_retry_allowed: false };
    const terminalTail = JSON.stringify({ ...metadata, postimage_diff: "original postimage diff" }) + "\n";
    const complete = batchReceipt + middle + terminalTail;
    const terminal = { exit_code: waited ? 130 : 1, output: waited ? terminalTail : complete,
      truncated: false, output_truncated: false, ...metadata };
    const result = await nativeTransport(waited ? [
      { session_id: 42, output: batchReceipt }, { session_id: 42, output: middle }, terminal,
    ] : [terminal]);
    assert.equal(result.output, complete.slice(0, limit) + clipMarker + complete.slice(-limit));
    assert.ok(result.output.startsWith(batchReceipt));
    assert.ok(result.output.endsWith(terminalTail));
    assert.equal(result.terminal, true);
    assert.equal(result.exit_code, terminal.exit_code);
    assert.equal(result.recovery_session_id, waited ? 42 : null);
    assert.equal(result.truncated, true);
    assert.equal(result.output_truncated, true);
    for (const [name, value] of Object.entries(metadata)) assert.deepEqual(result[name], value);
  });
}

for (const clipping of [{ truncated: true }, { output_truncated: true }]) {
  test(`intermediate host ${Object.keys(clipping)[0]} survives an ordinary terminal`, async () => {
    const terminal = { exit_code: 0, output: "ordinary terminal\n", truncated: false, output_truncated: false,
      cleanup: { status: "complete" }, authority_effect: "none" };
    const result = await nativeTransport([
      { session_id: 42, output: batchReceipt },
      { session_id: 42, output: "host-retained fragment\n", ...clipping }, terminal,
    ]);
    assert.equal(result.output.replace(clipMarker, ""), batchReceipt + "host-retained fragment\n" + terminal.output);
    assert.ok(result.output.includes(clipMarker));
    assert.equal(result.truncated, true);
    assert.equal(result.output_truncated, true);
    assert.equal(result.terminal, true);
    assert.equal(result.recovery_session_id, 42);
    assert.deepEqual(result.cleanup, terminal.cleanup);
    assert.equal(result.authority_effect, "none");
  });
}

test("clipped wait failure retains original recovery, cancellation and authority without replay", async () => {
  const metadata = { cleanup: { status: "pending" }, cancellation: { requested: true }, authority_effect: "none" };
  const result = await nativeTransport([
    { session_id: 42, output: batchReceipt },
    { session_id: 42, output: "clipped fragment\n", truncated: true, ...metadata },
    new Error("observation unavailable"),
  ]);
  assert.equal(result.terminal, false);
  assert.equal(result.exit_code, null);
  assert.equal(result.session_id, 42);
  assert.equal(result.recovery_session_id, 42);
  assert.equal(result.truncated, true);
  assert.equal(result.output_truncated, true);
  assert.ok(result.output.startsWith(batchReceipt));
  assert.match(result.wait_error, /Recover the original session; do not replay/);
  for (const [name, value] of Object.entries(metadata)) assert.deepEqual(result[name], value);
});

test("run_ref retains original-session uncertain recovery and never re-executes", async () => {
  const projected = nativeToolItem(item(reference(), "custom"), projection("custom"));
  let dispatches = 0, waits = 0;
  const printed = [];
  await runInNewContext(`(async () => { ${projected.input}\n })()`, {
    tools: {
      async exec_command() { dispatches++; return { session_id: "original-session", output: "receipt\n" }; },
      async write_stdin(args) {
        waits++;
        assert.equal(args.session_id, "original-session");
        return { session_id: "different-session", output: "unknown\n" };
      },
    },
    text(value) { printed.push(value); },
  }, { timeout: 1000 });
  assert.equal(dispatches, 1);
  assert.equal(waits, 1);
  assert.equal(printed[0].terminal, false);
  assert.equal(printed[0].recovery_session_id, "original-session");
  assert.equal(printed[0].observed_session_id, "different-session");
  assert.match(printed[0].wait_error, /Do not replay/);
});

test("malformed reference wrappers and identity injection are rejected before native routing", () => {
  const payload = reference();
  const invalid = [
    ...[null, 7, "a".repeat(63), "a".repeat(65), "AB".repeat(32), "g".repeat(64), "a".repeat(64) + "\n"]
      .map((input_sha256) => ({ ...payload, input_sha256 })),
    ...[null, 7, "", "relative.json"].map((input_path) => ({ ...payload, input_path })),
    ...["draft", "action", "surface_id", "model", "native_thread_id", "HOME", "CODEX_HOME", "CODEX_THREAD_ID"]
      .map((key) => ({ ...payload, [key]: "injected" })),
    { operation: "run_ref", input_path: payload.input_path },
    { operation: "run_ref", input_sha256: payload.input_sha256 },
  ];
  for (const value of invalid) {
    assert.throws(() => nativeToolItem(item(value), projection()), { code: "NOKIY_NATIVE_TOOL_INVALID" });
  }
});

test("the existing 96KiB JS payload byte limit remains exact for run and run_ref", () => {
  const admitted = projection();
  for (const payload of [reference(), filePayload]) {
    const original = item(payload);
    const bytes = Buffer.byteLength(original.arguments);
    const bounded = { ...original, arguments: original.arguments + " ".repeat(96 * 1024 - bytes) };
    assert.equal(Buffer.byteLength(bounded.arguments), 96 * 1024);
    assert.deepEqual(envelope(JSON.parse(nativeToolItem(bounded, admitted).arguments)).payload, payload);
    assert.throws(() => nativeToolItem({ ...bounded, arguments: bounded.arguments + " " }, admitted),
      { code: "NOKIY_NATIVE_TOOL_INVALID" });
  }
});

test("existing inline and recovery/batch operations keep their routing and serial restrictions", () => {
  for (const workflow of ["nokiy", "nokiy-direct"]) {
    const admitted = projection("function", workflow);
    for (const operation of ["run", "read_result", ...(workflow === "nokiy-direct" ? ["run_batch", "read_batch"] : [])]) {
      const payload = { operation };
      assert.deepEqual(envelope(JSON.parse(nativeToolItem(item(payload), admitted).arguments)),
        expectedEnvelope(payload, workflow));
    }
    for (const operation of ["unknown", ...(workflow === "nokiy" ? ["run_batch", "read_batch"] : [])]) {
      assert.throws(() => nativeToolItem(item({ operation }), admitted), { code: "NOKIY_NATIVE_TOOL_INVALID" });
    }
  }
});

test("event projection preserves run_ref call/output IDs and suppresses only virtual argument deltas", () => {
  for (const type of ["function", "custom"]) {
    const admitted = projection(type);
    const project = nativeEventProjector(admitted);
    const original = item(reference(), type);
    assert.equal(project({ type: "response.output_item.added", item: original }), null);
    const delta = { type: type === "function" ? "response.function_call_arguments.delta"
      : "response.custom_tool_call_input.delta", item_id: original.id, delta: "fragment" };
    assert.equal(project(delta), null);
    const unrelated = { ...delta, item_id: "unrelated-id" };
    assert.equal(project(unrelated), unrelated);
    const done = project({ type: "response.output_item.done", item: original });
    assert.equal(done.item.id, original.id);
    assert.equal(done.item.call_id, original.call_id);
    assert.equal(done.item.type, original.type);
    const completed = project({ type: "response.completed", response: { output: [original] } });
    assert.deepEqual(completed.response.output[0], done.item);
    const toolOutput = { type: "response.output_item.done", item: {
      type: type === "function" ? "function_call_output" : "custom_tool_call_output",
      call_id: original.call_id, output: "caller evidence",
    } };
    assert.equal(project(toolOutput), toolOutput);
  }
});

test("SSE Unicode byte splits preserve reference payload and call IDs", () => {
  const admitted = projection();
  const original = item(reference());
  const event = { type: "response.output_item.done", item: original };
  const stream = nativeSseProjector(nativeEventProjector(admitted));
  const bytes = Buffer.from(`data: ${JSON.stringify(event)}\r\n\r\n`, "utf8");
  let output = "";
  for (let offset = 0; offset < bytes.length; offset++) output += stream.push(bytes.subarray(offset, offset + 1));
  output += stream.end();
  const projected = JSON.parse(output.slice("data: ".length).trim());
  assert.equal(projected.item.id, original.id);
  assert.equal(projected.item.call_id, original.call_id);
  assert.deepEqual(envelope(JSON.parse(projected.item.arguments)), expectedEnvelope(reference(), "nokiy-direct"));
});

test("native preparation propagates only bounded static issues, never execution or retry grants", () => {
  const script = String.raw`
import hashlib, importlib.util, itertools, json, subprocess, sys, tempfile
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("native_host_fixture", sys.argv[1])
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)
secret = "NOKIY_HOST_DIAGNOSTIC_SECRET_SENTINEL"
safe = {"path": "action.read_scopes", "reason": "input_missing"}
failure = {"schema_version": "tura_embedded_failure_v1", "status": "BLOCKED_PREEXECUTION",
           "authority_effect": "none",
           "first_typed_blocker": "NOKIY_LOCAL_CONTEXT_INVALID", "detail": secret,
           "detail_sha256": hashlib.sha256(secret.encode()).hexdigest(), "prompt": secret,
           "provider_execution_started": True, "same_input_retry_allowed": True,
           "safe_to_correct_input": False, "issues": [safe]}

with tempfile.TemporaryDirectory(prefix="nokiy-prepare-fixture-") as temporary:
    root = Path(temporary).resolve()
    installed = root / "installed"
    installed.mkdir()
    entry = installed / "version" / "bin" / "nokiy"
    image = root / "runtime-image.json"
    image.write_text(secret)
    (installed / "caller-binding.json").write_text(json.dumps({
        "runtime_image": {"path": str(image), "sha256": host.sha(image)}}))
    native = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex")
    original_exists, original_resolve, original_sha = Path.exists, Path.resolve, host.sha
    cases = itertools.count()

    def exists(path):
        return True if path == native else original_exists(path)

    def resolve(path, strict=False):
        return path if path == native else original_resolve(path, strict=strict)

    def sha(path):
        return "a" * 64 if Path(path) == native else original_sha(path)

    def check(detail, expected=None, exit_code=2):
        artifacts = root / ("case-" + str(next(cases)))
        artifacts.mkdir()
        draft = {"artifact_root": str(artifacts), "workspace": str(root)}
        payload = {"draft": draft, "action": {}}
        envelope = {"payload": payload}
        stdout = detail if isinstance(detail, str) else json.dumps(detail)

        def prepare(command, **kwargs):
            assert command[1] == "prepare", "only the stubbed prepare command is allowed"
            assert kwargs == {"capture_output": True, "text": True}
            if exit_code == 0:
                prepared = Path(command[command.index("--output-dir") + 1])
                prepared.mkdir()
                request = json.loads(Path(command[command.index("--request") + 1]).read_text())
                (prepared / "request.json").write_text(json.dumps(request))
            return subprocess.CompletedProcess(command, exit_code, stdout, secret)

        args = (envelope, entry, "fixture-thread", "fixture-worker", "max", "default", payload, draft)
        with patch.object(Path, "exists", exists), patch.object(Path, "resolve", resolve), \
                patch.object(host, "sha", sha), patch.object(host.subprocess, "run", side_effect=prepare) as run:
            request, outcome = host.prepare_action(*args)
            run.assert_called_once()
            receipt_path, = artifacts.glob("native-input-*/prepare-outcome.json")
            receipt = json.loads(receipt_path.read_text())
            assert receipt["exit_code"] == exit_code
            assert receipt["provider_execution_started"] is False
            assert receipt["same_input_retry_allowed"] is False
            assert secret not in json.dumps(receipt)
            if exit_code == 0:
                assert request.is_file() and outcome is None
                assert receipt["status"] == "PREPARED"
                assert receipt["safe_to_correct_input"] is False
                assert "issues" not in receipt and "first_typed_blocker" not in receipt
                return
            assert request is None and receipt == outcome
            assert outcome["status"] == "REJECTED_PREPARATION"
            assert outcome["safe_to_correct_input"] is True
            assert outcome["diagnostic_sha256"] == hashlib.sha256((stdout + secret).encode()).hexdigest()
            assert outcome.get("issues") == expected
            if expected is None:
                assert "issues" not in outcome
            else:
                assert outcome["first_typed_blocker"] == "NOKIY_LOCAL_CONTEXT_INVALID"
            assert host.prepare_action(*args) == (None, outcome)
            run.assert_called_once()
            receipt["issues"] = [{"path": secret, "reason": "input_missing"}]
            receipt_path.write_text(json.dumps(receipt))
            _, cached = host.prepare_action(*args)
            assert "issues" not in cached and secret not in json.dumps(cached)
            run.assert_called_once()

    allowed = [safe] + [{"path": "action.command_templates", "reason": reason}
                        for reason in ("invalid_shape", "unsupported_command", "invalid_bindings")]
    check(failure, [safe])
    check({**failure, "issues": allowed * 2}, allowed * 2)
    check(failure, [safe], exit_code=7)
    invalid = [None, [], {}, [safe] * 9, [None], [secret],
               [{"path": "action.read_scopes"}], [{**safe, "detail": secret}],
               [{"path": secret, "reason": "input_missing"}],
               [{"path": "action.read_scopes", "reason": secret}],
               [{"path": "action.read_scopes", "reason": "invalid_shape"}],
               [{"path": "action.command_templates[0]", "reason": "invalid_shape"}],
               [{"path": [], "reason": "input_missing"}],
               [{"path": "action.read_scopes", "reason": False}],
               [{"path": secret * 1024, "reason": "input_missing"}],
               [safe, {"path": "action.read_scopes", "reason": secret}]]
    for issues in invalid:
        check({**failure, "issues": issues})
    for changes in ({"schema_version": "unknown"}, {"status": "PREPARED"},
                    {"first_typed_blocker": "NOKIY_UNKNOWN_FAILURE"}):
        check({**failure, **changes})
    old = {key: value for key, value in failure.items() if key not in {"issues", "detail", "prompt"}}
    check(old)
    check("raw exception: " + secret)
    check(failure, exit_code=0)
print("ok")
`;
  const result = spawnSync("python3", ["-c", script,
    fileURLToPath(new URL("../src/nokiy-native-host.py", import.meta.url))], {
    encoding: "utf8", timeout: 15000,
  });
  assert.ifError(result.error);
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout, "ok\n");
});

test("coordinator setup guidance avoids serial pre-dispatch round trips", () => {
  for (const type of ["custom", "function"]) {
    const direct = projection(type, "nokiy-direct").body.tools.at(-1).description;
    assert.match(direct, /do not open the nokiy SKILL\.md/);
    assert.match(direct, /Dispatch run_batch directly: the host creates one missing batch artifact_root leaf and resolves omitted DCF surfaces/);
    assert.match(direct, /Combine any other independent setup into one call; every call is a separate coordinator round trip/);
    assert.doesNotMatch(direct, /parallel tool calls/);
    const serial = projection(type, "nokiy").body.tools.at(-1).description;
    assert.match(serial, /do not open the nokiy SKILL\.md/);
    assert.doesNotMatch(serial, /parallel tool calls/);
  }
});

test("host creates one missing batch leaf and picks a majority DCF surface without granting reads", () => {
  const script = String.raw`
import importlib.util, os, sys, tempfile
from pathlib import Path

spec = importlib.util.spec_from_file_location("native_host_fixture", sys.argv[1])
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)
with tempfile.TemporaryDirectory() as raw:
    base = Path(raw).resolve()
    assert host.batch_directory(str(base)) == (base, False)
    leaf = base / "plan"
    assert host.batch_directory(str(leaf)) == (leaf, True)
    assert not leaf.exists()
    for bad in (str(base / "missing" / "plan"), str(leaf) + "/", str(base / "x" / ".." / "plan"),
                "relative/plan", "//" + str(leaf)):
        try:
            host.batch_directory(bad)
        except (ValueError, OSError):
            continue
        raise AssertionError(bad)
    os.symlink(base / "absent", base / "link")
    try:
        host.batch_directory(str(base / "link"))
    except (ValueError, OSError):
        pass
    else:
        raise AssertionError("dangling symlink accepted")
    assert host.majority_surface([["a"], ["a", "b"], []]) == "a"
    assert host.majority_surface([["a", "b"]]) is None
    assert host.majority_surface([[], []]) is None
    assert host.majority_surface("bad") is None
    (base / "src.py").write_text("x = 1\n")
    # A non-DCF workspace never triggers discovery, so no caller is executed.
    assert host.resolve_surface(base / "no-such-entry", base, {"read_scopes": ["src.py"]}) is None
    (base / "scripts" / "ops").mkdir(parents=True)
    (base / "scripts" / "ops" / "dcf.py").write_text("")
    assert host.resolve_surface(base / "no-such-entry", base, {"read_scopes": ["src.py"]}) is None
    assert host.resolve_surface(base / "no-such-entry", base, {"read_scopes": ["*.py", "missing.py"]}) is None
print("ok")
`;
  const result = spawnSync("python3", ["-c", script,
    fileURLToPath(new URL("../src/nokiy-native-host.py", import.meta.url))], { encoding: "utf8", timeout: 15000 });
  assert.ifError(result.error);
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout, "ok\n");
});


test("batch members sharing read scopes run one omitted-surface discovery", () => {
  const script = String.raw`
import importlib.util, sys, tempfile, threading, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

spec = importlib.util.spec_from_file_location("native_host_fixture", sys.argv[1])
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)
calls = []
def fake(entry, workspace, files, writes):
    calls.append(tuple(files))
    time.sleep(0.2)
    return "surface_a", {"status": "SELECTED", "selected": "surface_a"}
host._query_ranked_surface = fake
with tempfile.TemporaryDirectory() as raw:
    base = Path(raw).resolve()
    (base / "scripts" / "ops").mkdir(parents=True)
    (base / "scripts" / "ops" / "dcf.py").write_text("")
    for name in ("a.py", "b.py"):
        (base / name).write_text("x = 1\n")
    action = {"read_scopes": ["a.py", "b.py"]}
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: host.resolve_surface(Path("/unused"), base, action), range(3)))
    assert results == ["surface_a"] * 3, results
    assert calls == [("a.py", "b.py")], calls
    assert host.resolve_surface(Path("/unused"), base, {"read_scopes": ["b.py"]}) == "surface_a"
    assert calls == [("a.py", "b.py"), ("b.py",)], calls
print("ok")
`;
  const result = spawnSync("python3", ["-c", script,
    fileURLToPath(new URL("../src/nokiy-native-host.py", import.meta.url))], { encoding: "utf8", timeout: 15000 });
  assert.ifError(result.error);
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout, "ok\n");
});


test("omitted-surface ranking covers every write target and reports only surface IDs", () => {
  const script = String.raw`
import importlib.util, json, sys, tempfile
from pathlib import Path

spec = importlib.util.spec_from_file_location("native_host_fixture", sys.argv[1])
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)
# Reads favour "read_side", but only "both" admits the write target: the write decides.
selected, resolution = host.rank_surfaces([["both", "write_only"], ["read_side", "both"], ["read_side"], ["read_side"]], 1)
assert selected == "both", resolution
assert resolution["status"] == "SELECTED" and resolution["write_targets_queried"] == 1, resolution
# Without writes the same reads keep the old majority rule.
assert host.rank_surfaces([["read_side", "both"], ["read_side"], ["read_side"]])[0] == "read_side"
selected, resolution = host.rank_surfaces([["a"], ["b"], ["a", "b"]], 2)
assert selected is None and resolution["status"] == "WRITES_SPAN_SURFACES", resolution
selected, resolution = host.rank_surfaces([["a", "b"], ["a", "b"]], 1)
assert selected is None and resolution["status"] == "TIED" and resolution["tied"] == ["a", "b"], resolution
assert host.rank_surfaces([[], []])[1]["status"] == "EMPTY"
assert host.rank_surfaces([["../secret path"], ["ok_id"]])[1]["ranked"] == [["ok_id", 1]]
seen = []
def fake(entry, workspace, files, writes):
    seen.append((tuple(files), tuple(sorted(writes))))
    return None, {"status": "TIED", "ranked": [["a", 2], ["b", 2]], "files_queried": len(files),
                  "write_targets_queried": len(writes), "tied": ["a", "b"]}
host._query_ranked_surface = fake
with tempfile.TemporaryDirectory() as raw:
    base = Path(raw).resolve()
    (base / "scripts" / "ops").mkdir(parents=True)
    (base / "scripts" / "ops" / "dcf.py").write_text("")
    for name in ("r1.py", "r2.py", "w.py"):
        (base / name).write_text("x = 1\n")
    action = {"read_scopes": ["r1.py", "w.py", "r2.py"], "write_scopes": ["w.py"],
              "target_paths": ["w.py", "new.py"]}
    surface, resolution = host.resolve_surface_detail(Path("/unused"), base, action)
    assert surface is None and resolution["status"] == "TIED", resolution
    # Existing write targets are queried first; a target that does not exist yet is skipped.
    assert seen == [(("w.py", "r1.py", "r2.py"), ("w.py",))], seen
    assert str(base) not in json.dumps(resolution)
    empty = host.resolve_surface_detail(Path("/unused"), base, {"read_scopes": ["missing.py"]})
    assert empty == (None, {"status": "NO_QUERYABLE_FILES"}), empty
    # A read-only action takes the first tied surface that covers every queried file.
    host._query_ranked_surface = lambda entry, workspace, files, writes: (None, {
        "status": "TIED", "ranked": [["b", len(files)], ["a", len(files)]], "files_queried": len(files),
        "write_targets_queried": 0, "tied": ["b", "a"]})
    read_only = {"read_scopes": ["r1.py"], "operations": ["read", "command"]}
    surface, resolution = host.resolve_surface_detail(Path("/unused"), base, read_only)
    assert surface == "a" and resolution["status"] == "SELECTED_READ_ONLY_TIE", resolution
    # Tied surfaces that each miss a read stay tied, and so does any action that may write.
    host._query_ranked_surface = lambda entry, workspace, files, writes: (None, {
        "status": "TIED", "ranked": [["a", 1], ["b", 1]], "files_queried": 2,
        "write_targets_queried": 0, "tied": ["a", "b"]})
    partial = {"read_scopes": ["r1.py", "r2.py"], "operations": ["read"]}
    assert host.resolve_surface_detail(Path("/unused"), base, partial)[1]["status"] == "TIED"
    writer = {"read_scopes": ["r1.py"], "operations": ["read", "create"], "target_paths": ["new.py"]}
    host._query_ranked_surface = lambda entry, workspace, files, writes: (None, {
        "status": "TIED", "ranked": [["a", 1], ["b", 1]], "files_queried": 1,
        "write_targets_queried": 0, "tied": ["a", "b"]})
    assert host.resolve_surface_detail(Path("/unused"), base, writer)[0] is None
print("ok")
`;
  const result = spawnSync("python3", ["-c", script,
    fileURLToPath(new URL("../src/nokiy-native-host.py", import.meta.url))], { encoding: "utf8", timeout: 15000 });
  assert.ifError(result.error);
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout, "ok\n");
});

