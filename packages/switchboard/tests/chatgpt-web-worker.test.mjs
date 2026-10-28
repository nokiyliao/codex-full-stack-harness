import assert from "node:assert/strict";
import test from "node:test";

import { ChatGptWebWorker } from "../src/chatgpt-web-worker.mjs";
import { SwitchboardError } from "../src/shared.mjs";
import { createCanonicalCompaction, normalizeCompactions } from "../src/history-neutral.mjs";

function sse(events) {
  return Buffer.from(events.map((event) => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`).join(""));
}

test("ChatGPT Web worker forwards the native Responses body and preserves provider identity", async () => {
  const calls = [];
  const events = [];
  const worker = new ChatGptWebWorker({
    endpoint: "http://web.invalid/v1/responses",
    transport: async (call) => {
      calls.push(call);
      call.markSubmitted();
      return {
        status: 200,
        headers: { "content-type": "text/event-stream" },
        body: sse([
          { type: "response.created", response: { id: "resp_web_1" } },
          { type: "response.completed", response: { id: "resp_web_1", output: [] } },
        ]),
      };
    },
  });
  const nativeMetadata = { "x-codex-turn-metadata": "{\"turn_id\":\"turn-1\"}" };
  const result = await worker.runResponses({
    model: "chatgpt-web/high",
    input: [],
    client_metadata: nativeMetadata,
  }, {
    deliveryId: "delivery-1",
    onEvent: (event) => events.push(event),
  });

  assert.equal(calls.length, 1);
  assert.equal(calls[0].headers["idempotency-key"], "chatgpt-web:delivery-1");
  assert.equal(calls[0].endpoint, "http://web.invalid/v1/responses");
  assert.deepEqual(JSON.parse(calls[0].payload), {
    model: "chatgpt-web/high",
    input: [],
    client_metadata: nativeMetadata,
    stream: true,
  });
  assert.equal(result.outbound_identity, "chatgpt-web:delivery-1");
  assert.equal(result.provider_response_id, "resp_web_1");
  assert.equal(events.at(-1).type, "response.completed");
  assert.equal(events.at(-1).response.id, "resp_web_1");
});

test("ChatGPT Web worker distinguishes pre-submit failure from submitted uncertainty without retry", async () => {
  let preSubmitCalls = 0;
  const preSubmit = new ChatGptWebWorker({
    endpoint: "http://web.invalid/v1/responses",
    transport: async () => {
      preSubmitCalls += 1;
      throw new SwitchboardError("WEB_PROVIDER_UNAVAILABLE", "Injected before submission.");
    },
  });
  await assert.rejects(
    preSubmit.runResponses({ model: "chatgpt-web/high", input: [] }, { deliveryId: "pre" }),
    (error) => error.code === "WEB_PROVIDER_UNAVAILABLE",
  );
  assert.equal(preSubmitCalls, 1);

  let uncertainCalls = 0;
  const uncertain = new ChatGptWebWorker({
    endpoint: "http://web.invalid/v1/responses",
    transport: async ({ markSubmitted }) => {
      uncertainCalls += 1;
      markSubmitted();
      throw new Error("Injected connection loss after submission.");
    },
  });
  await assert.rejects(
    uncertain.runResponses({ model: "chatgpt-web/pro", input: [] }, { deliveryId: "uncertain" }),
    (error) => error.code === "WEB_DELIVERY_UNCERTAIN"
      && error.details.outbound_identity === "chatgpt-web:uncertain"
      && error.details.retry_allowed === false
      && error.details.fallback_allowed === false,
  );
  assert.equal(uncertainCalls, 1);
});

test("ChatGPT Web worker treats an HTTP rejection as known and does not retry", async () => {
  const calls = [];
  const worker = new ChatGptWebWorker({
    endpoint: "http://web.invalid/v1/responses",
    transport: async (call) => {
      calls.push(call);
      call.markSubmitted();
      return {
        status: 409,
        headers: { "content-type": "application/json" },
        body: Buffer.from(JSON.stringify({
          error: {
            code: "turn_identity_conflict",
            message: "current user message conflicts with native turn metadata",
          },
        })),
      };
    },
  });
  await assert.rejects(
    worker.runResponses({ model: "chatgpt-web/high", input: [] }, { deliveryId: "rejected" }),
    (error) => error.code === "WEB_REQUEST_REJECTED"
      && error.status === 409
      && error.message.includes("current user message conflicts")
      && error.details.downstream_status === 409
      && error.details.downstream_code === "turn_identity_conflict"
      && error.details.retry_allowed === false
      && error.details.fallback_allowed === false,
  );
  assert.equal(calls.length, 1);
});

test("ChatGPT Web worker maps a settled downstream server error to non-retryable conflict", async () => {
  const worker = new ChatGptWebWorker({
    endpoint: "http://web.invalid/v1/responses",
    transport: async ({ markSubmitted }) => {
      markSubmitted();
      return {
        status: 503,
        headers: { "content-type": "application/json" },
        body: Buffer.from(JSON.stringify({ error: { message: "launcher unavailable" } })),
      };
    },
  });
  await assert.rejects(
    worker.runResponses({ model: "chatgpt-web/pro", input: [] }, { deliveryId: "settled-503" }),
    (error) => error.code === "WEB_UPSTREAM_REJECTED"
      && error.status === 409
      && error.details.downstream_status === 503
      && error.details.retry_allowed === false,
  );
});

test("unary Web compaction accepts exact v1 replacement history without replay", async () => {
  const prefix = "Another language model started to solve this problem and produced a summary of its thinking process. You also have access to the state of the tools that were used by that language model. Use this to build on the work that has already been done and avoid duplicating work. Here is the summary produced by the other language model, use the information in this summary to assist with your own analysis:";
  const message = text => ({ type: "message", role: "user", content: [{ type: "input_text", text }] });
  const valid = { output: [message("preserved user request"), message(`${prefix}\nexact summary`)] };
  const cases = [
    [valid, true],
    [{ output: [] }, false],
    [{ output: [message("arbitrary answer")] }, false],
    [{ output: [message(`${prefix}\n`)] }, false],
    [{ output: [{ ...valid.output[1], role: "assistant" }] }, false],
    [{ ...valid, status: "failed" }, false],
    [{ ...valid, error: { message: "failed" } }, false],
    [{ output: [valid.output[1], message("not a terminal summary")] }, false],
  ];
  for (const [payload, accepted] of cases) {
    let calls = 0;
    const worker = new ChatGptWebWorker({
      endpoint: "http://web.invalid/v1/responses",
      transport: async () => {
        calls += 1;
        return { status: 200, headers: { "content-type": "application/json" }, body: JSON.stringify(payload) };
      },
    });
    if (accepted) {
      const result = await worker.compact({ model: "chatgpt-web/pro", input: [] });
      assert.equal(result.summary, "exact summary");
      assert.deepEqual(result.provider_item, valid.output.at(-1));
      const canonical = createCanonicalCompaction({ model: "chatgpt-web/pro", route: "web", summary: result.summary });
      const state = new Map([[canonical.stateKey, { item: result.provider_item }]]);
      assert.deepEqual(normalizeCompactions([canonical.item], "web", state).input, [valid.output.at(-1)]);
      assert.deepEqual(result.replacement_history, valid.output);
    } else {
      await assert.rejects(worker.compact({ model: "chatgpt-web/pro", input: [] }), error => error.code === "WEB_COMPACTION_INVALID");
    }
    assert.equal(calls, 1);
  }
});

test("ChatGPT Web worker decodes provider-native compaction without a second request", async () => {
  const calls = [];
  const summary = "bounded compact summary";
  const providerItem = {
    type: "compaction",
    id: "cmp_web_1",
    encrypted_content: `ocx1:${Buffer.from(summary).toString("base64")}`,
  };
  const worker = new ChatGptWebWorker({
    endpoint: "http://web.invalid/v1/responses",
    transport: async (call) => {
      calls.push(call);
      call.markSubmitted();
      return {
        status: 200,
        headers: { "content-type": "application/json" },
        body: Buffer.from(JSON.stringify({ status: "completed", output: [providerItem], usage: { total_tokens: 7 } })),
      };
    },
  });
  const result = await worker.compact({ model: "chatgpt-web/pro", input: [] }, { deliveryId: "compact-1" });

  assert.equal(calls.length, 1);
  assert.equal(calls[0].endpoint, "http://web.invalid/v1/responses/compact");
  assert.deepEqual(JSON.parse(calls[0].payload), {
    model: "chatgpt-web/pro",
    input: [],
    stream: false,
    store: false,
  });
  assert.equal(result.summary, summary);
  assert.deepEqual(result.provider_item, providerItem);
  assert.equal(result.usage.total_tokens, 7);
});
