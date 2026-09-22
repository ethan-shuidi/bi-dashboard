from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_SRC = PROJECT_ROOT / "frontend" / "src"
FRONTEND_ROOT = PROJECT_ROOT / "frontend"


def _source(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def _check_frontend_write_auth(errors: list[str]) -> None:
    source = _source("frontend/src/dashboardAuth.js")
    required_fragments = (
        "/api/dashboard/write-token",
        "X-Dashboard-Write-Token",
        "protectedDashboardHeaderNames",
        "preserveProtectedHeaders",
        "...safeHeaders(options.headers || {})",
        "encodeURIComponent",
        "cachedDashboardEditorId",
        "0x7e",
        "invalidateDashboardWriteToken",
        "headerEntries",
        "safeHttpHeaderName",
        "dashboardRequestUrl",
        'cache: "no-store"',
        'credentials: "omit"',
        'mode: "cors"',
        'redirect: "error"',
    )
    for fragment in required_fragments:
        if fragment not in source:
            errors.append(f"dashboardAuth.js 缺少发布保护片段：{fragment}")
    if "VITE_DASHBOARD_API_KEY" in source or '"X-Sync-Key"' in source:
        errors.append("浏览器端不允许再内置或主动携带长期 X-Sync-Key")


def _check_frontend_runtime(errors: list[str]) -> None:
    runtime_source = _source("frontend/src/dashboardRuntime.js")
    if "ideadock.runtime.v1" not in runtime_source:
        errors.append("运行配置加载器缺少 schema 校验")
    if "deployment_status" not in runtime_source or "healthy" not in runtime_source:
        errors.append("运行配置加载器缺少后端健康状态校验")
    if "生产环境后端必须使用 HTTPS" not in runtime_source:
        errors.append("生产运行配置必须强制 HTTPS")

    for relative_path in ("frontend/src/App.vue", "frontend/src/AmazonDashboard.vue"):
        source = _source(relative_path)
        if "loadDashboardRuntimeOnce" not in source:
            errors.append(f"{relative_path} 必须使用共享运行配置加载器")
        if "VITE_API_BASE" in source or "127.0.0.1:8000" in source:
            errors.append(f"{relative_path} 不允许绕开共享运行配置回退本机后端")


def _check_direct_frontend_fetches(errors: list[str]) -> None:
    fetch_pattern = re.compile(r"(?<![A-Za-z])fetch\s*\(")
    legacy_network_pattern = re.compile(r"\b(?:XMLHttpRequest|sendBeacon\s*\(|axios\s*\()")
    allowed_markers = ("ideadock.runtime.json", "ideadock.verify.json")
    for path in sorted(FRONTEND_SRC.rglob("*")):
        if not path.is_file() or path.suffix not in {".js", ".vue"}:
            continue
        source = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(source.splitlines(), start=1):
            if not fetch_pattern.search(line):
                continue
            if path.name in {"dashboardAuth.js", "dashboardRuntime.js"}:
                continue
            if any(marker in line for marker in allowed_markers):
                continue
            errors.append(f"{path.relative_to(PROJECT_ROOT)}:{line_number} 存在未统一的 fetch 调用")
        for line_number, line in enumerate(source.splitlines(), start=1):
            if legacy_network_pattern.search(line):
                errors.append(f"{path.relative_to(PROJECT_ROOT)}:{line_number} 使用了未统一的网络请求通道")


def _check_frontend_secrets(errors: list[str]) -> None:
    secret_pattern = re.compile(r"VITE_[A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD)")
    credential_pattern = re.compile(r"\b(?:sk-[A-Za-z0-9]|hook/[0-9a-f]{8}-)")
    public_root = FRONTEND_ROOT / "public"
    candidate_paths = [
        *FRONTEND_SRC.rglob("*"),
        *(public_root.rglob("*") if public_root.exists() else []),
        *FRONTEND_ROOT.glob("*.json"),
        *FRONTEND_ROOT.glob("*.mjs"),
        *FRONTEND_ROOT.glob("*.html"),
    ]
    for path in sorted(set(candidate_paths)):
        if not path.is_file() or path.suffix not in {".js", ".vue", ".json", ".mjs", ".html"}:
            continue
        source = path.read_text(encoding="utf-8")
        if secret_pattern.search(source):
            errors.append(f"{path.relative_to(PROJECT_ROOT)} 疑似把凭据构建进前端")
        if credential_pattern.search(source):
            errors.append(f"{path.relative_to(PROJECT_ROOT)} 疑似包含硬编码凭据或通知地址")


def _manifest_routes() -> dict[str, set[str]]:
    manifest = json.loads(_source("backend/ideadock.service.json"))
    routes: dict[str, set[str]] = {}
    for route in manifest.get("routes", []):
        routes[str(route["path"])] = {str(method).upper() for method in route.get("methods", [])}
    return routes


def _fastapi_routes() -> dict[str, set[str]]:
    backend_root = str(PROJECT_ROOT / "backend")
    if backend_root not in sys.path:
        sys.path.insert(0, backend_root)
    from app import app

    routes: dict[str, set[str]] = {}
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods or (path != "/health" and not path.startswith("/api/")):
            continue
        normalized = {method.upper() for method in methods if method.upper() not in {"HEAD", "OPTIONS"}}
        if normalized:
            routes[path] = routes.get(path, set()) | normalized
    return routes


def _check_route_manifest(errors: list[str]) -> None:
    expected = _manifest_routes()
    actual = _fastapi_routes()
    for path in sorted(set(expected) - set(actual)):
        errors.append(f"后端接口清单声明了不存在的路由：{path}")
    for path in sorted(set(actual) - set(expected)):
        errors.append(f"后端接口清单漏掉路由：{path}")
    for path in sorted(set(expected) & set(actual)):
        if expected[path] != actual[path]:
            errors.append(f"路由 {path} 方法声明不一致：{sorted(expected[path])} != {sorted(actual[path])}")
    if "GET" not in expected.get("/api/dashboard/write-token", set()):
        errors.append("短期写权限接口必须在接口清单中声明 GET /api/dashboard/write-token")


def _check_runtime_manifest(errors: list[str]) -> None:
    config: dict[str, Any] = json.loads(_source("frontend/public/ideadock.runtime.json"))
    if config.get("schema") != "ideadock.runtime.v1":
        errors.append("前端运行配置 schema 无效")
    url = config.get("backend_base_url")
    if not isinstance(url, str) or not url.startswith("https://") or "127.0.0.1" in url or "localhost" in url:
        errors.append("生产运行配置的后端地址无效")
    if not isinstance(config.get("active_deployment_id"), int) or config.get("active_deployment_id", 0) <= 0:
        errors.append("生产运行配置缺少有效部署版本")
    if config.get("deployment_status") != "healthy":
        errors.append("生产运行配置绑定的后端不是 healthy 状态")


def _check_dist_if_present(errors: list[str]) -> None:
    dist = PROJECT_ROOT / "frontend" / "dist"
    if not dist.exists():
        return

    javascript_files = sorted(dist.rglob("*.js"))
    all_javascript = "\n".join(path.read_text(encoding="utf-8") for path in javascript_files)
    auth_chunks = [
        path for path in javascript_files
        if "/api/dashboard/write-token" in path.read_text(encoding="utf-8")
    ]
    runtime_path = dist / "ideadock.runtime.json"
    if not runtime_path.is_file():
        errors.append("构建产物缺少后端运行配置")
    else:
        runtime_config = json.loads(runtime_path.read_text(encoding="utf-8"))
        if runtime_config.get("deployment_status") != "healthy":
            errors.append("构建产物绑定了非健康后端部署")

    if not auth_chunks:
        errors.append("构建产物缺少短期写权限获取逻辑")
    for path in auth_chunks:
        javascript = path.read_text(encoding="utf-8")
        if "X-Dashboard-Write-Token" not in javascript:
            errors.append(f"{path.relative_to(PROJECT_ROOT)} 缺少短期写权限请求头")
        if "X-Dashboard-Editor" not in javascript:
            errors.append(f"{path.relative_to(PROJECT_ROOT)} 缺少看板编辑者请求头")
        for option in ("cache", "credentials", "mode", "redirect"):
            if not re.search(rf"{option}\s*:\s*[\"'](no-store|omit|cors|error)[\"']", javascript):
                errors.append(f"{path.relative_to(PROJECT_ROOT)} 缺少 {option} 请求安全策略")

    # 第三方组件（例如上传组件）可能自带 XMLHttpRequest。绕过通道以第一方源码
    # 检查为准；构建产物继续检查长期密钥与前端构建凭据，避免 vendor 噪音掩盖真实风险。
    if '"X-Sync-Key"' in all_javascript:
        errors.append("构建产物不允许携带长期同步密钥请求头")
    if re.search(r"VITE_[A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD)", all_javascript):
        errors.append("构建产物疑似包含前端构建凭据")


def run_all_checks() -> list[str]:
    errors: list[str] = []
    _check_frontend_write_auth(errors)
    _check_frontend_runtime(errors)
    _check_direct_frontend_fetches(errors)
    _check_frontend_secrets(errors)
    _check_route_manifest(errors)
    _check_runtime_manifest(errors)
    _check_dist_if_present(errors)
    return errors


if __name__ == "__main__":
    checks = run_all_checks()
    for check in checks:
        print(check)
    raise SystemExit(bool(checks))
