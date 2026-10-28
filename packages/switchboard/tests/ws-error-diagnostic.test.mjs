import assert from "node:assert/strict";
import test from "node:test";

import { untypedErrorDiagnostic } from "../src/server.mjs";
import { SwitchboardError } from "../src/shared.mjs";

test("untyped errors are located by name and stack frames, never by message", () => {
  let thrown;
  const chain = null;
  try { void chain.responseId; } catch (error) { thrown = error; }
  const diagnostic = untypedErrorDiagnostic(thrown);
  assert.equal(diagnostic.name, "TypeError");
  assert.ok(diagnostic.site.length >= 1);
  assert.ok(diagnostic.site.every((line) => line.startsWith("at ")));
  assert.equal(JSON.stringify(diagnostic).includes("Cannot read"), false);
  assert.deepEqual(untypedErrorDiagnostic(new SwitchboardError("TYPED", "typed")), {});
  assert.deepEqual(untypedErrorDiagnostic("text"), { name: "string", site: [] });
});
