// Launchd stdout log rotation: own file only, whole-line tail kept, O_APPEND continues at the new end.
import assert from "node:assert/strict";
import { test } from "node:test";
import { closeSync, mkdtempSync, openSync, readFileSync, rmSync, statSync, writeFileSync, writeSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { rotateAppendLog } from "../src/log-rotation.mjs";

const lines = (count) => Array.from({ length: count }, (_, index) => `{"line":${index}}\n`).join("");

test("rotation keeps a whole-line tail and appends from the truncated end", () => {
  const directory = mkdtempSync(join(tmpdir(), "log-rotation-"));
  try {
    const path = join(directory, "switchboard.stdout.log");
    writeFileSync(path, lines(40));
    const fd = openSync(path, "a");
    try {
      assert.equal(rotateAppendLog(path, { maxBytes: 100, keepBytes: 50, fd }), true);
      assert.equal(statSync(path).size, 0);
      const kept = readFileSync(`${path}.1`, "utf8");
      assert.ok(kept.length > 0 && kept.length <= 50);
      assert.ok(kept.startsWith("{\"line\":") && kept.endsWith("{\"line\":39}\n"));
      writeSync(fd, "after\n");
      assert.equal(readFileSync(path, "utf8"), "after\n");
    } finally {
      closeSync(fd);
    }
  } finally {
    rmSync(directory, { recursive: true });
  }
});

test("rotation never touches a file that is not this descriptor or is under the limit", () => {
  const directory = mkdtempSync(join(tmpdir(), "log-rotation-"));
  try {
    const path = join(directory, "switchboard.stdout.log");
    const other = join(directory, "other.log");
    writeFileSync(path, lines(40));
    writeFileSync(other, "x");
    const fd = openSync(other, "a");
    try {
      assert.equal(rotateAppendLog(path, { maxBytes: 100, keepBytes: 50, fd }), false);
      assert.equal(readFileSync(path, "utf8"), lines(40));
    } finally {
      closeSync(fd);
    }
    const own = openSync(path, "a");
    try {
      assert.equal(rotateAppendLog(path, { maxBytes: 10_000, keepBytes: 50, fd: own }), false);
      assert.equal(readFileSync(path, "utf8"), lines(40));
      assert.equal(rotateAppendLog(join(directory, "missing.log"), { maxBytes: 1, fd: own }), false);
    } finally {
      closeSync(own);
    }
  } finally {
    rmSync(directory, { recursive: true });
  }
});

