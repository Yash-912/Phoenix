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

// A ridge line that draws itself on entry. Line work only; pathLength=1 lets one dash value fit every path.
const RIDGE_ART = raw(`<svg class="hero__art" viewBox="0 0 420 170" aria-hidden="true" focusable="false">
  <path pathLength="1" d="M0 160 L46 118 L78 138 L130 66 L160 98 L206 34 L246 108 L276 80 L330 140 L372 108 L420 160"/>
  <path pathLength="1" d="M0 166 L60 140 L104 152 L170 112 L214 140 L284 104 L346 150 L420 130"/>
  <path pathLength="1" d="M206 34 L206 12 L228 19 L206 26"/>
  <circle cx="206" cy="34" r="2.5"/>
</svg>`);

export function renderList({ incidents, filter, now }) {
  if (incidents.length === 0) {
    return html`<div class="page-head"><h1 class="display">Nothing has fired yet.</h1></div>
      ${emptyState("No incidents recorded", "When an alert reaches Phoenix it appears here, with what the agent made of it.")}`;
  }
  const counts = filterCounts(incidents);
  const shown = applyFilter(incidents, filter);
  return html`<header class="page-head hero">
      ${RIDGE_ART}
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
