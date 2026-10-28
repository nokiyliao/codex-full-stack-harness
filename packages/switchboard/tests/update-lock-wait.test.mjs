// The shared update lock waits out a brief holder and still refuses a persistent one.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const source = await readFile(new URL("../src/update-lock.mjs", import.meta.url), "utf8");
function load(fake) {
  const body = source.replace(/^import [^\n]+;\n/gm, "").replace(/^export /gm, "");
  return new Function("open", "unlink", "process", body + "\nreturn { withUpdateLock, UPDATE_LOCK };")(fake.open, fake.unlink, fake.process);
}
function fakeFs(env) {
  const files = new Map();
  const err = (code) => Object.assign(new Error(code), { code });
  return { files, process: { pid: 7, env },
    open: async (file, flag) => {
      if (flag === "wx" && files.has(file)) throw err("EEXIST");
      files.set(file, "");
      return { writeFile: async (text) => { files.set(file, text); }, close: async () => {} };
    },
    unlink: async (file) => { files.delete(file); } };
}

test("a briefly held lock is waited out, then acquired and released", async () => {
  const fs = fakeFs({ SWITCHBOARD_UPDATE_LOCK_WAIT_MS: "2000" });
  const { withUpdateLock, UPDATE_LOCK } = load(fs);
  fs.files.set(UPDATE_LOCK, "watcher");
  setTimeout(() => fs.files.delete(UPDATE_LOCK), 250);
  const started = Date.now();
  assert.equal(await withUpdateLock(async () => "done"), "done");
  assert.ok(Date.now() - started >= 200);
  assert.equal(fs.files.has(UPDATE_LOCK), false);
});

test("a persistent holder is refused with EEXIST after the bound and keeps its lock", async () => {
  const fs = fakeFs({ SWITCHBOARD_UPDATE_LOCK_WAIT_MS: "300" });
  const { withUpdateLock, UPDATE_LOCK } = load(fs);
  fs.files.set(UPDATE_LOCK, "other-owner");
  let ran = false;
  await assert.rejects(withUpdateLock(async () => { ran = true; }), { code: "EEXIST" });
  assert.equal(ran, false);
  assert.equal(fs.files.get(UPDATE_LOCK), "other-owner");
});

