import { mkdirSync, readFileSync, renameSync, statSync, unlinkSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

const FERNET_RE = /^gAAAAA[A-Za-z0-9_-]{20,}$/;
const TASK_NAME_RE = /Task name:\s*(\S+)/i;
const SPAWN_TOOL_NAMES = new Set(["spawn_agent", "spawn_agent_v2"]);
const FOLLOWUP_TOOL_NAMES = new Set(["followup_task", "send_message", "send_input"]);
const DEFAULT_CACHE_PATH = new URL("../runtime/spawn-payload-cache.json", import.meta.url);
const MAX_ENTRIES = 256;
const MAX_FOLLOWUPS = 16;
const TTL_MS = 30 * 60_000;

const payloads = new Map();
const stats = { writes: 0, hits: 0, misses: 0, last_error: null };
let diskMtimeMs = -1;
let persistDisabled = false;
let overridePath = null;

function now() {
  return Date.now();
}

function cachePath() {
  return overridePath
    || process.env.SWITCHBOARD_SPAWN_PAYLOAD_CACHE_PATH
    || DEFAULT_CACHE_PATH;
}

function resolvedCachePath() {
  const path = cachePath();
  return typeof path === "string" ? path : fileURLToPath(path);
}

export function spawnCacheStats() {
  return {
    ...stats,
    path: resolvedCachePath(),
    persist_disabled: persistDisabled,
    entries: payloads.size,
  };
}

function cloneEntry(entry) {
  return {
    spawn: entry.spawn || null,
    followups: Array.isArray(entry.followups) ? [...entry.followups] : [],
    ts: entry.ts,
  };
}

function prune(ts = now()) {
  for (const [key, entry] of payloads) {
    if (ts - entry.ts > TTL_MS) payloads.delete(key);
  }
  while (payloads.size > MAX_ENTRIES) {
    payloads.delete(payloads.keys().next().value);
  }
}

function loadFromDisk() {
  if (persistDisabled) return;
  const path = cachePath();
  try {
    const stat = statSync(path);
    if (stat.mtimeMs === diskMtimeMs) return;
    const parsed = JSON.parse(readFileSync(path, "utf8"));
    if (parsed?.v !== 1 || !parsed.entries || typeof parsed.entries !== "object") return;
    payloads.clear();
    for (const [key, entry] of Object.entries(parsed.entries)) {
      if (!entry || typeof entry !== "object") continue;
      payloads.set(key, {
        spawn: typeof entry.spawn === "string" ? entry.spawn : null,
        followups: Array.isArray(entry.followups)
          ? entry.followups.filter((item) => typeof item === "string")
          : [],
        ts: Number(entry.ts) || now(),
      });
    }
    prune();
    diskMtimeMs = stat.mtimeMs;
  } catch (error) {
    if (error?.code !== "ENOENT") return;
    diskMtimeMs = -1;
  }
}

function persist() {
  if (persistDisabled) return;
  const filePath = resolvedCachePath();
  try {
    mkdirSync(dirname(filePath), { recursive: true });
    const tmp = `${filePath}.${process.pid}.tmp`;
    const entries = {};
    for (const [key, entry] of payloads) entries[key] = cloneEntry(entry);
    writeFileSync(tmp, `${JSON.stringify({ v: 1, entries })}\n`, { mode: 0o600 });
    renameSync(tmp, filePath);
    stats.writes += 1;
    stats.last_error = null;
    try {
      diskMtimeMs = statSync(filePath).mtimeMs;
    } catch {
      diskMtimeMs = Date.now();
    }
  } catch (error) {
    stats.last_error = error?.message || String(error);
  }
}

export function isOpaqueEncryptedPayload(value) {
  const text = String(value || "").trim();
  if (!text) return false;
  if (FERNET_RE.test(text.replace(/\s+/g, ""))) return true;
  if (text.length >= 80 && /^[A-Za-z0-9+/=_-]+$/.test(text) && !/\s/.test(text)) return true;
  return false;
}

export function taskNameFromAgentHeader(text) {
  const match = TASK_NAME_RE.exec(String(text || ""));
  return match?.[1] || null;
}

function rememberNames(taskName) {
  const name = String(taskName || "").trim();
  if (!name) return [];
  const names = [name];
  const leaf = name.split("/").filter(Boolean).at(-1);
  if (leaf && leaf !== name) names.push(leaf);
  return names;
}

function ensureEntry(taskName, ts = now()) {
  loadFromDisk();
  prune(ts);
  let entry = payloads.get(taskName);
  if (!entry) {
    entry = { spawn: null, followups: [], ts };
    payloads.set(taskName, entry);
  }
  entry.ts = ts;
  return entry;
}

function remember(taskName, message, kind = "spawn") {
  const payload = String(message || "").trim();
  if (!payload || isOpaqueEncryptedPayload(payload)) return;
  const ts = now();
  for (const name of rememberNames(taskName)) {
    const entry = ensureEntry(name, ts);
    if (kind === "followup") {
      if (entry.followups.at(-1) !== payload) {
        entry.followups.push(payload);
        if (entry.followups.length > MAX_FOLLOWUPS) entry.followups.shift();
      }
    } else if (entry.spawn !== payload) {
      entry.spawn = payload;
    }
  }
  persist();
}

function resolveEntry(taskName) {
  loadFromDisk();
  prune();
  const name = String(taskName || "").trim();
  if (!name) return null;
  return payloads.get(name)
    || payloads.get(name.split("/").filter(Boolean).at(-1) || "")
    || null;
}

export function lookupSpawnPayload(taskName) {
  const value = resolveEntry(taskName)?.spawn || null;
  if (value) stats.hits += 1;
  else stats.misses += 1;
  return value;
}

export function lookupFollowupPayload(taskName, cursor = new Map()) {
  const entry = resolveEntry(taskName);
  if (!entry?.followups?.length) return null;
  const key = String(taskName || "").trim();
  const index = cursor.get(key) || 0;
  if (index >= entry.followups.length) return entry.followups.at(-1) || null;
  cursor.set(key, index + 1);
  return entry.followups[index];
}

function parseJsonObject(raw) {
  if (!raw) return null;
  if (typeof raw === "object") return raw;
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    return null;
  }
}

