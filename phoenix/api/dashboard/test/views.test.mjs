// The views, rendered over traces the Python side built from the fixtures.
// PHOENIX_TRACE_DIR points at the JSON test_dashboard_js.py writes; without it these skip.
import test from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { toString } from "../js/html.js";
import { renderList } from "../js/views/list.js";
import { renderDetail } from "../js/views/detail.js";
import { renderApprovals } from "../js/views/approvals.js";

const dir = process.env.PHOENIX_TRACE_DIR;
const have = dir && existsSync(join(dir, "overview.json"));
const load = (name) => JSON.parse(readFileSync(join(dir, name), "utf8"));
const NOW = Date.parse("2026-10-30T16:00:00Z");
const ui = (extra = {}) => ({ showMinor: false, audit: null, auditLoading: false, auditError: "", decision: null, creds: { approver: "", token: "" }, ...extra });
const detail = (id, config = { approvals_enabled: true }, extra = {}) => toString(renderDetail({ trace: load(`trace_${id}.json`), ui: ui(extra), config, now: NOW }));
const opts = { skip: have ? false : "PHOENIX_TRACE_DIR not set" };

test("the list shows the alert status and the outcome apart, and links every row", opts, () => {
  const out = toString(renderList({ incidents: load("overview.json").incidents, filter: "all", now: NOW }));
  assert.match(out, /href="#\/incidents\/33"/);
  assert.match(out, /Resolved/);
  assert.match(out, /alert resolved/);
  assert.match(out, /Model unavailable/);
  assert.match(out, /aria-pressed="true"[^>]*>All/);
});

test("the list filter narrows the rows", opts, () => {
  const out = toString(renderList({ incidents: load("overview.json").incidents, filter: "done", now: NOW }));
  assert.match(out, /#\/incidents\/33"/);
  assert.doesNotMatch(out, /#\/incidents\/38"/);
});

test("an empty list says so rather than showing an empty table", opts, () => {
  assert.match(toString(renderList({ incidents: [], filter: "all", now: NOW })), /No incidents recorded/);
});

test("a rolled-back incident reads start to finish on one page", opts, () => {
  const out = detail(33);
  for (const text of ["What happened", "What Phoenix did", "What it believed", "Step by step", "What it read", "Seen before?", "Under the hood"]) {
    assert.ok(out.includes(text), text);
  }
  assert.match(out, /Tier 2/);
  assert.match(out, /tester via api/);
  assert.match(out, /Verification pass/);
  assert.match(out, /Incident #23/);
});

test("an answered request shows no form, an open one does", opts, () => {
  assert.doesNotMatch(detail(33), /data-action="decide-form"/);
  const open = detail(39);
  assert.match(open, /data-action="decide-form"/);
  assert.match(open, /type="password"/);
  assert.match(open, /data-decision="approve"/);
  assert.match(open, /data-decision="deny"/);
  assert.equal((open.match(/data-action="decide-form"/g) || []).length, 1);
});

test("with approvals switched off the form is replaced by a plain explanation", opts, () => {
  const out = detail(39, { approvals_enabled: false });
  assert.doesNotMatch(out, /data-action="decide-form"/);
  assert.match(out, /switched off/);
});

test("the typed name is kept in the form and the token is not echoed into text", opts, () => {
  const out = detail(39, { approvals_enabled: true }, { creds: { approver: "tester", token: "s3cret" } });
  assert.match(out, /name="approver" value="tester"/);
  assert.match(out, /type="password" value="s3cret"/);
});

test("a refused answer is shown where the buttons are", opts, () => {
  const out = detail(39, { approvals_enabled: true }, { decision: { approvalId: 15, busy: false, ok: false, message: "ignored: the first answer already won" } });
  assert.match(out, /role="status"/);
  assert.match(out, /first answer already won/);
});

test("the new outcomes and events each have words, not raw event names", opts, () => {
  assert.match(detail(38), /Model could not be reached/);
  assert.match(detail(42), /Investigation crashed and was given up on/);
  assert.match(detail(41), /No allowed action for this cause/);
  assert.match(detail(36), /Why it stopped without a finding/);
  assert.match(detail(36), /read failed/);
  assert.match(detail(34), /An open pull request already has this fix/);
  assert.match(detail(35), /Pull request opened/);
  assert.match(detail(35), /rel="noopener noreferrer"/);
});

test("prompts are reported as not recorded instead of silently absent", opts, () => {
  assert.match(detail(33), /does not record LLM prompts/);
});

test("raw audit rows load only when asked", opts, () => {
  assert.match(detail(33), /Show the raw audit rows/);
  const withAudit = detail(33, { approvals_enabled: true }, { audit: load("audit_33.json") });
  assert.match(withAudit, /Audit rows \(/);
  assert.match(withAudit, /observer_pass/);
});

test("text from the database cannot become markup", opts, () => {
  const trace = load("trace_33.json");
  trace.narrative[0] = '<script>alert("x")</script>';
  trace.tier3 = { defect: null, target: null, proposals: [], validation: null, branch: null, branch_cleanup: null, stopped: null, pr: { status: "opened", url: "javascript:alert(1)", number: 1, error: null } };
  const out = toString(renderDetail({ trace, ui: ui(), config: { approvals_enabled: true }, now: NOW }));
  assert.doesNotMatch(out, /<script>/);
  assert.doesNotMatch(out, /href="javascript:/);
});

test("the approvals queue links to the incident where the answer is given", opts, () => {
  const out = toString(renderApprovals({ approvals: load("approvals.json").approvals, now: NOW }));
  assert.match(out, /href="#\/incidents\/39"/);
  assert.match(toString(renderApprovals({ approvals: [], now: NOW })), /Nothing is waiting/);
});
