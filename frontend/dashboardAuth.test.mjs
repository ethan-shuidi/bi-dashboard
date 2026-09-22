import assert from "node:assert/strict"
import test from "node:test"

const storedEditor = "编辑者-abc"
globalThis.window = {
  crypto: { randomUUID: () => "11111111-2222-3333-4444-555555555555" },
  localStorage: {
    getItem: (key) => key === "ideadock.dashboard.editor-id.v1" ? storedEditor : null,
    setItem: () => {},
  },
}

const requests = []
globalThis.fetch = async (url, init = {}) => {
  requests.push({ url: String(url), headers: { ...(init.headers || {}) } })
  const value = String(url).endsWith("/api/dashboard/write-token")
    ? { token: `${Math.floor(Date.now() / 1000) + 600}.test-signature`, expires_at: Math.floor(Date.now() / 1000) + 600 }
    : { ok: true }
  return { ok: true, status: 200, json: async () => value }
}

const { fetchWithDashboardAuth } = await import("./src/dashboardAuth.js")

test("write requests keep the trusted short-lived token headers", async () => {
  const response = await fetchWithDashboardAuth("https://backend.example.test/api/amazon/ad-plan", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Sync-Key": "caller-must-not-override",
      "X-Dashboard-Write-Token": "caller-must-not-override",
    },
    body: "{}",
  })

  assert.equal(response.status, 200)
  assert.equal(requests.length, 2)
  assert.equal(requests[0].url, "https://backend.example.test/api/dashboard/write-token")
  assert.equal(requests[0].headers["X-Dashboard-Editor"], "%E7%BC%96%E8%BE%91%E8%80%85-abc")

  assert.equal(requests[1].url, "https://backend.example.test/api/amazon/ad-plan")
  assert.equal(requests[1].headers["Content-Type"], "application/json")
  assert.equal(requests[1].headers["X-Dashboard-Editor"], "%E7%BC%96%E8%BE%91%E8%80%85-abc")
  assert.match(requests[1].headers["X-Dashboard-Write-Token"], /^\d+\.test-signature$/)
  assert.equal(Object.hasOwn(requests[1].headers, "X-Sync-Key"), false)
  for (const value of Object.values(requests[1].headers)) {
    assert.equal(value, Buffer.from(value, "latin1").toString("latin1"), `header must be ISO-8859-1: ${value}`)
  }
})
