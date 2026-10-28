import { execFile, spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import {
  chmod,
  copyFile,
  cp,
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  rename,
  rm,
  stat,
  writeFile,
} from "node:fs/promises";
import path from "node:path";
import { createInterface } from "node:readline";
import { promisify } from "node:util";

import Ajv from "ajv";

import {
  DEFAULT_MAX_EXTERNAL_HISTORY_BYTES,
  boundExternalHistory,
  boundedExternalInstructions,
  catalogExtends,
  externalAvailableIdentities,
  flattenTools,
  restoreToolItem,
  selectDefinitions,
  stableDefinitions,
  trimInheritedParentHistory,
} from "./tool-bridge.mjs";
import {
  recoverAgentMessageText,
  rememberSpawnPayloads,
} from "./spawn-payload-cache.mjs";
import { canonicalAgyModel } from "./shared.mjs";
import { isCanonicalSummary } from "./history-neutral.mjs";

const execFileAsync = promisify(execFile);
const AGY_PATH = process.env.SWITCHBOARD_AGY_PATH || "/Users/nokiy/.local/bin/agy";
const TOKEN_PATH = process.env.SWITCHBOARD_AGY_TOKEN_PATH || "/Users/nokiy/.gemini/antigravity-cli/antigravity-oauth-token";
const SOURCE_CONFIG = process.env.SWITCHBOARD_AGY_CONFIG_PATH || "/Users/nokiy/.codex-multi/agy-runtime-home/.gemini/config/config.json";
const SOURCE_MCP_CONFIG = process.env.SWITCHBOARD_AGY_MCP_CONFIG_PATH || "/Users/nokiy/.codex-multi/agy-runtime-home/.gemini/config/mcp_config.json";
const SOURCE_SETTINGS = process.env.SWITCHBOARD_AGY_SETTINGS_PATH || "/Users/nokiy/.codex-multi/agy-runtime-home/.gemini/antigravity-cli/settings.json";
const SOURCE_CLI_DIR = process.env.SWITCHBOARD_AGY_SEED_DIR || "/Users/nokiy/.codex-multi/agy-runtime-home/.gemini/antigravity-cli";
const SOURCE_PROJECTS_DIR = process.env.SWITCHBOARD_AGY_PROJECTS_DIR || "/Users/nokiy/.codex-multi/agy-runtime-home/.gemini/config/projects";
const REQUEST_ROOT = process.env.SWITCHBOARD_AGY_REQUEST_ROOT || "/Users/nokiy/.codex-multi/switchboard/runtime/agy-requests";
const SESSION_STATE_DIR = process.env.SWITCHBOARD_AGY_SESSION_DIR || path.join(path.dirname(REQUEST_ROOT), "agy-sessions");
const SESSION_STATE_TTL_MS = Number(process.env.SWITCHBOARD_AGY_SESSION_TTL_MS || 6 * 60 * 60_000);
const LOGIN_KEYCHAIN_PATH = process.env.SWITCHBOARD_AGY_LOGIN_KEYCHAIN_PATH || "/Users/nokiy/Library/Keychains/login.keychain-db";
const REQUEST_TIMEOUT_MS = Number(process.env.SWITCHBOARD_EXTERNAL_TIMEOUT_MS || 330_000);
const requestedKeepaliveMs = Number(process.env.SWITCHBOARD_AGY_KEEPALIVE_MS);
const KEEPALIVE_INTERVAL_MS = Number.isFinite(requestedKeepaliveMs) && requestedKeepaliveMs >= 10 && requestedKeepaliveMs <= 10_000
  ? requestedKeepaliveMs : 10_000;
const PERSISTENT_SESSIONS_ENABLED = process.env.SWITCHBOARD_AGY_PERSISTENT_SESSIONS !== "0";
const SESSION_IDLE_MS = Number(process.env.SWITCHBOARD_AGY_SESSION_IDLE_MS || 20 * 60_000);
const MAX_STREAM_BYTES = 24 * 1024 * 1024;
const MAX_IMAGE_BYTES = 20 * 1024 * 1024;
const MAX_TOTAL_IMAGE_BYTES = 40 * 1024 * 1024;
const MAX_IMAGES = 8;
const MAX_ACTION_GUIDANCE_CHARS = 2 * 1024;
// Antigravity CLI hard-truncates USER_INPUT at ~191,409 bytes; keep total prompt safely below that ceiling.
const MAX_AGY_PROMPT_BYTES = 160 * 1024;
const CODEX_INTERNAL_ITEM_TYPES = new Set([
  "reasoning",
  "additional_tools",
  "turn_context",
  "turn_aborted",
  "compaction_trigger",
  "thread_settings_applied",
  "token_count",
  "token_usage_record",
  "session_meta",
  "world_state",
  "item_completed",
  "task_started",
  "task_complete",
  "event_msg",
]);
const AGY_CONTEXT_TOKENS = 1_000_000;
const AGY_EFFECTIVE_CONTEXT_TOKENS = Math.floor(AGY_CONTEXT_TOKENS * 0.95);
const AGY_OUTPUT_RESERVE_TOKENS = 32_000;
const AGY_CLI_OVERHEAD_TOKENS = 20_000;
const MAX_PROMPT_TOKENS = AGY_EFFECTIVE_CONTEXT_TOKENS
  - AGY_OUTPUT_RESERVE_TOKENS
  - AGY_CLI_OVERHEAD_TOKENS;
// Cold reconstruction spans CLI messages; its text still passes the model token gate.
const MAX_AGY_COLD_CONTEXT_BYTES = MAX_PROMPT_TOKENS * 3;
const PUBLIC_STATUS_MARKER = "agy_processing_v1";
const AGY_REASONING_EFFORT = "high";
const ajv = new Ajv({ allErrors: true, strict: false });

function safeError(code, message, status = 502, details = {}) {
  const bounded = Object.fromEntries([
    "max_bytes", "mandatory_bytes", "prompt_bytes", "validated_prefix_items", "new_items",
  ].filter((key) => Number.isSafeInteger(details?.[key]) && details[key] >= 0)
    .map((key) => [key, details[key]]));
  if (/^[a-f0-9]{64}$/.test(details?.current_request_sha256 || ""))
    bounded.current_request_sha256 = details.current_request_sha256;
  return { code, message, status, ...(Object.keys(bounded).length ? { details: bounded } : {}) };
}

async function copyPrivate(source, destination) {
  await copyFile(source, destination);
  await chmod(destination, 0o600);
}

async function bindDefaultKeychain(home) {
  const env = {
    HOME: home,
    PATH: "/usr/bin:/bin",
  };
  try {
    await execFileAsync("/usr/bin/security", [
      "default-keychain",
      "-d", "user",
      "-s", LOGIN_KEYCHAIN_PATH,
    ], { env, timeout: 10_000 });
    const { stdout } = await execFileAsync("/usr/bin/security", [
      "default-keychain",
      "-d", "user",
    ], { env, timeout: 10_000 });
    if (stdout.trim().replace(/^"|"$/g, "") !== LOGIN_KEYCHAIN_PATH) {
      throw new Error("default keychain readback did not match");
    }
  } catch (error) {
    throw Object.assign(new Error("AGY request HOME could not bind the existing login keychain"), {
      code: "AGY_KEYCHAIN_BINDING_FAILED",
      cause: error,
    });
  }
}

async function prepareEphemeralHome() {
  await mkdir(REQUEST_ROOT, { recursive: true, mode: 0o700 });
  await chmod(REQUEST_ROOT, 0o700);
  const root = await mkdtemp(path.join(REQUEST_ROOT, "request-"));
  const home = path.join(root, "home");
  const cliDir = path.join(home, ".gemini", "antigravity-cli");
  const configDir = path.join(home, ".gemini", "config");
  const cacheDir = path.join(cliDir, "cache");
  const mediaDir = path.join(root, "media");
  await Promise.all([
    mkdir(cliDir, { recursive: true, mode: 0o700 }),
    mkdir(configDir, { recursive: true, mode: 0o700 }),
    mkdir(cacheDir, { recursive: true, mode: 0o700 }),
    mkdir(mediaDir, { recursive: true, mode: 0o700 }),
    mkdir(path.join(home, "Library", "Preferences"), { recursive: true, mode: 0o700 }),
    mkdir(path.join(home, "Library", "Keychains"), { recursive: true, mode: 0o700 }),
  ]);
  await bindDefaultKeychain(home);
  await Promise.all([
    copyPrivate(TOKEN_PATH, path.join(cliDir, "antigravity-oauth-token")),
    copyPrivate(SOURCE_CONFIG, path.join(configDir, "config.json")),
    copyPrivate(SOURCE_MCP_CONFIG, path.join(configDir, "mcp_config.json")),
    copyPrivate(SOURCE_SETTINGS, path.join(cliDir, "settings.json")),
    cp(path.join(SOURCE_CLI_DIR, "bin"), path.join(cliDir, "bin"), { recursive: true, force: false }),
    cp(path.join(SOURCE_CLI_DIR, "builtin"), path.join(cliDir, "builtin"), { recursive: true, force: false }),
    cp(SOURCE_PROJECTS_DIR, path.join(configDir, "projects"), { recursive: true, force: false }),
    copyPrivate(path.join(SOURCE_CLI_DIR, "jetski_state.pbtxt"), path.join(cliDir, "jetski_state.pbtxt")),
    copyPrivate(path.join(SOURCE_CLI_DIR, "installation_id"), path.join(cliDir, "installation_id")),
    copyPrivate(path.join(SOURCE_CLI_DIR, "cache", "default_project_id.txt"), path.join(cacheDir, "default_project_id.txt")),
    writeFile(path.join(configDir, ".migrated"), "", { mode: 0o600 }),
    writeFile(path.join(cliDir, "last_check.timestamp"), "", { mode: 0o600 }),
  ]);
  return { root, home, mediaDir, cleanup: () => rm(root, { recursive: true, force: true }) };
}

function decodeImage(imageUrl) {
  const match = /^data:(image\/(?:png|jpeg|jpg|webp|gif));base64,([A-Za-z0-9+/=\r\n]+)$/i.exec(imageUrl);
  if (!match) throw Object.assign(new Error("AGY image must be an inline PNG, JPEG, WebP, or GIF"), { code: "AGY_IMAGE_UNSUPPORTED" });
  const mime = match[1].toLowerCase().replace("image/jpg", "image/jpeg");
  const encoded = match[2].replace(/\s/g, "");
  const bytes = Buffer.from(encoded, "base64");
  if (!bytes.length || bytes.toString("base64").replace(/=+$/, "") !== encoded.replace(/=+$/, "")) {
    throw Object.assign(new Error("AGY image contains invalid base64"), { code: "AGY_IMAGE_INVALID" });
  }
  if (bytes.length > MAX_IMAGE_BYTES) throw Object.assign(new Error("AGY image exceeds the per-image limit"), { code: "AGY_IMAGE_TOO_LARGE" });
  const valid = mime === "image/png"
    ? bytes.subarray(0, 8).equals(Buffer.from("89504e470d0a1a0a", "hex"))
    : mime === "image/jpeg"
      ? bytes.subarray(0, 3).equals(Buffer.from("ffd8ff", "hex"))
      : mime === "image/gif"
        ? ["GIF87a", "GIF89a"].includes(bytes.subarray(0, 6).toString("ascii"))
        : bytes.subarray(0, 4).toString("ascii") === "RIFF" && bytes.subarray(8, 12).toString("ascii") === "WEBP";
  if (!valid) throw Object.assign(new Error("AGY image MIME does not match its bytes"), { code: "AGY_IMAGE_INVALID" });
  return {
    bytes,
    extension: { "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif" }[mime],
  };
}

async function materializeImages(input, mediaDir) {
  const urls = [];
  for (const item of Array.isArray(input) ? input : []) {
    if (item?.type !== "message") continue;
    for (const part of Array.isArray(item.content) ? item.content : []) {
      if (part?.type === "input_image" && typeof part.image_url === "string") urls.push(part.image_url);
    }
  }
  const unique = [...new Set(urls.reverse())].slice(0, MAX_IMAGES).reverse();
  const paths = new Map();
  let total = 0;
  for (const [index, url] of unique.entries()) {
    const image = decodeImage(url);
    total += image.bytes.length;
    if (total > MAX_TOTAL_IMAGE_BYTES) throw Object.assign(new Error("AGY images exceed the total limit"), { code: "AGY_IMAGES_TOO_LARGE" });
    const digest = createHash("sha256").update(image.bytes).digest("hex").slice(0, 12);
    const file = path.join(
      mediaDir,
      `attachment-${String(index + 1).padStart(2, "0")}-${digest}-${randomUUID()}.${image.extension}`,
    );
    await writeFile(file, image.bytes, { mode: 0o600, flag: "wx" });
    paths.set(url, file);
  }
  return paths;
}

function itemText(item, imagePaths, cursor) {
  if (item?.type === "message") {
    const content = typeof item.content === "string"
      ? item.content
      : (Array.isArray(item.content) ? item.content : [])
          .map((part) => {
            if (part?.type === "input_image") return `[image attachment: ${imagePaths.get(part.image_url) || "unavailable"}]`;
            return part?.text || part?.input_text || part?.output_text || "";
          })
          .filter(Boolean)
          .join("\n");
    return `${item.role || "unknown"}: ${content}`;
  }
  if (item?.type === "agent_message") {
    const recovered = recoverAgentMessageText(item, cursor);
    return recovered ? `user: ${recovered}` : "";
  }
  if (item?.type === "function_call" || item?.type === "custom_tool_call") {
    return `assistant_tool_call ${item.namespace ? `${item.namespace}.` : ""}${item.name}: ${item.arguments || item.input || ""}`;
  }
  if (item?.type === "function_call_output" || item?.type === "custom_tool_call_output") {
    return `tool_result ${item.call_id || "unknown"}: ${typeof item.output === "string" ? item.output : JSON.stringify(item.output)}`;
  }
  if (item?.type === "tool_search_call" || item?.type === "tool_search_output") return JSON.stringify(item);
  if (CODEX_INTERNAL_ITEM_TYPES.has(item?.type)) return "";
  return JSON.stringify(item);
}

function tokenUnitsForCodePoint(codePoint) {
  if (codePoint <= 0x7f) {
    const char = String.fromCodePoint(codePoint);
    if (/[A-Za-z0-9_]/.test(char) || /\s/.test(char)) return 1;
    return 3;
  }
  if ((codePoint >= 0x3400 && codePoint <= 0x9fff)
      || (codePoint >= 0x3040 && codePoint <= 0x30ff)
      || (codePoint >= 0xac00 && codePoint <= 0xd7af)) return 3;
  return 6;
}

export function estimateTextTokens(value) {
  let units = 0;
  for (const char of String(value || "")) units += tokenUnitsForCodePoint(char.codePointAt(0));
  return Math.ceil(units / 3);
}

function stripCodexInternalItems(input) {
  return (Array.isArray(input) ? input : []).filter((item) => !CODEX_INTERNAL_ITEM_TYPES.has(item?.type));
}

function conversationText(input, imagePaths, maxTokens, continuation = false, maxBytes = MAX_AGY_PROMPT_BYTES) {
  rememberSpawnPayloads(input);
  const cursor = new Map();
  const trimmed = stripCodexInternalItems(trimInheritedParentHistory(input));
  const sourceItems = continuation
    ? trimmed
    : boundExternalHistory(trimmed, maxBytes);
  const entries = sourceItems
    .map((item) => itemText(item, imagePaths, cursor))
    .filter(Boolean);
  const full = entries.join("\n\n");
  const byteCeiling = maxBytes;
  if (Buffer.byteLength(full) > byteCeiling) {
    throw Object.assign(new Error("Exact new AGY input exceeds the remaining UTF-8 prompt budget."), {
      code: "AGY_PROMPT_BYTE_LIMIT", status: 413,
      details: { max_bytes: byteCeiling, prompt_bytes: Buffer.byteLength(full) },
    });
  }
  if (estimateTextTokens(full) <= maxTokens) return full;
  throw Object.assign(new Error("Exact new AGY input exceeds the remaining token budget."), {
    code: "EXTERNAL_CONTEXT_TOO_LARGE", status: 413,
  });
}

function selectedDefinitions(body, limit = 128) {
  const { bridge } = flattenTools(body.tools, body.input);
  return stableDefinitions([...bridge.values()], body.input, limit);
}

function itemContentText(item) {
  if (item?.type !== "message") return "";
  if (typeof item.content === "string") return item.content;
  return (Array.isArray(item.content) ? item.content : [])
    .map((part) => part?.text || part?.input_text || part?.output_text || "")
    .filter(Boolean)
    .join("\n");
}

function latestUserAction(input) {
  const items = Array.isArray(input) ? input : [];
  let userIndex = -1;
  let text = "";
  for (let index = 0; index < items.length; index += 1) {
    const item = items[index];
    if (item?.role === "user" && !isCanonicalSummary(item)) {
      userIndex = index;
      text = itemContentText(item);
    }
  }
  if (userIndex < 0) return {
    required: false,
    hasEvidence: false,
    verificationPending: false,
    text: "",
  };
  const explicitGoal = text.includes('<codex_internal_context source="goal">');
  const probeOnly = /^(?:test(?:ing)?|ping|hello|hi|help|probe)(?:\s+[0-9]+)?$/i.test(text.trim());
  const noAction = probeOnly
    || /\b(?:do not|don't|without) (?:call|use|invoke|run)(?:ing)? (?:any )?tools?\b/i.test(text)
    || /(?:不要|不得|無需|不需)(?:呼叫|使用|執行)(?:任何)?工具/.test(text);
  const actionVerb = /\b(?:apply|browse|build|call|collect|commit|copy|create|delete|deploy|download|edit|execute|fetch|fix|generate|implement|inspect|install|invoke|modify|move|open|persist|push|read|remove|rename|repair|run|save|search|store|test|update|verify|write)\b/i.test(text)
    || /(?:建立|創建|新增|寫入|寫檔|修改|實作|實現|修好|修復|收集|下載|儲存|存入|落盤|刪除|移除|重新命名|搬移|複製|呼叫|調用|執行|測試|驗證|檢查|搜尋|瀏覽|查找|安裝|更新|建置|產生|提交|推送|部署|讀取)/.test(text);
  const afterUser = items.slice(userIndex + 1);
  const hasEvidence = afterUser.some((item) => [
    "function_call_output",
    "custom_tool_call_output",
  ].includes(item?.type));
  const verificationRequested = /\b(?:read(?:\s+it)?\s+back|verify|inspect|check)\b/i.test(text)
    || /(?:讀回|讀取|驗證|檢查)/.test(text);
  const hasVerificationCall = afterUser.some((item) => {
    if (!["function_call", "custom_tool_call"].includes(item?.type)) return false;
    const descriptor = `${item.name || ""} ${item.arguments || item.input || ""}`;
    return /(?:read|view|cat\b|\bod\b|stat\b|xxd\b|hexdump|sha(?:sum|256)|get[_-]?file)/i.test(descriptor);
  });
  return {
    required: !noAction && (explicitGoal || actionVerb),
    hasEvidence,
    verificationPending: verificationRequested && hasEvidence && !hasVerificationCall,
    text,
  };
}

function statelessMessageFastPath(body, action, imagePaths, summaryOnly) {
  const items = Array.isArray(body.input) ? body.input : [];
  if (items.some((item) => isCanonicalSummary(item) || item?.type === "compaction")) {
    return { enabled: false, reason: "compaction_present", message_count: items.length, tool_choice_kind: "compaction" };
  }
  const messages = items
    .filter((item) => item?.type === "message" && ["user", "assistant"].includes(item?.role) && !isCanonicalSummary(item));
  const toolChoiceKind = body.tool_choice == null ? "unset" : typeof body.tool_choice;
  const decision = (enabled, reason) => ({
    enabled,
    reason,
    message_count: messages.length,
    tool_choice_kind: toolChoiceKind,
  });
  if (summaryOnly) return decision(false, "summary_only");
  if (action.required) return decision(false, "action_required");
  if (action.hasEvidence) return decision(false, "evidence_present");
  if (action.verificationPending) return decision(false, "verification_pending");
  if (body.tool_choice === "required") return decision(false, "tool_choice_required");
  if (body.tool_choice && typeof body.tool_choice === "object") return decision(false, "tool_choice_object");
  const text = action.text.trim();
  if (!text) return decision(false, "empty_request");
  if (text.length > 2_000) return decision(false, "request_too_large");
  if (text.includes('<codex_internal_context source="goal">')) return decision(false, "goal_context_present");
  const precedingMessages = messages.slice(0, -1);
  const precedingMessagesAreScaffolds = precedingMessages.every(
    (item) => item?.role === "user" && itemContentText(item).includes("<environment_context>"),
  );
  if (messages.at(-1)?.role !== "user" || !precedingMessagesAreScaffolds) {
    return decision(false, "conversation_history_present");
  }
  const latestMessageImageUrls = new Set(
    (Array.isArray(messages.at(-1)?.content) ? messages.at(-1).content : [])
      .filter((part) => part?.type === "input_image" && typeof part.image_url === "string")
      .map((part) => part.image_url),
  );
  if (imagePaths.size && (
    latestMessageImageUrls.size !== imagePaths.size
    || [...latestMessageImageUrls].some((url) => !imagePaths.has(url))
  )) {
    return decision(false, "image_history_present");
  }
  const contextDependent = imagePaths.size
    ? /\b(?:above|below|earlier|last|previous|prior|same|those)\b/i.test(text)
      || /(?:上述|上面|下面|剛才|先前|前面|同一|那個|那些|本機|本地|工作區|任務|對話)/.test(text)
    : /\b(?:above|attached|below|code|codebase|current|currently|earlier|file|files|here|last|previous|prior|project|repo|repository|same|screenshot|this|those|these|workspace)\b/i.test(text)
      || /(?:上述|上面|下面|附件|圖片|截圖|剛才|先前|前面|目前|當前|同一|這個|這些|那個|那些|本機|本地|工作區|任務|對話|專案|本專案|程式碼|代碼|架構|檔案|文件|倉庫|倉儲)/.test(text);
  const structuredMaterial = /```|https?:\/\/|\/(?:Users|Volumes)\/|[A-Za-z]:\\/.test(text);
  if (contextDependent) return decision(false, "context_dependent_request");
  if (structuredMaterial) return decision(false, "structured_material_present");
  return decision(true, imagePaths.size ? "current_turn_vision" : "eligible");
}

// The caller's tool_choice decides, as it does for Claude: the model chooses
// between a message and a tool call unless the caller requires a tool. Wording
// in the user's message never forces a call.
function actionPolicy(body, definitions, summaryOnly, fastPath = false) {
  if (summaryOnly || fastPath) return "message_only";
  if (definitions.length === 0 || body.tool_choice === "none") return "message_only";
  if (body.tool_choice === "required" || (body.tool_choice && typeof body.tool_choice === "object")) {
    return "tool_required";
  }
  return "auto";
}

function responseSchema(definitions, policy) {
  if (policy === "message_only") {
    return {
      type: "object",
      properties: { kind: { type: "string", enum: ["message"] }, text: { type: "string", minLength: 1 } },
      required: ["kind", "text"],
      additionalProperties: false,
    };
  }
  if (policy === "tool_required") {
    return {
      type: "object",
      properties: {
        kind: { type: "string", enum: ["tool_call"] },
        tool_name: { type: "string", enum: definitions.map((item) => item.alias) },
        arguments: { type: "object", additionalProperties: true },
        input: { type: "string" },
      },
      required: ["kind", "tool_name", "arguments"],
      additionalProperties: false,
    };
  }
  return {
    type: "object",
    properties: {
      kind: { type: "string", enum: policy === "tool_required" ? ["tool_call"] : ["message", "tool_call"] },
      text: { type: "string" },
      tool_name: { type: "string", enum: ["__none__", ...definitions.map((item) => item.alias)] },
      arguments: { type: "object", additionalProperties: true },
      input: { type: "string" },
    },
    required: ["kind", "text", "tool_name", "arguments", "input"],
    additionalProperties: false,
  };
}

function parsedStructuredOutput(response, schema) {
  const text = String(response || "").trim();
  const candidates = [text];
  const fenced = /^```(?:json)?\s*([\s\S]*?)\s*```$/i.exec(text);
  if (fenced) candidates.push(fenced[1].trim());
  const validate = ajv.compile(schema);
  const allowedTools = Array.isArray(schema?.properties?.tool_name?.enum)
    ? schema.properties.tool_name.enum
    : [];
  const requiredFields = new Set(Array.isArray(schema?.required) ? schema.required : []);
  const resolveAlias = (toolName) => {
    if (typeof toolName !== "string" || allowedTools.includes(toolName)) return toolName;
    const matches = allowedTools.filter((alias) => alias !== "__none__" && alias.startsWith(`${toolName}__`));
    return matches.length === 1 ? matches[0] : toolName;
  };
  // The auto contract also names fields a compact answer leaves out; they are
  // filled with their empty values before the answer is checked.
  const completed = (value) => {
    if (!value || typeof value !== "object" || Array.isArray(value)) return value;
    if (value.kind === "message") {
      return requiredFields.has("tool_name")
        ? { tool_name: "__none__", arguments: {}, input: "", ...value }
        : { kind: "message", text: value.text };
    }
    if (value.kind === "tool_call") {
      const normalized = {
        ...value,
        tool_name: resolveAlias(value.tool_name),
        arguments: value.arguments && typeof value.arguments === "object" && !Array.isArray(value.arguments)
          ? value.arguments
          : {},
      };
      if (requiredFields.has("text")) {
        return { text: "", input: "", ...normalized };
      }
      const { text: _unusedText, ...rest } = normalized;
      if (!rest.input) delete rest.input;
      return rest;
    }
    return value;
  };
  for (const candidate of candidates) {
    try {
      const parsed = JSON.parse(candidate);
      if (validate(parsed)) return parsed;
      const filled = completed(parsed);
      if (validate(filled)) return filled;
    } catch {
      // Only complete JSON values that satisfy the active schema are accepted.
    }
  }
  return null;
}

function containsCallerProtocol(text, quotedExamples = false) {
  const value = quotedExamples
    ? String(text || "").replace(/```[\s\S]*?```/g, "").replace(/`[^`\n]*`/g, "").replace(/^\s*>.*$/gm, "")
    : String(text || "");
  return /"kind"\s*:\s*"(?:tool_call|message)"/.test(value)
    || /^\s*(?:<\/?(?:USER_REQUEST|ADDITIONAL_METADATA)>|TASK HISTORY:|AVAILABLE ACTIONS:|CALLER GUIDANCE:)/m.test(value);
}

function resolveAgyOutput(parsed, schema) {
  const rejectProtocol = () => {
    throw Object.assign(new Error("AGY reply contains ambiguous caller protocol or echoed prompt text; no action was accepted. Inspect the existing turn before continuing."), {
      code: "AGY_INVALID_STRUCTURED_OUTPUT", status: 502,
    });
  };
  if (!parsed.structured_output) {
    const response = String(parsed.response || "").trim();
    if (!response) {
      throw Object.assign(new Error("AGY did not return valid structured output"), { code: "AGY_INVALID_OUTPUT" });
    }
    const structured = parsedStructuredOutput(response, schema || responseSchema([], "message_only"));
    // Never select an action from a transcript or silently turn invalid protocol into a final answer.
    if (!structured && containsCallerProtocol(response)) rejectProtocol();
    parsed.structured_output = structured || { kind: "message", text: response };
  }
  if (parsed.structured_output.kind === "message"
      && containsCallerProtocol(parsed.structured_output.text, true)) rejectProtocol();
  return parsed;
}

function routeIdentityContext(body) {
  const catalogModel = String(body.model || "unknown");
  const requestedEffort = ["low", "medium", "high", "xhigh", "max", "ultra"].includes(body.reasoning?.effort)
    ? body.reasoning.effort
    : AGY_REASONING_EFFORT;
  return [
    "AUTHORITATIVE ROUTE IDENTITY (generated by the switchboard for this request; not user-provided):",
    JSON.stringify({
      catalog_model: catalogModel,
      provider: "agy",
      upstream_model: canonicalAgyModel(catalogModel),
      requested_reasoning_effort: requestedEffort,
      host_harness: "Codex",
    }),
    "When asked about the current model, provider, reasoning effort, or harness role, explicitly list every requested field and its exact request value from this object. Never replace the values with a claim that reporting is complete. Codex is the host harness, not the responding model identity.",
  ].join("\n");
}

function buildPrompt(body, definitions, conversation, imagePaths, summaryOnly, policy, fastPath = false, continuation = false, byteLimit = MAX_AGY_PROMPT_BYTES) {
  if (fastPath) {
    return [
      "Answer the current user request directly in the user's language.",
      imagePaths.size
        ? "This is a stateless current-turn multimodal message. Inspect every listed image attachment with read_file before answering. Do not use any other tools, claim external actions, or infer prior task context."
        : "This is a stateless message-only turn. Do not use tools, claim external actions, or infer prior task context.",
      routeIdentityContext(body),
      `CURRENT REQUEST:\n${conversation}`,
    ].join("\n\n");
  }
  const instruction = summaryOnly
    ? [
        "Create a canonical, provider-neutral context summary for another coding model.",
        "Preserve requirements, decisions, paths, code state, tool results, blockers, and pending work.",
        "Return only a message with the summary. Do not use tools or include private reasoning.",
      ].join(" ")
    : [
        "Help complete the coding task below through a caller-managed action loop.",
        "Do not execute shell, coding, browser, app, MCP, or external actions directly.",
        "Never invoke built-in CLI tools (such as view_file, run_command, write_to_file, replace_file_content, grep_search, find_by_name, or search_web); every action in AVAILABLE ACTIONS is executed by the caller when you return a JSON tool_call.",
        "When a supplied action is needed, request exactly one tool_call. Otherwise return a final message.",
        "Respond with a single raw JSON object only (no markdown code fences or extra prose): use {\"kind\":\"tool_call\",\"tool_name\":\"<exact name from AVAILABLE ACTIONS>\",\"arguments\":{...}} to invoke an action, or {\"kind\":\"message\",\"text\":\"<response>\"} to return a final message.",
        "Do not claim an action completed until its result appears in the task history.",
        policy === "tool_required"
          ? "This inference requires one supplied action. Request exactly one tool_call now; a message or completion claim is invalid until the caller returns its result."
          : "",
        "Prefer a supplied concrete local action when its schema fits. Use tool_search only when no supplied concrete action can perform the next step.",
        "Tasks that create, change, collect, download, store, inspect, or verify external state require caller-executed action evidence before a final message.",
        imagePaths.size ? "Inspect every listed image with read_file before answering about its pixels." : "",
      ].filter(Boolean).join(" ");
  const prompt = continuation
    ? [
      "Continue this session. The caller guidance, route identity, and available actions already bound to this session stay in effect.",
      "Do not invoke built-in CLI tools. Respond with a single raw JSON object only (no markdown code fences): use {\"kind\":\"tool_call\",\"tool_name\":\"<bound action name>\",\"arguments\":{...}} to request a bound action, or {\"kind\":\"message\",\"text\":\"<response>\"} for a final message.",
      policy === "tool_required"
        ? "This inference requires one supplied action. Request exactly one tool_call now; a message or completion claim is invalid until the caller returns its result."
        : "",
      `TASK HISTORY:\n${conversation}`,
    ].filter(Boolean).join("\n\n")
    : [
      instruction,
      summaryOnly ? "" : routeIdentityContext(body),
      `CALLER GUIDANCE:\n${boundedExternalInstructions(
        body.instructions,
        policy === "tool_required" ? MAX_ACTION_GUIDANCE_CHARS : undefined,
      )}`,
      `TASK HISTORY:\n${conversation}`,
      `AVAILABLE ACTIONS:\n${JSON.stringify(definitions.map((item) => ({
        name: item.alias,
        description: item.description,
        parameters: item.parameters,
      })))}`,
    ].filter(Boolean).join("\n\n");
  if (Buffer.byteLength(prompt) > byteLimit) {
    throw Object.assign(new Error("AGY prompt including fixed guidance and tools exceeds the UTF-8 byte ceiling."), {
      code: "AGY_PROMPT_BYTE_LIMIT", status: 413,
      details: { max_bytes: byteLimit, prompt_bytes: Buffer.byteLength(prompt) },
    });
  }
  if (estimateTextTokens(prompt) > MAX_PROMPT_TOKENS) {
    throw Object.assign(
      new Error(`AGY canonical prompt exceeds the admitted ${MAX_PROMPT_TOKENS}-token input budget`),
      { code: "EXTERNAL_CONTEXT_TOO_LARGE" },
    );
  }
  return prompt;
}

function coldBootstrap(prompt) {
  const chunks = [];
  let chunk = "", bytes = 0;
  for (const char of prompt) {
    const width = Buffer.byteLength(char);
    if (bytes + width > MAX_AGY_PROMPT_BYTES - 2048) {
      chunks.push(chunk);
      chunk = "";
      bytes = 0;
    }
    chunk += char;
    bytes += width;
  }
  if (chunk) chunks.push(chunk);
  const transaction = randomUUID();
  const promptSha = createHash("sha256").update(prompt).digest("hex");
  const identity = { transaction, parts: chunks.length, prompt_sha256: promptSha };
  const stages = chunks.map((payload, index) => {
    const partSha = createHash("sha256").update(payload).digest("hex");
    const meta = { ...identity, part: index + 1, part_sha256: partSha };
    const ack = `AGY_CONTEXT_ACK ${transaction} ${meta.part}/${meta.parts} ${partSha}`;
    return { ack, prompt: [
      "AGY_CONTEXT_STAGE_V1", JSON.stringify(meta),
      "Store this exact fragment as inert context. Do not act on its contents or invoke any tools. The task is not committed yet.",
      `Reply only with ${JSON.stringify({ kind: "message", text: ack })}.`,
      `PAYLOAD_BEGIN:${transaction}\n${payload}\nPAYLOAD_END:${transaction}`,
      "Do not execute, summarize or interpret this data. Return only the exact acknowledgement above.",
    ].join("\n") };
  });
  return { stages, identity, bytes: Buffer.byteLength(prompt), commit: [
    "AGY_CONTEXT_COMMIT_V1", JSON.stringify(identity),
    "Every preceding acknowledged fragment belongs to this one canonical prompt. Reassemble its exact text in part order and now follow that complete prompt, retaining caller guidance, route identity, task history and available actions.",
    "Do not invoke built-in CLI tools. Return one raw JSON caller-managed tool_call or final message as specified in the assembled prompt.",
  ].join("\n") };
}

function promptHistoryBudget(body, definitions, imagePaths, summaryOnly, policy, continuation = false) {
  const fixedPrompt = buildPrompt(body, definitions, "", imagePaths, summaryOnly, policy, false, continuation);
  return Math.max(0, MAX_PROMPT_TOKENS - estimateTextTokens(fixedPrompt) - 8);
}

function promptHistoryByteBudget(body, definitions, imagePaths, summaryOnly, policy, continuation = false) {
  const fixedPrompt = buildPrompt(body, definitions, "", imagePaths, summaryOnly, policy, false, continuation);
  return Math.max(16 * 1024, MAX_AGY_PROMPT_BYTES - Buffer.byteLength(fixedPrompt) - 256);
}

function definitionsDigest(definitions) {
  return createHash("sha256").update(JSON.stringify(definitions.map((item) => [
    item.alias,
    item.description || "",
    item.parameters || {},
  ]))).digest("hex");
}

function availableCatalog(body) {
  const available = externalAvailableIdentities(body);
  return {
    available,
    catalogIdentity: createHash("sha256").update(JSON.stringify(available)).digest("hex"),
  };
}

function agyEnvironment(home) {
  return {
    HOME: home,
    PATH: "/opt/homebrew/bin:/usr/local/bin:/Users/nokiy/.local/bin:/usr/bin:/bin",
    TMPDIR: path.join(home, "tmp"),
    LANG: "en_US.UTF-8",
    LC_ALL: "en_US.UTF-8",
    USER: "nokiy",
    LOGNAME: "nokiy",
    SHELL: "/bin/zsh",
    NO_COLOR: "1",
  };
}

function usageDelta(current = {}, previous = {}) {
  const delta = {};
  for (const key of ["input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens"]) {
    delta[key] = Math.max(0, Number(current[key] || 0) - Number(previous[key] || 0));
  }
  return delta;
}

function isProviderEcho(item) {
  return item?.type === "reasoning"
    || item?.type === "agent_message"
    || ((item?.type === "message") && item?.role === "assistant")
    || ["function_call", "custom_tool_call", "tool_search_call"].includes(item?.type);
}

// The session's history is the caller's own items (messages, tool results,
// checkpoints). The model's echoed output is left out, so a full history sent
// after a reconnect still lines up with turns that arrived as deltas, as the
// Claude bridge's record of seen input does.
function callerDigests(input) {
  return (Array.isArray(input) ? input : [])
    .filter((item) => !isProviderEcho(item))
    .map((item) => createHash("sha256").update(JSON.stringify(item)).digest("hex"));
}

function shouldPersistAgySession(sessionKey) {
  return PERSISTENT_SESSIONS_ENABLED
    && typeof sessionKey === "string"
    && (sessionKey.startsWith("thread-") || Boolean(process.env.SWITCHBOARD_AGY_SESSION_DIR));
}

function sessionStatePath(sessionKey) {
  const safeKey = String(sessionKey || "")
    .replace(/[^A-Za-z0-9_-]/g, "_")
    .slice(0, 96);
  const digest = createHash("sha256").update(String(sessionKey || "")).digest("hex").slice(0, 16);
  return path.join(SESSION_STATE_DIR, `${safeKey}-${digest}.json`);
}

async function savePersistedAgySession(session) {
  if (!shouldPersistAgySession(session.sessionKey) || !session.canPersistToDisk()) return;
  try {
    await mkdir(SESSION_STATE_DIR, { recursive: true, mode: 0o700 });
    const target = sessionStatePath(session.sessionKey);
    const temp = `${target}.${process.pid}.${randomUUID()}.tmp`;
    const payload = {
      version: 1,
      sessionKey: session.sessionKey,
      model: session.model,
      conversationId: session.conversationId,
      turnCount: session.turnCount,
      priorCallerDigests: session.priorCallerDigests,
      instructionsDigest: session.instructionsDigest,
      toolDigest: session.toolDigest,
      catalogIdentity: session.catalogIdentity,
      available: session.available,
      boundDefinitions: session.boundDefinitions,
      admissionContext: session.admissionContext,
      ephemeral: {
        root: session.ephemeral.root,
        home: session.ephemeral.home,
        mediaDir: session.ephemeral.mediaDir,
      },
      updatedAt: Date.now(),
    };
    await writeFile(temp, JSON.stringify(payload), { mode: 0o600 });
    await rename(temp, target);
  } catch {
    // Session metadata persistence is an optimization; never fail an active turn.
  }
}

async function removePersistedAgySession(sessionKey, cleanupHome = false) {
  if (!shouldPersistAgySession(sessionKey)) return;
  const target = sessionStatePath(sessionKey);
  if (cleanupHome) {
    try {
      const parsed = JSON.parse(await readFile(target, "utf8"));
      if (parsed?.ephemeral?.root) {
        await rm(parsed.ephemeral.root, { recursive: true, force: true });
      }
    } catch {
      // Ignore missing or unreadable session state.
    }
  }
  await rm(target, { force: true }).catch(() => {});
}

async function loadPersistedAgySession(sessionKey, model) {
  if (!shouldPersistAgySession(sessionKey)) return null;
  const target = sessionStatePath(sessionKey);
  let parsed;
  try {
    parsed = JSON.parse(await readFile(target, "utf8"));
  } catch {
    return null;
  }
  const expired = !(Number(parsed?.updatedAt) > 0)
    || (Date.now() - Number(parsed.updatedAt) > SESSION_STATE_TTL_MS);
  const validShape = parsed?.version === 1
    && parsed.sessionKey === sessionKey
    && parsed.model === model
    && typeof parsed.conversationId === "string"
    && parsed.conversationId.length > 0
    && Number.isInteger(parsed.turnCount)
    && parsed.turnCount > 0
    && Array.isArray(parsed.priorCallerDigests)
    && typeof parsed.ephemeral?.root === "string"
    && typeof parsed.ephemeral?.home === "string"
    && typeof parsed.ephemeral?.mediaDir === "string";
  if (expired || !validShape) {
    await removePersistedAgySession(sessionKey, true);
    return null;
  }
  try {
    const [homeStat, mediaStat] = await Promise.all([
      stat(parsed.ephemeral.home),
      stat(parsed.ephemeral.mediaDir),
    ]);
    if (!homeStat.isDirectory() || !mediaStat.isDirectory()) {
      await removePersistedAgySession(sessionKey, true);
      return null;
    }
  } catch {
    await removePersistedAgySession(sessionKey, true);
    return null;
  }
  return parsed;
}

class PersistentAgySession {
  constructor({ sessionKey, model, ephemeral, onClosed, restored = null }) {
    this.sessionKey = sessionKey;
    this.model = model;
    this.ephemeral = ephemeral;
    this.onClosed = onClosed;
    this.child = null;
    this.lines = null;
    this.pending = null;
    this.closed = false;
    this.closeRequested = false;
    this.closeReason = null;
    this.cleanupPromise = null;
    this.idleTimer = null;
    this.turnCount = restored ? restored.turnCount : 0;
    this.conversationId = restored ? restored.conversationId : null;
    this.cumulativeUsage = {};
    this.priorCallerDigests = restored ? restored.priorCallerDigests : null;
    this.instructionsDigest = restored ? restored.instructionsDigest : null;
    this.toolDigest = restored ? restored.toolDigest : null;
    this.catalogIdentity = restored ? restored.catalogIdentity : null;
    this.available = restored ? restored.available : null;
    this.boundDefinitions = restored ? restored.boundDefinitions : null;
    this.admissionContext = Array.isArray(restored?.admissionContext) ? restored.admissionContext : [];
  }

  canPersistToDisk() {
    return shouldPersistAgySession(this.sessionKey)
      && this.turnCount > 0
      && typeof this.conversationId === "string"
      && this.conversationId.length > 0
      && Array.isArray(this.priorCallerDigests);
  }

  preserveOnClose() {
    return (this.closeReason === "worker_shutdown" || this.closeReason === "idle_timeout")
      && this.canPersistToDisk();
  }

  sameBinding(instructionsDigest, catalogIdentity, available) {
    if (this.turnCount === 0) return true;
    if (this.instructionsDigest !== instructionsDigest) return false;
    if (this.catalogIdentity === catalogIdentity) return true;
    return catalogExtends(this.available, available);
  }

  async start() {
    await mkdir(path.join(this.ephemeral.home, "tmp"), { recursive: true, mode: 0o700 });
    const args = [
      "--add-dir", this.ephemeral.mediaDir,
      "--sandbox",
      "--model", this.model,
      "--effort", AGY_REASONING_EFFORT,
      ...(this.conversationId ? ["--conversation", this.conversationId] : []),
      "--input-format", "stream-json",
      "--output-format", "stream-json",
      "--disable-slash-commands",
      "--log-file", "/dev/null",
      "--print-timeout", "5m",
    ];
    const child = spawn(AGY_PATH, args, {
      cwd: "/Users/nokiy",
      env: agyEnvironment(this.ephemeral.home),
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.child = child;
    this.lines = createInterface({ input: child.stdout, crlfDelay: Infinity });
    this.lines.on("line", (line) => this.handleLine(line));
    child.stderr.on("data", (chunk) => this.handleStderr(chunk));
    child.stdin.on("error", (error) => {
      if (error?.code !== "EPIPE") this.failPending(Object.assign(
        new Error("AGY persistent prompt transport failed"),
        { code: "AGY_PROMPT_TRANSPORT_FAILED", cause: error },
      ));
    });
    child.once("error", (error) => this.failPending(Object.assign(
      new Error("AGY persistent process failed to start"),
      { code: "AGY_PROCESS_START_FAILED", cause: error },
    )));
    child.once("close", (code, childSignal) => this.handleClose(code, childSignal));
    return this;
  }

  // The request extends this session when the caller items it has already
  // seen open the request's caller items, in order, and at least one is new.
  // A request with nothing new (regeneration or replay) starts over.
  canContinue(input) {
    if (this.turnCount === 0 || !this.priorCallerDigests) return true;
    const current = callerDigests(input);
    if (current.length <= this.priorCallerDigests.length) return false;
    return this.priorCallerDigests.every((digest, index) => current[index] === digest);
  }

  continuationInput(input) {
    const current = Array.isArray(input) ? input : [];
    if (this.turnCount === 0 || !this.priorCallerDigests) return current;
    let seen = 0;
    let start = 0;
    while (start < current.length && seen < this.priorCallerDigests.length) {
      if (!isProviderEcho(current[start])) seen += 1;
      start += 1;
    }
    const delta = current.slice(start);
    while (delta.length && isProviderEcho(delta[0])) delta.shift();
    return delta;
  }

  suffixInput(input) {
    const delta = Array.isArray(input) ? [...input] : [];
    while (delta.length && isProviderEcho(delta[0])) delta.shift();
    return delta;
  }

  promptInstructions(instructions) {
    const value = String(instructions || "");
    const digest = createHash("sha256").update(value).digest("hex");
    if (this.turnCount > 0 && digest === this.instructionsDigest) {
      return "[Caller guidance is byte-identical to the guidance already bound to this task-scoped AGY session.]";
    }
    return value;
  }

  async remember(body, toolDigest, catalogIdentity, definitions, available, append, pendingCalls) {
    const digests = callerDigests(body?.input);
    this.priorCallerDigests = append && Array.isArray(this.priorCallerDigests)
      ? [...this.priorCallerDigests, ...digests]
      : digests;
    this.instructionsDigest = createHash("sha256").update(String(body?.instructions || "")).digest("hex");
    this.toolDigest = toolDigest;
    const currentUser = (Array.isArray(body?.input) ? body.input : [])
      .findLast((item) => item?.type === "message" && item.role === "user" && !isCanonicalSummary(item))
      || this.admissionContext.find((item) => item?.type === "message" && item.role === "user");
    this.admissionContext = currentUser ? [currentUser, ...pendingCalls] : [];
    if (!append) {
      this.catalogIdentity = catalogIdentity;
      this.available = available;
      this.boundDefinitions = definitions;
    }
    await savePersistedAgySession(this);
  }

  scheduleIdleClose() {
    clearTimeout(this.idleTimer);
    if (!(SESSION_IDLE_MS > 0)) return;
    this.idleTimer = setTimeout(() => this.close("idle_timeout"), SESSION_IDLE_MS);
    this.idleTimer.unref?.();
  }

  failPending(error) {
    const pending = this.pending;
    if (!pending) return;
    this.pending = null;
    clearTimeout(pending.timer);
    pending.signal?.removeEventListener("abort", pending.onAbort);
    pending.reject(error);
  }

  handleStderr(chunk) {
    if (!this.pending) return;
    this.pending.stderrBytes += chunk.length;
    if (this.pending.stderrBytes > MAX_STREAM_BYTES) {
      this.failPending(Object.assign(
        new Error("AGY persistent stderr exceeded the admitted in-memory limit"),
        { code: "AGY_STREAM_TOO_LARGE" },
      ));
      this.close("stderr_overflow");
    }
  }

  handleLine(line) {
    let event;
    try {
      event = JSON.parse(line);
    } catch {
      return;
    }
    if (event?.event === "init") {
      this.conversationId = this.conversationId || event.conversation_id;
      return;
    }
    const pending = this.pending;
    if (!pending) return;
    pending.stdoutBytes += Buffer.byteLength(line) + 1;
    if (pending.stdoutBytes > MAX_STREAM_BYTES) {
      this.failPending(Object.assign(
        new Error("AGY persistent stream exceeded the admitted in-memory limit"),
        { code: "AGY_STREAM_TOO_LARGE" },
      ));
      this.close("stdout_overflow");
      return;
    }
    const update = event?.event === "step_update" ? event.step_update : null;
    const delta = update?.step_type === "agent_response" ? update.text_delta : null;
    if (typeof delta === "string" && delta && !isStructuredOutputDelta(delta)) pending.onTextDelta?.(delta);
    if (event?.event !== "result") return;

    this.pending = null;
    clearTimeout(pending.timer);
    pending.signal?.removeEventListener("abort", pending.onAbort);
    const result = event.result || {};
    const cumulativeUsage = result.usage || {};
    const turnUsage = usageDelta(cumulativeUsage, this.cumulativeUsage);
    this.cumulativeUsage = cumulativeUsage;
    this.turnCount += 1;
    this.conversationId = this.conversationId || result.conversation_id;
    this.scheduleIdleClose();
    pending.resolve({
      ...result,
      usage: turnUsage,
      persistent_session: {
        conversation_id: this.conversationId,
        reused: this.turnCount > 1,
        turn_index: this.turnCount,
      },
    });
  }

  turn({ prompt, signal, onTextDelta = null }) {
    if (signal?.aborted) {
      return Promise.reject(Object.assign(new Error("AGY request was cancelled"), { name: "AbortError", code: "ABORT_ERR" }));
    }
    if (this.closed || this.closeRequested || !this.child || this.child.exitCode != null) {
      return Promise.reject(Object.assign(new Error("AGY persistent session is closed"), { code: "AGY_SESSION_CLOSED" }));
    }
    if (this.pending) {
      return Promise.reject(Object.assign(new Error("AGY persistent session already has a turn in flight"), { code: "AGY_SESSION_BUSY", status: 409 }));
    }
    clearTimeout(this.idleTimer);
    return new Promise((resolve, reject) => {
      const onAbort = () => {
        this.failPending(Object.assign(new Error("AGY request was cancelled"), { name: "AbortError", code: "ABORT_ERR" }));
        this.close("downstream_cancelled");
      };
      const timer = setTimeout(() => {
        this.failPending(Object.assign(new Error("AGY request timed out"), { code: "AGY_REQUEST_TIMEOUT" }));
        this.close("turn_timeout");
      }, REQUEST_TIMEOUT_MS);
      this.pending = {
        resolve,
        reject,
        signal,
        onAbort,
        onTextDelta,
        timer,
        stdoutBytes: 0,
        stderrBytes: 0,
      };
      signal?.addEventListener("abort", onAbort, { once: true });
      const frame = `${JSON.stringify({ event: "user", message: { content: prompt } })}\n`;
      this.child.stdin.write(frame, (error) => {
        if (!error) return;
        this.failPending(Object.assign(
          new Error("AGY persistent prompt transport failed"),
          { code: "AGY_PROMPT_TRANSPORT_FAILED", cause: error },
        ));
        this.close("stdin_write_failed");
      });
    });
  }

  async handleClose(code, childSignal) {
    if (this.closed) return;
    this.closed = true;
    clearTimeout(this.idleTimer);
    this.lines?.close();
    this.failPending(Object.assign(
      new Error(`AGY persistent stream ended before a valid result (exit ${code}, signal ${childSignal || "none"})`),
      { code: this.closeRequested ? "AGY_REQUEST_CANCELLED" : "AGY_INVALID_OUTPUT" },
    ));
    if (this.preserveOnClose()) {
      this.cleanupPromise ||= Promise.resolve();
    } else {
      this.cleanupPromise ||= Promise.all([
        this.ephemeral.cleanup(),
        removePersistedAgySession(this.sessionKey, false),
      ]);
    }
    await this.cleanupPromise.catch(() => {});
    this.onClosed?.(this);
  }

  async close(reason = "closed") {
    if (this.closed) return this.cleanupPromise;
    this.closeRequested = true;
    this.closeReason = reason;
    clearTimeout(this.idleTimer);
    this.child?.kill("SIGTERM");
    const guard = setTimeout(() => this.child?.kill("SIGKILL"), 2_000);
    guard.unref?.();
    while (!this.closed) await new Promise((resolve) => setTimeout(resolve, 10));
    clearTimeout(guard);
    return this.cleanupPromise;
  }
}

const persistentSessions = new Map();
const persistentSessionCreations = new Map();
// Native reconnects must not re-infer identical protocol-rejected or incomplete turns.
// Keep only one bounded fingerprint per thread, never another copy of its prompt.
const failedAgyRequests = new Map();

async function closePersistentSession(sessionKey, reason = "closed") {
  // Startup may still own an ephemeral directory without a registered session.
  // Wait for that exact creation before acknowledging cleanup, never a successor.
  const session = persistentSessions.get(sessionKey)
    || await persistentSessionCreations.get(sessionKey)?.catch(() => null);
  if (!session) {
    if (reason !== "worker_shutdown" && reason !== "idle_timeout") {
      await removePersistedAgySession(sessionKey, true);
    }
    return;
  }
  if (persistentSessions.get(sessionKey) === session) persistentSessions.delete(sessionKey);
  await session.close(reason);
}

async function persistentSessionFor(sessionKey, model, forceNew = false) {
  const existing = persistentSessions.get(sessionKey);
  if (existing
      && !forceNew
      && existing.model === model
      && !existing.closed
      && !existing.closeRequested) return existing;
  if (existing) await closePersistentSession(sessionKey, forceNew ? "context_reset" : "model_changed");
  else if (forceNew) await removePersistedAgySession(sessionKey, true);
  if (persistentSessionCreations.has(sessionKey)) return persistentSessionCreations.get(sessionKey);
  const creation = (async () => {
    const restored = forceNew ? null : await loadPersistedAgySession(sessionKey, model);
    const ephemeral = restored
      ? {
          root: restored.ephemeral.root,
          home: restored.ephemeral.home,
          mediaDir: restored.ephemeral.mediaDir,
          cleanup: () => rm(restored.ephemeral.root, { recursive: true, force: true }),
        }
      : await prepareEphemeralHome();
    try {
      const session = await new PersistentAgySession({
        sessionKey,
        model,
        ephemeral,
        restored,
        onClosed: (closed) => {
          if (persistentSessions.get(sessionKey) === closed) persistentSessions.delete(sessionKey);
        },
      }).start();
      persistentSessions.set(sessionKey, session);
      return session;
    } catch (error) {
      await ephemeral.cleanup();
      await removePersistedAgySession(sessionKey, false);
      throw error;
    }
  })();
  persistentSessionCreations.set(sessionKey, creation);
  try {
    return await creation;
  } finally {
    persistentSessionCreations.delete(sessionKey);
  }
}

function isStructuredOutputDelta(value) {
  const text = String(value || "").trim();
  if (!text.startsWith("{") || !text.endsWith("}")) return false;
  try {
    const parsed = JSON.parse(text);
    return parsed?.kind === "message" || parsed?.kind === "tool_call";
  } catch {
    return false;
  }
}

async function callAgyOneShot({ home, mediaDir, model, prompt, schema = null, imagePaths, signal, onTextDelta = null }) {
  await mkdir(path.join(home, "tmp"), { recursive: true, mode: 0o700 });
  if (signal?.aborted) throw Object.assign(new Error("AGY request was cancelled"), { name: "AbortError", code: "ABORT_ERR" });
  const args = [
    ...(imagePaths.size ? ["--add-dir", mediaDir] : []),
    "--sandbox",
    "--model", model,
    "--effort", AGY_REASONING_EFFORT,
    "--output-format", "stream-json",
    ...(schema ? ["--json-schema", JSON.stringify(schema)] : []),
    "--disable-slash-commands",
    "--log-file", "/dev/null",
    "--print-timeout", "5m",
  ];
  const parsed = await new Promise((resolve, reject) => {
    const child = spawn(AGY_PATH, args, {
      cwd: "/Users/nokiy",
      env: agyEnvironment(home),
      stdio: ["pipe", "pipe", "pipe"],
    });
    const lines = createInterface({ input: child.stdout, crlfDelay: Infinity });
    let result = null;
    let stdoutBytes = 0;
    let stderrBytes = 0;
    let timedOut = false;
    let overflow = false;
    let stdinError = null;
    let killTimer = null;

    const stop = () => {
      child.kill("SIGTERM");
      killTimer = setTimeout(() => child.kill("SIGKILL"), 2_000);
      killTimer.unref?.();
    };
    const onAbort = () => stop();
    signal?.addEventListener("abort", onAbort, { once: true });
    const timer = setTimeout(() => {
      timedOut = true;
      stop();
    }, REQUEST_TIMEOUT_MS);

    lines.on("line", (line) => {
      stdoutBytes += Buffer.byteLength(line) + 1;
      if (stdoutBytes > MAX_STREAM_BYTES) {
        overflow = true;
        stop();
        return;
      }
      let event;
      try {
        event = JSON.parse(line);
      } catch {
        return;
      }
      if (event?.event === "result") result = event.result;
      else if (typeof event?.status === "string") result = event;
      const update = event?.event === "step_update" ? event.step_update : null;
      const delta = update?.step_type === "agent_response" ? update.text_delta : null;
      if (typeof delta === "string" && delta && !isStructuredOutputDelta(delta)) onTextDelta?.(delta);
    });
    child.stderr.on("data", (chunk) => {
      stderrBytes += chunk.length;
      if (stderrBytes > MAX_STREAM_BYTES) {
        overflow = true;
        stop();
      }
    });
    child.stdin.on("error", (error) => {
      if (error?.code !== "EPIPE") stdinError = error;
    });
    child.stdin.end(prompt);
    child.once("error", reject);
    child.once("close", (code, childSignal) => {
      clearTimeout(timer);
      if (killTimer) clearTimeout(killTimer);
      signal?.removeEventListener("abort", onAbort);
      lines.close();
      if (signal?.aborted) {
        reject(Object.assign(new Error("AGY request was cancelled"), { name: "AbortError", code: "ABORT_ERR" }));
      } else if (timedOut) {
        reject(Object.assign(new Error("AGY request timed out"), { code: "AGY_REQUEST_TIMEOUT" }));
      } else if (overflow) {
        reject(Object.assign(new Error("AGY stream exceeded the admitted in-memory limit"), { code: "AGY_STREAM_TOO_LARGE" }));
      } else if (stdinError) {
        reject(Object.assign(new Error("AGY prompt transport failed"), { code: "AGY_PROMPT_TRANSPORT_FAILED", cause: stdinError }));
      } else if (code !== 0 || !result) {
        reject(Object.assign(new Error(`AGY stream ended before a valid result (exit ${code}, signal ${childSignal || "none"})`), { code: "AGY_INVALID_OUTPUT" }));
      } else {
        resolve(result);
      }
    });
  });
  if (parsed.status !== "SUCCESS") {
    throw Object.assign(new Error("AGY upstream did not complete successfully"), { code: "AGY_UPSTREAM_INCOMPLETE" });
  }
  return resolveAgyOutput(parsed, schema);
}

async function callAgyPersistent({ session, prompt, schema, signal, onTextDelta = null }) {
  const parsed = await session.turn({ prompt, signal, onTextDelta });
  if (parsed.status !== "SUCCESS") {
    throw Object.assign(new Error("AGY upstream did not complete successfully"), { code: "AGY_UPSTREAM_INCOMPLETE" });
  }
  return resolveAgyOutput(parsed, schema);
}

function messageText(agy, output) {
  const response = String(agy.response || "").trim();
  const structured = String(output.text || "").trim();
  // The schema-validated message is the contract surface. Some AGY versions
  // append provider metadata to `response`; exposing that raw JSON would turn
  // transport metadata into user-visible answer text.
  if (output.kind === "message" && structured) return structured;
  if (!response) return structured;
  const serialized = JSON.stringify(output);
  if (response.endsWith(serialized)) {
    return response.slice(0, -serialized.length).trim() || structured;
  }
  const lines = response.split("\n");
  try {
    if (JSON.stringify(JSON.parse(lines.at(-1))) === JSON.stringify(output)) {
      return lines.slice(0, -1).join("\n").trim() || structured;
    }
  } catch {
    // The final line is ordinary response text.
  }
  return response;
}

async function repairArguments(context, definition, output) {
  const schema = definition.parameters || { type: "object", additionalProperties: true };
  const prompt = [
    `Return only valid JSON arguments for tool ${definition.alias}.`,
    `TOOL SCHEMA:\n${JSON.stringify(schema)}`,
    `INVALID ARGUMENTS:\n${JSON.stringify(output.arguments || {})}`,
  ].join("\n\n");
  const ephemeral = await prepareEphemeralHome();
  try {
    const repaired = await callAgyOneShot({
      home: ephemeral.home,
      mediaDir: ephemeral.mediaDir,
      model: context.model,
      signal: context.signal,
      prompt,
      schema,
      imagePaths: new Map(),
    });
    return repaired.structured_output;
  } finally {
    await ephemeral.cleanup();
  }
}

function completedEvent(responseId, usage = {}) {
  const input = Number(usage.input_tokens || 0);
  const output = Number(usage.output_tokens || 0);
  return {
    type: "response.completed",
    response: {
      id: responseId,
      usage: {
        input_tokens: input,
        input_tokens_details: {
          cached_tokens: Math.max(0, Number(usage.cache_read_tokens || 0)),
        },
        output_tokens: output,
        output_tokens_details: null,
        total_tokens: Number(usage.total_tokens || input + output),
      },
    },
  };
}

async function runAgy(body, summaryOnly, emit, signal, sessionKey) {
  if (body.reasoning?.effort && body.reasoning.effort !== AGY_REASONING_EFFORT) {
    throw Object.assign(
      new Error(`AGY route requires reasoning effort ${AGY_REASONING_EFFORT}`),
      { code: "AGY_REASONING_EFFORT_MISMATCH", status: 422 },
    );
  }
  const model = canonicalAgyModel(String(body.model || ""));
  const usePersistent = PERSISTENT_SESSIONS_ENABLED
    && !summaryOnly
    && typeof sessionKey === "string"
    && !sessionKey.startsWith("http-");
  const instructionDigest = createHash("sha256").update(String(body.instructions || "")).digest("hex");
  const { available, catalogIdentity } = availableCatalog(body);
  const rejectionDigest = usePersistent ? createHash("sha256").update(JSON.stringify([
    model, instructionDigest, catalogIdentity, body.tool_choice ?? null,
    Boolean(body.agy_linear_extension), callerDigests(body.input),
  ])).digest("hex") : null;
  const rejected = failedAgyRequests.get(sessionKey);
  if (rejected && rejected.digest === rejectionDigest && rejected.expiresAt > Date.now()) {
    throw Object.assign(new Error(rejected.message), { code: rejected.code, status: 502 });
  }
  if (rejected) failedAgyRequests.delete(sessionKey);
  let session = usePersistent ? await persistentSessionFor(sessionKey, model) : null;
  if (session?.bootstrapInProgress) {
    throw Object.assign(new Error("AGY session is receiving its cold context"), { code: "AGY_SESSION_BUSY", status: 409 });
  }
  const bindingHeld = body.tool_choice !== "none"
    && Boolean(session?.sameBinding(instructionDigest, catalogIdentity, available));
  let linearExtension = Boolean(
    session
    && session.turnCount > 0
    && body.agy_linear_extension === true
    && bindingHeld,
  );
  let inheritedDefinitions = null;
  if (session && !linearExtension && (!session.canContinue(body.input) || !bindingHeld)) {
    if (bindingHeld && Array.isArray(session.boundDefinitions)) inheritedDefinitions = session.boundDefinitions;
    session = await persistentSessionFor(sessionKey, model, true);
    linearExtension = false;
  }
  const definitions = summaryOnly
    ? []
    : (session?.turnCount > 0 && Array.isArray(session.boundDefinitions)
      ? session.boundDefinitions
      : (Array.isArray(inheritedDefinitions) ? inheritedDefinitions : selectedDefinitions(body)));
  const toolDigest = definitionsDigest(definitions);
  const ephemeral = session?.ephemeral || await prepareEphemeralHome();
  const ownsEphemeral = !session;
  let inferenceStarted = false;
  let bootstrap = null;
  let keepaliveTimer = null;
  let keepaliveEvents = 0;
  const stopKeepalive = () => {
    clearInterval(keepaliveTimer);
    keepaliveTimer = null;
    signal?.removeEventListener("abort", stopKeepalive);
  };
  try {
    const promptInput = linearExtension
      ? session.suffixInput(body.input)
      : (session ? session.continuationInput(body.input) : body.input);
    const imagePaths = await materializeImages(promptInput, ephemeral.mediaDir);
    if (session?.bootstrapInProgress) {
      throw Object.assign(new Error("AGY session is receiving its cold context"), { code: "AGY_SESSION_BUSY", status: 409 });
    }
    const action = latestUserAction(body.input);
    const fastPathDecision = session?.turnCount > 0
      ? {
          enabled: false,
          reason: "persistent_continuation",
          message_count: (Array.isArray(body.input) ? body.input : []).filter((item) => item?.type === "message").length,
          tool_choice_kind: body.tool_choice == null ? "unset" : typeof body.tool_choice,
        }
      : statelessMessageFastPath(body, action, imagePaths, summaryOnly);
    if (session && definitions.length > 0 && fastPathDecision.enabled) {
      fastPathDecision.enabled = false;
      fastPathDecision.reason = "tools_bound";
    }
    const fastPath = fastPathDecision.enabled;
    const policy = actionPolicy(body, definitions, summaryOnly, fastPath);
    const schema = responseSchema(definitions, policy);
    const promptBody = session
      ? { ...body, input: promptInput, instructions: session.promptInstructions(body.instructions) }
      : body;
    const continuation = Boolean(session && session.turnCount > 0);
    const historyBytes = promptHistoryByteBudget(promptBody, definitions, imagePaths, summaryOnly, policy, continuation);
    if (continuation) {
      const deltaInput = stripCodexInternalItems(trimInheritedParentHistory(promptInput));
      const fullInput = linearExtension
        ? [...session.admissionContext, ...deltaInput]
        : stripCodexInternalItems(trimInheritedParentHistory(body.input));
      // canContinue and sameBinding proved this prefix; still validate all pairs and sensitive input.
      boundExternalHistory(fullInput, Math.min(DEFAULT_MAX_EXTERNAL_HISTORY_BYTES, historyBytes), {
        validatedPrefixLength: fullInput.length - deltaInput.length,
        allowProviderContinuation: true,
        includeOptionalHistory: false,
      });
    }
    const renderConversation = (byteBudget) => fastPath
      ? itemText(
          (Array.isArray(promptInput) ? promptInput : []).findLast((item) => item?.role === "user" && !isCanonicalSummary(item)),
          imagePaths,
          new Map(),
        )
      : conversationText(
          promptInput,
          imagePaths,
          promptHistoryBudget(promptBody, definitions, imagePaths, summaryOnly, policy, continuation),
          continuation,
          byteBudget,
        );
    let prompt;
    try {
      const conversation = renderConversation(historyBytes);
      prompt = buildPrompt(promptBody, definitions, conversation, imagePaths, summaryOnly, policy, fastPath, continuation);
    } catch (error) {
      if (!session || continuation || fastPath || summaryOnly
          || error.code !== "EXTERNAL_MANDATORY_CONTEXT_TOO_LARGE"
          || Buffer.byteLength(action.text) > historyBytes) throw error;
      // A cancelled long tool chain must be rebuilt, not treated as received.
      // Admit it against the model budget while retaining every per-message cap.
      const conversation = renderConversation(MAX_AGY_COLD_CONTEXT_BYTES);
      prompt = buildPrompt(promptBody, definitions, conversation, imagePaths, summaryOnly, policy,
        false, false, MAX_AGY_COLD_CONTEXT_BYTES);
      bootstrap = coldBootstrap(prompt);
      if (bootstrap.stages.some((stage) => Buffer.byteLength(stage.prompt) > MAX_AGY_PROMPT_BYTES)) {
        throw Object.assign(new Error("AGY bootstrap stage exceeds its CLI byte ceiling"), {
          code: "AGY_PROMPT_BYTE_LIMIT", status: 413,
        });
      }
      prompt = bootstrap.commit;
      session.bootstrapInProgress = true;
    }
    const context = { home: ephemeral.home, mediaDir: ephemeral.mediaDir, model, signal };
    const responseId = summaryOnly ? null : `resp_agy_${randomUUID()}`;
    const messageId = summaryOnly ? null : `msg_agy_${randomUUID()}`;
    const statusId = summaryOnly ? null : `rs_agy_${randomUUID()}`;
    const statusText = "AGY processing";
    const outputIndex = summaryOnly ? 0 : 1;
    let messageStarted = false;
    let streamedText = "";
    if (responseId) {
      emit({ type: "response.created", response: { id: responseId } });
      emit({
        switchboard_public_status: PUBLIC_STATUS_MARKER,
        type: "response.output_item.added",
        output_index: 0,
        item: {
          type: "reasoning",
          id: statusId,
          summary: [],
          encrypted_content: null,
        },
      });
      emit({ switchboard_public_status: PUBLIC_STATUS_MARKER, type: "response.reasoning_summary_part.added", item_id: statusId, output_index: 0, summary_index: 0 });
      emit({ switchboard_public_status: PUBLIC_STATUS_MARKER, type: "response.reasoning_summary_text.delta", item_id: statusId, output_index: 0, summary_index: 0, delta: statusText });
      if (!signal?.aborted) {
        // Pending locally, not provider progress or usage. Data frames reset both SSE and WS idle readers.
        keepaliveTimer = setInterval(() => {
          if (signal?.aborted) return stopKeepalive();
          try {
            emit({ type: "response.in_progress", response: { id: responseId, status: "in_progress" } });
            keepaliveEvents += 1;
          } catch {
            stopKeepalive();
          }
        }, KEEPALIVE_INTERVAL_MS);
        keepaliveTimer.unref?.();
        signal?.addEventListener("abort", stopKeepalive, { once: true });
      }
    }
    // Persistent AGY step deltas can contain provider-side intermediate drafts,
    // so publish only the locally validated terminal message.
    const onTextDelta = summaryOnly || policy !== "message_only" || session ? null : (delta) => {
        if (!messageStarted) {
          emit({
            type: "response.output_item.added",
            item: {
              type: "message",
              role: "assistant",
              id: messageId,
              content: [{ type: "output_text", text: "" }],
            },
            output_index: outputIndex,
          });
          messageStarted = true;
        }
        streamedText += delta;
        emit({
          type: "response.output_text.delta",
          item_id: messageId,
          output_index: outputIndex,
          content_index: 0,
          delta,
        });
      };
    inferenceStarted = true;
    const bootstrapUsage = {};
    for (const stage of bootstrap?.stages || []) {
      const accepted = await callAgyPersistent({ session, prompt: stage.prompt, schema: responseSchema([], "message_only"), signal })
        .catch((error) => {
          if (error.code !== "AGY_INVALID_STRUCTURED_OUTPUT") throw error;
          throw Object.assign(new Error("AGY cold context fragment was not acknowledged; task not committed"), {
            code: "AGY_CONTEXT_STAGE_NOT_ACKNOWLEDGED", status: 409,
          });
        });
      if (accepted.structured_output?.kind !== "message" || accepted.structured_output.text !== stage.ack) {
        throw Object.assign(new Error("AGY cold context fragment was not acknowledged; task not committed"), {
          code: "AGY_CONTEXT_STAGE_NOT_ACKNOWLEDGED", status: 409,
        });
      }
      for (const [key, value] of Object.entries(accepted.usage || {})) {
        if (Number.isFinite(value)) bootstrapUsage[key] = (bootstrapUsage[key] || 0) + value;
      }
    }
    const agy = session
      ? await callAgyPersistent({ session, prompt, schema, signal, onTextDelta })
      : await callAgyOneShot({ ...context, prompt, schema: fastPath ? null : schema, imagePaths, onTextDelta });
    for (const [key, value] of Object.entries(bootstrapUsage)) {
      agy.usage[key] = (agy.usage[key] || 0) + value;
    }
    let result = agy.structured_output;
    if (policy === "message_only" && result.kind === "message") {
      result = { kind: "message", text: result.text };
    } else if (policy === "tool_required" && result.kind === "tool_call") {
      result = {
        kind: "tool_call",
        tool_name: result.tool_name,
        arguments: result.arguments,
        ...(result.input ? { input: result.input } : {}),
      };
    } else if (policy === "auto") {
      result = result.kind === "message"
        ? { kind: "message", text: result.text, tool_name: "__none__", arguments: {}, input: "" }
        : { text: "", input: "", ...result };
    }
    if (policy === "tool_required" && result.kind !== "tool_call") {
      throw Object.assign(
        new Error("AGY did not return the required caller-executed tool call"),
        { code: "AGY_REQUIRED_TOOL_CALL_MISSING", status: 502 },
      );
    }
    const validateResult = ajv.compile(schema);
    if (!validateResult(result)) {
      throw Object.assign(new Error("AGY output failed the active response contract"), {
        code: "AGY_INVALID_STRUCTURED_OUTPUT",
      });
    }
    const text = messageText(agy, result).trim();
    if (summaryOnly) {
      if (!text) throw Object.assign(new Error("AGY returned an empty canonical summary"), { code: "AGY_COMPACT_EMPTY" });
      return { summary: text, usage: agy.usage || {} };
    }
    emit({
      switchboard_public_status: PUBLIC_STATUS_MARKER,
      type: "response.reasoning_summary_text.done",
      item_id: statusId,
      output_index: 0,
      summary_index: 0,
      text: statusText,
    });
    emit({
      switchboard_public_status: PUBLIC_STATUS_MARKER,
      type: "response.output_item.done",
      output_index: 0,
      item: {
        type: "reasoning",
        id: statusId,
        summary: [{ type: "summary_text", text: statusText }],
        encrypted_content: null,
      },
    });
    const pendingCalls = [];
    if (result.kind === "tool_call") {
      if (messageStarted) {
        emit({
          type: "response.output_item.done",
          output_index: outputIndex,
          item: {
            type: "message",
            role: "assistant",
            id: messageId,
            content: [{ type: "output_text", text: streamedText.trim() }],
          },
        });
      }
      const definition = definitions.find((item) => item.alias === result.tool_name);
      if (!definition) throw Object.assign(new Error("AGY selected an unavailable tool"), { code: "AGY_INVALID_TOOL" });
      let args = result.arguments && typeof result.arguments === "object"
        ? result.arguments
        : (result.input ? { input: result.input } : {});
      let validate = ajv.compile(definition.parameters || { type: "object" });
      if (!validate(args)) {
        args = await repairArguments(context, definition, result);
        validate = ajv.compile(definition.parameters || { type: "object" });
        if (!validate(args)) throw Object.assign(new Error("AGY tool arguments failed schema validation"), { code: "AGY_INVALID_TOOL_ARGUMENTS" });
      }
      const callId = `call_agy_${randomUUID()}`;
      const bridgeMap = new Map(definitions.map((item) => [item.alias, item]));
      const baseItem = {
        type: "function_call",
        call_id: callId,
        name: definition.alias,
        arguments: JSON.stringify(args),
      };
      const item = restoreToolItem(baseItem, bridgeMap);
      pendingCalls.push(item);
      emit({ type: "response.output_item.done", output_index: outputIndex + (messageStarted ? 1 : 0), item });
    } else {
      if (!text) throw Object.assign(new Error("AGY returned an empty message"), { code: "AGY_EMPTY_MESSAGE" });
      emit({
        type: "response.output_item.done",
        output_index: outputIndex,
        item: {
          type: "message",
          role: "assistant",
          id: messageId,
          content: [{ type: "output_text", text }],
        },
      });
    }
    stopKeepalive();
    emit(completedEvent(responseId, agy.usage));
    await session?.remember(body, toolDigest, catalogIdentity, definitions, available, linearExtension, pendingCalls);
    return {
      status: 200,
      transport: session ? "agy-cli-persistent" : (fastPath ? "agy-cli-fast" : "agy-cli"),
      tool_projection: {
        diagnostic: `agy_fast_path:${fastPathDecision.reason}${bootstrap
          ? `;cold_context_parts=${bootstrap.stages.length};cold_context_bytes=${bootstrap.bytes};cold_context_sha256=${bootstrap.identity.prompt_sha256}`
          : ""}${keepaliveEvents ? `;liveness_events=${keepaliveEvents};liveness_interval_ms=${KEEPALIVE_INTERVAL_MS}` : ""}`,
        agy_fast_path: fastPath,
        agy_fast_path_reason: fastPathDecision.reason,
        agy_message_count: fastPathDecision.message_count,
        agy_tool_choice_kind: fastPathDecision.tool_choice_kind,
        ...(session ? {
          agy_persistent_session: true,
          agy_session_reused: bootstrap ? false : Boolean(agy.persistent_session?.reused),
          agy_session_turn_index: agy.persistent_session?.turn_index || 0,
          agy_prefix_reused: continuation,
          ...(bootstrap ? {
            agy_bootstrap_parts: bootstrap.stages.length,
            agy_bootstrap_bytes: bootstrap.bytes,
            agy_bootstrap_sha256: bootstrap.identity.prompt_sha256,
          } : {}),
        } : {}),
      },
    };
  } catch (error) {
    stopKeepalive();
    if (inferenceStarted && rejectionDigest
        && ["AGY_INVALID_STRUCTURED_OUTPUT", "AGY_UPSTREAM_INCOMPLETE"].includes(error?.code)) {
      failedAgyRequests.delete(sessionKey);
      failedAgyRequests.set(sessionKey, {
        digest: rejectionDigest,
        code: error.code,
        message: String(error.message).slice(0, 2048),
        expiresAt: Date.now() + SESSION_IDLE_MS,
      });
      while (failedAgyRequests.size > 64)
        failedAgyRequests.delete(failedAgyRequests.keys().next().value);
    }
    // A thread session busy with another owner's turn is not this turn's to close.
    if (session && (inferenceStarted || session.turnCount === 0) && error?.code !== "AGY_SESSION_BUSY")
      await closePersistentSession(sessionKey, "failed_turn");
    throw error;
  } finally {
    stopKeepalive();
    if (bootstrap) session.bootstrapInProgress = false;
    if (ownsEphemeral) await ephemeral.cleanup();
  }
}

function send(message) {
  if (process.send) process.send(message);
}

const activeSessions = new Map();
const activeWork = new Set();
let terminating = false;

function registerSession(sessionKey, controller) {
  const controllers = activeSessions.get(sessionKey) || new Set();
  controllers.add(controller);
  activeSessions.set(sessionKey, controllers);
}

function unregisterSession(sessionKey, controller) {
  const controllers = activeSessions.get(sessionKey);
  if (!controllers) return;
  controllers.delete(controller);
  if (controllers.size === 0) activeSessions.delete(sessionKey);
}

process.on("message", (job) => {
  if (!job || typeof job.id !== "string") return;
  if (job.op === "close_session") {
    for (const controller of activeSessions.get(job.session_key) || []) controller.abort();
    closePersistentSession(job.session_key, "downstream_closed").then(
      () => send({ id: job.id, kind: "done", result: { status: 200 } }),
      (error) => send({
        id: job.id,
        kind: "error",
        error: safeError(error.code || "AGY_SESSION_CLOSE_FAILED", error.message || "AGY session close failed"),
      }),
    );
    return;
  }
  if (job.op === "crash" && process.env.SWITCHBOARD_TEST_ALLOW_CRASH === "1") process.exit(87);
  const sessionKey = job.session_key || job.id;
  // Closing an owner (a connection or request) cancels its jobs only; closing
  // the session key also closes the thread session itself.
  const ownerKey = job.owner_key || sessionKey;
  const controller = new AbortController();
  registerSession(sessionKey, controller);
  if (ownerKey !== sessionKey) registerSession(ownerKey, controller);
  const events = [];
  const work = runAgy(job.body, job.op === "compact", (event) => {
    events.push(event);
    send({ id: job.id, kind: "event", event });
  }, controller.signal, sessionKey);
  const completion = Promise.resolve(work).then(
    (result) => send({ id: job.id, kind: "done", result }),
    (error) => {
      const cancelled = controller.signal.aborted || error.name === "AbortError" || error.code === "ABORT_ERR";
      send({
        id: job.id,
        kind: "error",
        error: cancelled
          ? safeError("AGY_REQUEST_CANCELLED", "AGY request was cancelled when the downstream session closed.", 499)
          : safeError(error.code || "AGY_WORKER_ERROR", error.message || "AGY worker failed", error.status || 502, error.details),
      });
    },
  ).finally(() => {
    unregisterSession(sessionKey, controller);
    unregisterSession(ownerKey, controller);
  });
  activeWork.add(completion);
  completion.then(
    () => activeWork.delete(completion),
    () => activeWork.delete(completion),
  );
});

process.on("SIGTERM", () => {
  if (terminating) return;
  terminating = true;
  for (const controllers of activeSessions.values()) {
    for (const controller of controllers) controller.abort();
  }
  const closingSessions = [...persistentSessions.keys()].map(
    (sessionKey) => closePersistentSession(sessionKey, "worker_shutdown"),
  );
  const guard = setTimeout(() => process.exit(1), 5_000);
  guard.unref?.();
  Promise.allSettled([...activeWork, ...closingSessions]).then(() => {
    clearTimeout(guard);
    process.exit(0);
  });
});