function parseSpawnArguments(item) {
  const raw = item?.arguments ?? item?.input;
  const parsed = parseJsonObject(raw);
  if (!parsed) return null;
  if (typeof parsed.input === "string" && parsed.message == null && parsed.task_name == null && parsed.target == null) {
    return parseJsonObject(parsed.input) || parsed;
  }
  return parsed;
}

function rememberFromCall(item) {
  const name = item?.name || item?.tool_name;
  const kind = SPAWN_TOOL_NAMES.has(name) ? "spawn" : FOLLOWUP_TOOL_NAMES.has(name) ? "followup" : null;
  if (!kind) return false;
  const args = parseSpawnArguments(item);
  if (!args) {
    persist();
    return true;
  }
  remember(
    args.task_name || args.agent_name || args.name || args.target || args.recipient,
    args.message || (typeof args.input === "string" ? args.input : null) || args.prompt,
    kind,
  );
  persist();
  return true;
}

export function rememberSpawnPayloads(value) {
  if (!value) return;
  if (Array.isArray(value)) {
    for (const item of value) rememberSpawnPayloads(item);
    return;
  }
  if (typeof value !== "object") return;
  if (value.type === "function_call" || value.type === "custom_tool_call") {
    rememberFromCall(value);
  }
  if (value.item) rememberSpawnPayloads(value.item);
  if (Array.isArray(value.input)) rememberSpawnPayloads(value.input);
  if (Array.isArray(value.output)) rememberSpawnPayloads(value.output);
  if (Array.isArray(value.response?.output)) rememberSpawnPayloads(value.response.output);
}

function headerText(item) {
  return (Array.isArray(item?.content) ? item.content : [])
    .map((part) => part?.text || part?.input_text || part?.output_text || "")
    .filter(Boolean)
    .join("");
}

function visiblePartText(part) {
  if (!part || typeof part !== "object") return "";
  if (part.type === "encrypted_content") {
    const raw = String(part.encrypted_content || "");
    if (!raw || isOpaqueEncryptedPayload(raw)) return "";
    return raw;
  }
  return part.text || part.input_text || part.output_text || "";
}

export function recoverAgentMessageText(item, cursor = new Map()) {
  if (item?.type !== "agent_message" || !Array.isArray(item.content)) return null;
  const header = headerText(item);
  const plaintext = item.content.map(visiblePartText).filter(Boolean).join("");
  const hasOpaquePayload = item.content.some((part) => (
    part?.type === "encrypted_content" && isOpaqueEncryptedPayload(part.encrypted_content)
  ));
  if (!hasOpaquePayload) return plaintext || header || null;
  const taskName = taskNameFromAgentHeader(header) || item.recipient;
  const isNewTask = /Message Type:\s*NEW_TASK/i.test(header);
  const recovered = isNewTask
    ? lookupSpawnPayload(taskName)
    : lookupFollowupPayload(taskName, cursor) || lookupSpawnPayload(taskName);
  if (recovered) return `${header.trimEnd()}\n${recovered}`;
  return `${header}\n[Official spawn payload is encrypted and unreadable by this external model. Use only the plaintext task name/sender above; do not invent the missing payload.]`;
}

export function _resetSpawnPayloadCacheForTests({ persist = false, path = null } = {}) {
  payloads.clear();
  diskMtimeMs = -1;
  persistDisabled = !persist;
  overridePath = path;
  if (path) {
    try {
      unlinkSync(path);
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
  }
}

export function _simulateNewProcessForTests() {
  payloads.clear();
  diskMtimeMs = -1;
}
