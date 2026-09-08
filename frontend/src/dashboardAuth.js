export function dashboardHeaders() {
  return {}
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
