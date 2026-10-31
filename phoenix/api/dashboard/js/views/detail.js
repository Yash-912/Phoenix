// One incident, in the order someone would ask the questions: what happened, what did it do,
// why did it think so, what were the steps, what did it read, has this happened before, and
// the raw record behind all of it. Decisions come first; the raw layer is one click deeper.
import { html, raw } from "../html.js";
import { clockTime, diffLines, factValue, fullTime, humanKey, percent, pretty, safeUrl, score, splitWords, timeLeft } from "../format.js";
import { alertChip, emptyState, outcomeChip } from "./shared.js";

function section(number, label, heading, body, note) {
  return html`<section class="section" id="section-s${number}" data-reveal aria-labelledby="s${number}">
    <div class="section__label"><p class="label">${String(number).padStart(2, "0")} · ${label}</p><h2 id="s${number}">${heading}</h2>${note ? html`<p>${note}</p>` : ""}</div>
    <div class="section__body">${body}</div>
  </section>`;
}

function facts(object) {
  const entries = Object.entries(object || {}).filter(([, v]) => v !== null && v !== undefined && v !== "");
  if (!entries.length) return "";
  return html`<dl class="facts">${entries.map(([k, v]) => html`<dt>${humanKey(k)}</dt><dd>${typeof v === "object" && !Array.isArray(v) ? html`<span class="mono">${JSON.stringify(v)}</span>` : factValue(v)}</dd>`)}</dl>`;
}

// The reference's "call record": a dashed card of mono key/value rows with one row lit.
// rows are [key, value, hot?]; a null or empty value is left out rather than shown blank.
function record(title, meta, rows, foot) {
  const shown = rows.filter(([, value]) => value !== null && value !== undefined && value !== "");
  return html`<div class="record"><div class="record__head"><b>${title}</b><span>${meta}</span></div>
    <dl>${shown.map(([key, value, hot]) => html`<div class="record__row ${hot ? "record__row--hot" : ""}"><dt>${key}</dt><dd>${value}</dd></div>`)}</dl>
    ${foot ? html`<div class="record__foot">${foot}</div>` : ""}</div>`;
}

