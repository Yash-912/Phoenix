// Pure logic: escaping, formatting, filtering and answering an approval. No DOM needed.
import test from "node:test";
import assert from "node:assert/strict";
import { html, raw, toString } from "../js/html.js";
import { applyFilter, diffLines, factValue, filterCounts, relativeTime, safeUrl, stats, timeLeft } from "../js/format.js";
import { countAt, easeOutCubic } from "../js/motion.js";
import { clearCredentials, decide, loadCredentials, saveCredentials } from "../js/approve.js";

test("html escapes by default and trusts only raw()", () => {
  const out = toString(html`<p>${'<img src=x onerror="alert(1)">'}</p>${raw("<b>ok</b>")}`);
  assert.equal(out, "<p>&lt;img src=x onerror=&quot;alert(1)&quot;&gt;</p><b>ok</b>");
});

test("html renders arrays and drops null, undefined and false", () => {
  assert.equal(toString(html`${[1, html`<i>${"a&b"}</i>`, null, undefined, false]}`), "1<i>a&amp;b</i>");
});

test("a nested template is not escaped twice", () => {
  const inner = html`<em>${"x"}</em>`;
  assert.equal(toString(html`<p>${inner}</p>`), "<p><em>x</em></p>");
});

test("relativeTime reads in words and survives bad input", () => {
  const now = Date.parse("2026-10-30T12:00:00Z");
  assert.equal(relativeTime("2026-10-30T11:59:40Z", now), "just now");
  assert.equal(relativeTime("2026-10-30T11:15:00Z", now), "45 min ago");
  assert.equal(relativeTime("2026-10-30T07:00:00Z", now), "5 h ago");
  assert.equal(relativeTime("2026-10-27T12:00:00Z", now), "3 d ago");
  assert.equal(relativeTime(null, now), "—");
  assert.equal(relativeTime("not a date", now), "—");
});

test("timeLeft says when an open request is past its deadline", () => {
  const now = Date.parse("2026-10-30T12:00:00Z");
  assert.equal(timeLeft("2026-10-30T12:20:00Z", now), "20 min left");
  assert.equal(timeLeft("2026-10-30T11:00:00Z", now), "past its deadline");
});

test("factValue is readable for booleans, lists and floats", () => {
  assert.equal(factValue(true), "yes");
  assert.equal(factValue([]), "none");
  assert.equal(factValue(["a", "b"]), "a, b");
  assert.equal(factValue(0.5), "0.50");
  assert.equal(factValue(3100), "3100");
});

const rows = [
  { outcome: { key: "resolved" }, pending_approvals: 0 },
  { outcome: { key: "pr_exists" }, pending_approvals: 0 },
  { outcome: { key: "awaiting_approval" }, pending_approvals: 1 },
  { outcome: { key: "llm_unavailable" }, pending_approvals: 0 },
  { outcome: { key: "insufficient_evidence" }, pending_approvals: 0 },
  { outcome: { key: "investigating" }, pending_approvals: 0 },
];

test("filters group the outcomes the way a reader would ask", () => {
  assert.deepEqual(filterCounts(rows), { all: 6, attention: 2, done: 2, noaction: 1, open: 1 });
  assert.equal(applyFilter(rows, "attention").length, 2);
  assert.equal(applyFilter(rows, "no-such-filter").length, 6);
});

test("stats count what needs a person and what waits on an approval", () => {
  const [total, attention, done, waiting] = stats(rows);
  assert.deepEqual([total.n, attention.n, done.n, waiting.n], [6, 2, 2, 1]);
});

test("diffLines marks additions and removals by class and keeps the sign in the text", () => {
  const lines = diffLines("--- a\n+++ b\n-old\n+new\n ctx");
  assert.deepEqual(lines.map((l) => l.cls), ["", "", "del", "add", ""]);
  assert.equal(lines[2].text, "-old");
});

test("safeUrl follows http and https only", () => {
  assert.equal(safeUrl("https://github.com/example/lab/pull/44"), "https://github.com/example/lab/pull/44");
  assert.equal(safeUrl("javascript:alert(1)"), null);
  assert.equal(safeUrl("data:text/html,<script>"), null);
  assert.equal(safeUrl("not a url"), null);
});

// --- answering ---------------------------------------------------------------------------

