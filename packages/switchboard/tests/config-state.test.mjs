import assert from "node:assert/strict";
import test from "node:test";

import { parse } from "smol-toml";

import {
  bridgeConfig,
  configEquivalentModuloAppManagedMetadata,
  officialDirectConfig,
  restoreRoutingSettings,
  routingSettings,
  routingSettingsMatch,
} from "../src/config-state.mjs";

test("config transforms only the two top-level routing keys", () => {
  const source = [
    "approval_policy = \"never\"",
    "model_catalog_json = \"/legacy/catalog.json\"",
    "",
    "[features]",
    "multi_agent = true",
    "",
  ].join("\n");
  const bridged = bridgeConfig(source, {
    openaiBaseUrl: "http://127.0.0.1:43130/cap/backend-api/codex/v1",
    catalogPath: "/candidate/catalog.json",
  });
  const parsed = parse(bridged);
  assert.equal(parsed.openai_base_url, "http://127.0.0.1:43130/cap/backend-api/codex/v1");
  assert.equal(parsed.model_catalog_json, "/candidate/catalog.json");
  assert.equal(parsed.features.multi_agent, true);
  assert.equal((bridged.match(/^model_catalog_json\s*=/gm) || []).length, 1);
  const direct = officialDirectConfig(bridged);
  const directParsed = parse(direct);
  assert.equal(directParsed.openai_base_url, undefined);
  assert.equal(directParsed.model_catalog_json, undefined);
  assert.equal(directParsed.features.multi_agent, true);
});

test("route rollback preserves later user settings and rejects a different routing owner", () => {
  const original = 'model = "gpt-6.1-sol"\nmodel_reasoning_effort = "max"\n';
  const expected = { openai_base_url: "http://127.0.0.1:43130/cap/backend-api/codex/v1", model_catalog_json: "/models/unified.json" };
  const installed = bridgeConfig(original, { openaiBaseUrl: expected.openai_base_url, catalogPath: expected.model_catalog_json });
  const edited = installed.replace('effort = "max"', 'effort = "high"');
  assert.equal(routingSettingsMatch(edited, expected), true);
  const restored = restoreRoutingSettings(edited, { expected, previous: routingSettings(original) });
  assert.equal(parse(restored).model_reasoning_effort, "high");
  assert.deepEqual(routingSettings(restored), { openai_base_url: null, model_catalog_json: null });
  assert.throws(() => restoreRoutingSettings(edited.replace("43130", "9999"), { expected, previous: routingSettings(original) }),
    { code: "ROUTING_CAS_MISMATCH" });
});

test("post-restart config comparison allows only App-managed marketplace metadata", () => {
  const expected = {
    openai_base_url: "http://127.0.0.1:43130/cap/backend-api/codex/v1",
    marketplaces: {
      official: { source: "official", last_updated: "old", last_revision: "abc" },
    },
  };
  const appUpdated = structuredClone(expected);
  appUpdated.marketplaces.official.last_updated = "new";
  appUpdated.marketplaces.official.last_revision = "def";
  assert.equal(configEquivalentModuloAppManagedMetadata(expected, appUpdated), true);

  const routingDrift = structuredClone(appUpdated);
  routingDrift.openai_base_url = "https://unexpected.example/v1";
  assert.equal(configEquivalentModuloAppManagedMetadata(expected, routingDrift), false);
});

test("App-managed CLI launcher rewrite is accepted only for a verified official path pair", () => {
  const officialCliPaths = { launch_path: "/signed-app/bin/codex", cli_path: "/signed-app/payload/codex" };
  const expected = { model: "gpt-fixture", mcp_servers: { node_repl: { env: { CODEX_CLI_PATH: officialCliPaths.cli_path } } } };
  const actual = structuredClone(expected);
  actual.mcp_servers.node_repl.env.CODEX_CLI_PATH = officialCliPaths.launch_path;
  assert.equal(configEquivalentModuloAppManagedMetadata(expected, actual), false);
  assert.equal(configEquivalentModuloAppManagedMetadata(expected, actual, { officialCliPaths }), true);
  assert.equal(expected.mcp_servers.node_repl.env.CODEX_CLI_PATH, officialCliPaths.cli_path);
  actual.mcp_servers.node_repl.env.CODEX_CLI_PATH = "/unrelated/codex";
  assert.equal(configEquivalentModuloAppManagedMetadata(expected, actual, { officialCliPaths }), false);
  actual.mcp_servers.node_repl.env.CODEX_CLI_PATH = officialCliPaths.launch_path;
  actual.model = "unexpected-model";
  assert.equal(configEquivalentModuloAppManagedMetadata(expected, actual, { officialCliPaths }), false);
});
