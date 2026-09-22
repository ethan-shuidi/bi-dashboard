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
let fetchSequence = 0
globalThis.fetch = async (url, init = {}) => {
  requests.push({
    url: String(url),
    headers: { ...(init.headers || {}) },
    cache: init.cache,
    credentials: init.credentials,
    mode: init.mode,
    redirect: init.redirect,
  })
  fetchSequence += 1
  const value = String(url).endsWith("/api/dashboard/write-token")
    ? {
        token: `${Math.floor(Date.now() / 1000) + 600}.test-signature-${fetchSequence}`,
        expires_at: Math.floor(Date.now() / 1000) + 600,
      }
    : { ok: true }
  return { ok: true, status: 200, json: async () => value }
}

const { DashboardApiError, fetchWithDashboardAuth } = await import("./src/dashboardAuth.js")

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
  assert.equal(requests[0].cache, "no-store")
  assert.equal(requests[0].credentials, "omit")
  assert.equal(requests[0].mode, "cors")
  assert.equal(requests[0].redirect, "error")

  assert.equal(requests[1].url, "https://backend.example.test/api/amazon/ad-plan")
  assert.equal(requests[1].headers["Content-Type"], "application/json")
  assert.equal(requests[1].headers["X-Dashboard-Editor"], "%E7%BC%96%E8%BE%91%E8%80%85-abc")
  assert.match(requests[1].headers["X-Dashboard-Write-Token"], /^\d+\.test-signature-\d+$/)
  assert.equal(Object.hasOwn(requests[1].headers, "X-Sync-Key"), false)
  for (const value of Object.values(requests[1].headers)) {
    assert.equal(value, Buffer.from(value, "latin1").toString("latin1"), `header must be ISO-8859-1: ${value}`)
  }
  assert.equal(requests[1].cache, "no-store")
  assert.equal(requests[1].credentials, "omit")
  assert.equal(requests[1].mode, "cors")
  assert.equal(requests[1].redirect, "error")
})

test("standard Headers objects keep business headers and cannot disable request safety", async () => {
  requests.length = 0
  const response = await fetchWithDashboardAuth("https://headers-object.example.test/api/amazon/ad-plan", {
    method: "POST",
    cache: "force-cache",
    credentials: "include",
    headers: new Headers({
      "Content-Type": "application/json",
      "X-Custom-Editor": "custom-value",
      "X-Sync-Key": "caller-must-not-override",
    }),
    body: "{}",
  })

  assert.equal(response.status, 200)
  assert.equal(requests[1].headers["content-type"], "application/json")
  assert.equal(requests[1].headers["x-custom-editor"], "custom-value")
  assert.equal(Object.hasOwn(requests[1].headers, "X-Sync-Key"), false)
  assert.match(requests[1].headers["X-Dashboard-Write-Token"], /^\d+\.test-signature-\d+$/)
  assert.equal(requests[1].cache, "no-store")
  assert.equal(requests[1].credentials, "omit")
})

