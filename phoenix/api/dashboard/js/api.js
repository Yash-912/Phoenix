// Reads from phoenix-api. Same origin, so the paths are bare.

async function get(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    let detail = "";
    try {
      detail = (await response.json()).detail || "";
    } catch {
      // not JSON
    }
    const error = new Error(typeof detail === "string" && detail ? detail : `${response.status}`);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

export const api = {
  config: () => get("/dashboard-config"),
  overview: () => get("/incidents/overview"),
  trace: (id) => get(`/incidents/${encodeURIComponent(id)}/trace`),
  audit: (id) => get(`/incidents/${encodeURIComponent(id)}/audit`),
  pending: () => get("/approvals"),
};
