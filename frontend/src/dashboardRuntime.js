const RUNTIME_CONFIG_PATH = "./ideadock.runtime.json"
const DEVELOPMENT_API_BASE = "http://127.0.0.1:8000"
const PREVIEW_RESOLVE_URL = "/api/kratos/idea-dock/previews/resolve"
const RUNTIME_FRESHNESS_TIMEOUT = 30_000

let runtimePromise = null
let runtimeFreshnessPromise = null
let runtimeFreshnessCheckedAt = 0

export class DashboardRuntimeStaleError extends Error {
  constructor(message = "看板预览已发布新版本，当前页面仍在旧版本。") {
    super(message)
    this.name = "DashboardRuntimeStaleError"
  }
}

export function isDashboardRuntimeStaleError(error) {
  return error instanceof DashboardRuntimeStaleError
}

function normalizeBackendBaseUrl(value) {
  const normalized = String(value || "").trim().replace(/\/$/, "")
  if (!normalized) return ""
  try {
    const url = new URL(normalized)
    return url.toString().replace(/\/$/, "")
  } catch {
    return ""
  }
}

function isDevelopmentRuntime() {
  return Boolean(import.meta.env?.DEV)
}

function validateRuntimeConfig(config) {
  if (!config || typeof config !== "object") {
    throw new Error("运行配置格式无效")
  }
  if (config.schema !== "ideadock.runtime.v1") {
    throw new Error("运行配置版本无效")
  }
  const backendBaseUrl = normalizeBackendBaseUrl(config.backend_base_url)
  if (!backendBaseUrl) {
    throw new Error("运行配置缺少有效的后端地址")
  }
  const parsedUrl = new URL(backendBaseUrl)
  if (isDevelopmentRuntime()) {
    if (!["http:", "https:"].includes(parsedUrl.protocol)) {
      throw new Error("开发环境后端协议无效")
    }
  } else if (parsedUrl.protocol !== "https:") {
    throw new Error("生产环境后端必须使用 HTTPS")
  }
  if (
    !isDevelopmentRuntime() &&
    ["localhost", "127.0.0.1", "[::1]"].includes(parsedUrl.hostname.toLowerCase())
  ) {
    throw new Error("生产环境后端地址不允许指向本机")
  }
  if (parsedUrl.username || parsedUrl.password) {
    throw new Error("运行配置后端地址不允许携带凭据")
  }
  const deploymentId = Number(config.active_deployment_id)
  if (!Number.isSafeInteger(deploymentId) || deploymentId <= 0) {
    throw new Error("运行配置缺少有效部署版本")
  }
  if (config.deployment_status !== "healthy") {
    throw new Error(`后端部署状态无效：${config.deployment_status || "未知"}`)
  }
  return { backendBaseUrl }
}

export async function loadDashboardRuntime() {
  if (isDevelopmentRuntime()) {
    try {
      const response = await fetch(RUNTIME_CONFIG_PATH, { cache: "no-store" })
      if (response.ok) return validateRuntimeConfig(await response.json())
    } catch {
      // Local development can start before a runtime manifest is generated.
    }
    return { backendBaseUrl: DEVELOPMENT_API_BASE }
  }
  const response = await fetch(RUNTIME_CONFIG_PATH, { cache: "no-store" })
  if (!response.ok) {
    throw new Error(`运行配置加载失败：HTTP ${response.status}`)
  }
  const config = await response.json()
  return validateRuntimeConfig(config)
}

export function loadDashboardRuntimeOnce() {
  runtimePromise ??= loadDashboardRuntime().catch((error) => {
    runtimePromise = null
    throw error
  })
  return runtimePromise
}

function currentRuntimeId() {
  const pathname = globalThis.location?.pathname || ""
  const match = pathname.match(/\/preview-runtime\/(v_[A-Za-z0-9_-]+)\/?$/)
  return match?.[1] || ""
}

function currentPreviewToken() {
  if (typeof window === "undefined" || window.parent === window) return ""
  try {
    const ownUrl = new URL("", globalThis.location?.href)
    const parentUrl = new URL("", window.parent.location.href)
    if (ownUrl.origin !== parentUrl.origin) return ""
    const parentHref = String(window.parent.location.href)
    const match = parentHref.match(/(?:^|&|\/|#\/)preview\/(t_[A-Za-z0-9_-]+)/)
    return match?.[1] || ""
  } catch {
    // A preview frame deployed on another origin cannot safely ask its parent
    // which runtime is current. Keep normal API requests available in that case.
    return ""
  }
}

function validatePreviewResolveResponse(body) {
  if (body?.code !== 0 || !body?.data) return ""
  const runtimeId = String(body.data.runtime_id || "")
  const iframeUrl = String(body.data.iframe_url || "")
  if (!/^v_[A-Za-z0-9_-]+$/.test(runtimeId)) return ""
  if (!iframeUrl.includes(runtimeId)) return ""
  return runtimeId
}

export async function ensureDashboardRuntimeCurrent({ force = false } = {}) {
  const runtimeId = currentRuntimeId()
  const previewToken = currentPreviewToken()
  if (!runtimeId || !previewToken) return false
  if (!force && Date.now() - runtimeFreshnessCheckedAt < RUNTIME_FRESHNESS_TIMEOUT) return true

  runtimeFreshnessPromise ??= (async () => {
    let response
    try {
      response = await fetch(PREVIEW_RESOLVE_URL, {
        method: "POST",
        cache: "no-store",
        credentials: "same-origin",
        redirect: "error",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: previewToken }),
      })
    } catch {
      // Preview resolution is a safety check, not the primary API path. If the
      // IdeaDock shell is temporarily unavailable, dashboard APIs still run.
      return false
    }
    if (!response.ok) return false
    const currentId = validatePreviewResolveResponse(await response.json().catch(() => null))
    if (!currentId) return false
    if (currentId !== runtimeId) {
      throw new DashboardRuntimeStaleError(
        "看板预览已发布新版本，当前页面仍在旧版本；为避免保存到错误链路，请先确认未保存内容已备份，然后完整刷新页面再保存。",
      )
    }
    runtimeFreshnessCheckedAt = Date.now()
    return true
  })()

  try {
    return await runtimeFreshnessPromise
  } finally {
    runtimeFreshnessPromise = null
  }
}
