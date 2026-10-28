// Bounded in-place rotation for the launchd StandardOutPath log. Launchd opens
// that file O_APPEND, so after truncation the next write lands at the new end
// instead of leaving a sparse hole at the old offset. Only the file that is this
// process's own stdout (same device and inode) is ever touched.
import { closeSync, fstatSync, openSync, readSync, statSync, truncateSync, writeFileSync } from "node:fs";

const MiB = 2 ** 20;

export function rotateAppendLog(path, { maxBytes = 64 * MiB, keepBytes = 32 * MiB, fd = 1 } = {}) {
  let file, own;
  try {
    file = statSync(path);
    own = fstatSync(fd);
  } catch {
    return false;
  }
  if (!file.isFile() || file.ino !== own.ino || file.dev !== own.dev || file.size <= maxBytes) return false;
  const keep = Math.min(keepBytes, file.size);
  const tail = Buffer.alloc(keep);
  const reader = openSync(path, "r");
  try {
    let read = 0;
    while (read < keep) {
      const count = readSync(reader, tail, read, keep - read, file.size - keep + read);
      if (count === 0) break;
      read += count;
    }
  } finally {
    closeSync(reader);
  }
  // Keep whole lines only: drop the partial first line of the retained tail.
  const lineStart = tail.indexOf(0x0a) + 1;
  writeFileSync(`${path}.1`, lineStart > 0 ? tail.subarray(lineStart) : tail);
  truncateSync(path, 0);
  return true;
}

export function startStdoutLogRotation(path, { intervalMs = 10 * 60_000, startupMaxBytes = 64 * MiB,
  runningMaxBytes = 256 * MiB, keepBytes = 32 * MiB } = {}) {
  const rotate = (maxBytes) => {
    try {
      rotateAppendLog(path, { maxBytes, keepBytes });
    } catch {
      // Log housekeeping must never interrupt serving requests.
    }
  };
  // Rotating at startup cannot split a reader's window: a restart already ends it.
  rotate(startupMaxBytes);
  // While running, rotate only past a hard cap. Readers that pinned an offset in
  // this file (the benchmark collector's provider_log_path) fail closed if it shrinks.
  const timer = setInterval(() => rotate(runningMaxBytes), intervalMs);
  timer.unref();
  return timer;
}

