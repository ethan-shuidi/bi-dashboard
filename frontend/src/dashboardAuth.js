import { ElMessageBox } from "element-plus"

const editorStorageKey = "ideadock.dashboard.editor-id.v1"
const anonymousEditorPrefix = "Editor"

function randomEditorToken() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID().slice(0, 8)
  return Math.random().toString(36).slice(2, 10)
}

export function dashboardEditorId() {
  try {
    const existing = window.localStorage.getItem(editorStorageKey)
    if (existing) return existing
    const created = `${anonymousEditorPrefix}-${randomEditorToken()}`
    window.localStorage.setItem(editorStorageKey, created)
    return created
  } catch {
    return `${anonymousEditorPrefix}-local`
  }
}

function safeHttpHeaderValue(value) {
  const normalized = String(value ?? "")
    .replace(/[\u0000-\u001f\u007f]/g, " ")
    .trim()
  return Array.from(normalized).map((char) => (
    char.charCodeAt(0) >= 0x20 && char.charCodeAt(0) <= 0x7e ? char : encodeURIComponent(char)
  )).join("")
}

const protectedDashboardHeaderNames = new Set([
  "x-sync-key",
  "x-dashboard-write-token",
  "x-dashboard-editor",
])

function safeHeaders(headers) {
  return Object.fromEntries(
    Object.entries(headers || {})
      .filter(([name]) => !protectedDashboardHeaderNames.has(String(name).toLowerCase()))
      .map(([name, value]) => [name, safeHttpHeaderValue(value)])
  )
}

const writeTokenStates = new Map()

function dashboardWriteTokenUrl(url) {
  const value = String(url)
  const apiIndex = value.indexOf("/api/")
  const base = apiIndex >= 0 ? value.slice(0, apiIndex) : new URL("/", value).toString().replace(/\/$/, "")
  return `${base}/api/dashboard/write-token`
}

async function requestDashboardWriteToken(editor, targetUrl) {
  const response = await fetch(dashboardWriteTokenUrl(targetUrl), {
    cache: "no-store",
    headers: safeHeaders({ "X-Dashboard-Editor": editor }),
  })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new DashboardApiError(dashboardApiErrorMessage(body, response.status), response.status, body)
  }
  return body
}

function writeTokenState(tokenUrl) {
  let state = writeTokenStates.get(tokenUrl)
  if (!state) {
    state = { editor: null, token: null, expiresAt: 0, promise: null }
    writeTokenStates.set(tokenUrl, state)
  }
  return state
}

function validateDashboardWriteTokenResponse(body, nowMilliseconds) {
  const token = String(body?.token || "")
  const expiresAtSeconds = Number(body?.expires_at)
  if (!/^\d+\.[A-Za-z0-9_-]+$/.test(token)) {
    throw new DashboardApiError("看板写权限格式无效", 502, body)
  }
  if (!Number.isSafeInteger(expiresAtSeconds) || expiresAtSeconds <= 0) {
    throw new DashboardApiError("看板写权限过期时间无效", 502, body)
  }
  if (Number(token.split(".", 1)[0]) !== expiresAtSeconds) {
    throw new DashboardApiError("看板写权限过期信息不一致", 502, body)
  }
  const expiresAtMilliseconds = expiresAtSeconds * 1000
  if (expiresAtMilliseconds <= nowMilliseconds + 5_000) {
    throw new DashboardApiError("看板写权限已过期", 502, body)
  }
  if (expiresAtMilliseconds > nowMilliseconds + 24 * 60 * 60 * 1000) {
    throw new DashboardApiError("看板写权限有效期异常", 502, body)
  }
  return { token, expiresAtMilliseconds }
}

async function ensureDashboardWriteToken(targetUrl) {
  const editor = dashboardEditorId()
  const tokenUrl = dashboardWriteTokenUrl(targetUrl)
  const state = writeTokenState(tokenUrl)
  const now = Date.now()
  if (state.editor === editor && state.token && state.expiresAt > now + 60_000) {
    return state.token
  }
  if (!state.promise) {
    state.promise = requestDashboardWriteToken(editor, tokenUrl)
      .then((body) => {
        const validated = validateDashboardWriteTokenResponse(body, Date.now())
        state.editor = editor
        state.token = validated.token
        state.expiresAt = validated.expiresAtMilliseconds
        return validated.token
      })
      .finally(() => {
        state.promise = null
      })
  }
  return state.promise
}

