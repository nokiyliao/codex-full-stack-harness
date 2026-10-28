import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, mkdir, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { isOfficialAppServerCommand } from "../src/post-restart.mjs";

test("post-restart recognizes a native payload reached through bin/.. and symlinks", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "switchboard-app-process-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  await mkdir(join(root, "bin"));
  const payload = join(root, "codex");
  await writeFile(payload, "native fixture");
  const alias = join(root, "codex-alias");
  await symlink(payload, alias);
  assert.equal(await isOfficialAppServerCommand(`${root}/bin/../codex -c features.test=true app-server --listen stdio://`, payload), true);
  assert.equal(await isOfficialAppServerCommand(`${alias} app-server`, payload), true);
  assert.equal(await isOfficialAppServerCommand(`${payload} debug models`, payload), false);
  assert.equal(await isOfficialAppServerCommand(`${root}/missing app-server`, payload), false);
  await writeFile(join(root, "other"), "different native fixture");
  assert.equal(await isOfficialAppServerCommand(`${root}/other app-server`, payload), false);
});
