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
    char.charCodeAt(0) <= 0xff ? char : encodeURIComponent(char)
  )).join("")
}

function safeHeaders(headers) {
  return Object.fromEntries(
    Object.entries(headers || {}).map(([name, value]) => [name, safeHttpHeaderValue(value)])
  )
}

export function dashboardHeaders() {
  const key = String(import.meta.env?.VITE_DASHBOARD_API_KEY || "").trim()
  return safeHeaders({
    ...(key ? { "X-Sync-Key": key } : {}),
    "X-Dashboard-Editor": dashboardEditorId(),
  })
}

export function clearDashboardKey() {
  return undefined
}

export async function fetchWithDashboardAuth(url, options = {}) {
  const response = await fetch(url, {
    cache: "no-store",
    ...options,
    headers: safeHeaders({ ...(options.headers || {}), ...dashboardHeaders() }),
  })
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
