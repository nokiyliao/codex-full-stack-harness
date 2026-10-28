import { createHash, randomUUID } from "node:crypto";

import {
  isCanonicalSummary,
  isCompactionItem,
  trustedProviderStateMarker,
} from "./history-neutral.mjs";
import { SwitchboardError } from "./shared.mjs";
import {
  recoverAgentMessageText,
  rememberSpawnPayloads,
} from "./spawn-payload-cache.mjs";

const MAX_EXTERNAL_INSTRUCTIONS_CHARS = 24 * 1024;
const MAX_EXTERNAL_TOOLS = 24;
// xAI documents 350 tools per request; Codex has sent at most about 100.
const MAX_XAI_TOOLS = 128;
const MAX_TOOL_DIAGNOSTIC_COUNT = 999;
export const DEFAULT_MAX_EXTERNAL_HISTORY_BYTES = Number(
  process.env.SWITCHBOARD_MAX_EXTERNAL_HISTORY_BYTES || 256 * 1024,
);

export function boundedExternalInstructions(value, maxChars = MAX_EXTERNAL_INSTRUCTIONS_CHARS) {
  const text = String(value || "");
  if (text.length <= maxChars) return text;
  const blocks = text.split(/\n{2,}/).map((block) => block.trim()).filter(Boolean);
  const unique = [];
  const seen = new Set();
  for (const block of blocks) {
    const digest = createHash("sha256").update(block).digest("hex");
    if (seen.has(digest)) continue;
    seen.add(digest);
    unique.push(block);
  }
  const deduplicated = unique.join("\n\n");
  if (deduplicated.length <= maxChars) return deduplicated;
  const safetyPattern = /\b(?:must|never|do not|forbid|authority|permission|sandbox|approval|credential|secret|broker|order|trading|runtime|goal|lease|session|task)\b|(?:必須|不得|禁止|權限|授權|憑證|密鑰|交易|下單|實盤|租約|工作階段|任務)/i;
  const markerFor = (selected) => {
    const omitted = unique.filter((_, index) => !selected.has(index));
    const payload = {
      schema_version: "external_instruction_omission_v1",
      block_count: omitted.length,
      char_count: omitted.reduce((total, block) => total + block.length, 0),
      sha256: createHash("sha256").update(omitted.join("\n\n")).digest("hex"),
      authority_expansion: "denied",
    };
    return `[external_instruction_omission_v1 ${JSON.stringify(payload)}]`;
  };
  const assemble = (selected) => {
    const result = [];
    let markerInserted = false;
    for (let index = 0; index < unique.length; index += 1) {
      if (selected.has(index)) result.push(unique[index]);
      else if (!markerInserted) {
        result.push(markerFor(selected));
        markerInserted = true;
      }
    }
    return result.join("\n\n");
  };
  const ranked = unique.map((block, index) => ({
    index,
    score: (safetyPattern.test(block) ? 100_000 : 0)
      + (index === 0 ? 50_000 : 0)
      + (index >= unique.length - 12 ? 20_000 : 0)
      + index,
  })).sort((left, right) => right.score - left.score || left.index - right.index);
  const selected = new Set();
  let bounded = assemble(selected);
  for (const { index } of ranked) {
    const proposed = new Set(selected);
    proposed.add(index);
    const candidate = assemble(proposed);
    if (candidate.length <= maxChars) {
      selected.add(index);
      bounded = candidate;
    }
  }
  if (bounded.length > maxChars) {
    throw new SwitchboardError(
      "EXTERNAL_INSTRUCTION_MARKER_TOO_LARGE",
      "External instruction omission metadata exceeds the caller guidance budget.",
      413,
    );
  }
  return bounded;
}

function aliasFor(namespace, name, responseType) {
  const source = `${responseType}:${namespace || ""}:${name}`;
  const stem = `${namespace ? `${namespace}__` : ""}${name}`
    .replace(/[^A-Za-z0-9_-]/g, "_")
    .slice(0, 48);
  return `${stem}__${createHash("sha256").update(source).digest("hex").slice(0, 10)}`;
}

function definitionFromTool(tool, namespace = null) {
  if (tool?.type === "function" && typeof tool.name === "string") {
    return {
      alias: aliasFor(namespace, tool.name, "function_call"),
      namespace,
      responseName: tool.name,
      responseType: "function_call",
      description: tool.description || "",
      parameters: tool.parameters || tool.input_schema || tool.inputSchema || { type: "object" },
    };
  }
  if (tool?.type === "custom" && typeof tool.name === "string") {
    return {
      alias: aliasFor(namespace, tool.name, "custom_tool_call"),
      namespace,
      responseName: tool.name,
      responseType: "custom_tool_call",
      description: tool.description || "",
      parameters: {
        type: "object",
        properties: { input: { type: "string" } },
        required: ["input"],
        additionalProperties: false,
      },
    };
  }
  return null;
}

function isOfficialExecTool(tool) {
  return (tool?.type === "custom" || tool?.type === "function")
    && (tool.name === "exec" || tool.name === "exec_command");
}

