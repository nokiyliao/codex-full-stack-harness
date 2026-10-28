import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, describe, test } from "node:test";

import { elideStaleAppshots, projectNokiyDirect } from "../src/nokiy-direct-route.mjs";

const catalog = {
  models: ["gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"].map((slug) => ({
    slug, supported_reasoning_levels: ["low", "medium", "high", "max", "xhigh", "ultra"].map((effort) => ({ effort })),
    multi_agent_reasoning_effort: "xhigh",
  })),
};
const selector = { default_effort: "medium", models: { low: "gpt-6-luna", medium: "gpt-6.1-sol", high: "gpt-6-astra" } };
const profiles = [{ slug: "nokiy", display_name: "Nokiy appshot test", workflow: "nokiy",
  coordinator: { model: "gpt-6-astra", reasoning_effort: "xhigh" },
  worker: { model: "gpt-6.1-sol", reasoning_effort: "max", service_tier: "default" }, worker_selector: selector }];
const host = { python: "/synthetic/nokiy/python", script: "/synthetic/nokiy/host.py" };
const AT = { type: "additional_tools", role: "developer", tools: [
  { type: "custom", name: "exec", description: "declare const tools: { exec_command(args: { cmd: string; }): Promise<{ output: string; }>; };", format: { type: "text" } },
] };
const BI = { type: "message", role: "developer", content: [{ type: "input_text", text: "BASE" }] };
const tree = (label) => `${label} `.repeat(400);
const shot = (app, label, title = "Home") => `<appshot app="${app}" window-title="${title}">\n${tree(label)}\n</appshot>`;
const user = (text) => ({ type: "message", role: "user", content: [{ type: "input_text", text }] });
const COMPACTION = { type: "compaction", encrypted_content: "opaque-summary" };
const history = () => [AT, BI,
  user(`# Applications mentioned by the user:\n\n${shot("Google Chrome", "old-a")}\n## My request:\nfirst`),
  user(`${shot("Antigravity", "old-b", "a > b")}\nsecond`),
  user(`${shot("Google Chrome", "recent-c")}\nthird`),
  user(`${shot("Finder", "recent-d")}\nfourth`),
  COMPACTION,
  user(`${shot("Google Chrome", "after-e")}\nafter compaction`)];
const textOf = (item) => item.content.map((part) => part.text).join("");

