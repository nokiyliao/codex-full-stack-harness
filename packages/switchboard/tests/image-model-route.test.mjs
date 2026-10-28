import assert from "node:assert/strict";
import { test } from "node:test";
import { projectImageModel } from "../src/image-model-route.mjs";
for (const endpoint of ["/images/generations", "/images/edits"]) {
  test(`${endpoint}: legacy image tool selects 2.5 and preserves payload`, () => {
    const body = { model: "gpt-image-2", prompt: "garment hunter", image: [{ opaque: "input" }], size: "auto", background: "transparent" };
    const before = structuredClone(body);
    const result = projectImageModel(body, endpoint);
    assert.deepEqual(body, before);
    assert.deepEqual(result.body, { ...before, model: "gpt-image-2.5-flare" });
    assert.equal(result.requestedModel, "gpt-image-2");
  });
  test(`${endpoint}: explicit 2.5 variants remain unchanged`, () => {
    for (const model of ["gpt-image-2.5-flare", "gpt-image-2.5-sunburst", "gpt-image-2.5-flare-2026-09-08", "gpt-image-2.5-sunburst-2026-09-08"])
      assert.deepEqual(projectImageModel({ model }, endpoint).body, { model });
    for (const model of ["unknown", "gpt-image-2.5-typo", "gpt-6-astra", undefined])
      assert.equal(projectImageModel({ model }, endpoint), null);
  });
}
test("image routes never widen text, realtime or arbitrary endpoints", () => {
  for (const endpoint of ["/responses", "/responses/compact", "/realtime/calls", "/alpha/search", "/models", "/other"])
    for (const model of ["gpt-image-2", "gpt-image-2.5-flare"])
      assert.equal(projectImageModel({ model }, endpoint), null);
  for (const body of [null, [], "bad"])
    assert.equal(projectImageModel(body, "/images/edits"), null);
});

test("HTTP image edit forwards to official upstream with 2.5 and unchanged auth", async (t) => {
  const { default: http } = await import("node:http");
  const { createSwitchboardServer } = await import("../src/server.mjs");
  let received;
  const upstream = http.createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    received = { path: req.url, body: JSON.parse(Buffer.concat(chunks)), auth: req.headers.authorization };
    res.writeHead(200, { "content-type": "application/json" }); res.end(JSON.stringify({ data: [] }));
  });
  await new Promise(resolve => upstream.listen(0, "127.0.0.1", resolve));
  const worker = { close() {} };
  const router = await createSwitchboardServer({
    runtime: { installation_capability: "image-route-fixture", host: "127.0.0.1", port: 0 },
    admission: { status: "ADMITTED", official_models: ["gpt-6-astra"] },
    skipFileGate: true, officialOrigin: `http://127.0.0.1:${upstream.address().port}`,
    xaiWorker: worker, agyWorker: worker, openrouterWorker: worker, webWorker: worker,
  });
  t.after(async () => { await router.close(); await new Promise(resolve => upstream.close(resolve)); });
  const address = await router.listen({ host: "127.0.0.1", port: 0 });
  const body = { model: "gpt-image-2", prompt: "garment hunter", images: [{ opaque: "reference" }] };
  const response = await fetch(`http://127.0.0.1:${address.port}/image-route-fixture/backend-api/codex/v1/images/edits`, {
    method: "POST", headers: { "content-type": "application/json", authorization: "Bearer test-only-canary" }, body: JSON.stringify(body),
  });
  assert.equal(response.status, 200);
  assert.equal(received.path, "/backend-api/codex/images/edits");
  assert.deepEqual(received.body, { ...body, model: "gpt-image-2.5-flare" });
  assert.equal(received.auth, "Bearer test-only-canary");
});