test("write requests refresh the short-lived token once after 401", async () => {
  requests.length = 0
  const originalFetch = globalThis.fetch
  let targetCalls = 0
  globalThis.fetch = async (url, init = {}) => {
    const response = await originalFetch(url, init)
    if (!String(url).endsWith("/api/dashboard/write-token")) {
      targetCalls += 1
      return targetCalls === 1 ? { ...response, ok: false, status: 401 } : response
    }
    return response
  }
  try {
    const response = await fetchWithDashboardAuth("https://retry-token.example.test/api/amazon/ad-plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    })
    assert.equal(response.status, 200)
    assert.equal(requests.length, 4)
    assert.equal(requests.filter((request) => request.url.endsWith("/api/dashboard/write-token")).length, 2)
    assert.notEqual(requests[1].headers["X-Dashboard-Write-Token"], requests[3].headers["X-Dashboard-Write-Token"])
  } finally {
    globalThis.fetch = originalFetch
  }
})

test("write-token requests retry once after a transient network failure", async () => {
  requests.length = 0
  const originalFetch = globalThis.fetch
  let tokenCalls = 0
  globalThis.fetch = async (url, init = {}) => {
    if (String(url).endsWith("/api/dashboard/write-token")) {
      tokenCalls += 1
      if (tokenCalls === 1) throw new TypeError("Failed to fetch")
    }
    return originalFetch(url, init)
  }
  try {
    const response = await fetchWithDashboardAuth("https://token-retry.example.test/api/amazon/ad-plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    })
    assert.equal(response.status, 200)
    assert.equal(tokenCalls, 2)
    assert.equal(requests.filter((request) => request.url.endsWith("/api/dashboard/write-token")).length, 1)
    assert.equal(requests.filter((request) => !request.url.endsWith("/api/dashboard/write-token")).length, 1)
  } finally {
    globalThis.fetch = originalFetch
  }
})

test("write requests do not auto-retry and never expose raw Failed to fetch", async () => {
  requests.length = 0
  const originalFetch = globalThis.fetch
  globalThis.fetch = async (url, init = {}) => {
    if (String(url).endsWith("/api/dashboard/write-token")) return originalFetch(url, init)
    requests.push({ url: String(url), headers: { ...(init.headers || {}) } })
    throw new TypeError("Failed to fetch")
  }
  try {
    await assert.rejects(
      fetchWithDashboardAuth("https://write-network-failure.example.test/api/amazon/ad-plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }),
      (error) => {
        assert.ok(error instanceof DashboardApiError)
        assert.equal(error.status, 0)
        assert.equal(error.payload.detail.code, "network_error")
        assert.equal(error.message.includes("Failed to fetch"), false)
        assert.equal(error.message.includes("当前内容已保留"), true)
        return true
      },
    )
    assert.equal(requests.filter((request) => !request.url.endsWith("/api/dashboard/write-token")).length, 1)
  } finally {
    globalThis.fetch = originalFetch
  }
})

test("stale preview runtimes are blocked before a write is sent", async () => {
  requests.length = 0
  const originalFetch = globalThis.fetch
  const previousLocation = globalThis.location
  const previousParent = globalThis.window.parent
  globalThis.location = {
    href: "https://ideadock.example.test/api/kratos/idea-dock/preview-runtime/v_old/",
    pathname: "/api/kratos/idea-dock/preview-runtime/v_old/",
  }
  globalThis.window.parent = {
    location: {
      href: "https://ideadock.example.test/idea-dock/#/preview/t_current",
    },
  }
  globalThis.fetch = async (url, init = {}) => {
    requests.push({ url: String(url), headers: { ...(init.headers || {}) } })
    return {
      ok: true,
      json: async () => ({
        code: 0,
        data: {
          runtime_id: "v_current",
          iframe_url: "/api/kratos/idea-dock/preview-runtime/v_current/",
        },
      }),
    }
  }
  try {
    await assert.rejects(
      fetchWithDashboardAuth("https://backend.example.test/api/amazon/ad-plan", {
        method: "POST",
        body: "{}",
      }),
      (error) => {
        assert.ok(error instanceof DashboardApiError)
        assert.equal(error.status, 409)
        assert.equal(error.payload.detail.code, "stale_runtime")
        assert.equal(error.message.includes("完整刷新页面"), true)
        return true
      },
    )
    assert.deepEqual(requests.map((request) => request.url), ["/api/kratos/idea-dock/previews/resolve"])
  } finally {
    globalThis.fetch = originalFetch
    globalThis.location = previousLocation
    globalThis.window.parent = previousParent
  }
})

test("unsafe dashboard request URLs are rejected before fetching", async () => {
  requests.length = 0
  await assert.rejects(
    fetchWithDashboardAuth("file:///api/amazon/ad-plan", { method: "POST", body: "{}" }),
    (error) => error instanceof DashboardApiError && error.status === 400,
  )
  await assert.rejects(
    fetchWithDashboardAuth("https://user:password@backend.example.test/api/amazon/ad-plan", {
      method: "POST",
      body: "{}",
    }),
    (error) => error instanceof DashboardApiError && error.status === 400,
  )
  await assert.rejects(
    fetchWithDashboardAuth("https://backend.example.test/api/amazon/ad-plan", {
      method: "GET",
      headers: { "X-不良请求头": "value" },
    }),
    (error) => error instanceof DashboardApiError && error.status === 400,
  )
  await assert.rejects(
    fetchWithDashboardAuth("https://[invalid", { method: "GET" }),
    (error) => error instanceof DashboardApiError && error.status === 400,
  )
  assert.equal(requests.length, 0)
})
