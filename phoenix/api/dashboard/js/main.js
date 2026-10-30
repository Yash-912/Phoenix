// Routing, loading and events. The views are pure; everything that touches the page is here.
import { api } from "./api.js";
import { toString } from "./html.js";
import { decide, loadCredentials, saveCredentials } from "./approve.js";
import { renderList } from "./views/list.js";
import { renderDetail, renderNotFound } from "./views/detail.js";
import { renderApprovals } from "./views/approvals.js";
import { emptyState } from "./views/shared.js";

const app = document.getElementById("app");
const REFRESH_MS = 8000;

const state = {
  config: { approvals_enabled: false, outcomes: {}, tiers: {} },
  filter: "all",
  ui: { showMinor: false, audit: null, auditLoading: false, auditError: "", decision: null, creds: loadCredentials(window.sessionStorage) },
  last: { key: "", data: null },
};

function parseRoute() {
  const hash = window.location.hash.replace(/^#/, "") || "/";
  const detail = hash.match(/^\/incidents\/(\d+)$/);
  if (detail) return { name: "detail", id: Number(detail[1]) };
  if (hash === "/approvals") return { name: "approvals" };
  return { name: "list" };
}

function setNav(route, pending) {
  document.querySelectorAll("[data-nav]").forEach((link) => {
    const here = (link.dataset.nav === "approvals") === (route.name === "approvals");
    if (here && route.name !== "detail") link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  const badge = document.getElementById("pending-badge");
  badge.hidden = !pending;
  badge.textContent = pending ? String(pending) : "";
}

function paint(markup) {
  app.innerHTML = toString(markup);
}

function failure(error) {
  const text = error.status === 503 ? "The database behind Phoenix is not reachable right now." : "Phoenix did not answer.";
  paint(emptyState(text, "This page will keep trying.", null));
}

async function load(route) {
  if (route.name === "detail") {
    return { trace: await api.trace(route.id) };
  }
  if (route.name === "approvals") {
    return { approvals: (await api.pending()).approvals };
  }
  return { incidents: (await api.overview()).incidents };
}

function view(route, data) {
  const now = Date.now();
  if (route.name === "detail") return renderDetail({ trace: data.trace, ui: state.ui, config: state.config, now });
  if (route.name === "approvals") return renderApprovals({ approvals: data.approvals, now });
  return renderList({ incidents: data.incidents, filter: state.filter, now });
}

async function show({ force = false } = {}) {
  const route = parseRoute();
  try {
    const data = await load(route);
    const key = JSON.stringify([route, data]);
    if (!force && key === state.last.key) return;
    state.last = { key, data };
    paint(view(route, data));
    const pending = route.name === "approvals" ? data.approvals.length : route.name === "list" ? data.incidents.reduce((n, r) => n + (r.pending_approvals ? 1 : 0), 0) : null;
    if (pending !== null) state.pending = pending;
    setNav(route, state.pending || 0);
  } catch (error) {
    if (route.name === "detail" && error.status === 404) {
      paint(renderNotFound(route.id));
      return;
    }
    failure(error);
  }
}

function rerender() {
  const route = parseRoute();
  if (state.last.data) paint(view(route, state.last.data));
}

window.addEventListener("hashchange", () => {
  // A different page starts with its own raw rows, its own message and its own folds.
  state.ui = { ...state.ui, audit: null, auditLoading: false, auditError: "", decision: null };
  state.last = { key: "", data: null };
  window.scrollTo(0, 0);
  show({ force: true });
});

app.addEventListener("click", async (event) => {
  const target = event.target.closest("[data-action]");
  if (!target) return;
  const action = target.dataset.action;
  if (action === "filter") {
    state.filter = target.dataset.filter;
    rerender();
    app.querySelector(`[data-filter="${state.filter}"]`)?.focus();
  } else if (action === "toggle-minor") {
    state.ui.showMinor = !state.ui.showMinor;
    rerender();
    app.querySelector('[data-action="toggle-minor"]')?.focus();
  } else if (action === "load-audit") {
    const route = parseRoute();
    state.ui.auditLoading = true;
    state.ui.auditError = "";
    rerender();
    try {
      state.ui.audit = await api.audit(route.id);
    } catch {
      state.ui.auditError = "Could not load the audit rows.";
    }
    state.ui.auditLoading = false;
    rerender();
  }
});

app.addEventListener("submit", async (event) => {
  const form = event.target.closest('form[data-action="decide-form"]');
  if (!form) return;
  event.preventDefault();
  const decision = event.submitter?.dataset.decision;
  if (!decision) return;
  const approver = form.elements.approver.value;
  const token = form.elements.token.value;
  const approvalId = Number(form.dataset.approval);
  state.ui.creds = { approver, token };
  saveCredentials(window.sessionStorage, state.ui.creds);
  state.ui.decision = { approvalId, busy: true, message: "", ok: false };
  rerender();
  const result = await decide(window.fetch.bind(window), { approvalId, decision, approver, token });
  state.ui.decision = { approvalId, busy: false, message: result.message, ok: result.ok };
  if (result.ok) await show({ force: true });
  else rerender();
});

// Keep the page current without disturbing someone who is typing an answer.
setInterval(() => {
  if (document.hidden) return;
  if (app.contains(document.activeElement) && document.activeElement.tagName === "INPUT") return;
  show();
}, REFRESH_MS);

(async function start() {
  try {
    state.config = await api.config();
  } catch {
    // The page still reads; it just cannot offer answering until the config arrives.
  }
  await show({ force: true });
})();