function execAdapterDefinitions(tool, namespace = null) {
  if (!isOfficialExecTool(tool)) return [];
  const officialType = tool.type === "function" ? "function_call" : "custom_tool_call";
  const officialName = tool.name === "exec_command" ? "exec_command" : "exec";
  return [
    {
      alias: "exec_command",
      namespace,
      responseName: "exec_command",
      targetResponseName: officialName,
      responseType: officialType,
      adapter: "exec_command",
      description: "Run a shell command in a specified working directory and return output or a session ID for a long-running command.",
      parameters: {
        type: "object",
        properties: {
          cmd: { type: "string" },
          workdir: { type: "string" },
          yield_time_ms: { type: "integer", minimum: 250, maximum: 30000 },
          max_output_tokens: { type: "integer", minimum: 1 },
          tty: { type: "boolean" },
          login: { type: "boolean" },
          shell: { type: "string" },
          sandbox_permissions: { type: "string", enum: ["use_default", "require_escalated"] },
          justification: { type: "string" },
          prefix_rule: { type: "array", items: { type: "string" } },
        },
        required: ["cmd"],
        additionalProperties: false,
      },
    },
    {
      alias: "write_stdin",
      namespace,
      responseName: "write_stdin",
      targetResponseName: officialName,
      responseType: officialType,
      adapter: "write_stdin",
      description: "Continue or poll a long-running exec_command session by its session ID.",
      parameters: {
        type: "object",
        properties: {
          session_id: { type: "integer", minimum: 1 },
          chars: { type: "string" },
          yield_time_ms: { type: "integer", minimum: 250, maximum: 300000 },
          max_output_tokens: { type: "integer", minimum: 1 },
        },
        required: ["session_id"],
        additionalProperties: false,
      },
    },
  ];
}

export function discoveredTools(input) {
  return (Array.isArray(input) ? input : [])
    .filter((item) => item?.type === "tool_search_output" && Array.isArray(item.tools))
    .flatMap((item) => item.tools);
}

export function additionalTools(input) {
  return (Array.isArray(input) ? input : [])
    .filter((item) => item?.type === "additional_tools" && Array.isArray(item.tools))
    .flatMap((item) => item.tools);
}

function boundedToolCount(value) {
  return Math.min(Array.isArray(value) ? value.length : 0, MAX_TOOL_DIAGNOSTIC_COUNT);
}

function hasTopLevelCustomExec(tools) {
  return (Array.isArray(tools) ? tools : []).some((tool) => tool?.type === "custom"
    && tool.name === "exec"
    && typeof tool.namespace !== "string");
}

function hasNamespacedCustomExec(tools) {
  return (Array.isArray(tools) ? tools : []).some((tool) => (tool?.type === "custom"
    && tool.name === "exec"
    && typeof tool.namespace === "string")
    || (tool?.type === "namespace" && (Array.isArray(tool.tools) ? tool.tools : [])
      .some((member) => member?.type === "custom" && member.name === "exec")));
}

export function externalToolShape(body) {
  const topLevel = Array.isArray(body?.tools) ? body.tools : [];
  const additional = additionalTools(body?.input);
  const declared = [...topLevel, ...additional];
  const discovered = discoveredTools(body?.input);
  const namespaces = declared.filter((tool) => tool?.type === "namespace");
  return {
    tool_count: boundedToolCount(declared),
    additional_tool_count: boundedToolCount(additional),
    namespace_count: boundedToolCount(namespaces),
    namespace_member_count: Math.min(
      namespaces.reduce((total, tool) => total + boundedToolCount(tool.tools), 0),
      MAX_TOOL_DIAGNOSTIC_COUNT,
    ),
    discovered_tool_count: boundedToolCount(discovered),
    custom_exec_top_level: hasTopLevelCustomExec(declared),
    custom_exec_namespaced: hasNamespacedCustomExec(declared),
    custom_exec_discovered: hasTopLevelCustomExec(discovered) || hasNamespacedCustomExec(discovered),
  };
}

export function flattenTools(tools, input = []) {
  const definitions = [];
  for (const tool of [
    ...(Array.isArray(tools) ? tools : []),
    ...additionalTools(input),
    ...discoveredTools(input),
  ]) {
    if (tool?.type === "namespace" && typeof tool.name === "string") {
      for (const member of Array.isArray(tool.tools) ? tool.tools : []) {
        const adapters = execAdapterDefinitions(member, tool.name);
        if (adapters.length) {
          definitions.push(...adapters);
          continue;
        }
        const definition = definitionFromTool(member, tool.name);
        if (definition) definitions.push(definition);
      }
      continue;
    }
    if (tool?.type === "tool_search") {
      definitions.push({
        alias: aliasFor(null, "tool_search", "tool_search_call"),
        namespace: null,
        responseName: "tool_search",
        responseType: "tool_search_call",
        description: tool.description || "Discover additional Codex tools.",
        parameters: tool.parameters || tool.input_schema || {
          type: "object",
          properties: {
            query: { type: "string" },
            limit: { type: "integer" },
          },
          required: ["query"],
          additionalProperties: false,
        },
      });
      continue;
    }
    const namespace = typeof tool?.namespace === "string" ? tool.namespace : null;
    const adapters = execAdapterDefinitions(tool, namespace);
    if (adapters.length) {
      definitions.push(...adapters);
      continue;
    }
    const definition = definitionFromTool(tool, namespace);
    if (definition) definitions.push(definition);
  }
  const unique = new Map();
  const canonicalJson = (value) => {
    if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
    if (value && typeof value === "object") {
      return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
    }
    return JSON.stringify(value);
  };
  for (const definition of definitions) {
    const existing = unique.get(definition.alias);
    if (existing) {
      const identity = (value) => canonicalJson({
        responseType: value.responseType,
        namespace: value.namespace || null,
        responseName: value.responseName,
        targetResponseName: value.targetResponseName || null,
        description: value.description || "",
        parameters: value.parameters || {},
        adapter: value.adapter || null,
      });
      if (identity(existing) !== identity(definition)) {
        throw new SwitchboardError(
          "EXTERNAL_TOOL_IDENTITY_CONFLICT",
          `Conflicting definitions were supplied for external tool ${definition.alias}.`,
          409,
        );
      }
      continue;
    }
    unique.set(definition.alias, definition);
  }
  const bridge = unique;
  return {
    bridge,
    tools: [...unique.values()].map((definition) => ({
      type: "function",
      name: definition.alias,
      description: definition.description,
      parameters: definition.parameters,
    })),
  };
}

