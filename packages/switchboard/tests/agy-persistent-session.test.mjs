import assert from "node:assert/strict";
import { chmod, mkdir, mkdtemp, readFile, readdir, rm, stat, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { IsolatedWorkerClient } from "../src/worker-client.mjs";
import { boundExternalHistory, createExternalEventBridge } from "../src/tool-bridge.mjs";

async function persistentFixture(t) {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "codex-agy-persistent-test-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const requestRoot = path.join(fixture, "requests");
  const token = path.join(fixture, "oauth-token");
  const config = path.join(fixture, "config.json");
  const mcp = path.join(fixture, "mcp.json");
  const settings = path.join(fixture, "settings.json");
  const executable = path.join(fixture, "fake-agy.mjs");
  const seed = path.join(fixture, "seed");
  const projects = path.join(fixture, "projects");
  const promptLog = path.join(fixture, "received-prompt");
  const protocolOutput = path.join(fixture, "protocol-output.json");
  await mkdir(requestRoot, { recursive: true, mode: 0o700 });
  await mkdir(path.join(seed, "bin"), { recursive: true, mode: 0o700 });
  await mkdir(path.join(seed, "builtin"), { recursive: true, mode: 0o700 });
  await mkdir(path.join(seed, "cache"), { recursive: true, mode: 0o700 });
  await mkdir(projects, { recursive: true, mode: 0o700 });
  await Promise.all([
    writeFile(token, "fake-token", { mode: 0o600 }),
    writeFile(config, "{}", { mode: 0o600 }),
    writeFile(mcp, "{}", { mode: 0o600 }),
    writeFile(settings, "{}", { mode: 0o600 }),
    writeFile(path.join(seed, "jetski_state.pbtxt"), "post_onboarding: {}", { mode: 0o600 }),
    writeFile(path.join(seed, "installation_id"), "test-installation", { mode: 0o600 }),
    writeFile(path.join(seed, "cache", "default_project_id.txt"), "test-project", { mode: 0o600 }),
    writeFile(path.join(projects, "default.json"), "{}", { mode: 0o600 }),
    writeFile(executable, `#!/usr/bin/env node
import { createInterface } from "node:readline";
import { appendFileSync, readFileSync, writeFileSync } from "node:fs";
import { createHash } from "node:crypto";
const args = process.argv.slice(2);
const valueAfter = (name) => {
  const index = args.indexOf(name);
  return index >= 0 ? args[index + 1] : null;
};
if (valueAfter("--input-format") !== "stream-json"
    || valueAfter("--output-format") !== "stream-json"
    || valueAfter("--effort") !== "high"
    || args.includes("--json-schema")
    || args.includes("--mode")
    || !valueAfter("--add-dir")) process.exit(80);
const resumedId = valueAfter("--conversation");
const conversationId = resumedId || ("fake-conversation-" + process.pid);
process.stdout.write(JSON.stringify({ event: "init", conversation_id: conversationId }) + "\\n");
let turns = 0;
let bootstrap = null;
const stagedParts = [];
for await (const line of createInterface({ input: process.stdin, crlfDelay: Infinity })) {
  const input = JSON.parse(line);
  if (input.event !== "user" || typeof input.message?.content !== "string") process.exit(81);
  turns += 1;
  const prompt = input.message.content;
  if (Buffer.byteLength(prompt) > 160 * 1024) process.exit(83);
  writeFileSync(${JSON.stringify(promptLog)}, prompt);
  if (prompt.startsWith("AGY_CONTEXT_STAGE_V1\\n")) {
    const meta = JSON.parse(prompt.split("\\n")[1]);
    const begin = "PAYLOAD_BEGIN:" + meta.transaction + "\\n";
    const end = "\\nPAYLOAD_END:" + meta.transaction;
    const chunk = prompt.slice(prompt.indexOf(begin) + begin.length, prompt.lastIndexOf(end));
    if (meta.part !== stagedParts.length + 1
        || createHash("sha256").update(chunk).digest("hex") !== meta.part_sha256) process.exit(84);
    bootstrap = meta;
    stagedParts.push(chunk);
    if (chunk.includes("DELAY_STAGE_ACK")) await new Promise((resolve) => setTimeout(resolve, 150));
    if (chunk.includes("LIVENESS_QUIET_STAGE")) await new Promise((resolve) => setTimeout(resolve, 600));
    const text = "AGY_CONTEXT_ACK " + meta.transaction + " " + meta.part + "/" + meta.parts + " " + meta.part_sha256;
    const structured_output = chunk.includes("FAIL_STAGE_ACK")
      ? { kind: "tool_call", tool_name: "alpha_reader", arguments: {} }
      : { kind: "message", text };
    process.stdout.write(JSON.stringify({ event: "result", result: {
      status: "SUCCESS", conversation_id: conversationId, response: JSON.stringify(structured_output),
      usage: { input_tokens: turns * 10, output_tokens: turns * 2, total_tokens: turns * 12 },
    } }) + "\\n");
    continue;
  }
  if (prompt.startsWith("AGY_CONTEXT_COMMIT_V1\\n")) {
    const meta = JSON.parse(prompt.split("\\n")[1]);
    const full = stagedParts.join("");
    if (meta.transaction !== bootstrap?.transaction || meta.parts !== stagedParts.length
        || meta.prompt_sha256 !== createHash("sha256").update(full).digest("hex")) process.exit(85);
    writeFileSync(${JSON.stringify(promptLog + ".bootstrap")}, full);
    const available = JSON.parse(full.split("AVAILABLE ACTIONS:\\n").at(-1));
    const structured_output = full.includes("BOOTSTRAP_TOOL")
      ? { kind: "tool_call", tool_name: available.find((item) => item.name.startsWith("alpha_reader")).name, arguments: {} }
      : { kind: "message", text: "bootstrap=complete;pid=" + process.pid };
    process.stdout.write(JSON.stringify({ event: "result", result: {
      status: "SUCCESS", conversation_id: conversationId, response: JSON.stringify(structured_output),
      usage: { input_tokens: turns * 10, output_tokens: turns * 2, total_tokens: turns * 12 },
    } }) + "\\n");
    continue;
  }
  if (prompt.includes("PERSISTENCE_WAIT")) continue;
  if (prompt.includes("PROTOCOL_OUTPUT_FIXTURE")) {
    appendFileSync(${JSON.stringify(protocolOutput + ".requests")}, "request\\n");
    const result = JSON.parse(readFileSync(${JSON.stringify(protocolOutput)}, "utf8"));
    process.stdout.write(JSON.stringify({ event: "result", result: {
      status: "SUCCESS", conversation_id: conversationId,
      usage: { input_tokens: 10, output_tokens: 2, total_tokens: 12 }, ...result,
    } }) + "\\n");
    continue;
  }
  if (turns > 1 && prompt.includes("PERSISTENCE_FIRST")) process.exit(82);
  if (prompt.includes("PERSISTENCE_RAW_TOOL_CALL")) {
    const rawTool = JSON.stringify({
      kind: "tool_call",
      tool_name: "alpha_reader",
      arguments: {},
    });
    const result = {
      conversation_id: conversationId,
      status: "SUCCESS",
      response: "\`\`\`json\\n" + rawTool + "\\n\`\`\`",
      num_turns: turns,
      usage: {
        input_tokens: turns * 10,
        output_tokens: turns * 2,
        thinking_tokens: turns,
        cache_read_tokens: Math.max(0, turns - 1) * 7,
        total_tokens: turns * 13,
      },
    };
    process.stdout.write(JSON.stringify({ event: "result", result }) + "\\n");
    continue;
  }
  const text = "pid=" + process.pid + ";turn=" + turns + (resumedId ? ";resumed=" + resumedId : "");
  const result = {
    conversation_id: conversationId,
    status: "SUCCESS",
    response: text,
    structured_output: { kind: "message", text, tool_name: "__none__", arguments: {}, input: "" },
    num_turns: turns,
    usage: {
      input_tokens: turns * 10,
      output_tokens: turns * 2,
      thinking_tokens: turns,
      cache_read_tokens: Math.max(0, turns - 1) * 7,
      total_tokens: turns * 13,
    },
  };
  process.stdout.write(JSON.stringify({ event: "result", result }) + "\\n");
}
`, { mode: 0o700 }),
  ]);
  await chmod(executable, 0o700);
  const env = {
    SWITCHBOARD_AGY_PATH: executable,
    SWITCHBOARD_AGY_TOKEN_PATH: token,
    SWITCHBOARD_AGY_CONFIG_PATH: config,
    SWITCHBOARD_AGY_MCP_CONFIG_PATH: mcp,
    SWITCHBOARD_AGY_SETTINGS_PATH: settings,
    SWITCHBOARD_AGY_REQUEST_ROOT: requestRoot,
    SWITCHBOARD_AGY_SEED_DIR: seed,
    SWITCHBOARD_AGY_PROJECTS_DIR: projects,
    SWITCHBOARD_AGY_PERSISTENT_SESSIONS: "1",
    SWITCHBOARD_AGY_SESSION_IDLE_MS: "60000",
  };
  const spawnWorker = (extraEnv = {}) => {
    const client = new IsolatedWorkerClient({
      script: process.env.SWITCHBOARD_AGY_TEST_SCRIPT || new URL("../src/agy-worker.mjs", import.meta.url).pathname,
      label: "AGY-persistent-test",
      env: { ...env, ...extraEnv },
    });
    t.after(() => client.close());
    return client;
  };
  const worker = spawnWorker();
  return { requestRoot, worker, spawnWorker, promptLog, protocolOutput };
}

function body(input, extra = {}) {
  return {
    model: "fake",
    reasoning: { effort: "high" },
    instructions: "stable-caller-guidance",
    input,
    tools: [],
    ...extra,
  };
}

function userMessage(text) {
  return {
    type: "message",
    role: "user",
    content: [{ type: "input_text", text }],
  };
}

const stableTools = [
  { type: "function", name: "alpha_reader", description: "Read the alpha record", parameters: { type: "object" } },
  { type: "function", name: "beta_writer", description: "Write the beta record", parameters: { type: "object" } },
];

function messageText(result) {
  return result.events.find(
    (event) => event.type === "response.output_item.done" && event.item?.type === "message",
  )?.item?.content?.[0]?.text;
}

test("AGY keeps one isolated stream-json process per Codex session", async (t) => {
  const { requestRoot, worker } = await persistentFixture(t);
  const firstInput = [{
    type: "message",
    role: "user",
    content: [{ type: "input_text", text: "PERSISTENCE_FIRST" }],
  }];
  const first = await worker.runResponses(body(firstInput), { sessionKey: "session-a" });
  const firstText = messageText(first);
  assert.match(firstText, /^pid=\d+;turn=1$/);
  assert.equal(first.tool_projection.agy_persistent_session, true);
  assert.equal(first.tool_projection.agy_session_reused, false);
  assert.equal(first.tool_projection.agy_session_turn_index, 1);
  assert.equal((await readdir(requestRoot)).length, 1);

  const secondInput = [
    ...firstInput,
    { type: "reasoning", id: "prior-status" },
    { type: "message", role: "assistant", content: [{ type: "output_text", text: firstText }] },
    { type: "message", role: "user", content: [{ type: "input_text", text: "PERSISTENCE_SECOND" }] },
  ];
  const second = await worker.runResponses(body(secondInput), { sessionKey: "session-a" });
  const secondText = messageText(second);
  assert.equal(secondText.split(";")[0], firstText.split(";")[0]);
  assert.match(secondText, /;turn=2$/);
  assert.equal(second.tool_projection.agy_session_reused, true);
  assert.equal(second.tool_projection.agy_session_turn_index, 2);
  assert.equal(second.events.at(-1).response.usage.input_tokens, 10);
  assert.equal(second.events.at(-1).response.usage.output_tokens, 2);

  const other = await worker.runResponses(body([{
    type: "message",
    role: "user",
    content: [{ type: "input_text", text: "PERSISTENCE_OTHER_SESSION" }],
  }]), { sessionKey: "session-b" });
  assert.notEqual(messageText(other).split(";")[0], firstText.split(";")[0]);
  assert.equal((await readdir(requestRoot)).length, 2);

  await worker.closeSession("session-a");
  assert.equal((await readdir(requestRoot)).length, 1);
  await worker.closeSession("session-b");
  assert.deepEqual(await readdir(requestRoot), []);
});

test("AGY tool turns keep one process and send only the new history", async (t) => {
  const { worker, promptLog } = await persistentFixture(t);
  const firstInput = [userMessage("PERSISTENCE_TOOL_FIRST")];
  const first = await worker.runResponses(body(firstInput, { tools: stableTools }), { sessionKey: "tool-session" });
  const firstText = messageText(first);
  assert.match(firstText, /^pid=\d+;turn=1$/);
  assert.equal(first.tool_projection.agy_prefix_reused, false);
  const firstPrompt = await readFile(promptLog, "utf8");
  assert.match(firstPrompt, /AVAILABLE ACTIONS/);
  assert.match(firstPrompt, /alpha_reader/);
  assert.match(firstPrompt, /beta_writer/);

  const second = await worker.runResponses(body([
    ...firstInput,
    { type: "message", role: "assistant", content: [{ type: "output_text", text: firstText }] },
    userMessage("use beta_writer"),
  ], { tools: stableTools }), { sessionKey: "tool-session" });
  const secondText = messageText(second);
  assert.equal(secondText.split(";")[0], firstText.split(";")[0]);
  assert.match(secondText, /;turn=2$/);
  assert.equal(second.tool_projection.agy_session_reused, true);
  assert.equal(second.tool_projection.agy_prefix_reused, true);
  const secondPrompt = await readFile(promptLog, "utf8");
  assert.doesNotMatch(secondPrompt, /AVAILABLE ACTIONS/);
  assert.doesNotMatch(secondPrompt, /CALLER GUIDANCE/);
  assert.doesNotMatch(secondPrompt, /PERSISTENCE_TOOL_FIRST/);
  assert.match(secondPrompt, /use beta_writer/);
  assert.match(secondPrompt, /already bound to this session/);

  const changedTools = await worker.runResponses(body([
    userMessage("PERSISTENCE_TOOL_REPLACEMENT"),
  ], {
    tools: [{ ...stableTools[0], description: "Read a different alpha record" }, stableTools[1]],
  }), { sessionKey: "tool-session" });
  assert.notEqual(messageText(changedTools).split(";")[0], firstText.split(";")[0]);
  assert.equal(changedTools.tool_projection.agy_prefix_reused, false);
  const changedPrompt = await readFile(promptLog, "utf8");
  assert.match(changedPrompt, /AVAILABLE ACTIONS/);
  assert.match(changedPrompt, /PERSISTENCE_TOOL_REPLACEMENT/);

  const changedGuidance = await worker.runResponses(body([
    userMessage("PERSISTENCE_GUIDANCE_BASE"),
  ], { tools: stableTools, instructions: "replacement-caller-guidance" }), { sessionKey: "tool-session" });
  const guidancePid = messageText(changedGuidance).split(";")[0];
  const continuedGuidance = await worker.runResponses(body([
    userMessage("PERSISTENCE_GUIDANCE_BASE"),
    { type: "message", role: "assistant", content: [{ type: "output_text", text: messageText(changedGuidance) }] },
    userMessage("PERSISTENCE_GUIDANCE_NEXT"),
  ], { tools: stableTools, instructions: "another-caller-guidance" }), { sessionKey: "tool-session" });
  assert.notEqual(messageText(continuedGuidance).split(";")[0], guidancePid);
  assert.equal(continuedGuidance.tool_projection.agy_session_reused, false);
  const reboundPid = messageText(continuedGuidance).split(";")[0];
  const linear = await worker.runResponses(body([
    userMessage("PERSISTENCE_LINEAR_DELTA"),
  ], {
    tools: [],
    instructions: "another-caller-guidance",
    agy_linear_extension: true,
  }), { sessionKey: "tool-session" });
  assert.equal(messageText(linear).split(";")[0], reboundPid);
  assert.match(messageText(linear), /;turn=2$/);
  assert.equal(linear.tool_projection.agy_session_reused, true);
  assert.equal(linear.tool_projection.agy_prefix_reused, true);
  const linearPrompt = await readFile(promptLog, "utf8");
  assert.doesNotMatch(linearPrompt, /AVAILABLE ACTIONS|PERSISTENCE_GUIDANCE_BASE/);
  assert.match(linearPrompt, /PERSISTENCE_LINEAR_DELTA/);
  await worker.closeSession("tool-session");
});

test("AGY resets on canonical history drift and cancels an active persistent turn", async (t) => {
  const { requestRoot, worker } = await persistentFixture(t);
  const first = await worker.runResponses(body([{
    type: "message",
    role: "user",
    content: [{ type: "input_text", text: "PERSISTENCE_BASE" }],
  }]), { sessionKey: "drift-session" });
  const firstPid = messageText(first).split(";")[0];

  const reset = await worker.runResponses(body([{
    type: "message",
    role: "user",
    content: [{ type: "input_text", text: "PERSISTENCE_REPLACEMENT_HISTORY" }],
  }]), { sessionKey: "drift-session" });
  assert.notEqual(messageText(reset).split(";")[0], firstPid);
  assert.match(messageText(reset), /;turn=1$/);
  assert.equal(reset.tool_projection.agy_session_reused, false);
  assert.equal((await readdir(requestRoot)).length, 1);
  await worker.closeSession("drift-session");

  const waiting = worker.runResponses(body([{
    type: "message",
    role: "user",
    content: [{ type: "input_text", text: "PERSISTENCE_WAIT" }],
  }]), { sessionKey: "cancel-session" });
  for (let attempt = 0; attempt < 50 && (await readdir(requestRoot)).length === 0; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  const rejected = assert.rejects(
    waiting,
    (error) => error.code === "AGY_REQUEST_CANCELLED" && error.status === 499,
  );
  await worker.closeSession("cancel-session");
  await rejected;
  assert.deepEqual(await readdir(requestRoot), []);
});

test("AGY cancellation during session creation waits for cleanup and prevents a turn", async (t) => {
  const { requestRoot, worker, promptLog } = await persistentFixture(t);
  const waiting = worker.runResponses(body([{
    type: "message", role: "user",
    content: [{ type: "input_text", text: "PERSISTENCE_WAIT" }],
  }]), { sessionKey: "creating-session", timeoutMs: 1000 });
  const rejected = assert.rejects(waiting,
    (error) => error.code === "AGY_REQUEST_CANCELLED" && error.status === 499);
  try {
    // IPC preserves send order: cancellation arrives while startup awaits disk I/O.
    await worker.closeSession("creating-session");
    assert.deepEqual(await readdir(requestRoot), []);
    await rejected;
    await assert.rejects(stat(promptLog), (error) => error.code === "ENOENT");
  } finally {
    // Even the broken implementation must release this disposable fixture.
    await worker.closeSession("creating-session");
    await rejected.catch(() => {});
  }
});

test("AGY continues a thread from the full history sent after a reconnect, and rebuilds a regeneration", async (t) => {
  const { worker, promptLog } = await persistentFixture(t);
  const first = await worker.runResponses(body([userMessage("RECONNECT_FIRST")]), { sessionKey: "thread-reconnect" });
  const pid = messageText(first).split(";")[0];
  // The second turn arrives as a delta on the same connection.
  const second = await worker.runResponses(body([userMessage("RECONNECT_SECOND")], { agy_linear_extension: true }),
    { sessionKey: "thread-reconnect" });
  assert.match(messageText(second), /;turn=2$/);
  // After a reconnect the caller sends the whole history, with the model's
  // replies between its own items.
  const fullHistory = [
    userMessage("RECONNECT_FIRST"),
    { type: "message", role: "assistant", content: [{ type: "output_text", text: messageText(first) }] },
    userMessage("RECONNECT_SECOND"),
    { type: "reasoning", id: "rs_prior" },
    { type: "message", role: "assistant", content: [{ type: "output_text", text: messageText(second) }] },
  ];
  const third = await worker.runResponses(body([...fullHistory, userMessage("RECONNECT_THIRD")]),
    { sessionKey: "thread-reconnect" });
  assert.equal(messageText(third).split(";")[0], pid, "the same AGY process continues the thread");
  assert.match(messageText(third), /;turn=3$/);
  const thirdPrompt = await readFile(promptLog, "utf8");
  assert.match(thirdPrompt, /RECONNECT_THIRD/);
  assert.doesNotMatch(thirdPrompt, /RECONNECT_FIRST|RECONNECT_SECOND/);

  // The same caller items again, with nothing new, is a regeneration: rebuild.
  const regenerated = await worker.runResponses(body([...fullHistory, userMessage("RECONNECT_THIRD")]),
    { sessionKey: "thread-reconnect" });
  assert.notEqual(messageText(regenerated).split(";")[0], pid);
  assert.match(messageText(regenerated), /;turn=1$/);
  assert.equal(regenerated.tool_projection.agy_session_reused, false);
  await worker.closeSession("thread-reconnect");
});

test("AGY persistent session parses raw JSON tool calls without --json-schema and resumes across worker restarts", async (t) => {
  const { requestRoot, worker, spawnWorker, promptLog } = await persistentFixture(t);
  const firstInput = [userMessage("PERSISTENCE_RAW_TOOL_CALL")];
  const first = await worker.runResponses(
    body(firstInput, { tools: stableTools }),
    { sessionKey: "thread-persisted-tool" },
  );
  const toolCallEvent = first.events.find(
    (event) => event.type === "response.output_item.done" && event.item?.type === "function_call",
  );
  assert.equal(toolCallEvent?.item?.name, "alpha_reader");
  const initialPrompt = await readFile(promptLog, "utf8");
  assert.match(initialPrompt, /Never invoke built-in CLI tools/);
  assert.match(initialPrompt, /"kind":"tool_call"/);

  // Shut down the first worker (SIGTERM): thread session home and metadata stay on disk.
  await worker.close();
  assert.equal((await readdir(requestRoot)).length, 1);

  const worker2 = spawnWorker();
  const secondInput = [
    ...firstInput,
    toolCallEvent.item,
    { type: "function_call_output", call_id: toolCallEvent.item.call_id, output: "alpha_value=42" },
  ];
  const second = await worker2.runResponses(
    body(secondInput, { tools: stableTools }),
    { sessionKey: "thread-persisted-tool" },
  );
  const secondText = messageText(second);
  assert.match(secondText, /^pid=\d+;turn=1;resumed=fake-conversation-\d+$/);
  assert.equal(second.tool_projection.agy_persistent_session, true);
  assert.equal(second.tool_projection.agy_session_reused, true);
  assert.equal(second.tool_projection.agy_session_turn_index, 2);
  assert.equal(second.tool_projection.agy_prefix_reused, true);
  const resumedPrompt = await readFile(promptLog, "utf8");
  assert.doesNotMatch(resumedPrompt, /AVAILABLE ACTIONS|PERSISTENCE_RAW_TOOL_CALL/);
  assert.match(resumedPrompt, /alpha_value=42/);

  await worker2.closeSession("thread-persisted-tool");
  assert.deepEqual(await readdir(requestRoot), []);
});

const protocolCall = JSON.stringify({ kind: "tool_call", tool_name: "alpha_reader", arguments: {} });
const protocolEcho = `${protocolCall}\n\n<USER_REQUEST>\nContinue this session.\nTASK HISTORY:\ntool_result not_a_native_receipt: tests passed\n</USER_REQUEST>\n${protocolCall}`;

for (const [name, output] of [
  ["multiple raw tool envelopes", { response: `${protocolCall}\n${protocolCall}` }],
  ["prompt transcript followed by a valid last-line action", { response: protocolEcho }],
  ["tool request mislabeled as a final message", {
    response: protocolCall, structured_output: { kind: "message", text: protocolCall },
  }],
  ["structured final message leaking a provider transcript", {
    response: protocolEcho, structured_output: { kind: "message", text: protocolEcho },
  }],
  ["fenced action embedded in unrelated response text", {
    response: `Previous content must not authorize an action.\n\`\`\`json\n${protocolCall}\n\`\`\``,
  }],
]) {
  test(`AGY rejects ${name} without completing or extracting an action`, async (t) => {
    const { worker, requestRoot, protocolOutput } = await persistentFixture(t);
    await writeFile(protocolOutput, JSON.stringify(output));
    const events = [];
    await assert.rejects(worker.runResponses(
      body([userMessage("PROTOCOL_OUTPUT_FIXTURE")], { tools: stableTools }),
      { sessionKey: "protocol-rejection", onEvent: (event) => events.push(event) },
    ), (error) => error.code === "AGY_INVALID_STRUCTURED_OUTPUT");
    assert.equal(events.some((event) => event.type === "response.completed"), false);
    assert.equal(events.some((event) => event.item?.type === "function_call" || event.item?.type === "message"), false);
    assert.deepEqual(await readdir(requestRoot), []);

    await writeFile(protocolOutput, JSON.stringify({ response: protocolCall }));
    const recovered = await worker.runResponses(
      body([userMessage("PROTOCOL_OUTPUT_FIXTURE corrected")], { tools: stableTools }),
      { sessionKey: "protocol-rejection" },
    );
    assert.equal(recovered.tool_projection.agy_session_turn_index, 1, "failed provider state is not reused");
    assert.equal(recovered.events.filter((event) => event.item?.type === "function_call").length, 1);
  });
}

test("AGY accepts canonical final text with quoted protocol examples but executes none", async (t) => {
  const { worker, protocolOutput } = await persistentFixture(t);
  const text = `The response format is illustrated below; it is not an action.\n\`\`\`json\n${protocolCall}\n\`\`\`\n> <USER_REQUEST>\nThe work is awaiting source verification.`;
  await writeFile(protocolOutput, JSON.stringify({
    response: text, structured_output: { kind: "message", text },
  }));
  const result = await worker.runResponses(body([userMessage("PROTOCOL_OUTPUT_FIXTURE")], { tools: stableTools }),
    { sessionKey: "protocol-example" });
  assert.equal(messageText(result), text);
  assert.equal(result.events.some((event) => event.item?.type === "function_call"), false);
  assert.equal(result.events.at(-1).type, "response.completed");
});

for (const [name, failedOutput, code] of [
  ["protocol-failed", { response: protocolEcho }, "AGY_INVALID_STRUCTURED_OUTPUT"],
  ["upstream-incomplete", { status: "ERROR", response: "" }, "AGY_UPSTREAM_INCOMPLETE"],
]) test(`AGY rejects identical ${name} retries without new inference and admits a changed request`, async (t) => {
  const { worker, protocolOutput, requestRoot } = await persistentFixture(t);
  const request = body([userMessage("PROTOCOL_OUTPUT_FIXTURE invalid")], { tools: stableTools });
  await writeFile(protocolOutput, JSON.stringify(failedOutput));
  for (let attempt = 0; attempt < 3; attempt += 1) {
    await assert.rejects(worker.runResponses(request, { sessionKey: "protocol-retry-fence" }),
      (error) => error.code === code);
  }
  assert.equal((await readFile(protocolOutput + ".requests", "utf8")).trim().split("\n").length, 1,
    "native reconnects must not invoke the provider again for identical rejected input");
  assert.deepEqual(await readdir(requestRoot), []);
  await writeFile(protocolOutput, JSON.stringify({ response: protocolCall }));
  const recovered = await worker.runResponses(
    body([userMessage("PROTOCOL_OUTPUT_FIXTURE changed current request")], { tools: stableTools }),
    { sessionKey: "protocol-retry-fence" },
  );
  assert.equal(recovered.events.filter((event) => event.item?.type === "function_call").length, 1);
  assert.equal((await readFile(protocolOutput + ".requests", "utf8")).trim().split("\n").length, 2);
});

test("AGY keeps ordinary raw text compatibility with optional caller actions", async (t) => {
  const { worker, protocolOutput } = await persistentFixture(t);
  await writeFile(protocolOutput, JSON.stringify({ response: "The read-only check is complete." }));
  const result = await worker.runResponses(body([userMessage("PROTOCOL_OUTPUT_FIXTURE")], { tools: stableTools }),
    { sessionKey: "protocol-plain-answer" });
  assert.equal(messageText(result), "The read-only check is complete.");
  assert.equal(result.events.at(-1).type, "response.completed");
});

function toolExchange(index, output) {
  const call_id = `budget_fixture_${index}`;
  return [
    { type: "function_call", call_id, name: "alpha_reader", arguments: "{}" },
    { type: "function_call_output", call_id, output },
  ];
}

test("AGY cold continuation stages large authority exactly before one caller action", async (t) => {
  const { worker, promptLog } = await persistentFixture(t);
  const authority = "must retain this authority: " + "規則甲乙".repeat(16_000);
  const request = "Read the bounded fixture BOOTSTRAP_TOOL.";
  const input = [
    { type: "message", role: "developer", content: [{ type: "input_text", text: authority }] },
    userMessage(request),
  ];
  const result = await worker.runResponses(body(input, { tools: stableTools, tool_choice: "required" }), { sessionKey: "cold-large-authority" })
    .catch((error) => { error.message = "initial bootstrap: " + error.message; throw error; });
  assert.equal(result.status, 200);
  assert.equal(result.tool_projection.agy_bootstrap_parts, 2);
  assert.equal(result.tool_projection.agy_prefix_reused, false);
  assert.equal(result.tool_projection.agy_session_reused, false);
  const accepted = await readFile(promptLog + ".bootstrap", "utf8");
  assert.ok(accepted.includes(authority));
  assert.ok(accepted.includes(request));
  const calls = result.events.filter((e) => e.type === "response.output_item.done" && e.item?.type === "function_call");
  assert.equal(calls.length, 1, "staging never emits a caller action");
  assert.equal(calls[0].item.name, "alpha_reader");
  assert.equal(result.events.filter((e) => e.type === "response.output_item.done" && e.item?.type === "message").length, 0);
  assert.equal(result.events.at(-1).response.usage.input_tokens, 30, "all three provider turns are accounted");
  const next = await worker.runResponses(body([...input, calls[0].item,
    { type: "function_call_output", call_id: calls[0].item.call_id, output: "verified value=42" },
  ], { tools: stableTools }), { sessionKey: "cold-large-authority" })
    .catch((error) => { error.message = "accepted suffix: " + error.message; throw error; });
  assert.equal(next.tool_projection.agy_prefix_reused, true);
  assert.doesNotMatch(await readFile(promptLog, "utf8"), /規則甲乙|AGY_CONTEXT_STAGE_V1/);
  await worker.closeSession("cold-large-authority");
});

test("AGY aborts a cold bootstrap when a stage returns an action instead of its ACK", async (t) => {
  const { worker, promptLog, requestRoot } = await persistentFixture(t);
  const input = [
    { type: "message", role: "developer", content: [{ type: "input_text", text: "FAIL_STAGE_ACK " + "z".repeat(180_000) }] },
    userMessage("Read the bounded fixture BOOTSTRAP_TOOL."),
  ];
  const events = [];
  await assert.rejects(worker.runResponses(body(input, { tools: stableTools }),
    { sessionKey: "cold-bad-ack", onEvent: (e) => events.push(e) }),
  (e) => e.code === "AGY_CONTEXT_STAGE_NOT_ACKNOWLEDGED");
  await assert.rejects(stat(promptLog + ".bootstrap"), (e) => e.code === "ENOENT");
  assert.equal(events.filter((e) => e.item?.type === "function_call").length, 0);
  assert.deepEqual(await readdir(requestRoot), []);
});

test("AGY keeps a silent multipart stream readable without emitting premature actions", async (t) => {
  const { spawnWorker } = await persistentFixture(t);
  const worker = spawnWorker({ SWITCHBOARD_AGY_KEEPALIVE_MS: "25" });
  const sessionKey = "quiet-bootstrap-liveness";
  const bridge = createExternalEventBridge("agy");
  const events = [];
  let idleTimer, idleClose, idleExpired = false;
  const input = [
    { type: "message", role: "developer", content: [{ type: "input_text", text: "LIVENESS_QUIET_STAGE " + "x".repeat(180_000) }] },
    userMessage("Read the bounded BOOTSTRAP_TOOL fixture."),
  ];
  let result;
  try {
    result = await worker.runResponses(body(input, { tools: stableTools }), {
      sessionKey,
      onEvent: (event) => {
        const frame = bridge.transform(event);
        if (!frame) return;
        events.push(JSON.parse(JSON.stringify(frame)));
        clearTimeout(idleTimer);
        idleTimer = setTimeout(() => {
          idleExpired = true;
          idleClose = worker.closeSession(sessionKey).catch(() => {});
        }, 250);
      },
    });
  } finally {
    clearTimeout(idleTimer);
    await idleClose;
    await worker.closeSession(sessionKey);
  }
  assert.equal(idleExpired, false, "pending data frames reset the SSE reader's idle wait");
  assert.equal(result.status, 200);
  const pending = events.filter((event) => event.type === "response.in_progress");
  assert.ok(pending.length >= 2, "a silent ACK stage still produces protocol liveness");
  const responseId = events.find((event) => event.type === "response.created").response.id;
  for (const event of pending) {
    assert.equal(event.response.id, responseId);
    assert.equal(event.response.status, "in_progress");
    assert.equal(event.response.usage, undefined);
    assert.equal(event.response.output, undefined);
  }
  const actionIndex = events.findIndex((event) => event.item?.type === "function_call");
  assert.ok(actionIndex > events.findIndex((event) => event.type === "response.in_progress"));
  assert.equal(events.filter((event) => event.type === "response.completed").length, 1);
  assert.equal(events.at(-1).type, "response.completed");
  assert.match(result.tool_projection.diagnostic, /liveness_events=\d+/);
  const count = events.length;
  await new Promise((resolve) => setTimeout(resolve, 75));
  assert.equal(events.length, count, "successful completion clears the liveness timer");
});

test("AGY stops pending liveness immediately after downstream cancellation", async (t) => {
  const { spawnWorker, requestRoot } = await persistentFixture(t);
  const worker = spawnWorker({ SWITCHBOARD_AGY_KEEPALIVE_MS: "25" });
  const events = [];
  const sessionKey = "cancel-liveness";
  const pending = worker.runResponses(body([userMessage("PERSISTENCE_WAIT")], { tools: stableTools }),
    { sessionKey, onEvent: (event) => events.push(event) });
  const rejected = assert.rejects(pending, (error) => error.code === "AGY_REQUEST_CANCELLED");
  const deadline = Date.now() + 1500;
  while (Date.now() < deadline && events.filter((event) => event.type === "response.in_progress").length < 2) {
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  await worker.closeSession(sessionKey);
  await rejected;
  assert.ok(events.filter((event) => event.type === "response.in_progress").length >= 2);
  const count = events.length;
  await new Promise((resolve) => setTimeout(resolve, 75));
  assert.equal(events.length, count, "abort removes the pending timer");
  assert.equal(events.filter((event) => event.type === "response.completed").length, 0);
  assert.deepEqual(await readdir(requestRoot), []);
});

test("AGY stops liveness on an unacknowledged context stage without masking its error", async (t) => {
  const { spawnWorker, requestRoot } = await persistentFixture(t);
  const worker = spawnWorker({ SWITCHBOARD_AGY_KEEPALIVE_MS: "25" });
  const events = [];
  const input = [
    { type: "message", role: "developer", content: [{ type: "input_text", text: "FAIL_STAGE_ACK LIVENESS_QUIET_STAGE " + "x".repeat(180_000) }] },
    userMessage("Read the bounded fixture."),
  ];
  await assert.rejects(worker.runResponses(body(input, { tools: stableTools }),
    { sessionKey: "failed-liveness", onEvent: (event) => events.push(event) }),
    (error) => error.code === "AGY_CONTEXT_STAGE_NOT_ACKNOWLEDGED");
  assert.ok(events.filter((event) => event.type === "response.in_progress").length >= 2);
  const count = events.length;
  await new Promise((resolve) => setTimeout(resolve, 75));
  assert.equal(events.length, count, "error cleanup removes the pending timer");
  assert.equal(events.filter((event) => event.item?.type === "function_call").length, 0);
  assert.equal(events.filter((event) => event.type === "response.completed").length, 0);
  assert.deepEqual(await readdir(requestRoot), []);
});

test("AGY pending liveness does not extend the existing provider hard deadline", async (t) => {
  const { spawnWorker, requestRoot } = await persistentFixture(t);
  const worker = spawnWorker({ SWITCHBOARD_AGY_KEEPALIVE_MS: "25", SWITCHBOARD_EXTERNAL_TIMEOUT_MS: "200" });
  const events = [];
  await assert.rejects(worker.runResponses(body([userMessage("PERSISTENCE_WAIT")], { tools: stableTools }),
    { sessionKey: "hard-deadline-liveness", timeoutMs: 2000, onEvent: (event) => events.push(event) }),
    (error) => error.code === "AGY_REQUEST_TIMEOUT");
  assert.ok(events.filter((event) => event.type === "response.in_progress").length >= 2);
  const count = events.length;
  await new Promise((resolve) => setTimeout(resolve, 75));
  assert.equal(events.length, count);
  assert.equal(events.filter((event) => event.type === "response.completed").length, 0);
  assert.deepEqual(await readdir(requestRoot), []);
});

test("AGY preserves an in-flight cold bootstrap when a second request uses the same session", async (t) => {
  const { worker, promptLog } = await persistentFixture(t);
  const input = [
    { type: "message", role: "developer", content: [{ type: "input_text", text: "DELAY_STAGE_ACK " + "v".repeat(180_000) }] },
    userMessage("Return the bounded status."),
  ];
  const first = worker.runResponses(body(input, { tools: stableTools }), { sessionKey: "cold-concurrent" });
  const deadline = Date.now() + 3000;
  let staging = false;
  while (Date.now() < deadline) {
    try { staging = (await readFile(promptLog, "utf8")).startsWith("AGY_CONTEXT_STAGE_V1"); } catch {}
    if (staging) break;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  assert.ok(staging, "first request reached staging");
  await assert.rejects(worker.runResponses(body(input, { tools: stableTools }), { sessionKey: "cold-concurrent" }),
    (error) => error.code === "AGY_SESSION_BUSY" && error.status === 409);
  const result = await first;
  assert.match(messageText(result), /bootstrap=complete/);
  assert.equal(result.tool_projection.agy_bootstrap_parts, 2);
  await worker.closeSession("cold-concurrent");
});

test("AGY admits 24 full-history tool steps without re-budgeting its verified prefix", async (t) => {
  const { worker, promptLog } = await persistentFixture(t);
  const input = [userMessage("Inspect the bounded fixture.")];
  const first = await worker.runResponses(body(input, { tools: stableTools }), { sessionKey: "budget-loop" });
  const pid = messageText(first).split(";")[0];
  for (let index = 1; index <= 24; index += 1) {
    const marker = `EXACT_NEW_RESULT_${index}:`;
    input.push(...toolExchange(index, marker + "x".repeat(16 * 1024)));
    const next = await worker.runResponses(body(input, { tools: stableTools }), { sessionKey: "budget-loop" });
    assert.equal(messageText(next).split(";")[0], pid);
    assert.equal(next.tool_projection.agy_session_reused, true);
    const prompt = await readFile(promptLog, "utf8");
    assert.ok(Buffer.byteLength(prompt) <= 160 * 1024);
    assert.ok(prompt.includes(marker + "x".repeat(16 * 1024)), "new evidence is exact");
    if (index > 1) assert.ok(!prompt.includes(`EXACT_NEW_RESULT_${index - 1}:`));
  }
  assert.ok(Buffer.byteLength(JSON.stringify(input)) > 256 * 1024);
  await worker.closeSession("budget-loop");
});

test("AGY cold recovery reassembles authority and 18 accepted tool results after cancellation", async (t) => {
  const { worker, promptLog } = await persistentFixture(t);
  const sessionKey = "cancelled-long-tool-chain";
  const authority = "retain exact recovery authority: " + "規則甲乙".repeat(16_000);
  const input = [
    { type: "message", role: "developer", content: [{ type: "input_text", text: authority }] },
    userMessage("Inspect the exact recovery fixture."),
  ];
  await worker.runResponses(body(input, { tools: stableTools }), { sessionKey });
  const evidence = [];
  for (let index = 0; index < 18; index += 1) {
    const output = `RECOVERY_EVIDENCE_${index}:` + "x".repeat(26 * 1024);
    evidence.push(output);
    input.push(...toolExchange(`recovery_${index}`, output));
    const accepted = await worker.runResponses(body(input, { tools: stableTools }), { sessionKey });
    assert.equal(accepted.tool_projection.agy_prefix_reused, true);
  }
  assert.ok(Buffer.byteLength(JSON.stringify(input)) > 655_313);
  const pending = worker.runResponses(body([...input, ...toolExchange("cancel", "PERSISTENCE_WAIT")],
    { tools: stableTools }), { sessionKey });
  const cancelled = assert.rejects(pending, (error) => error.code === "AGY_REQUEST_CANCELLED");
  const deadline = Date.now() + 3000;
  let waiting = false;
  while (Date.now() < deadline) {
    waiting = (await readFile(promptLog, "utf8")).includes("PERSISTENCE_WAIT");
    if (waiting) break;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  assert.ok(waiting, "the cancelled request reached the exact provider session");
  await worker.closeSession(sessionKey);
  await cancelled;

  const recovered = await worker.runResponses(body(input, { tools: stableTools }), { sessionKey });
  assert.equal(recovered.status, 200);
  assert.ok(recovered.tool_projection.agy_bootstrap_parts >= 5);
  assert.equal(recovered.tool_projection.agy_session_reused, false);
  assert.equal(recovered.tool_projection.agy_prefix_reused, false);
  const assembled = await readFile(promptLog + ".bootstrap", "utf8");
  assert.ok(assembled.includes(authority));
  for (const output of evidence) assert.ok(assembled.includes(output), "every active tool result is exact");
  assert.ok(recovered.tool_projection.diagnostic.includes(`cold_context_bytes=${Buffer.byteLength(assembled)}`));
  assert.ok(recovered.tool_projection.diagnostic.includes(`cold_context_parts=${recovered.tool_projection.agy_bootstrap_parts}`));

  input.push(...toolExchange("after_recovery", "EXACT_POST_RECOVERY_RESULT"));
  const next = await worker.runResponses(body(input, { tools: stableTools }), { sessionKey });
  assert.equal(next.tool_projection.agy_prefix_reused, true);
  assert.equal(next.tool_projection.agy_session_reused, true);
  const suffix = await readFile(promptLog, "utf8");
  assert.match(suffix, /EXACT_POST_RECOVERY_RESULT/);
  assert.doesNotMatch(suffix, /RECOVERY_EVIDENCE_0|規則甲乙/);
  await worker.closeSession(sessionKey);
});

test("AGY cold recovery rejects context beyond its model token budget before staging", async (t) => {
  const { worker, promptLog } = await persistentFixture(t);
  const input = [userMessage("Inspect the complete model-budget fixture."),
    ...toolExchange("token_ceiling", "!".repeat(900_000))];
  await assert.rejects(worker.runResponses(body(input, { tools: stableTools }), { sessionKey: "cold-token-ceiling" }),
    (error) => error.code === "EXTERNAL_CONTEXT_TOO_LARGE" && error.status === 413);
  await assert.rejects(stat(promptLog), (error) => error.code === "ENOENT");
  await worker.closeSession("cold-token-ceiling");
});

test("AGY rejects oversized new evidence with IPC diagnostics and preserves the accepted session", async (t) => {
  const { worker, promptLog } = await persistentFixture(t);
  const input = [userMessage("Inspect the exact fixture.")];
  const first = await worker.runResponses(body(input, { tools: stableTools }), { sessionKey: "budget-refusal" });
  const originalPrompt = await readFile(promptLog, "utf8");
  await assert.rejects(
    worker.runResponses(body([...input, ...toolExchange("large", "x".repeat(300 * 1024))],
      { tools: stableTools }), { sessionKey: "budget-refusal" }),
    (error) => error.code === "EXTERNAL_MANDATORY_CONTEXT_TOO_LARGE"
      && error.status === 413
      && error.details?.max_bytes <= 160 * 1024
      && error.details?.mandatory_bytes > error.details.max_bytes
      && /^[a-f0-9]{64}$/.test(error.details?.current_request_sha256 || ""),
  );
  assert.equal(await readFile(promptLog, "utf8"), originalPrompt, "refused data never reaches the provider");
  const next = await worker.runResponses(body([...input, userMessage("Return the fixture status.")],
    { tools: stableTools }), { sessionKey: "budget-refusal" });
  assert.equal(messageText(next).split(";")[0], messageText(first).split(";")[0]);
  assert.equal(next.tool_projection.agy_session_reused, true);
  await worker.closeSession("budget-refusal");
});

test("AGY does not silently truncate an oversized multibyte current request", async (t) => {
  const { worker, promptLog } = await persistentFixture(t);
  await assert.rejects(
    worker.runResponses(body([userMessage("字".repeat(70_000))]), { sessionKey: "unicode-budget" }),
    (error) => error.code === "EXTERNAL_MANDATORY_CONTEXT_TOO_LARGE" && error.status === 413,
  );
  await assert.rejects(stat(promptLog), (error) => error.code === "ENOENT");
  await worker.closeSession("unicode-budget");
});

test("AGY still validates new tool pairs and sensitive data on a proven continuation", async (t) => {
  const { worker, promptLog } = await persistentFixture(t);
  const input = [userMessage("Inspect a public fixture.")];
  const first = await worker.runResponses(body(input), { sessionKey: "safe-delta" });
  const originalPrompt = await readFile(promptLog, "utf8");
  await assert.rejects(
    worker.runResponses(body([...input, { type: "function_call_output", call_id: "missing", output: "value" }]),
      { sessionKey: "safe-delta" }),
    (error) => error.code === "EXTERNAL_ACTIVE_TOOL_PAIR_INCOMPLETE" && error.status === 409,
  );
  await assert.rejects(
    worker.runResponses(body([...input, userMessage("api_key=abcdefghijklmnop123456")]), { sessionKey: "safe-delta" }),
    (error) => error.code === "EXTERNAL_SENSITIVE_CONTEXT_DETECTED" && error.status === 422,
  );
  assert.equal(await readFile(promptLog, "utf8"), originalPrompt);
  const next = await worker.runResponses(body([...input, userMessage("Return a public status.")]),
    { sessionKey: "safe-delta" });
  assert.equal(messageText(next).split(";")[0], messageText(first).split(";")[0]);
  await worker.closeSession("safe-delta");
});

test("AGY reconstructs drifted history exactly without exempting an unreceived prefix", async (t) => {
  const { worker, promptLog } = await persistentFixture(t);
  const first = await worker.runResponses(body([userMessage("Original fixture.")]), { sessionKey: "changed-prefix" });
  assert.ok(messageText(first));
  const evidence = "EXACT_CHANGED_PREFIX_EVIDENCE:" + "x".repeat(300 * 1024);
  const rebuilt = await worker.runResponses(body([
    userMessage("Changed fixture."), ...toolExchange("drift", evidence),
  ], { tools: stableTools }), { sessionKey: "changed-prefix" });
  assert.equal(rebuilt.tool_projection.agy_session_reused, false);
  assert.equal(rebuilt.tool_projection.agy_prefix_reused, false);
  assert.ok(rebuilt.tool_projection.agy_bootstrap_parts >= 2);
  const assembled = await readFile(promptLog + ".bootstrap", "utf8");
  assert.ok(assembled.includes("Changed fixture."));
  assert.ok(assembled.includes(evidence));
  assert.doesNotMatch(assembled, /Original fixture/);
  await worker.closeSession("changed-prefix");
});

test("AGY includes fixed tool-schema overhead in the final UTF-8 byte ceiling", async (t) => {
  const { worker, promptLog } = await persistentFixture(t);
  await assert.rejects(
    worker.runResponses(body([userMessage("Inspect the fixture.")], {
      tools: [{ ...stableTools[0], description: "x".repeat(180 * 1024) }],
    }), { sessionKey: "fixed-budget" }),
    (error) => error.code === "AGY_PROMPT_BYTE_LIMIT" && error.status === 413
      && error.details?.prompt_bytes > error.details.max_bytes,
  );
  await assert.rejects(stat(promptLog), (error) => error.code === "ENOENT");
  await worker.closeSession("fixed-budget");
});

test("external validated-prefix admission is explicit and retains exact new evidence", () => {
  const prefix = [userMessage("Inspect this fixture."), ...toolExchange("old", "x".repeat(4096))];
  const input = [...prefix, ...toolExchange("new", "EXACT_NEW_EVIDENCE")];
  assert.throws(() => boundExternalHistory(input, 2048, { validatedPrefixLength: prefix.length }),
    (error) => error.code === "EXTERNAL_CONTINUATION_PREFIX_INVALID");
  const accepted = boundExternalHistory(input, 2048, {
    validatedPrefixLength: prefix.length, allowProviderContinuation: true, includeOptionalHistory: false,
  });
  assert.ok(Buffer.byteLength(JSON.stringify(accepted)) <= 2048);
  assert.match(JSON.stringify(accepted), /EXACT_NEW_EVIDENCE/);
  assert.doesNotMatch(JSON.stringify(accepted), /x{4096}/);
  assert.throws(() => boundExternalHistory(input, 2048, {
    validatedPrefixLength: input.length + 1, allowProviderContinuation: true,
  }), (error) => error.code === "EXTERNAL_CONTINUATION_PREFIX_INVALID");
});

test("AGY does not charge the already received exact request against each tool-result delta", async (t) => {
  const { worker, promptLog } = await persistentFixture(t);
  const input = [userMessage("Inspect the exact specification. " + "x".repeat(96 * 1024))];
  const first = await worker.runResponses(body(input, { tools: stableTools }), { sessionKey: "large-received-request" });
  const evidence = "EXACT_LARGE_RESULT:" + "y".repeat(100 * 1024);
  const next = await worker.runResponses(body([...input, ...toolExchange("large-spec", evidence)],
    { tools: stableTools }), { sessionKey: "large-received-request" });
  assert.equal(messageText(next).split(";")[0], messageText(first).split(";")[0]);
  const prompt = await readFile(promptLog, "utf8");
  assert.ok(prompt.includes(evidence));
  assert.doesNotMatch(prompt, /Inspect the exact specification/);
  assert.ok(Buffer.byteLength(prompt) <= 160 * 1024);
  await worker.closeSession("large-received-request");
});

test("AGY linear deltas retain pairing, sensitive-input admission and the exact pending call", async (t) => {
  const { worker, promptLog } = await persistentFixture(t);
  const first = await worker.runResponses(body([userMessage("PERSISTENCE_RAW_TOOL_CALL")],
    { tools: stableTools }), { sessionKey: "linear-admission" });
  const call = first.events.find((event) => event.type === "response.output_item.done"
    && event.item?.type === "function_call")?.item;
  assert.ok(call?.call_id);
  const originalPrompt = await readFile(promptLog, "utf8");
  await assert.rejects(worker.runResponses(body([
    { type: "function_call_output", call_id: "not_the_pending_call", output: "value" },
  ], { tools: stableTools, agy_linear_extension: true }), { sessionKey: "linear-admission" }),
  (error) => error.code === "EXTERNAL_ACTIVE_TOOL_PAIR_INCOMPLETE");
  assert.equal(await readFile(promptLog, "utf8"), originalPrompt);
  const next = await worker.runResponses(body([
    { type: "function_call_output", call_id: call.call_id, output: "EXACT_LINEAR_RESULT" },
  ], { tools: stableTools, agy_linear_extension: true }), { sessionKey: "linear-admission" });
  assert.equal(next.tool_projection.agy_session_reused, true);
  const acceptedPrompt = await readFile(promptLog, "utf8");
  assert.match(acceptedPrompt, /EXACT_LINEAR_RESULT/);
  await assert.rejects(worker.runResponses(body([userMessage("api_key=abcdefghijklmnop123456")],
    { tools: stableTools, agy_linear_extension: true }), { sessionKey: "linear-admission" }),
  (error) => error.code === "EXTERNAL_SENSITIVE_CONTEXT_DETECTED");
  assert.equal(await readFile(promptLog, "utf8"), acceptedPrompt);
  await worker.closeSession("linear-admission");
});

test("AGY linear tool admission survives a verified persistent-worker restore", async (t) => {
  const { worker, spawnWorker, promptLog } = await persistentFixture(t);
  const first = await worker.runResponses(body([userMessage("PERSISTENCE_RAW_TOOL_CALL")],
    { tools: stableTools }), { sessionKey: "thread-linear-restore" });
  const call = first.events.find((event) => event.type === "response.output_item.done"
    && event.item?.type === "function_call")?.item;
  assert.ok(call?.call_id);
  worker.close();
  const restored = spawnWorker();
  const next = await restored.runResponses(body([
    { type: "function_call_output", call_id: call.call_id, output: "RESTORED_EXACT_RESULT" },
  ], { tools: stableTools, agy_linear_extension: true }), { sessionKey: "thread-linear-restore" });
  assert.equal(next.tool_projection.agy_session_reused, true);
  assert.match(await readFile(promptLog, "utf8"), /RESTORED_EXACT_RESULT/);
  await restored.closeSession("thread-linear-restore");
});