function fakeFetch(status, body) {
  const calls = [];
  const fn = async (url, init) => {
    calls.push({ url, init });
    return { ok: status >= 200 && status < 300, status, json: async () => body };
  };
  fn.calls = calls;
  return fn;
}

test("an approval posts to the existing endpoint with the bearer token and the approver", async () => {
  const fetchFn = fakeFetch(200, { result: "recorded" });
  const result = await decide(fetchFn, { approvalId: 15, decision: "approve", approver: " tester ", token: " secret " });
  assert.equal(result.ok, true);
  const [call] = fetchFn.calls;
  assert.equal(call.url, "/approvals/15/decision");
  assert.equal(call.init.method, "POST");
  assert.equal(call.init.headers.Authorization, "Bearer secret");
  assert.deepEqual(JSON.parse(call.init.body), { decision: "approve", approver: "tester" });
});

test("a denial says Phoenix will escalate, not that anything was undone", async () => {
  const result = await decide(fakeFetch(200, {}), { approvalId: 1, decision: "deny", approver: "a", token: "t" });
  assert.match(result.message, /escalate/);
});

test("nothing is sent when the name or the token is missing", async () => {
  const fetchFn = fakeFetch(200, {});
  assert.equal((await decide(fetchFn, { approvalId: 1, decision: "approve", approver: "", token: "t" })).ok, false);
  assert.equal((await decide(fetchFn, { approvalId: 1, decision: "approve", approver: "a", token: "  " })).ok, false);
  assert.equal(fetchFn.calls.length, 0);
});

test("each refusal is told to the person in words", async () => {
  const ask = (status, body) => decide(fakeFetch(status, body), { approvalId: 1, decision: "approve", approver: "a", token: "t" });
  assert.match((await ask(401, {})).message, /token was not accepted/);
  assert.match((await ask(403, {})).message, /not on the approver list/);
  assert.match((await ask(404, {})).message, /no longer exists/);
  assert.match((await ask(503, {})).message, /switched off/);
});

test("a second answer reports that the first one won, using the server's own words", async () => {
  const result = await decide(fakeFetch(409, { detail: { status: "approved", result: "ignored: the first answer already won" } }), {
    approvalId: 1, decision: "deny", approver: "a", token: "t",
  });
  assert.equal(result.ok, false);
  assert.equal(result.message, "ignored: the first answer already won");
});

test("an expired request is never reported as approved", async () => {
  const result = await decide(fakeFetch(409, { detail: { status: "expired", result: "refused: the deadline has passed, and expiry is never an approval" } }), {
    approvalId: 1, decision: "approve", approver: "a", token: "t",
  });
  assert.equal(result.ok, false);
  assert.match(result.message, /never an approval/);
});

test("a network failure is a message, not an exception", async () => {
  const result = await decide(async () => { throw new TypeError("failed to fetch"); }, { approvalId: 1, decision: "approve", approver: "a", token: "t" });
  assert.deepEqual([result.ok, result.message], [false, "Could not reach Phoenix."]);
});

test("credentials round-trip through storage and tolerate a broken one", () => {
  const store = new Map();
  const storage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v), removeItem: (k) => store.delete(k) };
  assert.deepEqual(loadCredentials(storage), { approver: "", token: "" });
  saveCredentials(storage, { approver: "tester", token: "t" });
  assert.deepEqual(loadCredentials(storage), { approver: "tester", token: "t" });
  clearCredentials(storage);
  assert.deepEqual(loadCredentials(storage), { approver: "", token: "" });
  store.set("phoenix.approver", "{not json");
  assert.deepEqual(loadCredentials(storage), { approver: "", token: "" });
  const throwing = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); }, removeItem() { throw new Error("blocked"); } };
  assert.deepEqual(loadCredentials(throwing), { approver: "", token: "" });
  assert.doesNotThrow(() => saveCredentials(throwing, { approver: "a", token: "t" }));
});

test("the count-up eases out, starts at zero and lands exactly on the number", () => {
  assert.equal(countAt(50, 0), 0);
  assert.equal(countAt(50, 1), 50);
  assert.equal(countAt(50, 2), 50);
  assert.equal(countAt(50, -1), 0);
  assert.ok(easeOutCubic(0.5) > 0.5, "fast at first, slow at the end");
  assert.ok(countAt(100, 0.3) > 30);
});
