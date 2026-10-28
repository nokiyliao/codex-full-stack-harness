import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { chmodSync, mkdirSync, mkdtempSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const SCRIPT = fileURLToPath(new URL("../scripts/nokiy-nav.py", import.meta.url));
const PYTHON = execFileSync("/usr/bin/env", ["which", "python3"], { encoding: "utf8" }).trim();

function run(cwd, args, env = {}) {
  return spawnSync(PYTHON, ["-I", SCRIPT, ...args], { cwd, encoding: "utf8", env: { ...process.env, ...env } });
}

function executable(file, body) {
  writeFileSync(file, body);
  chmodSync(file, 0o755);
}

test("DCF backend prints exact definitions first and omits target-less call sites", () => {
  const ws = mkdtempSync(path.join(tmpdir(), "nokiy-nav-dcf-"));
  mkdirSync(path.join(ws, "scripts/ops"), { recursive: true });
  mkdirSync(path.join(ws, "output/dcf_runtime/v2"), { recursive: true });
  mkdirSync(path.join(ws, ".venv/bin"), { recursive: true });
  writeFileSync(path.join(ws, "output/dcf_runtime/v2/current.json"), "{}");
  symlinkSync(PYTHON, path.join(ws, ".venv/bin/python"));
  const payload = { freshness_status: "current", generation_id: "20261026T0145-abc", result: {
    _projection: { next_offset: 13 },
    definitions: [
      { path: "pkg/b.py", line: 5, end_line: 9, kind: "python_function", symbol: "pkg.b.helper_main" },
      { path: "pkg/a.py", line: 20, end_line: 40, kind: "python_function", symbol: "pkg.a.main" },
    ],
    reference_fields: ["path", "line", "caller", "targets", "confidence", "reason", "direction"],
    references: [["pkg/a.py", 22, "pkg.a.main", "pkg.b.helper_main", "exact", "scope", "out"],
      ["pkg/a.py", 23, "pkg.a.main", "", "unresolved", "shadowed", "out"]],
    reference_count: 2 } };
  writeFileSync(path.join(ws, "scripts/ops/dcf.py"), "import json\nprint(json.dumps(" + JSON.stringify(payload) + "))\n");
  const out = run(ws, ["main", "--refs", "-C", ws]);
  assert.equal(out.status, 0, out.stderr);
  const lines = out.stdout.trim().split("\n");
  assert.match(lines[0], /^# main: dcf current .*\| 2 definitions \| refs 1 shown of 2 \(1 unresolved omitted\) \| more: --page 13$/);
  assert.deepEqual(lines.slice(1, 3), ["pkg/a.py", "        20-40 fn main"]);
  assert.ok(lines.includes("  pkg/a.py:22 main -> pkg.b.helper_main"));
  assert.ok(!out.stdout.includes("shadowed"));
});

test("caller backend reports deferred scopes and exits 1 when nothing matches", () => {
  const ws = mkdtempSync(path.join(tmpdir(), "nokiy-nav-map-"));
  const caller = path.join(ws, "fake-caller");
  executable(caller, "#!/bin/sh\necho '" + JSON.stringify({ status: "NAVIGATION_AVAILABLE", map_sha256: "0397b0f7aa",
    coverage: { deferred_scopes: ["vendor", "tests"], unsupported_extensions: { ".md": 3 } }, symbols: [], next_offset: null }) + "'\n");
  const out = run(ws, ["absent_name", "-C", ws], { NOKIY_NAV_CALLER: caller });
  assert.equal(out.status, 1, out.stderr);
  assert.match(out.stdout, /^# absent_name: nokiy-map 0397b0f7 \| 0 definitions/);
  assert.match(out.stdout, /# map deferred 2 scopes \(not searched; narrow with --in\): vendor, tests/);
  assert.match(out.stdout, /# unsupported: \.md/);
});
