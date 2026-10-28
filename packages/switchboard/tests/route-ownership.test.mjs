import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { parse } from "smol-toml";
import { CATALOG_PATH } from "../src/catalog.mjs";
import { PICKER_CATALOG_PATH } from "../src/picker-catalog.mjs";
import { assertRoutingOwnership, synchronizeRouting } from "../src/route-ownership.mjs";

const runtime = { port: 43130, installation_capability: "test-capability" };
const base = "http://127.0.0.1:43130/test-capability/backend-api/codex/v1";
const original = `openai_base_url = "${base}"\nmodel_catalog_json = "${CATALOG_PATH}"\nmodel = "gpt-6.1-sol"\nmodel_reasoning_effort = "max"\n[features]\nmulti_agent = true\n`;

test("routing owner rejects unrelated endpoints and later catalog takeover", () => {
  assert.throws(() => assertRoutingOwnership(original.replace("43130", "19480"), runtime), { code: "ROUTING_OWNER_CONFLICT" });
  const expected = assertRoutingOwnership(original, runtime);
  assert.equal(expected.model_catalog_json, PICKER_CATALOG_PATH);
  assert.throws(() => assertRoutingOwnership(original, runtime, { owner: "switchboard", expected }), { code: "ROUTING_CAS_MISMATCH" });
});

async function fixture(t) {
  const dir = await mkdtemp(path.join(tmpdir(), "switchboard-ownership-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const paths = { configPath: path.join(dir, "config.toml"), statePath: path.join(dir, "state.json"), runtimePath: path.join(dir, "runtime.json") };
  await writeFile(paths.configPath, original, { mode: 0o600 });
  await writeFile(paths.statePath, JSON.stringify({ state: "ACTIVE_VERIFIED", activation_id: "original-identity" }));
  await writeFile(paths.runtimePath, JSON.stringify(runtime));
  return paths;
}

test("single owner publishes unified picker and preserves user settings and activation identity", async (t) => {
  const paths = await fixture(t);
  let publications = 0;
  const options = { ...paths, loadBridge: async () => ({ models: new Set(["claude-haiku"]) }),
    publish: async ({ claudeBridge }) => { publications++; return { claude: Boolean(claudeBridge) }; } };
  const result = await synchronizeRouting(options);
  const config = parse(await readFile(paths.configPath, "utf8"));
  const state = JSON.parse(await readFile(paths.statePath, "utf8"));
  assert.equal(result.restart_required, true);
  assert.equal(config.openai_base_url, base);
  assert.equal(config.model_catalog_json, PICKER_CATALOG_PATH);
  assert.equal(config.model_reasoning_effort, "max");
  assert.equal(config.features.multi_agent, true);
  assert.equal(state.activation_id, "original-identity");
  assert.equal(state.routing_binding.status, "CONFIGURED");
  const repeated = await synchronizeRouting(options);
  assert.equal(repeated.changed, false);
  assert.equal(publications, 2);
  const disabled = await synchronizeRouting({ ...options, claudeEnabled: false });
  assert.equal(disabled.claude_enabled, false);
  assert.equal(disabled.catalog.claude, false);
  assert.equal(parse(await readFile(paths.configPath, "utf8")).openai_base_url, base);
});

test("publication races cannot overwrite newer configuration", async (t) => {
  const paths = await fixture(t);
  const changed = original.replace('effort = "max"', 'effort = "high"');
  await assert.rejects(() => synchronizeRouting({ ...paths, loadBridge: async () => ({}),
    publish: async () => { await writeFile(paths.configPath, changed); return {}; } }), { code: "CONFIG_CAS_MISMATCH" });
  assert.equal(await readFile(paths.configPath, "utf8"), changed);
});
