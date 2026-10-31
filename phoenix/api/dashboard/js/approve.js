// Answering an approval request from the page.
//
// The page never holds the approval token in its source. The person types it, with the
// name they are answering as, and it lives in sessionStorage for this tab only. Both go to
// the existing POST /approvals/{id}/decision, which checks the token and the approver list
// itself; nothing here decides who may approve.

const KEY = "phoenix.approver";

export function loadCredentials(storage) {
  try {
    const stored = JSON.parse(storage.getItem(KEY) || "null");
    if (stored && typeof stored.approver === "string" && typeof stored.token === "string") return stored;
  } catch {
    // storage missing or unreadable: the person is asked again
  }
  return { approver: "", token: "" };
}

export function saveCredentials(storage, { approver, token }) {
  try {
    storage.setItem(KEY, JSON.stringify({ approver, token }));
  } catch {
    // a private window may refuse storage; the answer still goes through this once
  }
}

export function clearCredentials(storage) {
  try {
    storage.removeItem(KEY);
  } catch {
    // nothing stored, nothing to clear
  }
}

function detailText(body) {
  const detail = body && body.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail.result === "string") return detail.result;
  return "";
}

const REFUSALS = {
  401: "The token was not accepted.",
  403: "That name is not on the approver list.",
  404: "That request no longer exists.",
  503: "Approvals are switched off on the server.",
};

// Returns { ok, message, status }. A refusal is a normal answer, not an exception: the
// first answer wins, so "someone already answered" is something the person needs to read.
export async function decide(fetchFn, { approvalId, decision, approver, token }) {
  if (!approver.trim()) return { ok: false, status: 0, message: "Enter the name you are answering as." };
  if (!token.trim()) return { ok: false, status: 0, message: "Enter the approval token." };
  let response;
  try {
    response = await fetchFn(`/approvals/${encodeURIComponent(approvalId)}/decision`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token.trim()}` },
      body: JSON.stringify({ decision, approver: approver.trim() }),
    });
  } catch {
    return { ok: false, status: 0, message: "Could not reach Phoenix." };
  }
  let body = null;
  try {
    body = await response.json();
  } catch {
    // an empty or non-JSON body: the status still says what happened
  }
  if (response.ok) {
    return { ok: true, status: response.status, message: decision === "approve" ? "Approved. Phoenix will resume on its next pass." : "Denied. Phoenix will escalate." };
  }
  const message = response.status === 409 ? detailText(body) || "This request was already answered or has expired." : REFUSALS[response.status] || detailText(body) || `The server answered ${response.status}.`;
  return { ok: false, status: response.status, message };
}
