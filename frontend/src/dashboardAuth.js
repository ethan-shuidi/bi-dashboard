export function dashboardHeaders() {
  const key = String(import.meta.env.VITE_DASHBOARD_API_KEY || "").trim()
  return key ? { "X-Sync-Key": key } : {}
}

export function clearDashboardKey() {
  return undefined
}

export async function fetchWithDashboardAuth(url, options = {}) {
  const request = () => fetch(url, {
    cache: "no-store",
    ...options,
    headers: { ...(options.headers || {}), ...dashboardHeaders() },
  })
  let response = await request()
  return response
}
