// Switchboard-generated deterministic errors reach Codex as 400 so it neither replays them
// nor disables WebSockets for the session; retryable conflicts and server errors keep their status.
import assert from "node:assert/strict";
import { test } from "node:test";
import { clientWireStatus } from "../src/server.mjs";
import { SwitchboardError } from "../src/shared.mjs";

test("deterministic validation errors are reported to the client as 400", () => {
  for (const status of [413, 415, 422]) {
    assert.equal(clientWireStatus(new SwitchboardError("X", "x", status)), 400);
  }
});

test("retryable conflicts, cancellation and server errors keep their status", () => {
  for (const status of [400, 401, 403, 404, 408, 409, 429, 499, 500, 502, 503]) {
    assert.equal(clientWireStatus(new SwitchboardError("X", "x", status)), status);
  }
  assert.equal(clientWireStatus(new Error("untyped")), 502);
});

