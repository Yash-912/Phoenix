// The incident list: what Phoenix has seen, with the outcome beside the alert status.
import { html, raw } from "../html.js";
import { FILTERS, applyFilter, filterCounts, relativeTime, stats } from "../format.js";
import { alertChip, emptyState, outcomeChip } from "./shared.js";

function row(incident, now, index) {
  const cause = incident.root_cause_category
    ? html`<span>${incident.root_cause_category.replace(/_/g, " ")}</span>`
    : html`<span class="muted">no root cause named</span>`;
  const tier = incident.tier_used ? html` · Tier ${incident.tier_used}` : "";
  const waiting = incident.pending_approvals
    ? html` · <strong>${incident.pending_approvals} approval${incident.pending_approvals > 1 ? "s" : ""} open</strong>`
    : "";
  return html`<a class="row" href="#/incidents/${incident.id}" data-reveal style="--i:${Math.min(index, 12)}">
    <span class="row__id">#${incident.id}</span>
    <span>
      <span class="row__title">${incident.alertname}</span><br>
      <span class="row__sub">${incident.service_name} · ${incident.severity}${incident.alert_count > 1 ? html` · fired ${incident.alert_count}×` : ""}</span>
    </span>
    <span class="row__outcome">${outcomeChip(incident.outcome)}<br><span class="row__sub">${alertChip(incident.alert_status)}</span></span>
    <span class="row__cause row__sub">${cause}${tier}${waiting}</span>
    <span class="row__time" title="${incident.first_seen_at}">${relativeTime(incident.first_seen_at, now)}</span>
    <span class="row__arrow" aria-hidden="true">→</span>
  </a>`;
}

// A plan of the lab, drawn as a blueprint that builds itself on entry: the gateway, four services,
// the database, and Phoenix watching them. Line work only; pathLength=1 lets one dash value fit every
// path. The two orange squares stand for services with an open incident.
const SYSTEM_PLAN = raw(`<svg class="hero__art" viewBox="0 0 480 210" aria-hidden="true" focusable="false">
  <path pathLength="1" d="M6 82h74v46H6zM138 12h104v44H138zM138 82h104v46H138zM138 152h104v46H138zM304 44h96v46H304zM304 118h96v46H304zM418 82h56v46h-56z"/>
  <path pathLength="1" d="M80 105h29V34h29M109 105h29M109 105v68h29M242 34h31v33h31M242 105h31V67M242 175h31v-34h31M400 67h9v38h9M400 141h9v-36"/>
  <path pathLength="1" d="M6 204h468M6 199v10M138 199v10M304 199v10M474 199v10"/>
  <text x="10" y="76">GATEWAY</text><text x="142" y="7">CHECKOUT</text><text x="142" y="77">PAYMENT</text><text x="142" y="147">AUTH</text>
  <text x="308" y="39">WORKER</text><text x="308" y="113">POSTGRES</text><text x="422" y="77">PHOENIX</text>
  <rect class="mark" x="228" y="20" width="8" height="8"/><rect class="mark" x="386" y="52" width="8" height="8"/>
</svg>`);

export function renderList({ incidents, filter, now }) {
  if (incidents.length === 0) {
    return html`<div class="page-head"><h1 class="display">Nothing has fired yet.</h1></div>
      ${emptyState("No incidents recorded", "When an alert reaches Phoenix it appears here, with what the agent made of it.")}`;
  }
  const counts = filterCounts(incidents);
  const shown = applyFilter(incidents, filter);
  return html`<header class="page-head hero">
      ${SYSTEM_PLAN}
      <p class="label">Incidents</p>
      <h1 class="display">What Phoenix saw, and what it did about it.</h1>
      <p class="lead">Open an incident to read the whole investigation: what it believed, why, and what it changed.</p>
    </header>
    <div class="stats" role="list">
      ${stats(incidents).map((s, index) => html`<div class="stat" role="listitem" data-reveal style="--i:${index}"><div class="stat__n" data-count="${s.n}">${s.n}</div><div class="stat__l">${s.label}</div></div>`)}
    </div>
    <div class="filters" role="group" aria-label="Filter incidents by outcome">
      ${FILTERS.map((f) => html`<button type="button" class="filter" data-action="filter" data-filter="${f.id}" aria-pressed="${String(f.id === filter)}">${f.label} <span class="num">${counts[f.id]}</span></button>`)}
    </div>
    <div class="rows rows--head" aria-hidden="true"><div class="row"><span>#</span><span>Alert</span><span>Outcome</span><span>Root cause</span><span style="text-align:right">First seen</span><span></span></div></div>
    <nav class="rows" aria-label="Incidents">
      ${shown.length ? shown.map((incident, index) => row(incident, now, index)) : raw('<p class="state muted">No incidents match this filter.</p>')}
    </nav>`;
}