function isCompetingExecDefinition(definition) {
  const identity = `${definition.namespace || ""}_${definition.responseName}`;
  return /(?:^|[_-])repl(?:$|[_-])/i.test(identity) || /devspace/i.test(identity);
}

function recentNameHit(recent, definition) {
  const names = [definition.responseName, definition.targetResponseName]
    .filter(Boolean)
    .map((name) => String(name).toLowerCase());
  return names.some((name) => recent.includes(`\"name\":\"${name}\"`));
}

export function selectDefinitions(definitions, input, limit = MAX_EXTERNAL_TOOLS) {
  const items = Array.isArray(input) ? input : [];
  const latestUser = [...items].reverse().find((item) => item?.type === "message" && item?.role === "user");
  const query = JSON.stringify(latestUser || "").toLowerCase();
  const recent = JSON.stringify(items.slice(-16)).toLowerCase();
  const explicitExec = /\bexec[_-]?command\b/.test(query);
  const terms = new Set(query.match(/[a-z0-9_]{3,}/g) || []);
  const actionPatterns = [];
  if (/\b(?:create|edit|write|save|store|persist|modify|apply|fix|repair)\b/i.test(query)
      || /(?:建立|創建|新增|寫入|寫檔|修改|實作|修好|修復|儲存|存入|落盤)/.test(query)) {
    actionPatterns.push(/(?:apply[_-]?patch|exec[_-]?command|run[_-]?command|shell|write|file|patch)/i);
  }
  if (/\b(?:read|inspect|verify|test|run|execute)\b/i.test(query)
      || /(?:讀取|檢查|驗證|測試|執行)/.test(query)) {
    actionPatterns.push(/(?:exec[_-]?command|run[_-]?command|shell|read|view|file|stat)/i);
  }
  const ranked = definitions
    .map((definition, index) => {
      const haystack = `${definition.namespace || ""} ${definition.responseName} ${definition.description}`.toLowerCase();
      let score = definition.responseType === "tool_search_call" ? 10_000 : 0;
      if (actionPatterns.some((pattern) => pattern.test(haystack))) score += 20_000;
      if (recentNameHit(recent, definition)) score += 5_000;
      for (const term of terms) if (haystack.includes(term)) score += 1;
      return { definition, index, score };
    })
    .sort((a, b) => b.score - a.score || a.index - b.index);
  const pinned = ranked.filter((item) => item.definition.adapter);
  const adaptersPresent = pinned.length > 0;
  const candidates = ranked.filter((item) => {
    if (item.definition.adapter) return false;
    if (adaptersPresent && isCompetingExecDefinition(item.definition)) return false;
    if (explicitExec && item.definition.responseType === "tool_search_call") return false;
    return true;
  });
  const relevant = candidates.filter((item) => item.score > 0);
  const selected = relevant.length ? relevant : candidates.slice(0, Math.min(8, limit));
  const remaining = Math.max(0, limit - pinned.length);
  return [...pinned, ...selected.slice(0, remaining)]
    .map(({ definition }) => definition);
}

// Grok gets the caller's whole tool list in the caller's order, as Claude
// does, so the cached prompt prefix does not change with the wording of each
// message. Exec adapters still replace competing shells. Only a list past the
// provider cap falls back to relevance selection.
export function stableDefinitions(definitions, input, limit = MAX_XAI_TOOLS) {
  const pinned = definitions.filter((definition) => definition.adapter);
  const rest = definitions.filter((definition) => !definition.adapter
    && !(pinned.length && isCompetingExecDefinition(definition)));
  const all = [...pinned, ...rest];
  return all.length <= limit ? all : selectDefinitions(definitions, input, limit);
}

