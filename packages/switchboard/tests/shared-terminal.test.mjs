import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import test from "node:test";

const sourceRoot = process.env.SWITCHBOARD_TEST_SOURCE_ROOT
  || fileURLToPath(new URL("..", import.meta.url));
const { officialErrorObservation } = await import(pathToFileURL(resolve(sourceRoot, "src/shared.mjs")));
const sha = (value) => createHash("sha256").update(value).digest("hex");
const event = (error, type = "response.failed") => type === "error"
  ? { type, error } : { type, response: { error } };

test("official terminal error observation export is available", () => {
  assert.equal(typeof officialErrorObservation, "function");
});

for (const type of ["response.failed", "response.incomplete", "error"]) {
  test(`${type} retains bounded error identity and exact complete message digest`, () => {
    const message = "FULL_MESSAGE_SECRET with whitespace\n" + String.fromCodePoint(0x96ea);
    const error = { code: "server_error", type: "server_error", message,
      headers: { "Retry-After": "HEADER_VALUE_SECRET", authorization: "AUTH_SECRET" } };
    const input = event(error, type), preimage = structuredClone(input);
    const observed = officialErrorObservation(input);
    assert.deepEqual(observed, { code: "server_error", type: "server_error",
      message_sha256: sha(message), message_bytes: Buffer.byteLength(message),
      message_hash_status: "hashed", retry_after_present: true });
    assert.deepEqual(input, preimage);
    const serialized = JSON.stringify(observed);
    for (const secret of [message, "HEADER_VALUE_SECRET", "AUTH_SECRET", "authorization"])
      assert.equal(serialized.includes(secret), false);
  });
}

test("URL, path, whitespace, overlong and nonstring error labels cannot enter diagnostics", () => {
  for (const label of ["https://secret.invalid/code", "/private/path", "with space", "bad\nlabel",
    "a".repeat(81), "", 42, null, {}, ["server_error"]]) {
    const observed = officialErrorObservation(event({ code: label, type: label, message: "message" }));
    assert.equal(observed.code, null);
    assert.equal(observed.type, null);
  }
  const label = "A" + "a".repeat(79);
  assert.equal(officialErrorObservation(event({ code: label, type: "server-error_1" })).code, label);
  assert.equal(officialErrorObservation(event({ code: label, type: "server-error_1" })).type, "server-error_1");
});

test("message hashing is UTF8-byte bounded, never prefix hashing or raw message output", () => {
  for (const message of ["", "a".repeat(65_536), String.fromCodePoint(0x96ea).repeat(21_845)]) {
    const observed = officialErrorObservation(event({ message }));
    assert.equal(observed.message_hash_status, "hashed");
    assert.equal(observed.message_bytes, Buffer.byteLength(message));
    assert.equal(observed.message_sha256, sha(message));
  }
  for (const message of ["a".repeat(65_537), String.fromCodePoint(0x96ea).repeat(21_846)]) {
    const observed = officialErrorObservation(event({ message }));
    assert.equal(observed.message_hash_status, "too_large");
    assert.equal(observed.message_bytes, Buffer.byteLength(message));
    assert.equal(observed.message_sha256, null);
    assert.equal(Object.hasOwn(observed, "message"), false);
  }
  for (const message of [undefined, null, 5, {}, ["message"]]) {
    const observed = officialErrorObservation(event({ message }));
    assert.equal(observed.message_hash_status, "missing");
    assert.equal(observed.message_bytes, null);
    assert.equal(observed.message_sha256, null);
  }
});

test("Retry-After exposes presence only, ignores invalid header containers", () => {
  for (const name of ["Retry-After", "retry-after", "RETRY-AFTER"]) {
    const observed = officialErrorObservation(event({ headers: { [name]: "VALUE_SECRET" } }));
    assert.equal(observed.retry_after_present, true);
    assert.equal(JSON.stringify(observed).includes("VALUE_SECRET"), false);
  }
  for (const headers of [undefined, null, "retry-after", ["retry-after"], { other: "VALUE_SECRET" }])
    assert.equal(officialErrorObservation(event({ headers })).retry_after_present, false);
});

test("unrelated terminal events and missing or malformed error objects are not error observations", () => {
  for (const type of ["response.completed", "response.created", "response.output_text.delta"])
    assert.equal(officialErrorObservation(event({ code: "server_error" }, type)), null);
  for (const error of [undefined, null, "server_error", []])
    assert.equal(officialErrorObservation(event(error)), null);
  assert.equal(officialErrorObservation(null), null);
});
