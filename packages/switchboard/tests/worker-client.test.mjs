import assert from "node:assert/strict";
import { chmod, mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { IsolatedWorkerClient } from "../src/worker-client.mjs";

test("one timed-out session does not terminate unrelated worker requests", async (t) => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "switchboard-worker-timeout-"));
  const workerPath = path.join(fixture, "worker.mjs");
  await writeFile(workerPath, `
const active = new Map();
process.on("message", (job) => {
  if (job.op === "close_session") {
    const timer = active.get(job.session_key);
    if (timer) clearTimeout(timer);
    active.delete(job.session_key);
    process.send?.({ id: job.id, kind: "done", result: { status: 200 } });
    return;
  }
  const delay = Number(job.body?.delay_ms || 0);
  const timer = setTimeout(() => {
    active.delete(job.session_key);
    process.send?.({ id: job.id, kind: "done", result: { status: 200, session: job.session_key } });
  }, delay);
  active.set(job.session_key, timer);
});
`, { mode: 0o700 });
  await chmod(workerPath, 0o700);
  const worker = new IsolatedWorkerClient({ script: workerPath, label: "timeout-isolation-test" });
  t.after(async () => {
    worker.close();
    await rm(fixture, { recursive: true, force: true });
  });

  const timedOut = worker.runResponses({ delay_ms: 5_000 }, {
    sessionKey: "slow-session",
    timeoutMs: 30,
  });
  const unrelated = worker.runResponses({ delay_ms: 100 }, {
    sessionKey: "healthy-session",
    timeoutMs: 1_000,
  });
  await assert.rejects(timedOut, (error) => error.code === "EXTERNAL_WORKER_TIMEOUT");
  assert.deepEqual(await unrelated, { status: 200, session: "healthy-session", events: [] });
});