function parseArguments(value) {
  if (value && typeof value === "object" && !Array.isArray(value)) return value;
  if (typeof value !== "string") return {};
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

export function restoreToolItem(item, bridge) {
  if (item?.type !== "function_call") return item;
  const definition = bridge.get(item.name);
  if (!definition) return item;
  if (definition.responseType === "tool_search_call") {
    const args = parseArguments(item.arguments);
    return {
      type: "tool_search_call",
      ...(item.id ? { id: item.id } : {}),
      call_id: item.call_id,
      execution: "client",
      status: item.status || "completed",
      arguments: args.query ? args : { query: "Codex tools", limit: 8 },
    };
  }
  if (definition.responseType === "custom_tool_call") {
    const args = parseArguments(item.arguments);
    const input = definition.adapter
      ? `const result = await tools.${definition.adapter}(${JSON.stringify(args)}); text(result);`
      : String(args.input || "");
    return {
      type: "custom_tool_call",
      call_id: item.call_id,
      name: definition.targetResponseName || definition.responseName,
      ...(definition.namespace ? { namespace: definition.namespace } : {}),
      input,
    };
  }
  return {
    ...item,
    name: definition.targetResponseName || definition.responseName,
    ...(definition.namespace ? { namespace: definition.namespace } : {}),
  };
}

export function restoreEvent(event, bridge) {
  const copy = structuredClone(event);
  if (copy?.item) copy.item = restoreToolItem(copy.item, bridge);
  if (Array.isArray(copy?.response?.output)) {
    copy.response.output = copy.response.output.map((item) => restoreToolItem(item, bridge));
  }
  return copy;
}

function outputText(output) {
  if (typeof output === "string") return output;
  if (!Array.isArray(output)) return JSON.stringify(output ?? "");
  return output
    .map((item) => item?.text || item?.input_text || item?.output_text || JSON.stringify(item))
    .filter(Boolean)
    .join("\n");
}

const CODEX_INTERNAL_TYPES = new Set([
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

const VALID_PROVIDER_ITEM_TYPES = new Set([
  "message",
  "function_call",
  "function_call_output",
  "compaction",
]);

export function normalizeHistory(input, bridge) {
  const reverse = new Map();
  for (const definition of bridge.values()) {
    reverse.set(`${definition.namespace || ""}:${definition.responseName}:${definition.responseType}`, definition);
  }
  rememberSpawnPayloads(input);
  const cursor = new Map();
  return trimInheritedParentHistory(input)
    .filter((item) => {
      if (!item || typeof item !== "object") return false;
      if (CODEX_INTERNAL_TYPES.has(item.type)) return false;
      if (item.type === "compaction" && item[trustedProviderStateMarker] !== true) return false;
      return true;
    })
    .map((item) => {
      const agentMessage = canonicalAgentMessage(item, cursor);
      if (agentMessage) return agentMessage;
      if (item?.type === "function_call") {
        const definition = reverse.get(`${item.namespace || ""}:${item.name}:function_call`);
        return {
          type: "function_call",
          call_id: item.call_id,
          name: definition?.alias || item.name,
          arguments: typeof item.arguments === "string" ? item.arguments : JSON.stringify(item.arguments || {}),
        };
      }
      if (item?.type === "custom_tool_call") {
        const definition = reverse.get(`${item.namespace || ""}:${item.name}:custom_tool_call`);
        let argsJson = JSON.stringify({ input: item.input || "" });
        if (definition?.adapter && typeof item.input === "string") {
          const match = item.input.match(new RegExp(`tools\\.${definition.adapter}\\((.*)\\);?\\s*text\\(result\\);?`));
          if (match && match[1]) {
            try {
              JSON.parse(match[1]);
              argsJson = match[1];
            } catch {}
          }
        }
        return {
          type: "function_call",
          call_id: item.call_id,
          name: definition?.alias || item.name,
          arguments: argsJson,
        };
      }
      if (item?.type === "function_call_output" || item?.type === "custom_tool_call_output") {
        return {
          type: "function_call_output",
          call_id: item.call_id,
          output: outputText(item.output),
        };
      }
      if (item?.type === "tool_search_call") {
        const definition = [...bridge.values()].find((candidate) => candidate.responseType === "tool_search_call");
        return {
          type: "function_call",
          call_id: item.call_id,
          name: definition?.alias || "tool_search",
          arguments: JSON.stringify(item.arguments || {}),
        };
      }
      if (item?.type === "tool_search_output") {
        return {
          type: "function_call_output",
          call_id: item.call_id,
          output: JSON.stringify({ tools: item.tools || [] }),
        };
      }
      if (item?.role && item?.content && !item?.type) {
        return { type: "message", ...item };
      }
      return item;
    })
    .filter((item) => VALID_PROVIDER_ITEM_TYPES.has(item?.type));
}

function stripPrivateState(value) {
  if (Array.isArray(value)) return value.map(stripPrivateState).filter((item) => item !== undefined);
  if (!value || typeof value !== "object") return value;
  if (value.type === "reasoning") return undefined;
  const clean = {};
  for (const [key, child] of Object.entries(value)) {
    if (key === trustedProviderStateMarker) continue;
    if (key === "encrypted_content"
      && value.type === "compaction"
      && value[trustedProviderStateMarker] === true) {
      clean[key] = child;
      continue;
    }
    if ([
      "encrypted_content",
      "api_key",
      "apiKey",
      "authorization",
      "cookie",
      "password",
      "secret",
      "token",
      "client_metadata",
      "prompt_cache_key",
      "safety_identifier",
      "service_tier",
      "stream",
      "background",
      "previous_response_id",
    ].includes(key)) continue;
    const sanitized = stripPrivateState(child);
    if (sanitized !== undefined) clean[key] = sanitized;
  }
  return clean;
}

function canonicalId(prefix) {
  return `${prefix}_ext_${randomUUID().replaceAll("-", "")}`;
}

function itemIdPrefix(type) {
  if (type === "message") return "msg";
  if (type === "function_call" || type === "custom_tool_call" || type === "tool_search_call") return "fc";
  return "item";
}

function canonicalAgentMessage(item, cursor = new Map()) {
  const text = recoverAgentMessageText(item, cursor);
  if (!text) return null;
  return {
    type: "message",
    role: "user",
    content: [{ type: "input_text", text }],
  };
}

function isNewTaskEnvelope(item) {
  if (item?.type !== "agent_message" || !Array.isArray(item.content)) return false;
  return item.content.some((part) => /Message Type:\s*NEW_TASK/i.test(part?.text || part?.input_text || ""));
}

export function trimInheritedParentHistory(input) {
  const items = Array.isArray(input) ? input : [];
  const taskIndex = items.findIndex(isNewTaskEnvelope);
  if (taskIndex < 0) return items;
  return items.filter((item, index) => {
    if (index >= taskIndex) return true;
    return item?.type === "message" && (item.role === "developer" || item.role === "system");
  });
}

export function boundExternalHistory(
  input,
  maxBytes = DEFAULT_MAX_EXTERNAL_HISTORY_BYTES,
  {
    allowEmptyCurrentRequest = false,
    allowProviderContinuation = false,
    retainAuthority = true,
    includeOptionalHistory = true,
    validatedPrefixLength = 0,
  } = {},
) {
  const source = Array.isArray(input) ? input : [];
  if (!Number.isSafeInteger(validatedPrefixLength) || validatedPrefixLength < 0
      || validatedPrefixLength > source.length
      || (validatedPrefixLength > 0 && !allowProviderContinuation)) {
    throw new SwitchboardError("EXTERNAL_CONTINUATION_PREFIX_INVALID",
      "Only a locally verified provider continuation may exclude a received prefix from its transport budget.", 409);
  }
  const sanitized = source.flatMap((item, index) => {
    const clean = stripPrivateState(item);
    if (clean?.type === "compaction" && item?.[trustedProviderStateMarker] === true) {
      clean[trustedProviderStateMarker] = true;
    }
    return clean === undefined ? [] : [{ item: clean, received: index < validatedPrefixLength }];
  });
  const items = sanitized.map((entry) => entry.item);
  const received = new Set(sanitized.flatMap((entry, index) => entry.received ? [index] : []));
  const byteLength = (value) => Buffer.byteLength(JSON.stringify(value));
  const digest = (value) => createHash("sha256").update(JSON.stringify(value)).digest("hex");
  const isAuthority = (item) => item?.type === "message"
    && (item.role === "developer" || item.role === "system");
  const isUser = (item) => item?.type === "message" && item.role === "user" && !isCanonicalSummary(item);
  const isAssistant = (item) => item?.type === "message" && item.role === "assistant";
  const isToolCall = (item) => item?.type === "function_call" || item?.type === "custom_tool_call";
  const isToolOutput = (item) => item?.type === "function_call_output"
    || item?.type === "custom_tool_call_output";
  const containsCredential = (item) => {
    const text = JSON.stringify(item);
    return /\bBearer\s+[A-Za-z0-9._-]{16,}/i.test(text)
      || /\b(?:api[_-]?key|token|password|secret)\s*[:=]\s*["']?[A-Za-z0-9._-]{16,}/i.test(text)
      || /\bsk-[A-Za-z0-9_-]{16,}/.test(text);
  };

  const currentUserIndex = items.findLastIndex(isUser);
  if (currentUserIndex < 0) {
    if (allowEmptyCurrentRequest && items.length === 0) return [];
    throw new SwitchboardError(
      "EXTERNAL_CURRENT_REQUEST_MISSING",
      "External-model history has no exact current user request.",
      400,
    );
  }

  const eligible = new Set(items.map((_, index) => index));
  const sensitiveIndexes = new Set();
  for (let index = 0; index < items.length; index += 1) {
    if (containsCredential(items[index])) {
      sensitiveIndexes.add(index);
      eligible.delete(index);
    }
  }
  const duplicateAuthority = new Set();
  const authorityDigests = new Set();
  for (let index = items.length - 1; index >= 0; index -= 1) {
    if (!isAuthority(items[index])) continue;
    const key = digest(items[index]);
    if (authorityDigests.has(key)) {
      duplicateAuthority.add(index);
      eligible.delete(index);
    } else authorityDigests.add(key);
  }

  const calls = new Map();
  const outputs = new Map();
  for (let index = 0; index < items.length; index += 1) {
    const item = items[index];
    if ((!isToolCall(item) && !isToolOutput(item)) || !item.call_id) continue;
    const target = isToolCall(item) ? calls : outputs;
    if (!target.has(item.call_id)) target.set(item.call_id, []);
    target.get(item.call_id).push(index);
  }
  const incompleteToolIndexes = new Set();
  for (const [callId, indexes] of [...calls, ...outputs]) {
    if (calls.has(callId) && outputs.has(callId)) continue;
    for (const index of indexes) incompleteToolIndexes.add(index);
  }
  if ([...incompleteToolIndexes].some((index) => index >= currentUserIndex)) {
    throw new SwitchboardError(
      "EXTERNAL_ACTIVE_TOOL_PAIR_INCOMPLETE",
      "The active external-model turn contains an unpaired tool call or output.",
      409,
    );
  }
  for (const index of incompleteToolIndexes) eligible.delete(index);

  const mandatory = new Set(
    retainAuthority ? [...eligible].filter((index) => isAuthority(items[index]) && !received.has(index)) : [],
  );
  if (!received.has(currentUserIndex)) mandatory.add(currentUserIndex);
  for (let index = currentUserIndex + 1; index < items.length; index += 1) {
    if (eligible.has(index) && !received.has(index)) mandatory.add(index);
  }
  if (validatedPrefixLength > 0) {
    for (const index of eligible) {
      if (!received.has(index)) mandatory.add(index);
    }
  }

  const currentMessage = items[currentUserIndex];
  const currentRequest = typeof currentMessage.content === "string"
    ? currentMessage.content
    : (Array.isArray(currentMessage.content) ? currentMessage.content : [])
      .map((part) => part?.text || part?.input_text || part?.output_text || "")
      .filter(Boolean)
      .join("\n");
  const generatedTaskEnvelope = /Message Type:\s*NEW_TASK/i.test(currentRequest);
  const continuation = !generatedTaskEnvelope
    && (/\b(?:continue|resume|previous|prior|above|these|those|same task|pick up)\b/i.test(currentRequest)
      || /(?:繼續|續接|承接|上一|前一|前述|上述|這些|那些|剛才|剛剛|同一個任務|這個工作)/.test(currentRequest));
  if (continuation && !allowProviderContinuation) {
    let assistantIndex = -1;
    for (let index = currentUserIndex - 1; index >= 0; index -= 1) {
      if (eligible.has(index) && isAssistant(items[index])) {
        assistantIndex = index;
        break;
      }
    }
    if (assistantIndex < 0) {
      let compactionIndex = -1;
      for (let index = items.length - 1; index >= 0; index -= 1) {
        if (eligible.has(index) && isCompactionItem(items[index])) {
          compactionIndex = index;
          break;
        }
      }
      if (compactionIndex < 0) {
        throw new SwitchboardError(
          "EXTERNAL_REFERENT_CONTEXT_MISSING",
          "The current request references prior context, but no preceding assistant referent or trusted compaction is available.",
          409,
        );
      }
      mandatory.add(compactionIndex);
    } else {
      mandatory.add(assistantIndex);
      for (let index = assistantIndex - 1; index >= 0; index -= 1) {
        if (eligible.has(index) && isUser(items[index])) {
          mandatory.add(index);
          break;
        }
      }
    }
  }

  if ([...sensitiveIndexes].some((index) => (retainAuthority && isAuthority(items[index])) || index >= currentUserIndex)
      || [...mandatory].some((index) => sensitiveIndexes.has(index))) {
    throw new SwitchboardError(
      "EXTERNAL_SENSITIVE_CONTEXT_DETECTED",
      "Mandatory external-model context appears to contain credential material.",
      422,
    );
  }

  const units = [];
  let activeUnit = null;
  for (const index of [...eligible].sort((a, b) => a - b)) {
    if (mandatory.has(index) || received.has(index) || isAuthority(items[index])) continue;
    if (isUser(items[index]) || !activeUnit) {
      activeUnit = [];
      units.push(activeUnit);
    }
    activeUnit.push(index);
  }

  const omissionMarker = (omittedIndexes) => {
    const omitted = omittedIndexes.map((index) => items[index]);
    const payload = {
      schema_version: "external_context_omission_v1",
      item_count: omitted.length,
      byte_count: byteLength(omitted),
      sha256: digest(omitted),
    };
    return {
      type: "message",
      role: "developer",
      content: [{ type: "input_text", text: `[external_context_omission_v1 ${JSON.stringify(payload)}]` }],
    };
  };
  const assemble = (selected) => {
    const omitted = items.map((_, index) => index)
      .filter((index) => !selected.has(index));
    if (!omitted.length) return items;
    const result = [];
    let markerInserted = false;
    for (let index = 0; index < items.length; index += 1) {
      if (selected.has(index)) result.push(items[index]);
      else if (!markerInserted) {
        result.push(omissionMarker(omitted));
        markerInserted = true;
      }
    }
    return result;
  };

  const selected = new Set(mandatory);
  let candidate = assemble(selected);
  if (byteLength(candidate) > maxBytes) {
    throw new SwitchboardError(
      "EXTERNAL_MANDATORY_CONTEXT_TOO_LARGE",
      "Exact current request, authority constraints, or active tool evidence exceed the external history budget.",
      413,
      {
        max_bytes: maxBytes,
        mandatory_bytes: byteLength([...selected].sort((a, b) => a - b).map((index) => items[index])),
        current_request_sha256: digest(items[currentUserIndex]),
        validated_prefix_items: received.size,
        new_items: items.length - received.size,
      },
    );
  }
  if (includeOptionalHistory) {
    for (let unitIndex = units.length - 1; unitIndex >= 0; unitIndex -= 1) {
      const proposed = new Set(selected);
      for (const index of units[unitIndex]) proposed.add(index);
      const proposedCandidate = assemble(proposed);
      if (byteLength(proposedCandidate) <= maxBytes) {
        for (const index of units[unitIndex]) selected.add(index);
        candidate = proposedCandidate;
      }
    }
  }
  return candidate;
}

export function createExternalEventBridge(route) {
  if (route === "web") {
    let providerResponseId = null;
    return {
      transform(event) {
        if (!event || typeof event !== "object") return event;
        const responseId = event.response?.id || event.response_id;
        if (typeof responseId === "string" && responseId) providerResponseId ||= responseId;
        return structuredClone(event);
      },
      get providerResponseId() {
        return providerResponseId;
      },
      publicResponseId: null,
    };
  }
  const publicResponseId = canonicalId(`resp_${route}`);
  const itemIds = new Map();
  let providerResponseId = null;

  const captureProviderResponseId = (value) => {
    if (typeof value === "string" && value && value !== publicResponseId) providerResponseId ||= value;
  };
  const publicItemId = (providerId, type = "item") => {
    if (typeof providerId !== "string" || !providerId) return undefined;
    if (!itemIds.has(providerId)) itemIds.set(providerId, canonicalId(itemIdPrefix(type)));
    return itemIds.get(providerId);
  };
  const sanitizeItem = (item) => {
    if (!item || item.type === "reasoning") return null;
    const clean = stripPrivateState(item);
    if (!clean) return null;
    if (clean.id) clean.id = publicItemId(clean.id, clean.type);
    return clean;
  };
  const sanitizePublicStatus = (event) => {
    const expectedMarker = route === "agy"
      ? "agy_processing_v1"
      : route === "xai"
        ? "xai_processing_v1"
        : null;
    if (!expectedMarker || event.switchboard_public_status !== expectedMarker) return null;
    const clean = structuredClone(event);
    delete clean.switchboard_public_status;
    if (clean.item?.type === "reasoning") {
      clean.item = {
        type: "reasoning",
        ...(clean.item.id ? { id: publicItemId(clean.item.id, "reasoning") } : {}),
        summary: Array.isArray(clean.item.summary)
          ? clean.item.summary.map((entry) => ({ type: "summary_text", text: String(entry?.text || "") }))
          : [],
        encrypted_content: null,
      };
    }
    if (typeof clean.item_id === "string") clean.item_id = publicItemId(clean.item_id, "reasoning");
    return clean;
  };

  return {
    transform(event) {
      if (!event || typeof event !== "object") return event;
      const publicStatus = sanitizePublicStatus(event);
      if (publicStatus) return publicStatus;
      if (typeof event.type === "string" && event.type.includes(".reasoning")) return null;
      if (event.item?.type === "reasoning") return null;

      captureProviderResponseId(event.response?.id);
      captureProviderResponseId(event.response_id);
      const clean = stripPrivateState(structuredClone(event));
      if (!clean) return null;
      if (clean.response && typeof clean.response === "object") {
        clean.response.id = publicResponseId;
        if (Array.isArray(clean.response.output)) {
          clean.response.output = clean.response.output.map(sanitizeItem).filter(Boolean);
        }
      }
      if (typeof clean.response_id === "string") clean.response_id = publicResponseId;
      if (clean.item) {
        clean.item = sanitizeItem(clean.item);
        if (!clean.item) return null;
      }
      if (typeof clean.item_id === "string") clean.item_id = publicItemId(clean.item_id);
      return clean;
    },
    get providerResponseId() {
      return providerResponseId;
    },
    publicResponseId,
  };
}

export function normalizeExternalItemsForOfficial(input) {
  if (!Array.isArray(input)) return { input, changed: false };
  // Web bridge UUID items are inline content, not official persisted objects.
  const isWebReasoning = (item) => item?.type === "reasoning"
    && /^rs_[0-9a-f]{32}$/i.test(item.id || "")
    && item.encrypted_content == null
    && (Array.isArray(item.summary) || Array.isArray(item.content));
  const hasWebHistory = input.some(isWebReasoning);
  let changed = false;
  const normalized = input.flatMap((item) => {
    // Native agent envelopes carry opaque payloads for the official provider.
    if (item?.type === "agent_message") return [item];
    if (isWebReasoning(item)) {
      const copy = structuredClone(item);
      delete copy.id;
      delete copy.encrypted_content;
      changed = true;
      return copy.summary?.length || copy.content?.length ? [copy] : [];
    }
    if (hasWebHistory
        && ["message", "function_call", "custom_tool_call", "tool_search_call"].includes(item?.type)
        && /^(?:msg|fc|ctc|tsc)_[0-9a-f]{32}$/i.test(item.id || "")) {
      const copy = structuredClone(item);
      delete copy.id;
      changed = true;
      return [copy];
    }
    if (item?.type === "reasoning" && /_ext_/.test(item?.id || "")) {
      changed = true;
      return [];
    }
    if (item && typeof item === "object" && /^(?:msg|fc|item)_ext_/.test(item.id || "")) {
      const copy = structuredClone(item);
      delete copy.id;
      changed = true;
      return [copy];
    }
    if (item && typeof item === "object" && Object.hasOwn(item, "is_canonical_summary")) {
      const copy = structuredClone(item);
      delete copy.is_canonical_summary;
      changed = true;
      return [copy];
    }
    return [item];
  });
  return { input: normalized, changed };
}

function xaiReasoningEffort(value) {
  if (value === "low" || value === "minimal" || value === "none") return "low";
  if (value === "medium") return "medium";
  if (value === "xhigh" || value === "max" || value === "ultra") return "xhigh";
  return "high";
}

function isQuarantined(definition, quarantine) {
  return quarantine.some((entry) => entry
    && entry.namespace === definition.namespace
    && entry.name === definition.responseName);
}

function rewriteNamedExecTool(value) {
  if (!value || typeof value !== "object" || value.name !== "exec") return value;
  const { namespace: _namespace, ...rest } = value;
  return { ...rest, type: "function", name: "exec_command" };
}

function rewriteExecToolChoice(toolChoice, definitions) {
  if (!toolChoice || typeof toolChoice !== "object") return toolChoice;
  if (!definitions.some((definition) => definition.adapter === "exec_command")) return toolChoice;
  const rewritten = rewriteNamedExecTool(toolChoice);
  if (!Array.isArray(rewritten.tools)) return rewritten;
  return { ...rewritten, tools: rewritten.tools.map(rewriteNamedExecTool) };
}

export function externalAvailableIdentities(body, toolQuarantine = []) {
  const { bridge } = flattenTools(body?.tools, body?.input);
  return [...bridge.values()]
    .filter((definition) => !isQuarantined(definition, toolQuarantine))
    .map((definition) => ({
      alias: definition.alias,
      description: definition.description || "",
      parameters: definition.parameters || {},
    }));
}

export function catalogExtends(stored, next) {
  if (!Array.isArray(stored) || !Array.isArray(next)) return false;
  const prior = new Map(stored.map((item) => [item.alias, JSON.stringify(item)]));
  return next.every((item) => prior.get(item.alias) === JSON.stringify(item));
}

export function prepareXaiBody(
  body,
  {
    toolQuarantine = [],
    allowProviderContinuation = false,
    retainHistoryAuthority = true,
    maxHistoryBytes = DEFAULT_MAX_EXTERNAL_HISTORY_BYTES,
    definitionsOverride = null,
    retainFrozenDefinitions = false,
  } = {},
) {
  rememberSpawnPayloads(body);
  const { bridge: fullBridge } = flattenTools(body.tools, body.input);
  const availableDefinitions = [...fullBridge.values()]
    .filter((definition) => !isQuarantined(definition, toolQuarantine));
  const available = externalAvailableIdentities(body, toolQuarantine);
  const catalogIdentity = createHash("sha256").update(JSON.stringify(available)).digest("hex");
  const definitions = Array.isArray(definitionsOverride)
    ? definitionsOverride.filter((definition) => !isQuarantined(definition, toolQuarantine)
      && (retainFrozenDefinitions || availableDefinitions.some((item) => item.alias === definition.alias)))
    : stableDefinitions(availableDefinitions, body.input);
  const bridge = new Map(definitions.map((definition) => [definition.alias, definition]));
  const clean = stripPrivateState({
    ...structuredClone(body),
    input: boundExternalHistory(
      normalizeHistory(body.input, fullBridge),
      maxHistoryBytes,
      {
        allowEmptyCurrentRequest: body.generate === false,
        allowProviderContinuation,
        includeOptionalHistory: !allowProviderContinuation,
        retainAuthority: retainHistoryAuthority,
      },
    ),
  });
  if (clean.tool_choice) clean.tool_choice = rewriteExecToolChoice(clean.tool_choice, definitions);
  // One bound for every turn keeps the instructions prefix stable.
  clean.instructions = boundedExternalInstructions(clean.instructions, MAX_EXTERNAL_INSTRUCTIONS_CHARS);
  clean.tools = definitions.map((definition) => ({
    type: "function",
    name: definition.alias,
    description: definition.description,
    parameters: definition.parameters,
  }));
  clean.store = false;
  if (clean.reasoning && typeof clean.reasoning === "object") {
    clean.reasoning = { effort: xaiReasoningEffort(clean.reasoning.effort) };
  }
  if (Array.isArray(clean.include)) {
    clean.include = clean.include.filter((value) => value !== "reasoning.encrypted_content");
  }
  return {
    clean,
    bridge,
    definitions,
    available,
    catalogIdentity,
    diagnostics: {
      ...externalToolShape(body),
      exec_adapter_projected: definitions.some((definition) => definition.adapter === "exec_command")
        && definitions.some((definition) => definition.adapter === "write_stdin"),
    },
  };
}