function invalidateDashboardWriteToken(targetUrl) {
  const state = writeTokenStates.get(dashboardWriteTokenUrl(targetUrl))
  if (!state) return
  state.token = null
  state.expiresAt = 0
  state.promise = null
}

function dashboardHeaders(writeToken = null) {
  return safeHeaders({
    ...(writeToken ? { "X-Dashboard-Write-Token": writeToken } : {}),
    "X-Dashboard-Editor": dashboardEditorId(),
  })
}

export async function fetchWithDashboardAuth(url, options = {}) {
  const method = String(options.method || "GET").toUpperCase()
  const isWriteMethod = method !== "GET" && method !== "HEAD" && method !== "OPTIONS"
  let writeToken = null
  let headers = dashboardHeaders()
  if (isWriteMethod) {
    writeToken = await ensureDashboardWriteToken(url)
    headers = dashboardHeaders(writeToken)
  }
  let response = await fetch(url, {
    cache: "no-store",
    ...options,
    headers: safeHeaders({ ...(options.headers || {}), ...headers }),
  })
  if (response.status === 401 && isWriteMethod && writeToken) {
    invalidateDashboardWriteToken(url)
    writeToken = await ensureDashboardWriteToken(url)
    response = await fetch(url, {
      cache: "no-store",
      ...options,
      headers: safeHeaders({ ...(options.headers || {}), ...dashboardHeaders(writeToken) }),
    })
  }
  return response
}

export class DashboardApiError extends Error {
  constructor(message, status, payload) {
    super(message)
    this.name = "DashboardApiError"
    this.status = status
    this.payload = payload
  }

  get isEditConflict() {
    return this.status === 409 && this.payload?.detail?.code === "edit_conflict"
  }
}

export function dashboardApiErrorMessage(body, status) {
  const detail = body?.detail
  if (typeof detail === "string" && detail) return detail
  if (detail?.message) return detail.message
  if (Array.isArray(detail) && detail.length) return detail[0]?.msg || `请求失败：HTTP ${status}`
  return `请求失败：HTTP ${status}`
}

export async function parseDashboardResponse(response) {
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new DashboardApiError(dashboardApiErrorMessage(body, response.status), response.status, body)
  }
  return body
}

export async function apiWithDashboardAuth(url, options = {}) {
  return parseDashboardResponse(await fetchWithDashboardAuth(url, options))
}

export function formatDashboardEditTime(value) {
  if (!value) return ""
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return ""
  const formatted = new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(parsed)
  return formatted.replace(/\//g, "-")
}

export function formatDashboardEditMetadata(metadata = {}) {
  const editor = metadata.updated_by ? String(metadata.updated_by).trim() : ""
  const time = formatDashboardEditTime(metadata.updated_at)
  if (!editor && !time) return "暂无编辑记录"
  return `云端最后编辑：${editor || "未知编辑者"}${time ? ` · ${time}` : ""}`
}

export async function chooseEditConflictAction(exception) {
  if (!(exception instanceof DashboardApiError) || !exception.isEditConflict) return "not-conflict"
  const detail = exception.payload?.detail || {}
  const metadata = formatDashboardEditMetadata(detail)
  try {
    await ElMessageBox.confirm(
      `${detail.message || "云端内容已被其他人更新。"}\n${metadata}\n\n覆盖保存会以当前页面内容为准；加载云端会替换当前内容；直接关闭则保留本地草稿。`,
      "云端内容已被他人更新",
      {
        confirmButtonText: "覆盖保存",
        cancelButtonText: "加载云端内容",
        distinguishCancelAndClose: true,
        type: "warning",
      },
    )
    return "overwrite"
  } catch (action) {
    return action === "cancel" ? "reload" : "cancel"
  }
}
