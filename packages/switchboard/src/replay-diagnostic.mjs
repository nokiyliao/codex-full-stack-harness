// Log-only diagnosis of full-input replays: where does a replayed conversation leave the
// last upstream chain of the same thread? Stores fingerprints and item kinds, never content.
import { createHash } from "node:crypto";

const MAX_THREADS = 64;
const chains = new Map();

export const fingerprint = (value) =>
  createHash("sha256").update(JSON.stringify(value) ?? "null").digest("hex").slice(0, 16);
const kind = (item) => [item?.type ?? typeof item, item?.role ?? item?.name ?? ""].filter(Boolean).join(":").slice(0, 80);
export const itemHashes = (items) => items.map(fingerprint);
export const itemKinds = (items) => items.map(kind);

function templateFingerprints(template) {
  return Object.fromEntries(Object.keys(template || {}).sort().map((key) => [key, fingerprint(template[key])]));
}

export function rememberChain(thread, template, hashes, kinds) {
  if (typeof thread !== "string" || !Array.isArray(hashes) || !Array.isArray(kinds)) return;
  chains.delete(thread);
  chains.set(thread, { at: Date.now(), template: templateFingerprints(template), hashes, kinds });
  while (chains.size > MAX_THREADS) chains.delete(chains.keys().next().value);
}

export function replayDivergence(thread, template, input, hashes = itemHashes(input)) {
  const chain = typeof thread === "string" ? chains.get(thread) : null;
  if (!chain || !Array.isArray(input)) return null;
  const now = templateFingerprints(template);
  const keys = [...new Set([...Object.keys(chain.template), ...Object.keys(now)])].sort();
  let first = 0;
  const limit = Math.min(chain.hashes.length, hashes.length);
  while (first < limit && chain.hashes[first] === hashes[first]) first += 1;
  return { chain_items: chain.hashes.length, input_items: input.length, first_diff: first,
    chain_kind: chain.kinds[first] ?? null, input_kind: first < input.length ? kind(input[first]) : null,
    template_keys_changed: keys.filter((key) => chain.template[key] !== now[key]),
    chain_age_s: Math.round((Date.now() - chain.at) / 1000) };
}

export function forgetChainsForTests() { chains.clear(); }

// Request-level fields that never reach the rendered prompt.
const UNRENDERED_TEMPLATE_KEYS = new Set(["client_metadata", "access_programs"]);
const count = (tools) => Array.isArray(tools) ? tools.length : null;
const length = (text) => typeof text === "string" ? text.length : null;

// Which prompt-shaping request fields changed between consecutive turns of one chain.
export function templateDiff(previous, current) {
  const before = templateFingerprints(previous), after = templateFingerprints(current);
  return [...new Set([...Object.keys(before), ...Object.keys(after)])].sort()
    .filter((key) => !UNRENDERED_TEMPLATE_KEYS.has(key) && before[key] !== after[key])
    .map((key) => ({ key, before: before[key] ?? null, after: after[key] ?? null,
      ...(key === "tools" ? { before_count: count(previous?.tools), after_count: count(current?.tools) } : {}),
      ...(key === "instructions" ? { before_length: length(previous?.instructions), after_length: length(current?.instructions) } : {}) }));
}
