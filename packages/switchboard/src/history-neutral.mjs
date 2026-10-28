import { createHash, randomUUID } from "node:crypto";

import { SwitchboardError } from "./shared.mjs";

const MARKER_PREFIX = "codex-switchboard-compaction-v1:";
const MAX_SUMMARY_CHARS = 48_000;
export const trustedProviderStateMarker = "switchboard_trusted_provider_state";

function digestSummary(summary) {
  return createHash("sha256").update(summary).digest("hex");
}

export function createCanonicalCompaction({ model, route, summary }) {
  const normalized = String(summary || "").trim();
  if (!normalized || normalized.length > MAX_SUMMARY_CHARS) {
    throw new SwitchboardError(
      "INVALID_COMPACTION_SUMMARY",
      `Canonical summary must contain 1-${MAX_SUMMARY_CHARS} characters.`,
    );
  }
  const stateKey = randomUUID();
  const payload = {
    v: 1,
    state_key: stateKey,
    source_route: route,
    source_model: model,
    summary: normalized,
    summary_sha256: digestSummary(normalized),
  };
  const encoded = Buffer.from(JSON.stringify(payload)).toString("base64url");
  return {
    stateKey,
    item: {
      type: "compaction",
      id: `cmp_switchboard_${randomUUID()}`,
      encrypted_content: `${MARKER_PREFIX}${encoded}`,
      created_by: model,
    },
  };
}

export function decodeCanonicalCompaction(item) {
  if (item?.type !== "compaction" || typeof item.encrypted_content !== "string") return null;
  if (!item.encrypted_content.startsWith(MARKER_PREFIX)) return null;
  try {
    const payload = JSON.parse(
      Buffer.from(item.encrypted_content.slice(MARKER_PREFIX.length), "base64url").toString("utf8"),
    );
    if (payload.v !== 1 || typeof payload.summary !== "string" || typeof payload.state_key !== "string") {
      throw new Error("invalid marker shape");
    }
    if (payload.summary.length > MAX_SUMMARY_CHARS || digestSummary(payload.summary) !== payload.summary_sha256) {
      throw new Error("invalid marker digest");
    }
    return payload;
  } catch {
    throw new SwitchboardError(
      "INVALID_CANONICAL_COMPACTION",
      "The switchboard compaction marker failed schema or digest validation.",
      400,
    );
  }
}

export function isCanonicalSummary(item) {
  if (!item) return false;
  if (item.is_canonical_summary === true) return true;
  if (item.type === "message") {
    const text = typeof item.content === "string"
      ? item.content
      : (Array.isArray(item.content) ? item.content[0]?.text || "" : "");
    return text.startsWith("[Canonical summary of prior context");
  }
  return false;
}

export function isCompactionItem(item) {
  return item?.type === "compaction" || isCanonicalSummary(item);
}

function summaryMessage(payload) {
  return {
    type: "message",
    role: "user",
    is_canonical_summary: true,
    content: [{
      type: "input_text",
      text: `[Canonical summary of prior context from ${payload.source_model}]\n${payload.summary}`,
    }],
  };
}

export function normalizeCompactions(input, route, providerState = new Map()) {
  const normalized = [];
  let changed = false;
  for (const item of Array.isArray(input) ? input : []) {
    const marker = decodeCanonicalCompaction(item);
    if (!marker) {
      normalized.push(item);
      continue;
    }
    changed = true;
    const privateState = providerState.get(marker.state_key);
    if (route === marker.source_route && privateState?.item) {
      normalized.push(route === "xai"
        ? { ...privateState.item, [trustedProviderStateMarker]: true }
        : privateState.item);
    }
    else normalized.push(summaryMessage(marker));
  }
  return { input: normalized, changed };
}

export class EphemeralProviderState {
  #entries = new Map();
  #limit;

  constructor(limit = 64) {
    this.#limit = limit;
  }

  set(key, value) {
    this.#entries.delete(key);
    this.#entries.set(key, value);
    while (this.#entries.size > this.#limit) {
      this.#entries.delete(this.#entries.keys().next().value);
    }
  }

  get(key) {
    const value = this.#entries.get(key);
    if (value !== undefined) {
      this.#entries.delete(key);
      this.#entries.set(key, value);
    }
    return value;
  }

  asMap() {
    return { get: (key) => this.get(key) };
  }
}

export const canonicalCompactionMarkerPrefix = MARKER_PREFIX;
