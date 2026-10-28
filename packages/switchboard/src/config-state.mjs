import { createHash } from "node:crypto";
import { chmod, open, readFile, rename } from "node:fs/promises";
import path from "node:path";

import { parse } from "smol-toml";

export const CONFIG_PATH = "/Users/nokiy/.codex/config.toml";
export const OFFICIAL_DIRECT_CANDIDATE_PATH = "/Users/nokiy/.codex-multi/switchboard/runtime/official-direct-config.toml";

function withoutAppManagedMetadata(value) {
  const copy = structuredClone(value);
  for (const marketplace of Object.values(copy.marketplaces || {})) {
    if (!marketplace || typeof marketplace !== "object") continue;
    delete marketplace.last_updated;
    delete marketplace.last_revision;
  }
  return copy;
}

export function configEquivalentModuloAppManagedMetadata(expected, actual, { officialCliPaths } = {}) {
  const left = withoutAppManagedMetadata(expected);
  const right = withoutAppManagedMetadata(actual);
  const paths = officialCliPaths && [officialCliPaths.launch_path, officialCliPaths.cli_path];
  const before = left.mcp_servers?.node_repl?.env;
  const after = right.mcp_servers?.node_repl?.env;
  if (paths?.every((entry) => typeof entry === "string" && entry.length > 0)
    && paths.includes(before?.CODEX_CLI_PATH) && paths.includes(after?.CODEX_CLI_PATH)) {
    before.CODEX_CLI_PATH = officialCliPaths.cli_path;
    after.CODEX_CLI_PATH = officialCliPaths.cli_path;
  }
  return JSON.stringify(left) === JSON.stringify(right);
}

export function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function topLevelTransform(source, replacements) {
  const newline = source.endsWith("\n") ? "\n" : "";
  const lines = source.split(/\r?\n/);
  if (newline) lines.pop();
  const firstTable = lines.findIndex((line) => /^\s*\[/.test(line));
  const boundary = firstTable < 0 ? lines.length : firstTable;
  const prefix = lines.slice(0, boundary).filter((line) => {
    const match = /^\s*([A-Za-z0-9_-]+)\s*=/.exec(line);
    return !match || !(match[1] in replacements);
  });
  const inserted = Object.entries(replacements)
    .filter(([, value]) => value !== null)
    .map(([key, value]) => `${key} = ${JSON.stringify(value)}`);
  const output = [...inserted, ...prefix, ...lines.slice(boundary)].join("\n") + newline;
  parse(output);
  return output;
}

export function bridgeConfig(source, { openaiBaseUrl, catalogPath }) {
  return topLevelTransform(source, {
    openai_base_url: openaiBaseUrl,
    model_catalog_json: catalogPath,
  });
}

export function routingSettings(source) {
  const config = typeof source === "string" ? parse(source) : source;
  return {
    openai_base_url: config.openai_base_url ?? null,
    model_catalog_json: config.model_catalog_json ?? null,
  };
}

export function routingSettingsMatch(source, expected) {
  const actual = routingSettings(source);
  return Object.keys(actual).every((key) => actual[key] === (expected[key] ?? null));
}

export function restoreRoutingSettings(source, { expected, previous }) {
  if (!routingSettingsMatch(source, expected)) {
    throw Object.assign(new Error("Routing ownership changed; no settings restored"), { code: "ROUTING_CAS_MISMATCH" });
  }
  return topLevelTransform(source, routingSettings(previous));
}

export function officialDirectConfig(source) {
  return topLevelTransform(source, {
    openai_base_url: null,
    model_catalog_json: null,
  });
}

export async function atomicWrite(filePath, bytes, mode = 0o600) {
  const buffer = Buffer.isBuffer(bytes) ? bytes : Buffer.from(bytes);
  const temporary = path.join(path.dirname(filePath), `.${path.basename(filePath)}.tmp-${process.pid}-${Date.now()}`);
  const handle = await open(temporary, "wx", mode);
  try {
    await handle.writeFile(buffer);
    await handle.sync();
  } finally {
    await handle.close();
  }
  await rename(temporary, filePath);
  await chmod(filePath, mode);
  try {
    const directory = await open(path.dirname(filePath), "r");
    try { await directory.sync(); } finally { await directory.close(); }
  } catch {
    // Some macOS filesystems reject directory fsync; the file was already synced.
  }
  return { sha256: sha256(buffer), bytes: buffer.length };
}

export async function prepareOfficialDirectCandidate(configPath = CONFIG_PATH) {
  const source = await readFile(configPath, "utf8");
  const candidate = officialDirectConfig(source);
  const receipt = await atomicWrite(OFFICIAL_DIRECT_CANDIDATE_PATH, candidate, 0o600);
  return { path: OFFICIAL_DIRECT_CANDIDATE_PATH, ...receipt };
}
