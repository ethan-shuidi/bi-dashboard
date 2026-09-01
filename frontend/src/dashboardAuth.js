// The public read-only key is injected at build time for the company-internal app.
// It is intentionally not persisted in localStorage or editable page state.
const configuredKey = String(import.meta.env.VITE_DASHBOARD_API_KEY || "").trim()
let sessionKey = configuredKey

export function dashboardHeaders() {
  return sessionKey ? { "X-Sync-Key": sessionKey } : {}
}

export function clearDashboardKey() {
  sessionKey = ""
}

export async function fetchWithDashboardAuth(url, options = {}) {
  const request = () => fetch(url, {
    cache: "no-store",
    ...options,
    headers: { ...(options.headers || {}), ...dashboardHeaders() },
  })
  let response = await request()
  // Public read-only deployments authenticate automatically from the build-time
  // configuration. Keep the 401 response intact when the key is absent/invalid;
  // never interrupt the user with a credential prompt.
  return response
}
