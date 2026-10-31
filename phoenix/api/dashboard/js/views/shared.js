// Pieces more than one view uses.
import { html } from "../html.js";
import { toneClass } from "../format.js";

export function outcomeChip(outcome) {
  return html`<span class="chip ${toneClass(outcome.tone)}" title="${outcome.summary}">${outcome.label}</span>`;
}

export function alertChip(status) {
  return html`<span class="chip chip--plain muted">alert ${status}</span>`;
}

// A small line drawing for the empty states: a ridge and a flag, in the spirit of the
// reference, drawn here rather than borrowed.
const RIDGE = html`<svg viewBox="0 0 72 72" aria-hidden="true" focusable="false">
  <path d="M4 58 L22 28 L31 42 L44 16 L68 58 Z" stroke-linejoin="round"/>
  <path d="M44 16 L44 6 L54 9 L44 12" stroke-linejoin="round"/>
  <path d="M4 64 H68" stroke-dasharray="3 3"/>
</svg>`;

export function emptyState(title, text, action) {
  return html`<div class="state">${RIDGE}<h2>${title}</h2><p>${text}</p>${action || ""}</div>`;
}
