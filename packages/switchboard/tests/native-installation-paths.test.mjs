import assert from "node:assert/strict";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";

import { resolveOfficialCliPaths } from "../src/catalog.mjs";

const macho = Buffer.from([0xcf, 0xfa, 0xed, 0xfe, 0, 0, 0, 0]);

async function fixture(t) {
  const app = await mkdtemp(join(tmpdir(), "switchboard-native-cli-"));
  t.after(() => rm(app, { recursive: true, force: true }));
  const resources = join(app, "Contents/Resources");
  await mkdir(resources, { recursive: true });
  return {
    app,
    launcher: join(resources, "codex-cli/bin/codex"),
    payload: join(resources, "codex-cli/CodexCLI.app/Contents/MacOS/codex"),
    legacy: join(resources, "codex"),
  };
}

async function executable(path, bytes) {
  await mkdir(dirname(path), { recursive: true });
  await writeFile(path, bytes, { mode: 0o755 });
}

test("new package resolves the signed executable separately from the launch entry", async (t) => {
  const { app, launcher, payload } = await fixture(t);
  await executable(launcher, '#!/bin/sh\nexec "$(dirname "$0")/../CodexCLI.app/Contents/MacOS/codex" "$@"\n');
  await executable(payload, macho);
  assert.deepEqual(await resolveOfficialCliPaths(app), { launch_path: launcher, cli_path: payload });
});

test("legacy package remains supported and resolution is fresh each time", async (t) => {
  const { app, launcher, payload, legacy } = await fixture(t);
  await executable(legacy, macho);
  assert.deepEqual(await resolveOfficialCliPaths(app), { launch_path: legacy, cli_path: legacy });
  await executable(launcher, '#!/bin/sh\nexec ../CodexCLI.app/Contents/MacOS/codex "$@"\n');
  await executable(payload, macho);
  assert.deepEqual(await resolveOfficialCliPaths(app), { launch_path: launcher, cli_path: payload });
  await rm(launcher);
  await rm(join(app, "Contents/Resources/codex-cli"), { recursive: true });
  assert.deepEqual(await resolveOfficialCliPaths(app), { launch_path: legacy, cli_path: legacy });
});

test("missing official CLI package is a typed inspection failure", async (t) => {
  const { app } = await fixture(t);
  await assert.rejects(resolveOfficialCliPaths(app), { code: "CLI_LAYOUT_MISSING" });
});

test("bad or incomplete packaged candidate never falls through to legacy", async (t) => {
  const { app, launcher, payload, legacy } = await fixture(t);
  await executable(legacy, macho);
  await executable(launcher, "#!/bin/sh\nexec /somewhere/else/codex\n");
  await executable(payload, macho);
  await assert.rejects(resolveOfficialCliPaths(app), { code: "CLI_LAYOUT_INVALID" });
  await writeFile(launcher, "#!/bin/sh\n# exec ../CodexCLI.app/Contents/MacOS/codex\nexec /somewhere/else/codex\n");
  await assert.rejects(resolveOfficialCliPaths(app), { code: "CLI_LAYOUT_INVALID" });
  await writeFile(payload, "not a Mach-O");
  await assert.rejects(resolveOfficialCliPaths(app), { code: "CLI_LAYOUT_INVALID" });
  await rm(launcher);
  await assert.rejects(resolveOfficialCliPaths(app), { code: "CLI_LAYOUT_INVALID" });
  await rm(payload);
  await assert.rejects(resolveOfficialCliPaths(app), { code: "CLI_LAYOUT_INVALID" });
});
