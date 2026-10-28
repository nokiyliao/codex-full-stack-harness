import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { chmod, cp, lstat, mkdir, mkdtemp, open, readdir, readFile, readlink, rm, stat, utimes, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { admittedHostSnapshot, NOKIY_HTTP_HELPER } from "../src/nokiy-native-tool.mjs";

const hash = (value) => createHash("sha256").update(value).digest("hex");
const changed = { code: "NOKIY_NATIVE_HOST_CHANGED", status: 503 };

async function fixture(t, hostText = "# host v1\n") {
  const root = await mkdtemp(path.join(os.tmpdir(), "nokiy-host-snapshot-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const source = path.join(root, "src");
  const helper = "# helper\n";
  const contract = JSON.stringify({ schema_version: "c", read_http_helper_sha256: hash(helper) });
  await mkdir(source);
  await writeFile(path.join(source, "nokiy-native-host.py"), hostText);
  await writeFile(path.join(source, "nokiy-native-contract.json"), contract);
  await writeFile(path.join(source, NOKIY_HTTP_HELPER), helper);
  const host = { python: "/usr/bin/python3", script: path.join(source, "nokiy-native-host.py"), sha256: hash(hostText),
    contract: { path: path.join(source, "nokiy-native-contract.json"), sha256: hash(contract) } };
  return { root, source, host, snapshots: path.join(root, "snapshots") };
}
const versions = async (snapshots) => (await readdir(snapshots)).filter((name) => name !== "current");

test("a predecessor keeps its admitted bytes when a successor publishes a different host", async (t) => {
  const { source, host, snapshots } = await fixture(t);
  const predecessor = await admittedHostSnapshot(host, { root: snapshots });
  const nextText = "# host v2\n";
  const nextHelper = "# helper v2\n";
  const nextContract = JSON.stringify({ schema_version: "c2", read_http_helper_sha256: hash(nextHelper) });
  await writeFile(path.join(source, "nokiy-native-host.py"), nextText);
  await writeFile(path.join(source, "nokiy-native-contract.json"), nextContract);
  await writeFile(path.join(source, NOKIY_HTTP_HELPER), nextHelper);
  const nextHost = { ...host, sha256: hash(nextText), contract: { ...host.contract, sha256: hash(nextContract) } };
  const successor = await admittedHostSnapshot(nextHost, { root: snapshots });
  const current = await readlink(path.join(snapshots, "current"));
  assert.notEqual(predecessor.script, successor.script);
  assert.equal(hash(await readFile(predecessor.script)), host.sha256);
  assert.equal(hash(await readFile(successor.script)), nextHost.sha256);
  assert.equal(hash(await readFile(predecessor.contract.path)), host.contract.sha256);
  assert.equal(hash(await readFile(path.join(path.dirname(predecessor.script), NOKIY_HTTP_HELPER))), hash("# helper\n"));
  assert.deepEqual(await admittedHostSnapshot(host, { root: snapshots }), predecessor);
  assert.equal(await readlink(path.join(snapshots, "current")), current);
});

test("the admitted host runs from its physical generation, which working-copy edits do not reach", async (t) => {
  const { source, host, snapshots } = await fixture(t);
  const effective = await admittedHostSnapshot(host, { root: snapshots });
  const current = path.join(snapshots, "current");
  const physical = path.join(snapshots, await readlink(current));
  assert.equal(effective.script, path.join(physical, "nokiy-native-host.py"));
  assert.equal(effective.contract.path, path.join(physical, "nokiy-native-contract.json"));
  assert.equal(effective.python, host.python);
  assert.ok((await lstat(current)).isSymbolicLink());
  assert.equal(hash(await readFile(effective.script)), host.sha256);
  assert.equal(hash(await readFile(path.join(current, NOKIY_HTTP_HELPER))), hash("# helper\n"));
  assert.equal((await stat(effective.script)).mode & 0o777, 0o400);
  await writeFile(path.join(source, "nokiy-native-host.py"), "# edited in place\n");
  assert.deepEqual(await admittedHostSnapshot(host, { root: snapshots }), effective);
  assert.equal(hash(await readFile(effective.script)), host.sha256);
  assert.equal((await versions(snapshots)).length, 1);
});

test("without a snapshot an edited working copy fails closed", async (t) => {
  const { source, host, snapshots } = await fixture(t);
  await writeFile(path.join(source, "nokiy-native-host.py"), "# edited before any snapshot\n");
  await assert.rejects(admittedHostSnapshot(host, { root: snapshots }), changed);
  const helperless = await fixture(t);
  await rm(path.join(helperless.source, NOKIY_HTTP_HELPER));
  await assert.rejects(admittedHostSnapshot(helperless.host, { root: helperless.snapshots }), changed);
});

test("a damaged snapshot is not executed and active generations are not pruned", async (t) => {
  const { host, snapshots } = await fixture(t);
  const effective = await admittedHostSnapshot(host, { root: snapshots });
  const first = await readlink(path.join(snapshots, "current"));
  await chmod(effective.script, 0o600);
  await writeFile(effective.script, "# tampered\n");
  const repaired = await admittedHostSnapshot(host, { root: snapshots });
  assert.notEqual(repaired.script, effective.script);
  assert.equal(hash(await readFile(repaired.script)), host.sha256);
  assert.equal((await readFile(effective.script, "utf8")), "# tampered\n");
  assert.notEqual(await readlink(path.join(snapshots, "current")), first);
  let latest;
  for (const version of ["# v2\n", "# v3\n", "# v4\n"]) {
    latest = await fixture(t, version);
    await admittedHostSnapshot(latest.host, { root: snapshots });
  }
  // All were admitted by a still-live publisher and can have in-flight dispatches.
  const kept = await versions(snapshots);
  assert.equal(kept.length, 5);
  assert.ok(kept.includes(await readlink(path.join(snapshots, "current"))));
  assert.equal(hash(await readFile(path.join(snapshots, "current", "nokiy-native-host.py"))), latest.host.sha256);
});

test("inactive intact unreferenced generations keep only one previous; unknown data survives", {
  skip: process.platform !== "darwin",
}, async (t) => {
  const { host, snapshots } = await fixture(t);
  const first = await admittedHostSnapshot(host, { root: snapshots });
  const digest = path.basename(path.dirname(first.script)).split("-")[0];
  for (let index = 1; index <= 3; index++) {
    await cp(path.dirname(first.script), path.join(snapshots, `${digest}-2147483647-${String(index).padStart(8, "0")}`), { recursive: true });
  }
  const held = path.join(snapshots, `${digest}-2147483647-00000001`);
  await utimes(held, new Date(0), new Date(0));
  const handle = await open(path.join(held, "nokiy-native-host.py"), "r");
  t.after(() => handle.close());
  await mkdir(path.join(snapshots, "unknown-deliverable"));
  await writeFile(path.join(snapshots, "unknown-deliverable", "keep.txt"), "unique data");
  const next = await fixture(t, "# new publisher generation\n");
  await admittedHostSnapshot(next.host, { root: snapshots });
  assert.equal((await versions(snapshots)).filter((name) => name.includes("-2147483647-")).length, 2);
  assert.equal(hash(await readFile(path.join(held, "nokiy-native-host.py"))), host.sha256);
  assert.equal(await readFile(path.join(snapshots, "unknown-deliverable", "keep.txt"), "utf8"), "unique data");
  assert.equal(hash(await readFile(first.script)), host.sha256);
});

test("a host without an intake contract publishes only itself", async (t) => {
  const { host, snapshots } = await fixture(t);
  const bare = { python: host.python, script: host.script, sha256: host.sha256 };
  const effective = await admittedHostSnapshot(bare, { root: snapshots });
  assert.deepEqual(await readdir(path.join(snapshots, "current")), ["nokiy-native-host.py"]);
  assert.equal(effective.contract, undefined);
});

test("only the production host is snapshotted by default, and concurrent requests publish once", async (t) => {
  const { root, host, snapshots } = await fixture(t);
  assert.equal(await admittedHostSnapshot(host), host);
  assert.deepEqual(await readdir(root), ["src"]);
  const [left, right] = await Promise.all([admittedHostSnapshot(host, { root: snapshots }), admittedHostSnapshot(host, { root: snapshots })]);
  assert.deepEqual(left, right);
  assert.equal((await versions(snapshots)).length, 1);
});
