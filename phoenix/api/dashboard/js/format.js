// Pure formatting and grouping helpers. No DOM, no network, so node can test them.

const MINUTE = 60 * 1000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

export function relativeTime(iso, now = Date.now()) {
  if (!iso) return "—";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "—";
  const age = now - then;
  if (age < 0) return "just now";
  if (age < MINUTE) return "just now";
  if (age < HOUR) return `${Math.floor(age / MINUTE)} min ago`;
  if (age < DAY) return `${Math.floor(age / HOUR)} h ago`;
  return `${Math.floor(age / DAY)} d ago`;
}

// Times are shown in UTC because the audit rows are, and a trace read in two time zones
// is a trace read wrongly.
export function clockTime(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return `${date.toISOString().slice(11, 19)} UTC`;
}

export function fullTime(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return `${date.toISOString().slice(0, 10)} ${date.toISOString().slice(11, 19)} UTC`;
}

export function score(value) {
  return typeof value === "number" ? value.toFixed(2) : "—";
}

export function percent(value) {
  if (typeof value !== "number") return 0;
  return Math.round(Math.min(1, Math.max(0, value)) * 100);
}

// WorkerServiceMemoryGrowth -> "Worker Service Memory Growth": a title that can wrap at its words.
export function splitWords(name) {
  return String(name).replace(/([a-z0-9])([A-Z])/g, "$1 $2").replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2");
}

export function humanKey(key) {
  return String(key).replace(/_/g, " ");
}

export function factValue(value) {
  if (Array.isArray(value)) return value.length ? value.join(", ") : "none";
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toFixed(2);
  return String(value);
}

// Chip classes follow the outcome's tone from /dashboard-config's catalogue (the
// server owns the vocabulary), with a safe default for a tone this page does not know.
const TONES = new Set(["good", "warn", "bad", "attention", "neutral"]);
export function toneClass(tone) {
  return `chip--${TONES.has(tone) ? tone : "neutral"}`;
}

export const FILTERS = [
  { id: "all", label: "All", test: () => true },
  {
    id: "attention",
    label: "Needs a person",
    test: (row) => ["awaiting_approval", "escalated", "action_unavailable", "llm_unavailable", "investigation_failed", "no_outcome"].includes(row.outcome.key),
  },
  { id: "done", label: "Resolved or fixed by PR", test: (row) => ["resolved", "pr_opened", "pr_exists"].includes(row.outcome.key) },
  { id: "noaction", label: "No action taken", test: (row) => ["insufficient_evidence", "symptom_cleared", "not_started"].includes(row.outcome.key) },
  { id: "open", label: "In progress", test: (row) => row.outcome.key === "investigating" },
];

export function applyFilter(rows, filterId) {
  const filter = FILTERS.find((f) => f.id === filterId) || FILTERS[0];
  return rows.filter(filter.test);
}

export function filterCounts(rows) {
  return Object.fromEntries(FILTERS.map((f) => [f.id, rows.filter(f.test).length]));
}

export function stats(rows) {
  const count = (id) => applyFilter(rows, id).length;
  return [
    { n: rows.length, label: "incidents recorded" },
    { n: count("attention"), label: "need a person" },
    { n: count("done"), label: "resolved or fixed by PR" },
    { n: rows.filter((r) => r.pending_approvals > 0).length, label: "waiting for an approval" },
  ];
}

// A unified diff as lines with a class, so additions and removals can be told apart
// without colour alone (the +/- stays in the text).
export function diffLines(diff) {
  return String(diff || "")
    .split("\n")
    .map((text) => ({ text, cls: text.startsWith("+") && !text.startsWith("+++") ? "add" : text.startsWith("-") && !text.startsWith("---") ? "del" : "" }));
}

// How long an open approval has left, as words. Past the deadline it says so.
export function timeLeft(iso, now = Date.now()) {
  const left = Date.parse(iso) - now;
  if (Number.isNaN(left)) return "";
  if (left <= 0) return "past its deadline";
  if (left < MINUTE) return "under a minute left";
  if (left < HOUR) return `${Math.ceil(left / MINUTE)} min left`;
  return `${Math.floor(left / HOUR)} h left`;
}

export function pretty(value) {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

// A link from the database is only ever followed when it is http(s). Anything else
// (javascript:, data:) is shown as text and not linked.
export function safeUrl(url) {
  try {
    const parsed = new URL(String(url));
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.href : null;
  } catch {
    return null;
  }
}
