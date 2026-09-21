const RUNTIME_CONFIG_PATH = "./ideadock.runtime.json"
const DEVELOPMENT_API_BASE = "http://127.0.0.1:8000"

let runtimePromise = null

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