test("only snapshots before the latest compaction are omitted, except the newest user messages", () => {
  const input = history();
  const snapshot = structuredClone(input);
  const result = elideStaleAppshots(input);
  assert.deepEqual(input, snapshot);
  assert.equal(result.stats.appshots, 2);
  const [, , first, second, third, fourth, compaction, after] = result.input;
  assert.match(textOf(first), /<appshot app="Google Chrome" window-title="Home">\[Earlier UI snapshot omitted after context compaction: \d+ characters\]<\/appshot>/);
  assert.match(textOf(first), /## My request:\nfirst$/);
  assert.match(textOf(second), /window-title="a > b">\[Earlier UI snapshot omitted/);
  assert.equal(textOf(second).includes("old-b"), false);
  for (const [index, kept] of [[4, third], [5, fourth], [7, after]]) assert.deepEqual(kept, input[index]);
  assert.deepEqual(compaction, COMPACTION);
  assert.deepEqual(elideStaleAppshots(history()), result);
});

test("small snapshots, string content and inputs without a compaction are handled", () => {
  assert.equal(elideStaleAppshots([AT, BI, user(shot("Chrome", "x")), user("y")]), null);
  const small = [user("<appshot app=\"Tiny\">ok</appshot>"), user("a"), user("b"), COMPACTION];
  assert.equal(elideStaleAppshots(small), null);
  const stringy = [{ type: "message", role: "user", content: shot("Chrome", "string-shot") }, user("a"), user("b"), COMPACTION];
  const result = elideStaleAppshots(stringy);
  assert.match(result.input[0].content, /^<appshot app="Chrome" window-title="Home">\[Earlier UI snapshot omitted/);
});

describe("Nokiy projection omits stale snapshots on every full request type", { concurrency: false }, () => {
  let home, originalHome, originalSwitch;
  before(() => {
    originalHome = process.env.CODEX_HOME;
    originalSwitch = process.env.SWITCHBOARD_STALE_APPSHOT_ELISION;
    home = mkdtempSync(join(tmpdir(), "nokiy-stale-appshots-"));
    writeFileSync(join(home, "config.toml"), "[agents]\nmax_threads = 2\n");
    process.env.CODEX_HOME = home;
  });
  after(() => {
    if (originalHome === undefined) delete process.env.CODEX_HOME; else process.env.CODEX_HOME = originalHome;
    if (originalSwitch === undefined) delete process.env.SWITCHBOARD_STALE_APPSHOT_ELISION;
    else process.env.SWITCHBOARD_STALE_APPSHOT_ELISION = originalSwitch;
    rmSync(home, { recursive: true, force: true });
  });
  const common = { model: "nokiy", tool_choice: "auto", parallel_tool_calls: false,
    reasoning: { effort: "medium", summary: "auto" }, store: false, stream: true,
    include: ["reasoning.encrypted_content"], prompt_cache_key: "fixture-thread" };
  const options = { endpoint: "/responses", catalog, profiles, host };
  const omitted = (input) => input.filter((item) => item?.role === "user" && textOf(item).includes("Earlier UI snapshot omitted")).length;

  test("execution and remote compaction requests share the same rewritten history", () => {
    const execution = projectNokiyDirect({ ...common, input: history() }, options);
    const compaction = projectNokiyDirect({ ...common, input: [...history(), { type: "compaction_trigger" }] }, options);
    assert.deepEqual(execution.staleAppshots, compaction.staleAppshots);
    assert.equal(omitted(execution.body.input), 2);
    const boundary = (input) => input.findIndex((item) => item?.type === "compaction");
    const head = (input) => input.slice(0, boundary(input) + 1);
    assert.deepEqual(head(compaction.body.input), head(execution.body.input));
  });

  test("chained deltas and the disabled switch leave the history unchanged", () => {
    const chained = projectNokiyDirect({ ...common, previous_response_id: "resp_fixture", input: history() }, options);
    assert.equal(chained.staleAppshots, null);
    assert.equal(omitted(chained.body.input), 0);
    process.env.SWITCHBOARD_STALE_APPSHOT_ELISION = "0";
    try {
      const disabled = projectNokiyDirect({ ...common, input: history() }, options);
      assert.equal(disabled.staleAppshots, null);
      assert.equal(omitted(disabled.body.input), 0);
    } finally { delete process.env.SWITCHBOARD_STALE_APPSHOT_ELISION; }
  });
});

const image = (label) => ({ type: "input_image", image_url: `data:image/png;base64,${label}` });
const withImages = () => [AT, BI,
  { type: "message", role: "user", content: [{ type: "input_text", text: "see screenshot" }, image("OLDIMG")] },
  user("short old instruction"),
  user("recent text"),
  { type: "message", role: "user", content: [{ type: "input_text", text: "latest before" }, image("KEEPIMG")] },
  COMPACTION,
  { type: "message", role: "user", content: [image("AFTERIMG")] }];

test("older screenshots become a note; the newest user messages and later items keep theirs", () => {
  const input = withImages();
  const snapshot = structuredClone(input);
  const result = elideStaleAppshots(input);
  assert.deepEqual(input, snapshot, "the request is not mutated");
  assert.deepEqual(result.input[2].content, [{ type: "input_text", text: "see screenshot" },
    { type: "input_text", text: "[Earlier image omitted after context compaction]" }]);
  assert.deepEqual(result.input[3], input[3], "text-only messages stay intact");
  assert.deepEqual(result.input[5], input[5], "the newest user messages before the boundary keep their images");
  assert.deepEqual(result.input[7], input[7], "messages after the compaction are untouched");
  assert.deepEqual(result.stats, { appshots: 0, chars: 0, images: 1 });
  assert.deepEqual(elideStaleAppshots(input), result, "the rewrite is deterministic for prompt caching");
});

test("SWITCHBOARD_STALE_IMAGE_ELISION=0 keeps images but still omits snapshots", () => {
  const previous = process.env.SWITCHBOARD_STALE_IMAGE_ELISION;
  process.env.SWITCHBOARD_STALE_IMAGE_ELISION = "0";
  try {
    assert.equal(elideStaleAppshots(withImages()), null);
    assert.equal(elideStaleAppshots(history()).stats.appshots, 2);
  } finally {
    if (previous === undefined) delete process.env.SWITCHBOARD_STALE_IMAGE_ELISION;
    else process.env.SWITCHBOARD_STALE_IMAGE_ELISION = previous;
  }
});
