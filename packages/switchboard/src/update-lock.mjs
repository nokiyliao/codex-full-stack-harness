import { open, unlink } from "node:fs/promises";

export const UPDATE_LOCK = "/Users/nokiy/.codex-multi/switchboard/runtime/workflow-update.lock";

// The admission watcher holds this lock for a few seconds on every check. Wait that out
// (bounded) instead of failing a deployment phase; never take over another owner's lock.
const LOCK_POLL_MS = 100;

export async function withUpdateLock(action) {
  const waitMs = Number(process.env.SWITCHBOARD_UPDATE_LOCK_WAIT_MS ?? 10000);
  const deadline = Date.now() + (Number.isFinite(waitMs) && waitMs >= 0 ? waitMs : 10000);
  let lock;
  for (;;) {
    try { lock = await open(UPDATE_LOCK, "wx", 0o600); break; }
    catch (error) {
      if (error?.code !== "EEXIST" || Date.now() >= deadline) throw error;
      await new Promise((resolve) => setTimeout(resolve, LOCK_POLL_MS));
    }
  }
  try {
    await lock.writeFile(JSON.stringify({ pid: process.pid, thread: process.env.CODEX_THREAD_ID || null }));
    return await action();
  } finally {
    await lock.close();
    await unlink(UPDATE_LOCK);
  }
}