// Everything someone needs to narrate the incident, on one card.
function incidentRecord(trace) {
  const i = trace.incident;
  const leader = trace.hypotheses.leader;
  const approval = trace.approvals[trace.approvals.length - 1];
  const approvalText = approval
    ? approval.can_decide ? "open, waiting for an answer"
      : approval.decided_by ? `${approval.status} by ${approval.decided_by}` : approval.status
    : trace.tier.tier === 3 ? "not needed, stops at a PR" : trace.tier.tier ? "not required" : null;
  const pr = trace.tier3 && trace.tier3.pr;
  const fix = pr && pr.url
    ? (safeUrl(pr.url) ? html`<a href="${safeUrl(pr.url)}" rel="noopener noreferrer" target="_blank">PR ${pr.number ? `#${pr.number}` : ""} (${pr.status})</a>` : `PR ${pr.status}`)
    : trace.actions.length ? actionSummary(trace.actions) : null;
  return record(
    "Incident record",
    `#${i.id} · ${fullTime(i.first_seen_at)}`,
    [
      ["service", i.service_name],
      ["alert", `${i.alertname} · ${i.severity}`],
      ["outcome", i.outcome.label, true],
      ["root cause", leader ? `${leader.category || "unclassified"} · ${score(leader.score)}` : "none named"],
      ["tier", trace.tier.tier ? `${(trace.tier.steps || []).length > 1 ? trace.tier.steps.map((r) => r.label).join(" → ") : trace.tier.label} · ${trace.tier.gate || ""}` : approval && approval.can_decide ? "not acted yet" : "no action"],
      ["approval", approvalText],
      ["fix", fix],
      ["verified", trace.verification ? trace.verification.outcome : trace.actions.length ? "no verdict recorded" : null],
    ],
    i.outcome.summary,
  );
}

function rawJson(title, value) {
  if (value === null || value === undefined) return "";
  return html`<details><summary>${title}</summary><pre class="code">${pretty(value)}</pre></details>`;
}

// --- 01 summary ---------------------------------------------------------------------

function summary(trace) {
  const [first, ...rest] = trace.narrative;
  return html`<div class="narrative"><p>${first}</p>${rest.map((line) => html`<p>${line}</p>`)}</div>
    <p class="outcome-note"><strong>${trace.incident.outcome.label}.</strong> ${trace.incident.outcome.summary}</p>
    ${trace.problems.length ? html`<div class="block"><p class="block__title">Things that went wrong or came back empty</p>
      <ul>${trace.problems.map((p) => html`<li class="muted">${p.text}</li>`)}</ul></div>` : ""}`;
}

// --- 02 what it did -------------------------------------------------------------------

function approvalCard(approval, ui, config, now) {
  const open = approval.can_decide;
  const state = ui.decision && ui.decision.approvalId === approval.id ? ui.decision : null;
  const status = open
    ? html`<span class="chip chip--attention">Open — ${timeLeft(approval.expires_at, now)}</span>`
    : approval.overdue
      ? html`<span class="chip chip--warn">Past its deadline, unanswered</span>`
      : html`<span class="chip chip--${approval.status === "approved" ? "good" : approval.status === "denied" ? "bad" : "warn"}">${approval.status[0].toUpperCase() + approval.status.slice(1)}</span>`;
  return html`<div class="callout ${open ? "" : "callout--soft"}">
    <div><h3>${approval.action.replace(/_/g, " ")} on ${approval.container}</h3><p class="muted">${approval.plan_reasoning}</p></div>
    ${record(`Approval request #${approval.id}`, `tier ${approval.tier}`, [
      ["status", status, true],
      ["arguments", (approval.args || []).join(" ") || "none"],
      ["asked", fullTime(approval.requested_at)],
      ["expires", fullTime(approval.expires_at)],
      ["answered by", approval.decided_by ? `${approval.decided_by} via ${approval.decision_channel || "unknown"} at ${clockTime(approval.decided_at)}` : null],
    ])}
    ${open ? decisionForm(approval, ui, config, state) : ""}
    ${rawJson("Incident summary and evidence sent with the request", { incident_summary: approval.incident_summary, evidence: approval.evidence })}
  </div>`;
}

function decisionForm(approval, ui, config, state) {
  if (!config.approvals_enabled) {
    return html`<p class="notice">Answering from the dashboard is switched off: this server has no approval token or approver list configured.</p>`;
  }
  const id = approval.id;
  return html`<form data-action="decide-form" data-approval="${id}" autocomplete="off">
    <div class="form-row">
      <label class="field">Answering as<input name="approver" value="${ui.creds.approver}" required autocomplete="off" spellcheck="false"></label>
      <label class="field">Approval token<input name="token" type="password" value="${ui.creds.token}" required autocomplete="off" spellcheck="false"></label>
    </div>
    <div class="actions" style="margin-top: var(--space-4)">
      <button class="btn" type="submit" data-decision="approve" ${state && state.busy ? "disabled" : ""}>Approve</button>
      <button class="btn btn--quiet" type="submit" data-decision="deny" ${state && state.busy ? "disabled" : ""}>Deny</button>
      <span class="muted label">The first answer wins. A request that has expired cannot be approved.</span>
    </div>
    <p class="muted label" style="margin-top: var(--space-2)">Kept in this tab only, never in the page.</p>
    ${state && state.message ? html`<p class="notice ${state.ok ? "" : "notice--bad"}" role="status" style="margin-top: var(--space-3)">${state.message}</p>` : ""}
  </form>`;
}

function verificationBlock(v) {
  if (!v) return "";
  const chip = v.outcome === "pass" ? "good" : v.outcome === "fail" ? "bad" : "warn";
  return html`<div class="block"><p class="block__title">Did it work?</p>
    <p><span class="chip chip--${chip}">Verification ${v.outcome}</span></p>
    <p class="muted" style="margin-top: var(--space-2); max-width: 62ch">${v.reason}</p>
    <div style="margin-top: var(--space-3)">${record("Verification", fullTime(v.at), [["check", v.check], ["after action", v.action], ["verdict", v.outcome, true]])}</div>
    ${rawJson("What the check measured", v.detail)}</div>`;
}

// A run that retried names each action once, with how many times: "restart_service ×3, rollback_config".
function actionSummary(actions) {
  const counts = new Map();
  for (const a of actions) counts.set(a.action, (counts.get(a.action) || 0) + 1);
  return [...counts].map(([name, n]) => (n > 1 ? `${name} ×${n}` : name)).join(", ");
}

function tier3Block(t) {
  if (!t) return "";
  const pr = t.pr;
  const prLine = pr
    ? pr.status === "failed"
      ? html`<span class="chip chip--bad">Pull request failed</span> ${pr.error || ""}`
      : html`<span class="chip chip--good">${pr.status === "opened" ? "Pull request opened" : "An open pull request already has this fix"}</span> ${pr.url ? (safeUrl(pr.url) ? html`<a href="${safeUrl(pr.url)}" rel="noopener noreferrer" target="_blank">#${pr.number || ""} ${pr.url}</a>` : html`<span class="mono">${pr.url}</span>`) : ""}`
    : "";
  return html`<div class="block"><p class="block__title">The code fix path (Tier 3)</p>
    ${t.defect ? html`<p style="max-width: 62ch">${t.defect.description}</p>${facts({ file: t.defect.file_path, function: t.defect.function_name, "fix approach": t.defect.fix_approach })}` : ""}
    ${t.proposals.map((p) => html`<details><summary>Patch attempt ${p.attempt} — ${p.changed_lines} line(s) changed, ${p.scope_ok ? "inside the target function" : "outside the target function"}</summary>
      <pre class="code diff">${diffLines(p.diff).map((l) => html`<span class="${l.cls}">${l.text}</span>\n`)}</pre></details>`)}
    ${t.validation ? html`<p style="margin-top: var(--space-3)" class="muted">Validation: tests ${t.validation.tests_passed ? "passed" : "failed"}, lint ${t.validation.lint_passed ? "passed" : "failed"}.</p>` : ""}
    ${t.branch ? html`<p class="muted">Branch pushed: <span class="mono">${t.branch}</span>${t.branch_cleanup ? html` — then ${t.branch_cleanup.result}${t.branch_cleanup.why ? ` (${t.branch_cleanup.why})` : ""}` : ""}</p>` : ""}
    ${prLine ? html`<p style="margin-top: var(--space-3)">${prLine}</p>` : ""}
    ${t.stopped ? html`<p class="notice notice--bad" style="margin-top: var(--space-3)">Stopped at ${humanKey(t.stopped.event_type)}: ${t.stopped.reason}</p>` : ""}</div>`;
}

function didBody(trace, ui, config, now) {
  const tier = trace.tier;
  // A request still open is shown once, at the top of the page, where it can be answered.
  const answered = trace.approvals.filter((a) => !a.can_decide);
  const rungs = tier.steps || [];
  const tierLine = rungs.length > 1
    ? html`<p class="lead">It climbed ${rungs.length} rungs, in this order.</p>
      <ol class="rungs">${rungs.map((r) => html`<li class="rung"><p><strong>${r.label}</strong> — ${r.what}</p>
        <p class="muted">${r.meaning}. Gate: ${r.gate}.</p>
        ${r.why ? html`<p class="muted" style="max-width: 62ch">${r.why}</p>` : ""}</li>`)}</ol>
      ${facts({ "policy mode": tier.policy_mode })}`
    : tier.tier
    ? html`<p class="lead">${tier.label} — ${tier.meaning}.</p>
      ${tier.why ? html`<p class="muted" style="max-width: 62ch; margin-top: var(--space-2)">${tier.why}</p>` : ""}
      ${facts({ "policy mode": tier.policy_mode, gate: tier.gate })}`
    : trace.approvals.some((a) => a.can_decide)
      ? html`<p class="lead">Waiting for an approval before it acts.</p>
        <p class="muted" style="max-width: 62ch; margin-top: var(--space-2)">Nothing has been changed yet. The request is at the top of this page.</p>`
      : html`<p class="lead">No action was taken.</p>
        ${trace.stop_reason ? html`<p class="muted" style="max-width: 62ch; margin-top: var(--space-2)">${trace.stop_reason}</p>` : ""}`;
  return html`<div class="block">${tierLine}</div>
    ${answered.length ? html`<div class="block"><p class="block__title">Approval</p>${answered.map((a) => approvalCard(a, ui, config, now))}</div>` : ""}
    ${trace.actions.length ? html`<div class="block"><p class="block__title">What it changed</p><div class="table-wrap"><table class="table">
      <thead><tr><th>Action</th><th>Target</th><th>Result</th><th>Why</th></tr></thead>
      <tbody>${trace.actions.map((a) => html`<tr><td>${a.action.replace(/_/g, " ")}<br><span class="mono muted">${(a.args || []).join(" ")}</span></td><td>${a.container}</td>
        <td>${a.ok === true ? html`<span class="chip chip--good">ok</span>` : a.ok === false ? html`<span class="chip chip--bad">failed</span> ${a.error || ""}` : html`<span class="chip chip--neutral">no result recorded</span>`}<br><span class="muted label">${clockTime(a.finished_at || a.started_at)}</span></td>
        <td class="muted">${a.why || ""}</td></tr>`)}</tbody></table></div></div>` : ""}
    ${verificationBlock(trace.verification)}
    ${tier3Block(trace.tier3)}`;
}

// --- 03 what it believed ---------------------------------------------------------------

function hypothesisItem(h, kind, threshold) {
  const lower = kind === "lower";
  return html`<div class="hyp ${lower ? "hyp--lower" : "hyp--leader"}">
    <div class="hyp__top"><div><p class="hyp__cat">${lower ? "Ranked below the leader" : "Leading hypothesis"}${h.category ? html` · ${h.category.replace(/_/g, " ")}` : ""}</p></div><div class="hyp__score" aria-label="score ${score(h.score)}">${score(h.score)}</div></div>
    <p class="hyp__text">${h.description}</p>
    <div class="bar" role="img" aria-label="${percent(h.score)} percent of the way to full confidence${threshold != null ? `, threshold ${percent(threshold)} percent` : ""}"><i style="width:${percent(h.score)}%"></i>${threshold != null ? html`<b style="left:${percent(threshold)}%"></b>` : ""}</div>
    ${lower && h.behind_leader_by != null ? html`<p class="hyp__note">${score(h.behind_leader_by)} behind the leader.</p>` : ""}
    ${rawJson("How the score was built", h.breakdown)}
  </div>`;
}

function believedBody(trace) {
  const h = trace.hypotheses;
  if (!h.leader) {
    return emptyState("Nothing was weighed", "No hypothesis was recorded. If the model could not be reached, the problem is listed in the summary above.");
  }
  const earlier = h.passes.slice(0, -1);
  return html`<p class="muted" style="margin-bottom: var(--space-4)">${h.confidence != null ? html`Confidence ${score(h.confidence)} against a threshold of ${score(h.confidence_threshold)}. The tick on each bar marks the threshold.` : ""}</p>
    ${hypothesisItem(h.leader, "leader", h.confidence_threshold)}
    ${h.discarded.map((d) => hypothesisItem(d, "lower", h.confidence_threshold))}
    ${trace.evidence_report ? html`<div class="block"><p class="block__title">Why it stopped without a finding</p>${evidenceReport(trace.evidence_report)}</div>` : ""}
    ${earlier.length ? html`<details><summary>Earlier passes (${earlier.length})</summary>${earlier.map((p) => html`<div class="block"><p class="block__title">Pass ${p.iteration}</p>
      <ul>${p.items.map((i) => html`<li style="padding: var(--space-2) 0"><span class="num"><strong>${score(i.score)}</strong></span> · ${i.category ? html`${i.category.replace(/_/g, " ")} · ` : ""}${i.description}</li>`)}</ul></div>`)}</details>` : ""}`;
}

function evidenceReport(report) {
  return html`<p style="max-width: 62ch">${report.summary}</p>
    ${report.failed_reads && report.failed_reads.length ? html`<ul style="margin-top: var(--space-3)">${report.failed_reads.map((f) => html`<li class="muted">Failed read: ${f.source} — ${f.error}</li>`)}</ul>` : ""}
    ${report.never_read && report.never_read.length ? html`<p class="muted" style="margin-top: var(--space-2)">Never read: ${report.never_read.join(", ")}</p>` : ""}`;
}

// --- 04 steps ---------------------------------------------------------------------------

function stepItem(step, index = 0) {
  const decision = ["diagnose", "route", "act", "approval", "verify"].includes(step.kind);
  const cls = ["step", decision ? "step--decision" : "", step.kind === "stop" ? "step--stop" : "", step.kind === "failure" ? "step--failure" : "", step.minor ? "step--minor" : ""].filter(Boolean).join(" ");
  const entries = Object.entries(step.facts || {});
  return html`<li class="${cls}" data-reveal style="--i:${Math.min(index, 10)}"><span class="step__dot" aria-hidden="true"></span><div>
    <div class="step__head"><span class="step__title">${step.title}</span><span class="step__kind">${step.node}${step.iteration ? ` · pass ${step.iteration}` : ""}</span><span class="step__time">${clockTime(step.at)}</span></div>
    ${step.text ? html`<p class="step__text">${step.text}</p>` : ""}
    ${entries.length ? html`<div class="step__facts">${entries.map(([k, v]) => html`<span>${humanKey(k)} <b>${factValue(v)}</b></span>`)}</div>` : ""}
  </div></li>`;
}

// A long investigation shows its first steps and says how many it is holding back, so the
// page stays readable and fast. The raw audit rows below always have all of them.
export const STEP_LIMIT = 60;

function stepsBody(trace, ui) {
  const minor = trace.timeline.filter((s) => s.minor).length;
  const steps = ui.showMinor ? trace.timeline : trace.timeline.filter((s) => !s.minor);
  if (!trace.timeline.length) return emptyState("No steps recorded yet", "Phoenix has not written anything to the audit trail for this incident.");
  const capped = !ui.showAllSteps && steps.length > STEP_LIMIT;
  const visible = capped ? steps.slice(0, STEP_LIMIT) : steps;
  return html`${minor ? html`<p style="margin-bottom: var(--space-4)"><button type="button" class="btn btn--link" data-action="toggle-minor" aria-pressed="${String(ui.showMinor)}">${ui.showMinor ? "Hide" : "Show"} ${minor} routine step${minor > 1 ? "s" : ""}</button></p>` : ""}
    <ol class="timeline">${visible.map(stepItem)}</ol>
    ${capped ? html`<p style="margin-top: var(--space-4)"><button type="button" class="btn btn--quiet" data-action="show-all-steps">Show the other ${steps.length - STEP_LIMIT} steps</button></p>` : ""}`;
}

// --- 05 evidence ------------------------------------------------------------------------

function evidenceBody(trace) {
  if (!trace.evidence.length) return emptyState("No evidence was read", "The agent stopped before it read anything, or the reads were not recorded.");
  return html`<div class="table-wrap"><table class="table"><thead><tr><th>Pass</th><th>Source</th><th>What it found</th><th>Read at</th></tr></thead>
    <tbody>${trace.evidence.map((e) => html`<tr><td class="num">${e.iteration}</td><td style="white-space: nowrap"><span class="mono">${e.source}</span></td>
      <td>${e.failed ? html`<span class="chip chip--bad">read failed</span> ` : ""}${e.summary}${rawJson("Raw response", e.raw_data)}</td><td class="muted num">${clockTime(e.collected_at)}</td></tr>`)}</tbody></table></div>`;
}

// --- 06 history --------------------------------------------------------------------------

function memoryBody(trace) {
  const { matches } = trace.memory;
  if (!matches.length) return html`<p class="muted">Phoenix found no earlier incident for this service and root-cause category.</p>`;
  return html`<ul>${matches.map((m) => html`<li class="block"><p><a href="#/incidents/${m.incident_id}">Incident #${m.incident_id}</a> · ended <strong>${(m.status || "").replace(/_/g, " ")}</strong>${m.category ? html` · ${m.category.replace(/_/g, " ")}` : ""}</p>
    <p class="muted" style="max-width: 62ch; margin-top: var(--space-1)">${m.text}</p></li>`)}</ul>`;
}

// --- 07 raw record -----------------------------------------------------------------------

function rawBody(trace, ui) {
  const redactions = Object.entries(trace.redaction || {});
  const llm = trace.llm.recorded
    ? html`<p>Prompts and responses are recorded; they appear in the audit rows below as <span class="mono">llm_call</span> events.</p>`
    : html`<p class="notice">${trace.llm.note} What the model was asked and answered is therefore not shown here.</p>`;
  const audit = ui.audit;
  return html`${llm}
    <p class="muted" style="margin-top: var(--space-4)">${redactions.length
      ? `This page redacted ${redactions.map(([k, n]) => `${n} ${humanKey(k)}`).join(", ")} before showing it. Redaction lowers the risk of a secret reaching a screen; it does not remove it.`
      : "No secrets were found to redact on this page. Redaction here is a pattern match, so it lowers the risk and does not remove it."}</p>
    <div style="margin-top: var(--space-5)">
      ${audit
        ? html`<details open><summary>Audit rows (${audit.rows.length})</summary><div class="table-wrap"><table class="table"><thead><tr><th>Id</th><th>Node</th><th>Event</th><th>Detail</th></tr></thead>
          <tbody>${audit.rows.map((r) => html`<tr><td class="num">${r.id}</td><td>${r.node}</td><td class="mono">${r.event_type}<br><span class="muted">${clockTime(r.at)}</span></td>
            <td>${r.reasoning_text ? html`<p style="white-space:pre-line">${r.reasoning_text}</p>` : ""}<pre class="code">${pretty(r.detail)}</pre></td></tr>`)}</tbody></table></div></details>`
        : html`<button type="button" class="btn btn--quiet" data-action="load-audit" ${ui.auditLoading ? "disabled" : ""}>${ui.auditLoading ? "Loading…" : "Show the raw audit rows"}</button>${ui.auditError ? html` <span class="notice notice--bad">${ui.auditError}</span>` : ""}`}
    </div>`;
}

// --- page -------------------------------------------------------------------------------

const JUMPS = [["s1", "Summary"], ["s2", "What it did"], ["s3", "Reasoning"], ["s4", "Trail"], ["s5", "Evidence"], ["s6", "History"], ["s7", "Record"]];

// Buttons, not links: the page routes on the URL hash, so a #anchor link would leave the page.
function jumpBar() {
  return html`<nav class="jump" aria-label="On this page">${JUMPS.map(([id, label]) => html`<button type="button" data-action="jump" data-jump="${id}">${label}</button>`)}</nav>`;
}

export function renderDetail({ trace, ui, config, now }) {
  const i = trace.incident;
  const open = trace.approvals.filter((a) => a.can_decide);
  return html`<nav class="crumb" aria-label="Breadcrumb"><a href="#/">Incidents</a> / <span>#${i.id}</span></nav>
    <header class="detail-head">
      <div>
        <h1 class="display" id="page-title" tabindex="-1">${splitWords(i.alertname)} on ${i.service_name}</h1>
        <div class="meta">${outcomeChip(i.outcome)}${alertChip(i.alert_status)}${i.alert_count > 1 ? html`<span>fired ${i.alert_count} times</span>` : ""}</div>
      </div>
      ${incidentRecord(trace)}
    </header>
    ${jumpBar()}
    ${open.length ? html`<div class="block" role="region" aria-label="Needs an answer"><p class="block__title">Needs an answer</p>${open.map((a) => approvalCard(a, ui, config, now))}</div>` : ""}
    ${section(1, "Summary", "What happened", summary(trace))}
    ${section(2, "Actions", "What Phoenix did", didBody(trace, ui, config, now), "The tier it chose, why, who approved it, what it changed and whether that worked.")}
    ${section(3, "Reasoning", "What it believed", believedBody(trace), "Each hypothesis, scored by code from the evidence. The model proposes; it does not score.")}
    ${section(4, "Trail", "Step by step", stepsBody(trace, ui), "Every decision in the order it was made.")}
    ${section(5, "Evidence", "What it read", evidenceBody(trace), "Failed reads count as no evidence, never as healthy.")}
    ${section(6, "History", "Seen before?", memoryBody(trace), "Earlier incidents are context only. They never change what Phoenix does.")}
    ${section(7, "Record", "Under the hood", rawBody(trace, ui), "The raw audit rows, and what the model was shown.")}
    <p class="footnote">Everything on this page is read from Phoenix's own records. Times are UTC.</p>`;
}

export function renderNotFound(id) {
  return html`<nav class="crumb"><a href="#/">Incidents</a> / <span>#${id}</span></nav>${emptyState(`No incident ${id}`, "It may have been removed, or the number is wrong.", raw('<a class="btn btn--quiet" href="#/">Back to incidents</a>'))}`;
}
