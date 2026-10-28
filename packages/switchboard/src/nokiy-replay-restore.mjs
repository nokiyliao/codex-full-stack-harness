// Restores what the upstream chain actually held when Codex replays a Nokiy conversation.
// The router changes three things on their way to or from the model, and Codex records the
// changed form: a projected native call (a ~7 KB exec wrapper in place of the model's short
// call), an expanded delivery message (the full worker answer in place of the model's compact
// acceptance), and a result-delivery developer message appended after a tool output. A full
// replay therefore left the cached chain at the first such item and grew ~15%. Each record is
// bound to the exact bytes Codex received; anything else in a replay is left unchanged.
import { appendFileSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

const CALL_TYPES = new Set(["custom_tool_call", "function_call"]);
const OUTPUT_TYPES = new Set(["custom_tool_call_output", "function_call_output"]);
const CALL_FIELDS = ["type", "call_id", "name", "namespace", "input", "arguments"];
const ORIGINAL_FIELDS = ["name", "namespace", "input", "arguments"];
const DEFAULT_PATH = new URL("../runtime/nokiy-replay-originals.jsonl", import.meta.url);
const MAX_BYTES = 32 * 1024 * 1024;

const digest = (value) => createHash("sha256").update(JSON.stringify(value)).digest("hex").slice(0, 32);
const isCall = (item) => CALL_TYPES.has(item?.type) && typeof item.call_id === "string" && item.call_id !== "";
const isOutput = (item) => OUTPUT_TYPES.has(item?.type) && typeof item.call_id === "string" && item.call_id !== "";
const messageTexts = (item) => item?.type === "message" && item.role === "assistant" && Array.isArray(item.content)
  && item.content.length > 0 && item.content.every((part) => part?.type === "output_text" && typeof part.text === "string")
  ? item.content.map((part) => part.text) : null;

// Keys never include per-thread ids or status, which Codex may drop or rewrite in a replay.
function keyOf(item) {
  if (isCall(item)) return `call:${digest(CALL_FIELDS.map((field) => item[field] ?? null))}`;
  const texts = messageTexts(item);
  if (texts) return `message:${digest(texts)}`;
  return null;
}
const anchorKey = (item) => isOutput(item) ? `after:${digest([item.type, item.call_id, item.output ?? null])}` : null;

export function createReplayRestorer({ path = null, maxBytes = MAX_BYTES } = {}) {
  const entries = new Map();
  let bytes = 0;
  const remember = (key, value, size) => {
    const previous = entries.get(key);
    if (previous) { bytes -= previous.size; entries.delete(key); }
    entries.set(key, { value, size });
    bytes += size;
    while (bytes > maxBytes && entries.size) {
      const [oldest, entry] = entries.entries().next().value;
      entries.delete(oldest);
      bytes -= entry.size;
    }
  };
  const stats = { recorded: 0, restored: 0, inserted: 0, persist_errors: 0 };
  const persist = (record) => {
    const line = JSON.stringify(record);
    if (path) {
      // One small synchronous append per record survives an immediate restart.
      try {
        mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
        appendFileSync(path, `${line}\n`, { mode: 0o600 });
      } catch { stats.persist_errors += 1; }
    }
    return line.length + 1;
  };
  const accept = (record, size) => {
    // Version 1 lines carried a projected-call fingerprint, which is the current call key.
    if (typeof record?.k === "string" && record.v !== undefined) remember(record.k, record.v, size);
    else if (typeof record?.c === "string" && typeof record.f === "string" && record.o && typeof record.o === "object") {
      remember(`call:${record.f}`, { call: record.o }, size);
    }
  };
  if (path) {
    try {
      const text = readFileSync(path, "utf8");
      for (const line of text.split("\n")) {
        if (!line) continue;
        try { accept(JSON.parse(line), line.length + 1); } catch { /* A torn final line is ignored. */ }
      }
      if (text.length > 2 * maxBytes) {
        const kept = [...entries].map(([k, entry]) => JSON.stringify({ k, v: entry.value })).join("\n") + "\n";
        writeFileSync(`${path}.tmp`, kept, { mode: 0o600 });
        renameSync(`${path}.tmp`, path);
      }
    } catch (error) {
      if (error?.code !== "ENOENT") entries.clear();
    }
  }
  return {
    stats: () => ({ ...stats, entries: entries.size, bytes }),
    // Exact emitted-call evidence, not proof of execution or a permission grant.
    originalCall(item) {
      if (!isCall(item)) return null;
      const call = entries.get(keyOf(item))?.value?.call;
      if (!call) return null;
      const { name: _name, namespace: _namespace, input: _input, arguments: _arguments, ...envelope } = item;
      return { ...envelope, ...call };
    },
    // original: the item the model produced; shown: the item Codex received instead.
    record(original, shown) {
      if (!original || !shown || original === shown) return;
      const key = keyOf(shown);
      if (!key || key === keyOf(original)) return;
      let value = null;
      if (isCall(original) && isCall(shown) && original.call_id === shown.call_id) {
        value = { call: Object.fromEntries(ORIGINAL_FIELDS.filter((field) => original[field] !== undefined)
          .map((field) => [field, original[field]])) };
      } else {
        const before = messageTexts(original), after = messageTexts(shown);
        if (before && after && before.length === after.length) value = { texts: before };
      }
      if (!value || JSON.stringify(entries.get(key)?.value) === JSON.stringify(value)) return;
      remember(key, value, persist({ k: key, v: value }));
      stats.recorded += 1;
    },
    // anchor: the client item after which the router appended items the client never saw.
    recordInsertion(anchor, items) {
      const key = anchorKey(anchor);
      if (!key || !Array.isArray(items) || !items.length) return;
      const value = { insert: items };
      if (JSON.stringify(entries.get(key)?.value) === JSON.stringify(value)) return;
      remember(key, value, persist({ k: key, v: value }));
      stats.recorded += 1;
    },
    restore(input) {
      if (!Array.isArray(input) || !entries.size) return { input, restored: 0, inserted: 0 };
      let restored = 0, inserted = 0;
      const output = [];
      for (let index = 0; index < input.length; index += 1) {
        const item = input[index];
        const entry = entries.get(keyOf(item));
        if (entry?.value?.call && isCall(item)) {
          const { name: _name, namespace: _namespace, input: _input, arguments: _arguments, ...envelope } = item;
          output.push({ ...envelope, ...entry.value.call });
          restored += 1;
        } else if (entry?.value?.texts && messageTexts(item)?.length === entry.value.texts.length) {
          output.push({ ...item, content: item.content.map((part, position) => ({ ...part, text: entry.value.texts[position] })) });
          restored += 1;
        } else output.push(item);
        // Only a historical position is restored: when the anchor is the newest item, the
        // current request itself decides again whether the router appends the transport.
        const insertion = index < input.length - 1 ? entries.get(anchorKey(item))?.value?.insert : null;
        if (insertion && !insertion.every((extra, offset) => JSON.stringify(input[index + 1 + offset]) === JSON.stringify(extra))) {
          output.push(...insertion);
          inserted += insertion.length;
        }
      }
      stats.restored += restored;
      stats.inserted += inserted;
      return { input: restored || inserted ? output : input, restored, inserted };
    },
  };
}

function defaultPath() {
  const override = process.env.SWITCHBOARD_NOKIY_REPLAY_STORE_PATH;
  if (override === "0") return null;
  if (override) return override;
  // Test processes never touch the live runtime store unless a path is supplied.
  if (process.env.NODE_TEST_CONTEXT) return null;
  return fileURLToPath(DEFAULT_PATH);
}

let shared = null;
export function sharedReplayRestorer() {
  if (!shared) shared = createReplayRestorer({ path: defaultPath() });
  return shared;
}

