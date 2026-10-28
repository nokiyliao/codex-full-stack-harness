import assert from "node:assert/strict";
import { chmod, mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { IsolatedWorkerClient } from "../src/worker-client.mjs";

test("AGY worker deletes its request HOME after a success", async (t) => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "codex-agy-worker-test-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const requestRoot = path.join(fixture, "requests");
  const token = path.join(fixture, "oauth-token");
  const config = path.join(fixture, "config.json");
  const mcp = path.join(fixture, "mcp.json");
  const settings = path.join(fixture, "settings.json");
  const executable = path.join(fixture, "fake-agy.mjs");
  const seed = path.join(fixture, "seed");
  const projects = path.join(fixture, "projects");
  const protocolOutput = path.join(fixture, "protocol-output.json");
  await mkdir(requestRoot, { mode: 0o700 });
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
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
const args = process.argv.slice(2);
if (process.env.SSH_AUTH_SOCK) process.exit(70);
const keychain = execFileSync("/usr/bin/security", ["default-keychain", "-d", "user"], { encoding: "utf8" });
if (!keychain.includes("login.keychain-db")) process.exit(73);
const logIndex = args.indexOf("--log-file");
if (logIndex < 0 || args[logIndex + 1] !== "/dev/null") process.exit(71);
const promptIndex = args.indexOf("-p");
if (promptIndex >= 0) process.exit(78);
let prompt = "";
for await (const chunk of process.stdin) prompt += chunk;
if (!prompt) process.exit(79);
const schemaIndex = args.indexOf("--json-schema");
const schema = schemaIndex >= 0 ? JSON.parse(args[schemaIndex + 1]) : {};
const effortIndex = args.indexOf("--effort");
if (effortIndex < 0 || args[effortIndex + 1] !== "high") process.exit(83);
if (prompt.includes("ONESHOT_PROTOCOL_OUTPUT_FIXTURE")) {
  process.stdout.write(JSON.stringify({
    status: "SUCCESS", usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 },
    ...JSON.parse(readFileSync(${JSON.stringify(protocolOutput)}, "utf8")),
  }));
  process.exit(0);
}
if (prompt.includes("EXPECT_BOUNDED_HISTORY")
    && (Buffer.byteLength(prompt) > 256 * 1024
      || !prompt.includes("external_context_omission_v1")
      || !prompt.includes("LATEST_HISTORY_SENTINEL"))) {
  process.exit(74);
}
if (prompt.includes("WAIT_FOR_CANCEL")) {
  await new Promise((resolve) => setTimeout(resolve, 60_000));
  process.exit(72);
}
if (prompt.includes("STATELESS_FAST_PATH_REQUEST")) {
  if (!prompt.includes("CURRENT REQUEST:")
      || prompt.includes("STATELESS_OLD_HISTORY_SENTINEL")
      || prompt.includes("STATELESS_GUIDANCE_SENTINEL")
      || prompt.includes("AVAILABLE ACTIONS:")
      || schemaIndex >= 0) {
    process.exit(81);
  }
  process.stdout.write(JSON.stringify({
    status: "SUCCESS",
    response: JSON.stringify({ kind: "message", text: "fast path ok" }),
    structured_output: { kind: "message", text: "fast path ok" },
    usage: { input_tokens: 12, output_tokens: 3, total_tokens: 15 }
  }));
  process.exit(0);
}
if (prompt.includes("CURRENT_TURN_VISION_FAST_PATH")) {
  const addDirIndex = args.indexOf("--add-dir");
  if (addDirIndex < 0
      || !args[addDirIndex + 1]
      || !prompt.includes("stateless current-turn multimodal message")
      || !prompt.includes("[image attachment:")
      || !prompt.includes("Inspect every listed image attachment with read_file")
      || prompt.includes("AVAILABLE ACTIONS:")
      || schemaIndex >= 0) {
    process.exit(84);
  }
  process.stdout.write(JSON.stringify({
    status: "SUCCESS",
    response: "vision fast path ok",
    usage: { input_tokens: 15, output_tokens: 4, total_tokens: 19 }
  }));
  process.exit(0);
}
if (prompt.includes("IDENTITY_METADATA_REQUEST")) {
  const identity = [
    '"catalog_model":"fake"',
    '"provider":"agy"',
    '"upstream_model":"fake"',
    '"requested_reasoning_effort":"high"',
    '"host_harness":"Codex"',
  ];
  if (!prompt.includes("AUTHORITATIVE ROUTE IDENTITY")
      || !prompt.includes("Never replace the values with a claim that reporting is complete")
      || !prompt.includes("Codex is the host harness, not the responding model identity")
      || (prompt.includes("AVAILABLE ACTIONS:") ? schemaIndex < 0 : schemaIndex >= 0)
      || identity.some((field) => !prompt.includes(field))) {
    process.exit(82);
  }
  process.stdout.write(JSON.stringify({
    status: "SUCCESS",
    response: JSON.stringify({ kind: "message", text: "identity metadata ok" }),
    structured_output: { kind: "message", text: "identity metadata ok" },
    usage: { input_tokens: 12, output_tokens: 3, total_tokens: 15 }
  }));
  process.exit(0);
}
if (prompt.includes("STREAM_EVENTS")) {
  process.stdout.write(JSON.stringify({ event: "init", conversation_id: "stream-test" }) + "\\n");
  process.stdout.write(JSON.stringify({
    event: "step_update",
    step_update: { step_index: 1, state: "DONE", step_type: "agent_response", text_delta: "stream visible" }
  }) + "\\n");
  process.stdout.write(JSON.stringify({
    event: "step_update",
    step_update: { step_index: 2, state: "DONE", step_type: "agent_response", text_delta: JSON.stringify({ kind: "message", text: "stream visible" }) }
  }) + "\\n");
  process.stdout.write(JSON.stringify({
    event: "result",
    result: {
      status: "SUCCESS",
      response: "stream visible\\n" + JSON.stringify({ kind: "message", text: "stream visible" }),
      structured_output: { kind: "message", text: "stream visible" },
      usage: { input_tokens: 1, output_tokens: 2, total_tokens: 3 }
    }
  }) + "\\n");
  process.exit(0);
}
if (prompt.includes("PLAIN_SUCCESS")) {
  process.stdout.write(JSON.stringify({
    status: "SUCCESS",
    response: "plain fallback ok",
    usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 }
  }));
  process.exit(0);
}
if (prompt.includes("STRUCTURED_MESSAGE_WINS")) {
  process.stdout.write(JSON.stringify({
    status: "SUCCESS",
    response: JSON.stringify({
      kind: "message",
      text: "canonical structured message",
      tool_name: "__none__",
      arguments: {},
      input: "",
      toolAction: "provider-only metadata"
    }),
    structured_output: {
      kind: "message",
      text: "canonical structured message",
      tool_name: "__none__",
      arguments: {},
      input: ""
    },
    usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 }
  }));
  process.exit(0);
}
if (prompt.includes("REQUIRED_TOOL_MISSING")) {
  process.stdout.write(JSON.stringify({
    status: "SUCCESS",
    response: "simulated action narration",
    usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 }
  }));
  process.exit(0);
}
if (prompt.includes("INVALID_MESSAGE_ONLY_TOOL")) {
  process.stdout.write(JSON.stringify({
    status: "SUCCESS",
    response: "must not escape as a tool call",
    structured_output: {
      kind: "tool_call",
      tool_name: "forbidden_tool",
      arguments: {},
      input: ""
    },
    usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 }
  }));
  process.exit(0);
}
if (prompt.includes("VALID_TOOL_JSON_FALLBACK")) {
  const required = schema?.required || [];
  const toolNames = schema?.properties?.tool_name?.enum || [];
  const toolName = toolNames.find((name) => name.includes("write_artifact"));
  // A compact tool call without the auto contract's text and input fields.
  if (!toolName || !required.includes("text") || !required.includes("input")) process.exit(77);
  process.stdout.write(JSON.stringify({
    status: "SUCCESS",
    response: JSON.stringify({ kind: "tool_call", tool_name: toolName, arguments: { path: "fallback.txt" } }),
    usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 }
  }));
  process.exit(0);
}
if (prompt.includes("PRIORITY_ACTION_TOOLS")) {
  const kinds = schema?.properties?.kind?.enum || [];
  const toolNames = schema?.properties?.tool_name?.enum || [];
  const execName = toolNames.find((name) => name.includes("exec_command"));
  const selectedName = execName || toolNames.find((name) => name !== "__none__");
  if (!kinds.includes("tool_call") || !selectedName) process.exit(76);
  process.stdout.write(JSON.stringify({
    status: "SUCCESS",
    response: JSON.stringify({ kind: "tool_call", text: "", tool_name: selectedName, arguments: { cmd: "cat artifact.txt" }, input: "" }),
    structured_output: { kind: "tool_call", text: "", tool_name: selectedName, arguments: { cmd: "cat artifact.txt" }, input: "" },
    usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 }
  }));
  process.exit(0);
}
if (prompt.includes("GOAL_ACTION_POLICY") || prompt.includes("REQUIRED_ACTION_POLICY")
    || prompt.includes("CALL_ACTION_POLICY")
    || prompt.includes("DIRECT_ACTION_POLICY") || prompt.includes("DISCOVERY_IS_NOT_ACTION")
    || prompt.includes("VERIFY_AFTER_WRITE")) {
  const kinds = schema?.properties?.kind?.enum || [];
  const toolNames = schema?.properties?.tool_name?.enum || [];
  const toolName = toolNames.find((name) => name.includes("write_artifact"))
    || toolNames.find((name) => name !== "__none__");
  // Only the caller's tool_choice "required" forces a call; action wording in
  // the user message leaves the model the auto choice.
  const forced = kinds.length === 1 && kinds[0] === "tool_call";
  const callerRequired = prompt.includes("REQUIRED_ACTION_POLICY");
  if (!kinds.includes("tool_call") || !toolName || forced !== callerRequired
      || prompt.includes("This inference requires one supplied action") !== callerRequired) {
    process.exit(75);
  }
  process.stdout.write(JSON.stringify({
    status: "SUCCESS",
    response: JSON.stringify({ kind: "tool_call", text: "", tool_name: toolName, arguments: { path: "artifact.txt" }, input: "" }),
    structured_output: { kind: "tool_call", text: "", tool_name: toolName, arguments: { path: "artifact.txt" }, input: "" },
    usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 }
  }));
  process.exit(0);
}
const dbDir = path.join(process.env.HOME, ".gemini", "antigravity-cli", "conversations");
mkdirSync(dbDir, { recursive: true });
writeFileSync(path.join(dbDir, "conversation.db"), "prompt-persistence-canary");
process.stdout.write(JSON.stringify({
  status: "SUCCESS",
  response: "ephemeral ok",
  structured_output: { kind: "message", text: "ephemeral ok" },
  usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 }
}));
`, { mode: 0o700 }),
  ]);
  await chmod(executable, 0o700);
  const worker = new IsolatedWorkerClient({
    script: process.env.SWITCHBOARD_AGY_TEST_SCRIPT || new URL("../src/agy-worker.mjs", import.meta.url).pathname,
    label: "AGY-test",
    env: {
      SWITCHBOARD_AGY_PATH: executable,
      SWITCHBOARD_AGY_TOKEN_PATH: token,
      SWITCHBOARD_AGY_CONFIG_PATH: config,
      SWITCHBOARD_AGY_MCP_CONFIG_PATH: mcp,
      SWITCHBOARD_AGY_SETTINGS_PATH: settings,
      SWITCHBOARD_AGY_REQUEST_ROOT: requestRoot,
      SWITCHBOARD_AGY_SEED_DIR: seed,
      SWITCHBOARD_AGY_PROJECTS_DIR: projects,
      SWITCHBOARD_AGY_PERSISTENT_SESSIONS: "0",
      SWITCHBOARD_TEST_ALLOW_CRASH: "1",
    },
  });
  t.after(() => worker.close());
  const result = await worker.runResponses({
    model: "fake",
    instructions: "prompt-persistence-canary",
    input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "hello" }] }],
    tools: [],
  });
  assert.equal(result.events.at(-1).type, "response.completed");
  assert.deepEqual(await readdir(requestRoot), []);

  const artifactTool = {
    type: "function",
    name: "write_artifact",
    description: "Write a requested artifact to the workspace.",
    parameters: {
      type: "object",
      properties: { path: { type: "string" } },
      required: ["path"],
      additionalProperties: false,
    },
  };
  const fastPath = await worker.runResponses({
    model: "fake",
    instructions: "STATELESS_GUIDANCE_SENTINEL ".repeat(1_000),
    input: [
      {
        type: "message",
        role: "user",
        content: [{ type: "input_text", text: "<environment_context>STATELESS_OLD_HISTORY_SENTINEL</environment_context>" }],
      },
      {
        type: "message",
        role: "user",
        content: [{ type: "input_text", text: "STATELESS_FAST_PATH_REQUEST: Name your model and reasoning level." }],
      },
    ],
    tools: [artifactTool],
  });
  const fastPathMessage = fastPath.events.find(
    (event) => event.type === "response.output_item.done" && event.item?.type === "message",
  );
  assert.equal(fastPathMessage.item.content[0].text, "fast path ok");
  assert.deepEqual(fastPath.tool_projection, {
    diagnostic: "agy_fast_path:eligible",
    agy_fast_path: true,
    agy_fast_path_reason: "eligible",
    agy_message_count: 2,
    agy_tool_choice_kind: "unset",
  });
  assert.deepEqual(await readdir(requestRoot), []);

  const currentTurnVision = await worker.runResponses({
    model: "fake",
    reasoning: { effort: "high" },
    input: [{
      type: "message",
      role: "user",
      content: [
        { type: "input_text", text: "CURRENT_TURN_VISION_FAST_PATH: Analyze this screenshot." },
        { type: "input_image", image_url: "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZlXcAAAAASUVORK5CYII=" },
      ],
    }],
    tools: [artifactTool],
  });
  const currentTurnVisionMessage = currentTurnVision.events.find(
    (event) => event.type === "response.output_item.done" && event.item?.type === "message",
  );
  assert.equal(currentTurnVisionMessage.item.content[0].text, "vision fast path ok");
  assert.equal(currentTurnVision.tool_projection.agy_fast_path, true);
  assert.equal(currentTurnVision.tool_projection.agy_fast_path_reason, "current_turn_vision");
  assert.deepEqual(await readdir(requestRoot), []);

  await assert.rejects(
    worker.runResponses({
      model: "fake",
      reasoning: { effort: "medium" },
      input: [{
        type: "message",
        role: "user",
        content: [{ type: "input_text", text: "Mismatched effort must fail." }],
      }],
      tools: [],
    }),
    (error) => error.code === "AGY_REASONING_EFFORT_MISMATCH",
  );
  assert.deepEqual(await readdir(requestRoot), []);

  const identity = await worker.runResponses({
    model: "fake",
    reasoning: { effort: "high" },
    input: [{
      type: "message",
      role: "user",
      content: [{ type: "input_text", text: "IDENTITY_METADATA_REQUEST: Name your model and reasoning level." }],
    }],
    tools: [],
  });
  const identityMessage = identity.events.find(
    (event) => event.type === "response.output_item.done" && event.item?.type === "message",
  );
  assert.equal(identityMessage.item.content[0].text, "identity metadata ok");
  assert.equal(identity.tool_projection.agy_fast_path, true);
  assert.deepEqual(await readdir(requestRoot), []);

  const contextualIdentity = await worker.runResponses({
    model: "fake",
    reasoning: { effort: "high" },
    input: [{
      type: "message",
      role: "user",
      content: [{ type: "input_text", text: "IDENTITY_METADATA_REQUEST: Name your current model and reasoning level." }],
    }],
    tools: [],
  });
  const contextualIdentityMessage = contextualIdentity.events.find(
    (event) => event.type === "response.output_item.done" && event.item?.type === "message",
  );
  assert.equal(contextualIdentityMessage.item.content[0].text, "identity metadata ok");
  assert.equal(contextualIdentity.tool_projection.agy_fast_path, false);
  assert.equal(contextualIdentity.tool_projection.agy_fast_path_reason, "context_dependent_request");
  assert.deepEqual(await readdir(requestRoot), []);

  const goalAction = await worker.runResponses({
    model: "fake",
    instructions: "GOAL_ACTION_POLICY",
    input: [{
      type: "message",
      role: "user",
      content: [{ type: "input_text", text: '<codex_internal_context source="goal">create a local artifact</codex_internal_context>' }],
    }],
    tools: [artifactTool],
  });
  const goalCall = goalAction.events.find((event) => event.type === "response.output_item.done" && event.item?.type === "function_call");
  assert.equal(goalCall?.item?.name, "write_artifact");
  assert.deepEqual(JSON.parse(goalCall.item.arguments), { path: "artifact.txt" });

  const requiredAction = await worker.runResponses({
    model: "fake",
    instructions: "REQUIRED_ACTION_POLICY",
    tool_choice: "required",
    input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "create an artifact" }] }],
    tools: [artifactTool],
  });
  const requiredCall = requiredAction.events.find((event) => event.type === "response.output_item.done" && event.item?.type === "function_call");
  assert.equal(requiredCall?.item?.name, "write_artifact");

  const directAction = await worker.runResponses({
    model: "fake",
    instructions: "DIRECT_ACTION_POLICY",
    input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "Create a local artifact." }] }],
    tools: [artifactTool],
  });
  const directCall = directAction.events.find((event) => event.type === "response.output_item.done" && event.item?.type === "function_call");
  assert.equal(directCall?.item?.name, "write_artifact");

  const calledAction = await worker.runResponses({
    model: "fake",
    instructions: "CALL_ACTION_POLICY",
    input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "Immediately call write_artifact once." }] }],
    tools: [artifactTool],
  });
  const calledTool = calledAction.events.find((event) => event.item?.type === "function_call");
  assert.equal(calledTool?.item?.name, "write_artifact");

  // Without a supported tool the turn is answered as a message, not refused.
  const unsupportedAction = await worker.runResponses({
    model: "fake",
    instructions: "UNSUPPORTED_ACTION_POLICY",
    input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "Create a local artifact." }] }],
    tools: [{ type: "local_shell" }],
  });
  assert.equal(unsupportedAction.events.some((event) => event.item?.type === "function_call"), false);
  assert.equal(unsupportedAction.events.at(-1).type, "response.completed");

  await assert.rejects(worker.runResponses({
    model: "fake",
    instructions: "REQUIRED_TOOL_MISSING",
    tool_choice: "required",
    input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "Create a local artifact." }] }],
    tools: [artifactTool],
  }), (error) => error.code === "AGY_REQUIRED_TOOL_CALL_MISSING" && error.status === 502);

  // Action wording alone does not require a call: a message is a valid answer.
  const narrated = await worker.runResponses({
    model: "fake",
    instructions: "REQUIRED_TOOL_MISSING without caller tool_choice",
    input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "What does a unit test usually check? Explain conceptually." }] }],
    tools: [artifactTool],
  });
  const narratedMessage = narrated.events.find((event) => event.type === "response.output_item.done" && event.item?.type === "message");
  assert.equal(narratedMessage?.item?.content[0].text, "simulated action narration");

  const recoveredJson = await worker.runResponses({
    model: "fake",
    instructions: "VALID_TOOL_JSON_FALLBACK",
    input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "Create a local artifact." }] }],
    tools: [artifactTool],
  });
  const recoveredCall = recoveredJson.events.find((event) => event.item?.type === "function_call");
  assert.equal(recoveredCall?.item?.name, "write_artifact");
  assert.deepEqual(JSON.parse(recoveredCall.item.arguments), { path: "fallback.txt" });

  const prioritizedAction = await worker.runResponses({
    model: "fake",
    instructions: "PRIORITY_ACTION_TOOLS",
    input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "Read the artifact and verify its bytes." }] }],
    tools: [
      {
        type: "function",
        name: "update_plan",
        description: "Update task planning state.",
        parameters: { type: "object", properties: {}, additionalProperties: true },
      },
      {
        type: "function",
        name: "exec_command",
        description: "Run a shell command in the workspace.",
        parameters: {
          type: "object",
          properties: { cmd: { type: "string" } },
          required: ["cmd"],
          additionalProperties: false,
        },
      },
      { type: "tool_search", description: "Discover more tools." },
    ],
  });
  const prioritizedCall = prioritizedAction.events.find((event) => event.type === "response.output_item.done" && event.item?.type === "function_call");
  assert.equal(prioritizedCall?.item?.name, "exec_command");

  const afterDiscovery = await worker.runResponses({
    model: "fake",
    instructions: "DISCOVERY_IS_NOT_ACTION",
    input: [
      { type: "message", role: "user", content: [{ type: "input_text", text: "Create a local artifact." }] },
      { type: "tool_search_call", call_id: "search-1", arguments: { query: "file write" } },
      { type: "tool_search_output", call_id: "search-1", tools: [artifactTool] },
    ],
    tools: [{ type: "tool_search", description: "Discover tools." }],
  });
  const discoveredCall = afterDiscovery.events.find((event) => event.type === "response.output_item.done" && event.item?.type === "function_call");
  assert.equal(discoveredCall?.item?.name, "write_artifact");

  const verifyAfterWrite = await worker.runResponses({
    model: "fake",
    instructions: "VERIFY_AFTER_WRITE",
    input: [
      { type: "message", role: "user", content: [{ type: "input_text", text: "Create the artifact, then read it back and verify it." }] },
      { type: "function_call", call_id: "write-1", name: "write_artifact", arguments: "{\"path\":\"artifact.txt\"}" },
      { type: "function_call_output", call_id: "write-1", output: "written" },
    ],
    tools: [artifactTool],
  });
  const verificationCall = verifyAfterWrite.events.find((event) => event.type === "response.output_item.done" && event.item?.type === "function_call");
  assert.equal(verificationCall?.item?.name, "write_artifact");
  assert.equal(await readFile(token, "utf8"), "fake-token");

  const plain = await worker.runResponses({
    model: "fake",
    instructions: "PLAIN_SUCCESS",
    input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "PLAIN_SUCCESS hello" }] }],
    tools: [],
  });
  const plainMessage = plain.events.find((event) => event.type === "response.output_item.done" && event.item?.type === "message");
  assert.equal(plainMessage.item.content[0].text, "plain fallback ok");
  assert.equal(plain.events.at(-1).type, "response.completed");
  assert.deepEqual(await readdir(requestRoot), []);

  const structuredMessage = await worker.runResponses({
    model: "fake",
    instructions: "STRUCTURED_MESSAGE_WINS",
    input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "STRUCTURED_MESSAGE_WINS hello" }] }],
    tools: [],
  });
  const structuredMessageItem = structuredMessage.events.find(
    (event) => event.type === "response.output_item.done" && event.item?.type === "message",
  );
  assert.equal(structuredMessageItem.item.content[0].text, "canonical structured message");

  const protocolCall = JSON.stringify({ kind: "tool_call", tool_name: "write_artifact", arguments: { path: "never-written.txt" } });
  for (const output of [
    { response: `${protocolCall}\n${protocolCall}` },
    { response: `${protocolCall}\n<USER_REQUEST>\nEchoed request\n</USER_REQUEST>\n${protocolCall}` },
    { response: protocolCall, structured_output: { kind: "message", text: protocolCall } },
  ]) {
    await writeFile(protocolOutput, JSON.stringify(output));
    const events = [];
    await assert.rejects(worker.runResponses({
      model: "fake", instructions: "ONESHOT_PROTOCOL_OUTPUT_FIXTURE",
      input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "Inspect the existing artifact." }] }],
      tools: [artifactTool],
    }, { onEvent: (event) => events.push(event) }),
    (error) => error.code === "AGY_INVALID_STRUCTURED_OUTPUT");
    assert.equal(events.some((event) => event.type === "response.completed" || event.item?.type === "function_call" || event.item?.type === "message"), false);
    assert.deepEqual(await readdir(requestRoot), []);
  }

  await assert.rejects(worker.runResponses({
    model: "fake",
    instructions: "INVALID_MESSAGE_ONLY_TOOL",
    input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "INVALID_MESSAGE_ONLY_TOOL" }] }],
    tools: [],
    tool_choice: "none",
  }), (error) => error.code === "AGY_INVALID_STRUCTURED_OUTPUT");
  assert.deepEqual(await readdir(requestRoot), []);

  const streamed = await worker.runResponses({
    model: "fake",
    instructions: "STREAM_EVENTS",
    input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "STREAM_EVENTS hello" }] }],
    tools: [],
  });
  assert.deepEqual(streamed.events.map((event) => event.type), [
    "response.created",
    "response.output_item.added",
    "response.reasoning_summary_part.added",
    "response.reasoning_summary_text.delta",
    "response.output_item.added",
    "response.output_text.delta",
    "response.reasoning_summary_text.done",
    "response.output_item.done",
    "response.output_item.done",
    "response.completed",
  ]);
  assert.equal(streamed.events[3].delta, "AGY processing");
  assert.equal(streamed.events[5].delta, "stream visible");
  assert.equal(streamed.events[8].item.id, streamed.events[4].item.id);
  assert.equal(streamed.events[8].item.content[0].text, "stream visible");
  assert.deepEqual(await readdir(requestRoot), []);

  const bounded = await worker.runResponses({
    model: "fake",
    instructions: "EXPECT_BOUNDED_HISTORY",
    input: [
      { type: "message", role: "user", content: [{ type: "input_text", text: "EARLIEST_HISTORY_SENTINEL" }] },
      { type: "message", role: "assistant", content: [{ type: "output_text", text: "middle ".repeat(500_000) }] },
      { type: "message", role: "user", content: [{ type: "input_text", text: "LATEST_HISTORY_SENTINEL" }] },
    ],
    tools: [],
  });
  assert.equal(bounded.events.at(-1).type, "response.completed");
  assert.deepEqual(await readdir(requestRoot), []);

  const earlyEvents = [];
  const cancelled = worker.runResponses({
    model: "fake",
    instructions: "WAIT_FOR_CANCEL",
    input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "WAIT_FOR_CANCEL cancel me" }] }],
    tools: [],
  }, { sessionKey: "cancel-test", onEvent: (event) => earlyEvents.push(event) });
  for (let attempt = 0; attempt < 20 && earlyEvents.length === 0; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  assert.equal(earlyEvents[0]?.type, "response.created");
  await worker.closeSession("cancel-test");
  await assert.rejects(cancelled, (error) => error.code === "AGY_REQUEST_CANCELLED" && error.status === 499);
  assert.deepEqual(await readdir(requestRoot), []);
});

test("worker crash is typed and a fresh worker can serve the next request", async (t) => {
  const worker = new IsolatedWorkerClient({
    script: new URL("../src/agy-worker.mjs", import.meta.url).pathname,
    label: "AGY-crash-test",
    env: { SWITCHBOARD_TEST_ALLOW_CRASH: "1" },
  });
  t.after(() => worker.close());
  await assert.rejects(worker.testCrash(), (error) => error.code === "EXTERNAL_WORKER_CRASHED");
});
