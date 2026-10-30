// The queue of requests still waiting for an answer. Answering happens on the incident's
// own page, beside the evidence the approver should read first.
import { html, raw } from "../html.js";
import { relativeTime, timeLeft } from "../format.js";
import { emptyState } from "./shared.js";

export function renderApprovals({ approvals, now }) {
  const body = approvals.length
    ? html`<nav class="rows" aria-label="Open approval requests">${approvals.map((a) => html`<a class="row" href="#/incidents/${a.incident_id}">
        <span class="row__id">#${a.incident_id}</span>
        <span><span class="row__title">${a.action.replace(/_/g, " ")} on ${a.container}</span><br><span class="row__sub">${a.service_name} · tier ${a.tier}</span></span>
        <span class="row__outcome"><span class="chip chip--attention">${timeLeft(a.expires_at, now)}</span></span>
        <span class="row__cause row__sub">${a.plan_reasoning}</span>
        <span class="row__time">asked ${relativeTime(a.requested_at, now)}</span>
        <span class="row__arrow" aria-hidden="true">→</span></a>`)}</nav>`
    : emptyState("Nothing is waiting", "No request is open. When Phoenix holds an action for approval, it is listed here.", raw('<a class="btn btn--quiet" href="#/">See all incidents</a>'));
  return html`<header class="page-head"><p class="label">Approvals</p><h1 class="display">Waiting for an answer.</h1>
    <p class="lead">A request that nobody answers expires and becomes an escalation. It is never approved by default.</p></header>${body}`;
}
