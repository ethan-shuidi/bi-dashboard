from __future__ import annotations

import asyncio
import calendar
import json
import os
import re
import base64
import copy
import hashlib
import hmac
import math
import time
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import quote, unquote
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import Body, FastAPI, Header, HTTPException, Query
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    func,
    select,
    text,
)
from sqlalchemy import inspect as sql_inspect
from sqlalchemy.orm import Session, declarative_base, relationship, sessionmaker


app = FastAPI(title="Shopify BI Dashboard API", version="1.0.0")


def dashboard_origins() -> list[str]:
    configured = os.environ.get("DASHBOARD_ALLOWED_ORIGINS", "").strip()
    if configured:
        return [origin.rstrip("/") for origin in configured.split(",") if origin.strip()]
    return [
        "https://ideadock.shuidihuzhu.com",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]


_DASHBOARD_ORIGINS = dashboard_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_DASHBOARD_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Sync-Key", "X-Dashboard-Editor", "X-Dashboard-Write-Token"],
)


def require_business_access(
    x_sync_key: str | None,
    *,
    allow_public: bool = True,
) -> None:
    """Allow public read-only access while protecting state-changing APIs."""
    if allow_public:
        return None
    if _dashboard_write_authorized.get():
        return None
    expected = os.environ.get("SYNC_API_KEY")
    if not expected or not x_sync_key or not hmac.compare_digest(x_sync_key, expected):
        raise HTTPException(status_code=401, detail="看板接口需要有效的 X-Sync-Key")


def dashboard_write_token_signature(editor: str, origin: str, expires_at: int) -> str:
    expected = os.environ.get("SYNC_API_KEY")
    if not expected:
        raise HTTPException(status_code=503, detail="看板写接口未配置访问密钥")
    message = f"dashboard-write-v1\n{editor}\n{origin}\n{expires_at}".encode()
    digest = hmac.new(expected.encode(), message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def issue_dashboard_write_token(
    editor: str | None,
    origin: str | None,
    *,
    now: int | None = None,
) -> dict[str, Any]:
    normalized_origin = (origin or "").rstrip("/")
    if not normalized_origin or normalized_origin not in _DASHBOARD_ORIGINS:
        raise HTTPException(status_code=403, detail="看板写权限来源不允许")
    editor_value = str(editor or "").strip() or "Editor-anonymous"
    issued_at = int(now if now is not None else time.time())
    expires_at = issued_at + 30 * 60
    canonical_editor = dashboard_editor(editor_value)
    signature = dashboard_write_token_signature(canonical_editor, normalized_origin, expires_at)
    return {
        "token": f"{expires_at}.{signature}",
        "expires_at": expires_at,
        "editor": canonical_editor,
        "expires_in": expires_at - issued_at,
    }


def dashboard_write_token_valid(
    token: str | None,
    editor: str | None,
    origin: str | None,
    *,
    now: int | None = None,
) -> bool:
    if not token or "." not in token:
        return False
    raw_expires, signature = token.rsplit(".", 1)
    if not raw_expires.isdigit():
        return False
    expires_at = int(raw_expires)
    if expires_at <= int(now if now is not None else time.time()):
        return False
    normalized_origin = (origin or "").rstrip("/")
    if not normalized_origin or normalized_origin not in _DASHBOARD_ORIGINS:
        return False
    raw_editor = str(editor or "").strip()
    editor_candidates = {raw_editor, ""}
    try:
        editor_candidates.add(unquote(raw_editor, errors="strict"))
        editor_candidates.add(quote(raw_editor, safe=""))
    except UnicodeDecodeError:
        pass
    editor_candidates.add(dashboard_editor(raw_editor))
    editor_candidates.add("Editor-anonymous")
    return any(
        hmac.compare_digest(signature, dashboard_write_token_signature(candidate, normalized_origin, expires_at))
        for candidate in editor_candidates
    )


def dashboard_write_request_credentials(request: Request) -> tuple[str | None, str | None]:
    return (
        request.headers.get("X-Dashboard-Write-Token") or request.query_params.get("dashboard_write_token"),
        request.headers.get("X-Dashboard-Editor") or request.query_params.get("dashboard_editor"),
    )


def rewrite_simple_json_request(request: Request) -> None:
    """Treat a browser simple POST body as JSON after authentication.

    The browser client uses ``text/plain;charset=UTF-8`` to avoid a CORS
    preflight. The credential is carried in short-lived query parameters and the
    request origin remains bound into the token. Header-authenticated callers
    continue to use ``application/json`` without any change.
    """

    content_type = request.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type != "text/plain":
        return
    scope_headers = [
        (name, value) for name, value in request.scope.get("headers", [])
        if name.lower() != b"content-type"
    ]
    editor = request.query_params.get("dashboard_editor")
    has_editor_header = any(name.lower() == b"x-dashboard-editor" for name, _ in scope_headers)
    if editor and not has_editor_header:
        scope_headers.append((b"x-dashboard-editor", quote(editor, safe="").encode("ascii")))
    request.scope["headers"] = scope_headers + [(b"content-type", b"application/json")]

Base = declarative_base()
_engine = None
_session_factory = None
_lingxing_token: dict[str, Any] = {}
_lingxing_token_lock = asyncio.Lock()
_lingxing_performance_lock = asyncio.Lock()
_lingxing_performance_last_call = 0.0
_lingxing_ad_report_lock = asyncio.Lock()
_lingxing_ad_report_last_call = 0.0
_lingxing_store_lock = asyncio.Lock()
_lingxing_mcp_metadata_lock = asyncio.Lock()
_keyword_dashboard_fetch_lock = asyncio.Lock()
_keyword_unavailable_weeks_lock = threading.RLock()
_keyword_unavailable_weeks: dict[tuple[str, date], datetime] = {}
_xiyou_keyword_dashboard_scope: ContextVar[bool] = ContextVar(
    "xiyou_keyword_dashboard_scope",
    default=False,
)
_dashboard_write_authorized: ContextVar[bool] = ContextVar(
    "dashboard_write_authorized",
    default=False,
)
_dashboard_request_editor: ContextVar[str | None] = ContextVar(
    "dashboard_request_editor",
    default=None,
)
_amazon_cache_scope: ContextVar[str] = ContextVar(
    "amazon_cache_scope",
    default="shared",
)
_lingxing_mcp_metadata_cache: dict[str, tuple[float, LingXingMCPMetadata]] = {}
_lingxing_mcp_catalog_version = ""


class ModuleScopedCache(dict[tuple[str, tuple[Any, ...]], tuple[float, Any]]):
    """Keep dashboard-level cache entries isolated by requesting module.

    Immutable reference data (exchange rates and the store catalogue) stays in
    a shared namespace. Raw upstream and derived dashboard entries follow the
    current request namespace, so refreshing one dashboard cannot evict another
    dashboard's completed response.
    """

    shared_prefixes = {
        "amazon-usd-fx",
        "lingxing-stores",
        "lingxing-mcp-ad-shops",
    }

    @staticmethod
    def storage_key(key: tuple[Any, ...]) -> tuple[str, tuple[Any, ...]]:
        prefix = str(key[0]) if key else ""
        namespace = "shared" if prefix in ModuleScopedCache.shared_prefixes else _amazon_cache_scope.get()
        return (namespace, tuple(key))

    def get(self, key: Any, default: Any = None) -> Any:
        return super().get(self.storage_key(tuple(key)), default)

    def __getitem__(self, key: Any) -> Any:
        return super().__getitem__(self.storage_key(tuple(key)))

    def __setitem__(self, key: Any, value: Any) -> None:
        super().__setitem__(self.storage_key(tuple(key)), value)

    def pop(self, key: Any, *default: Any) -> Any:
        return super().pop(self.storage_key(tuple(key)), *default)

    def clear_current_namespace(self) -> None:
        namespace = _amazon_cache_scope.get()
        for stored_key in list(self.keys()):
            if stored_key[0] == namespace:
                super().__delitem__(stored_key)

    def clear_namespaces(self, *namespaces: str) -> None:
        selected = set(namespaces)
        for stored_key in list(self.keys()):
            if stored_key[0] in selected:
                super().__delitem__(stored_key)


_amazon_cache = ModuleScopedCache()


@contextmanager
def xiyou_keyword_dashboard_scope():
    """Allow Xiyou requests only inside the keyword-dashboard request scope."""

    token = _xiyou_keyword_dashboard_scope.set(True)
    try:
        yield
    finally:
        _xiyou_keyword_dashboard_scope.reset(token)


@app.middleware("http")
async def scope_dashboard_request(request: Request, call_next):
    """Apply write auth and isolate dashboard caches and external APIs."""

    path = request.url.path.rstrip("/")
    method = request.method.upper()
    cache_namespaces = {
        "/api/amazon/dashboard": "amazon-product-dashboard",
        "/api/amazon/ads-charts": "amazon-ads-charts",
        "/api/amazon/strategy-board": "amazon-strategy-board",
        "/api/amazon/sales-dashboard": "amazon-sales-targets",
    }
    namespace = next((value for prefix, value in cache_namespaces.items() if path.startswith(prefix)), "shared")
    cache_token = _amazon_cache_scope.set(namespace)
    authorization_token = None
    editor_token = None
    try:
        if method not in {"GET", "HEAD", "OPTIONS"}:
            expected_key = os.environ.get("SYNC_API_KEY")
            supplied_key = request.headers.get("X-Sync-Key")
            supplied_write_token, supplied_editor = dashboard_write_request_credentials(request)
            editor_token = _dashboard_request_editor.set(dashboard_editor(supplied_editor))
            if not expected_key:
                return JSONResponse(status_code=503, content={"detail": "看板写接口未配置访问密钥"})
            if supplied_write_token and dashboard_write_token_valid(
                supplied_write_token,
                supplied_editor,
                request.headers.get("Origin"),
            ):
                authorization_token = _dashboard_write_authorized.set(True)
            elif not supplied_key or not hmac.compare_digest(supplied_key, expected_key):
                return JSONResponse(status_code=401, content={"detail": "看板接口需要有效的 X-Sync-Key"})
            origin = request.headers.get("Origin")
            if origin and origin.rstrip("/") not in _DASHBOARD_ORIGINS:
                return JSONResponse(status_code=403, content={"detail": "看板写接口来源不允许"})
            rewrite_simple_json_request(request)
        if path == "/api/keyword-dashboard":
            with xiyou_keyword_dashboard_scope():
                return await call_next(request)
        return await call_next(request)
    finally:
        if authorization_token is not None:
            _dashboard_write_authorized.reset(authorization_token)
        if editor_token is not None:
            _dashboard_request_editor.reset(editor_token)
        _amazon_cache_scope.reset(cache_token)
KEYWORD_CATEGORIES = (
    "comu品牌词",
    "AI核心词",
    "类目词",
    "Plaud品牌词",
    "Pocket品牌词",
    "其他品牌词",
)
KEYWORD_FETCH_TERM_BATCH_SIZE = 20
KEYWORD_FETCH_WEEK_CHUNK = 5
KEYWORD_UNAVAILABLE_WEEK_TTL_SECONDS = 6 * 60 * 60
KEYWORD_DASHBOARD_FETCH_LOCK_NAME = "bi_dashboard_keyword_fetch"
AMAZON_CACHE_TTL_SECONDS = 600
LINGXING_STORE_CACHE_TTL_SECONDS = 900
LINGXING_MCP_METADATA_TTL_SECONDS = 3600
AMAZON_UPSTREAM_CONCURRENCY = 5
AMAZON_CURRENCY_CODES = {
    "美国": "USD", "日本": "JPY", "加拿大": "CAD", "澳洲": "AUD",
    "英国": "GBP", "德国": "EUR", "法国": "EUR", "意大利": "EUR",
    "西班牙": "EUR", "荷兰": "EUR", "比利时": "EUR", "墨西哥": "MXN",
    "爱尔兰": "EUR", "波兰": "PLN", "瑞典": "SEK",
}
AMAZON_SUPPORTED_CURRENCIES = ("USD", "CNY", "JPY", "EUR", "GBP", "CAD", "AUD", "SEK", "MXN", "PLN")

DEFAULT_TIMEZONE = "America/Los_Angeles"
DEFAULT_API_VERSION = "2026-07"
DEFAULT_INITIAL_SYNC_DAYS = 90
DEFAULT_SYNC_COOLDOWN_SECONDS = 600

AMAZON_STRATEGY_OPTIONS = ("品类词", "品牌防御", "竞品词", "自动", "SB/SBV", "SD", "B2B", "bundle", "/")

LINGXING_API_BASE = "https://openapi.lingxing.com"
XIYOU_API_BASE = os.environ.get("XIYOU_API_BASE", "https://openapi.xydc.com").rstrip("/")
LINGXING_MCP_URL = "https://openmcp.lingxing.com/mcp-servers/lingxing-mcp"
LINGXING_MCP_PRODUCT_TOOL = "query_product_performance_asin_lists"
LINGXING_MCP_CAMPAIGN_TOOL = "ad_campaign_report"
LINGXING_MCP_SHOPS_TOOL = "ad_auth_shops"
LINGXING_MCP_KNOWN_VERSIONS = {
    LINGXING_MCP_PRODUCT_TOOL: (
        "lingxing-mcp-20260915-v1",
        "query_product_performance_asin_lists-v1-c500-20260907",
        288,
    ),
    LINGXING_MCP_CAMPAIGN_TOOL: (
        "lingxing-mcp-20260915-v1",
        "ad_campaign_report-260914-v1",
        180068,
    ),
    LINGXING_MCP_SHOPS_TOOL: (
        "lingxing-mcp-20260915-v1",
        "ad_auth_shops-v1-c500-20260907",
        199,
    ),
}


@dataclass(frozen=True)
class LingXingMCPMetadata:
    """A concrete LingXing MCP tool version resolved from its live catalog."""

    tool_id: str
    schema_version: str
    tool_version_id: int
    catalog_version: str
AMAZON_SERIES = [
    "TN10系列（主链接）汇总",
    "TN10系列（小链接）汇总",
    "TN20系列（主链接）汇总",
    "TN20系列（小链接）汇总",
]
AMAZON_PRODUCTS = [
    "TN10-主链接-黑色", "TN10-主链接-银色", "TN10-主链接-橙色",
    "TN10-小链接-黑色", "TN10-小链接-银色", "TN10-小链接-橙色",
    "TN20-主链接-黑色", "TN20-主链接-银色", "TN20-主链接-红",
    "TN20-小链接-黑色", "TN20-小链接-银色", "TN20-小链接-樱桃红",
]
AMAZON_SALES_MODELS = ("TN10", "TN20")
AMAZON_SALES_ALL_MODEL = "ALL"
AMAZON_SALES_MODEL_CHOICES = (AMAZON_SALES_ALL_MODEL, *AMAZON_SALES_MODELS)
AMAZON_SALES_ALL_SITES = "全部站点"
AMAZON_SALES_EUROPE = "欧洲"
AMAZON_SALES_EUROPE_SITES = (
    "英国", "德国", "法国", "意大利", "西班牙", "荷兰", "比利时", "爱尔兰", "波兰", "瑞典",
)
AMAZON_SALES_TN20_SMALL_SERIES = "TN20系列（小链接）汇总"
AMAZON_SALES_METRICS = (
    {
        "key": "units",
        "label": "销量",
        "format": "count",
        "completion": "ratio",
        "target_input": True,
    },
    {
        "key": "aov",
        "label": "客单",
        "format": "money",
        "completion": "difference",
        "difference_rule": "lower_is_red",
        "target_input": True,
    },
    {
        "key": "net_sales",
        "label": "销售额",
        "format": "money",
        "completion": "ratio",
        "target_formula": "销量 × 客单",
    },
    {
        "key": "cpc",
        "label": "CPC",
        "format": "money",
        "completion": "difference",
        "difference_rule": "lower_is_red",
        "target_input": True,
    },
    {
        "key": "ad_sales_share",
        "label": "广告销量占比",
        "format": "percent",
        "completion": "difference",
        "difference_rule": "lower_is_red",
        "target_input": True,
    },
    {
        "key": "acoas",
        "label": "广告费比",
        "format": "percent",
        "completion": "difference",
        "difference_rule": "lower_is_red",
        "target_formula": "广告花费 ÷ 销售额",
    },
    {
        "key": "ad_units",
        "label": "广告销量",
        "format": "count",
        "completion": "difference",
        "difference_rule": "higher_is_red",
        "target_formula": "广告点击 × 广告CVR",
    },
    {
        "key": "clicks",
        "label": "广告点击",
        "format": "count",
        "completion": "difference",
        "difference_rule": "higher_is_red",
        "target_formula": "销量 × 广告销量占比 ÷ 广告CVR",
    },
    {
        "key": "ad_cvr",
        "label": "广告CVR",
        "format": "percent",
        "completion": "difference",
        "difference_rule": "higher_is_red",
        "target_input": True,
    },
    {
        "key": "ad_cost",
        "label": "广告花费",
        "format": "money",
        "completion": "difference",
        "difference_rule": "higher_is_red",
        "target_formula": "CPC × 广告点击",
    },
)
AMAZON_SALES_COMPARISON_LOWER_IS_BETTER = frozenset(
    {"cpc", "ad_sales_share", "acoas", "ad_cost"}
)
AMAZON_SALES_TARGET_FIELDS = tuple(metric["key"] for metric in AMAZON_SALES_METRICS if metric.get("target_input"))
AMAZON_SITE_ORDER = ("美国", "日本", "德国", "英国", "法国", "加拿大", "澳洲", "西班牙", "意大利", "荷兰", "比利时", "墨西哥", "爱尔兰", "波兰", "瑞典")
AMAZON_SITE_CODES = {"美国": "US", "日本": "JP", "德国": "DE", "英国": "UK", "法国": "FR", "加拿大": "CA", "澳洲": "AU", "西班牙": "ES", "意大利": "IT", "荷兰": "NL", "比利时": "BE", "墨西哥": "MX", "爱尔兰": "IE", "波兰": "PL", "瑞典": "SE"}
AMAZON_SITE_TIMEZONES = {
    "美国": "America/Los_Angeles",
    "日本": "Asia/Tokyo",
    "加拿大": "America/Toronto",
    "澳洲": "Australia/Sydney",
    "德国": "Europe/Berlin",
    "法国": "Europe/Paris",
    "意大利": "Europe/Rome",
    "西班牙": "Europe/Madrid",
    "英国": "Europe/London",
    "荷兰": "Europe/Amsterdam",
    "比利时": "Europe/Brussels",
    "墨西哥": "America/Mexico_City",
    "爱尔兰": "Europe/Dublin",
    "波兰": "Europe/Warsaw",
    "瑞典": "Europe/Stockholm",
}
AMAZON_MAX_DATE_RANGE_DAYS = 400

# The Feishu mapping is keyed by site + ASIN.  Site-specific ASIN lists are
# intentionally kept as data, so a later refresh can replace this block
# without changing aggregation logic.
ASIN_MAPPING = {
    "US": {"B0G1XQ3H4H":"TN10-主链接-黑色","B0G1YMLFSZ":"TN10-小链接-银色","B0GMGP9B1D":"TN10-小链接-橙色","B0GSJMTSMQ":"TN10-小链接-黑色","B0GZNNL72W":"TN10-主链接-橙色","B0GR9CDQYG":"TN10-主链接-银色","B0H8SF6N61":"TN20-主链接-黑色","B0H8S9M43Y":"TN20-主链接-银色","B0H8SZZN8X":"TN20-主链接-红","B0H8MRQW7Q":"TN20-小链接-黑色","B0H8CKG9P5":"TN20-小链接-银色","B0H8NP9TVK":"TN20-小链接-樱桃红"},
    "JP": {"B0G4M5QMNG":"TN10-主链接-黑色","B0G4M4YMHZ":"TN10-主链接-银色","B0G4M4KZ5S":"TN10-主链接-橙色","B0HC6V88K5":"TN20-主链接-黑色","B0HC75XJ3D":"TN20-主链接-银色","B0HC78T99S":"TN20-主链接-红","B0HD7GRRL5":"TN20-小链接-黑色","B0HD77JKX5":"TN20-小链接-银色","B0HD7QJ1XJ":"TN20-小链接-樱桃红"},
    # Canada has a site-specific TN20 layout. Keep it explicit so positional
    # synchronization can never silently reassign these ASINs to TN10 variants.
    "CA": {
        "B0G1XQ3H4H": "TN10-主链接-黑色",
        "B0G1YMLFSZ": "TN10-主链接-银色",
        "B0G1YCTVJG": "TN10-主链接-橙色",
        "B0H8NCJLMD": "TN20-主链接-黑色",
        "B0H8RSZHB3": "TN20-主链接-银色",
        "B0H8S2TK5K": "TN20-主链接-红",
        "B0H94CHVCN": "TN20-小链接-黑色",
        "B0H94MYQP3": "TN20-小链接-银色",
        "B0H94QM3TZ": "TN20-小链接-樱桃红",
    },
}
for _site, _asins, _series in [
    ("AU", ["B0G1XQ3H4H","B0G1YMLFSZ","B0G1YCTVJG","B0H8N38BB4","B0H8N3KCBK","B0H8NGTX8G","B0H8PFZ3WW","B0H8PCPN8Z","B0H8PDYNH9"], None),
    ("DE", ["B0G4WGC459","B0G4WJMFB3","B0G55V8N7H","B0H7S1BDZ1","B0H8N8DLYX","B0H8N55JPT","B0H8CKG9P5","B0H8D4YZTS","B0H8D96XRQ"], None),
    ("FR", ["B0G4WGC459","B0G4WJMFB3","B0G55V8N7H","B0H7S1BDZ1","B0H8N8DLYX","B0H8N55JPT","B0H8CKG9P5","B0H8D4YZTS","B0H8D96XRQ"], None),
    ("IT", ["B0G4WGC459","B0G4WJMFB3","B0G55V8N7H","B0H7S1BDZ1","B0H8N8DLYX","B0H8N55JPT","B0H8CKG9P5","B0H8D4YZTS","B0H8D96XRQ"], None),
    ("ES", ["B0G4WGC459","B0G4WJMFB3","B0G55V8N7H","B0H7S1BDZ1","B0H8N8DLYX","B0H8N55JPT","B0H8CKG9P5","B0H8D4YZTS","B0H8D96XRQ"], None),
    ("UK", ["B0G1XQ3H4H","B0G1YMLFSZ","B0G1YCTVJG","B0H8CKG9P5","B0H936F1P3","B0H931MPDZ"], None),
    ("NL", ["B0G4WGC459","B0G4WJMFB3","B0G55V8N7H","B0H7S1BDZ1","B0H8N8DLYX","B0H8N55JPT","B0H8CKG9P5","B0H8D4YZTS","B0H8D96XRQ"], None),
    ("BE", ["B0G4WGC459","B0G4WJMFB3","B0G55V8N7H","B0H7S1BDZ1","B0H8N8DLYX","B0H8N55JPT","B0H8CKG9P5","B0H8D4YZTS","B0H8D96XRQ"], None),
    ("SE", ["B0G4WGC459","B0G4WJMFB3","B0G55V8N7H","B0H7S1BDZ1","B0H8N8DLYX","B0H8N55JPT","B0H8CKG9P5","B0H8D4YZTS","B0H8D96XRQ"], None),
]:
    _names = ["TN10-主链接-黑色","TN10-主链接-银色","TN10-主链接-橙色","TN10-小链接-黑色","TN10-小链接-银色","TN10-小链接-橙色","TN20-主链接-黑色","TN20-主链接-银色","TN20-主链接-红"]
    ASIN_MAPPING[_site] = dict(zip(_asins, _names))

SINGLE_VALUE_SKUS = {
    "TN10P051",
    "TN10P052",
    "TN10P053",
    "TN10P011",
    "TN10P012",
    "TN10P013",
    "X0051AFG1N",
}
ALLOWED_SUFFIX_VALUES = {2, 3, 5, 10, 50, 100, 300}
FINAL_PAYMENT_KEYWORDS = ("final payment", "balance payment", "remaining payment", "balance due")
WARRANTY_KEYWORDS = ("warranty", "extended warranty", "comucare")
PRESALE_KEYWORDS = ("presale", "pre-sale", "voucher", "privilege voucher")


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id = Column(Integer, primary_key=True)
    status = Column(String(24), nullable=False, index=True)
    started_at = Column(DateTime(timezone=True), nullable=False)
    finished_at = Column(DateTime(timezone=True))
    trigger = Column(String(32), nullable=False, default="dashboard")
    store_count = Column(Integer, nullable=False, default=0)
    order_count = Column(Integer, nullable=False, default=0)
    message = Column(Text, nullable=False, default="")


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("store_domain", "shopify_order_id", name="uq_store_order"),
        Index("ix_orders_date_store", "order_date", "store_domain"),
        Index("ix_orders_updated", "shopify_updated_at"),
    )

    id = Column(Integer, primary_key=True)
    store_name = Column(String(160), nullable=False)
    store_domain = Column(String(255), nullable=False)
    shopify_order_id = Column(String(255), nullable=False)
    order_name = Column(String(120), nullable=False)
    order_date = Column(Date, nullable=False)
    shopify_created_at = Column(DateTime(timezone=True), nullable=False)
    shopify_updated_at = Column(DateTime(timezone=True), nullable=False)
    cancelled_at = Column(DateTime(timezone=True))
    is_test = Column(Boolean, nullable=False, default=False)
    financial_status = Column(String(80), nullable=False, default="")
    fulfillment_status = Column(String(80), nullable=False, default="")
    risk_level = Column(String(32), nullable=False, default="")
    currency = Column(String(12), nullable=False, default="USD")
    sales_amount = Column(Numeric(18, 2), nullable=False, default=0)
    refund_amount = Column(Numeric(18, 2), nullable=False, default=0)
    unit_count = Column(Integer, nullable=False, default=0)
    presale_unit_count = Column(Integer, nullable=False, default=0)
    synced_at = Column(DateTime(timezone=True), nullable=False)

    items = relationship("OrderItem", cascade="all, delete-orphan", back_populates="order")


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        UniqueConstraint("order_id", "shopify_line_item_id", name="uq_order_item"),
        Index("ix_order_items_sku_color", "sku", "color"),
    )

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    shopify_line_item_id = Column(String(255), nullable=False)
    title = Column(String(500), nullable=False, default="")
    sku = Column(String(180), nullable=False, default="(无 SKU)")
    color = Column(String(80), nullable=False, default="未分类")
    raw_quantity = Column(Integer, nullable=False, default=0)
    unit_multiplier = Column(Integer, nullable=False, default=1)
    effective_units = Column(Integer, nullable=False, default=0)
    item_type = Column(String(32), nullable=False, default="product")

    order = relationship("Order", back_populates="items")


class AmazonCampaignStrategy(Base):
    __tablename__ = "amazon_campaign_strategies"
    __table_args__ = (
        UniqueConstraint("site_code", "campaign_id", name="uq_amazon_campaign_strategy"),
        Index("ix_amazon_campaign_strategy_site", "site_code"),
    )

    id = Column(Integer, primary_key=True)
    site_code = Column(String(12), nullable=False)
    campaign_id = Column(String(255), nullable=False)
    campaign_name = Column(String(500), nullable=False, default="")
    strategy = Column(String(80), nullable=False, default="/")
    updated_at = Column(DateTime(timezone=True), nullable=False)
    updated_by = Column(String(80), nullable=True)


class AmazonCampaignAssignment(Base):
    __tablename__ = "amazon_campaign_assignments"
    __table_args__ = (
        UniqueConstraint("site_code", "store_sid", "campaign_id", name="uq_amazon_campaign_assignment"),
        Index("ix_amazon_campaign_assignment_lookup", "site_code", "store_sid"),
    )

    id = Column(Integer, primary_key=True)
    site_code = Column(String(12), nullable=False)
    store_sid = Column(String(64), nullable=False)
    store_name = Column(String(255), nullable=False, default="")
    campaign_id = Column(String(255), nullable=False)
    campaign_name = Column(String(500), nullable=False, default="")
    strategy = Column(String(80), nullable=False, default="/")
    series = Column(String(160), nullable=False, default="")
    product = Column(String(160), nullable=False, default="")
    updated_at = Column(DateTime(timezone=True), nullable=False)
    updated_by = Column(String(80), nullable=True)


class AmazonAdPlan(Base):
    __tablename__ = "amazon_ad_plans"
    __table_args__ = (
        UniqueConstraint("week_start", "site_code", "series", name="uq_amazon_ad_plan_scope"),
        Index("ix_amazon_ad_plan_scope", "week_start", "site_code"),
    )

    id = Column(Integer, primary_key=True)
    week_start = Column(Date, nullable=False)
    site_code = Column(String(12), nullable=False)
    series = Column(String(160), nullable=False)
    review = Column(Text, nullable=False, default="")
    plan = Column(Text, nullable=False, default="")
    updated_at = Column(DateTime(timezone=True), nullable=False)
    updated_by = Column(String(80), nullable=True)


class AmazonOperationPlan(Base):
    __tablename__ = "amazon_operation_plans"
    __table_args__ = (
        UniqueConstraint("week_start", "site_code", "series", name="uq_amazon_operation_plan_scope"),
        Index("ix_amazon_operation_plan_scope", "week_start", "site_code"),
    )

    id = Column(Integer, primary_key=True)
    week_start = Column(Date, nullable=False)
    site_code = Column(String(12), nullable=False)
    series = Column(String(160), nullable=False)
    review = Column(Text, nullable=False, default="")
    plan = Column(Text, nullable=False, default="")
    updated_at = Column(DateTime(timezone=True), nullable=False)
    updated_by = Column(String(80), nullable=True)


class AmazonStrategyNote(Base):
    # Versioned table keeps legacy site+strategy notes from leaking into the
    # new week+site+series scope and avoids an unsafe in-place constraint change.
    __tablename__ = "amazon_strategy_notes_v2"
    __table_args__ = (
        UniqueConstraint("week_start", "site_code", "series", "strategy", name="uq_amazon_strategy_note_v2_scope"),
        Index("ix_amazon_strategy_note_v2_scope", "week_start", "site_code", "series"),
    )

    id = Column(Integer, primary_key=True)
    week_start = Column(Date, nullable=False)
    site_code = Column(String(12), nullable=False)
    series = Column(String(160), nullable=False)
    strategy = Column(String(80), nullable=False)
    note = Column(Text, nullable=False, default="")
    updated_at = Column(DateTime(timezone=True), nullable=False)
    updated_by = Column(String(80), nullable=True)


class AmazonMonthlyTarget(Base):
    __tablename__ = "amazon_monthly_targets"
    __table_args__ = (
        UniqueConstraint("year", "month", "model", "site", name="uq_amazon_monthly_target_scope_site"),
        Index("ix_amazon_monthly_target_scope", "year", "month", "model", "site"),
    )

    id = Column(Integer, primary_key=True)
    year = Column(Integer, nullable=False)
    month = Column(Integer, nullable=False)
    model = Column(String(16), nullable=False)
    site = Column(String(32), nullable=False, default=AMAZON_SALES_ALL_SITES)
    target_units = Column(Numeric(18, 4), nullable=True)
    target_aov = Column(Numeric(18, 4), nullable=True)
    target_net_sales = Column(Numeric(18, 4), nullable=True)
    target_cpc = Column(Numeric(18, 4), nullable=True)
    target_ad_sales_share = Column(Numeric(18, 8), nullable=True)
    target_acoas = Column(Numeric(18, 8), nullable=True)
    target_ad_units = Column(Numeric(18, 4), nullable=True)
    target_sessions = Column(Numeric(18, 4), nullable=True)
    target_ad_cvr = Column(Numeric(18, 8), nullable=True)
    target_ad_cost = Column(Numeric(18, 4), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    updated_by = Column(String(80), nullable=True)


class AmazonWeeklyTarget(Base):
    """Manually entered weekly targets for the sales dashboard."""

    __tablename__ = "amazon_weekly_targets"
    __table_args__ = (
        UniqueConstraint("week_start", "model", "site", name="uq_amazon_weekly_target_scope"),
        Index("ix_amazon_weekly_target_scope", "week_start", "model", "site"),
    )

    id = Column(Integer, primary_key=True)
    week_start = Column(Date, nullable=False)
    model = Column(String(16), nullable=False)
    site = Column(String(32), nullable=False, default=AMAZON_SALES_ALL_SITES)
    target_units = Column(Numeric(18, 4), nullable=True)
    target_aov = Column(Numeric(18, 4), nullable=True)
    target_cpc = Column(Numeric(18, 4), nullable=True)
    target_ad_sales_share = Column(Numeric(18, 8), nullable=True)
    target_ad_cvr = Column(Numeric(18, 8), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    updated_by = Column(String(80), nullable=True)


class KeywordDashboardTerm(Base):
    """Cloud-managed search terms for the Xiyou ABA keyword dashboard."""

    __tablename__ = "keyword_dashboard_terms"
    __table_args__ = (
        UniqueConstraint("site_code", "keyword", name="uq_keyword_dashboard_term_site_keyword"),
        Index("ix_keyword_dashboard_terms_site_category", "site_code", "category"),
    )

    id = Column(Integer, primary_key=True)
    site_code = Column(String(12), nullable=False)
    category = Column(String(80), nullable=False, default="未分类")
    keyword = Column(String(255), nullable=False)
    sort_order = Column(Integer, nullable=False, default=0)
    enabled = Column(Boolean, nullable=False, default=True)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    updated_by = Column(String(80), nullable=True)


class KeywordDashboardWeeklyRecord(Base):
    """Persistent Xiyou ABA history; null values mean a successful fetch had no data."""

    __tablename__ = "keyword_dashboard_weekly_records"
    __table_args__ = (
        UniqueConstraint(
            "site_code",
            "keyword",
            "week_start",
            name="uq_keyword_dashboard_weekly_site_keyword_week",
        ),
        Index(
            "ix_keyword_dashboard_weekly_scope",
            "site_code",
            "week_start",
        ),
    )

    id = Column(Integer, primary_key=True)
    site_code = Column(String(12), nullable=False)
    keyword = Column(String(255), nullable=False)
    week_start = Column(Date, nullable=False)
    search_rank = Column(Integer, nullable=True)
    search_volume = Column(Integer, nullable=True)
    fetched_at = Column(DateTime(timezone=True), nullable=False)


SHOPIFY_ORDERS_QUERY = """#graphql
query Orders($first: Int!, $after: String, $search: String!) {
  orders(first: $first, after: $after, query: $search, sortKey: UPDATED_AT, reverse: false) {
    pageInfo { hasNextPage endCursor }
    edges { node {
      id name createdAt updatedAt cancelledAt test
      displayFinancialStatus displayFulfillmentStatus riskLevel currencyCode
      currentTotalPriceSet { shopMoney { amount currencyCode } }
      totalPriceSet { shopMoney { amount currencyCode } }
      totalRefundedSet { shopMoney { amount currencyCode } }
      lineItems(first: 250) { edges { node { id title sku quantity } } }
    } }
  }
}
"""


def utcnow() -> datetime:
    # MySQL DATETIME does not preserve timezone information. Keep every
    # persisted and compared timestamp as naive UTC to avoid mixing aware
    # Shopify timestamps with naive values loaded back from MySQL.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def dashboard_editor(value: str | None) -> str:
    editor = str(value or "").strip()
    # Browser fetch headers only accept ISO-8859-1 values. The frontend sends
    # percent-encoded text for non-ASCII editor IDs; restore it before storing.
    try:
        editor = unquote(editor, errors="strict")
    except UnicodeDecodeError:
        pass
    editor = editor.replace("\x00", " ").strip()
    return editor[:80] if editor else "未知编辑者"


def dashboard_request_editor(fallback: str | None = None) -> str:
    """Resolve the authenticated editor from query/header once per request."""

    return dashboard_editor(_dashboard_request_editor.get() or fallback)


def edit_metadata(item: Any) -> dict[str, str | None]:
    updated_at = getattr(item, "updated_at", None)
    if updated_at is None:
        return {"updated_at": None, "updated_by": None}
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return {
        "updated_at": updated_at.isoformat(),
        "updated_by": getattr(item, "updated_by", None),
    }


def ensure_edit_freshness(
    item: Any,
    base_updated_at: Any,
    *,
    force: bool = False,
) -> None:
    """Reject stale overwrites so shared edits do not silently replace each other."""

    if force or item is None:
        return
    current = getattr(item, "updated_at", None)
    if current is None:
        return
    current_editor = dashboard_request_editor()
    cloud_editor = dashboard_editor(getattr(item, "updated_by", None))
    # A user's own later edit may advance the cloud version (for example after a
    # refresh in another tab). That is not a cross-user conflict and must not
    # interrupt their save. Unknown cloud editors still require an explicit choice.
    if current_editor != "未知编辑者" and current_editor == cloud_editor:
        return
    if base_updated_at in (None, ""):
        # New rows have no client version. An existing row requires a version.
        raise_conflict(item)
    try:
        base = parse_datetime(str(base_updated_at))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="保存基准时间格式无效") from None
    current_utc = current.astimezone(timezone.utc) if current.tzinfo is not None else current
    if base is None or base < current_utc:
        raise_conflict(item)


def raise_conflict(item: Any) -> None:
    raise HTTPException(status_code=409, detail={
        "code": "edit_conflict",
        "message": "云端内容已被其他人更新，请选择加载云端内容或覆盖保存。",
        **edit_metadata(item),
    })


def latest_edit_metadata(items: list[Any]) -> dict[str, str | None]:
    latest = max(items, key=lambda item: getattr(item, "updated_at", None) or datetime.min, default=None)
    return edit_metadata(latest)


def decimal_value(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except Exception:
        return Decimal("0")


async def lingxing_access_token() -> str:
    """Obtain and cache a LingXing token without exposing credentials."""
    global _lingxing_token
    now = datetime.now(timezone.utc).timestamp()
    if _lingxing_token.get("value") and now < float(_lingxing_token.get("expires_at", 0)) - 60:
        return str(_lingxing_token["value"])
    async with _lingxing_token_lock:
        now = datetime.now(timezone.utc).timestamp()
        if _lingxing_token.get("value") and now < float(_lingxing_token.get("expires_at", 0)) - 60:
            return str(_lingxing_token["value"])
        app_id = os.environ.get("LINGXING_APP_ID", "").strip()
        app_secret = os.environ.get("LINGXING_APP_SECRET", "").strip()
        if not app_id or not app_secret:
            raise RuntimeError("LINGXING_APP_ID / LINGXING_APP_SECRET missing")
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{LINGXING_API_BASE}/api/auth-server/oauth/access-token",
                files={"appId": (None, app_id), "appSecret": (None, app_secret)},
            )
            response.raise_for_status()
            payload = response.json()
        data = payload.get("data") or {}
        token = data.get("access_token")
        if not token:
            raise RuntimeError(f"LingXing token request failed: {payload.get('msg', 'unknown error')}")
        _lingxing_token = {"value": token, "expires_at": now + int(data.get("expires_in", 7199))}
        return token


def amazon_product(site: str, asin: Any) -> str | None:
    """Resolve a product from every ASIN candidate returned by LingXing.

    The product-performance endpoint returns ``asins`` as an array of objects,
    not a single scalar.  Taking only the first entry silently discarded a
    valid performance row whenever the mapped ASIN appeared later in that
    array.
    """
    mapping = ASIN_MAPPING.get(site, {})

    def candidates(value: Any):
        if isinstance(value, (list, tuple, set)):
            for item in value:
                yield from candidates(item)
            return
        if isinstance(value, dict):
            direct = value.get("asin") or value.get("ASIN")
            if direct:
                yield from candidates(direct)
            for key in ("asins", "items", "list"):
                if value.get(key):
                    yield from candidates(value[key])
            return
        text = str(value or "").strip().upper()
        if text:
            yield text

    for candidate in candidates(asin):
        product = mapping.get(candidate)
        if product:
            return product
    return None


def amazon_asins_for_product(site: str, product: str | None) -> list[str]:
    if not product:
        return []
    return [asin for asin, mapped_product in ASIN_MAPPING.get(site, {}).items() if mapped_product == product]


def amazon_series(product: str | None) -> str | None:
    if not product:
        return None
    if product.startswith("TN10-主链接"):
        return AMAZON_SERIES[0]
    if product.startswith("TN10-小链接"):
        return AMAZON_SERIES[1]
    if product.startswith("TN20-主链接"):
        return AMAZON_SERIES[2]
    if product.startswith("TN20-小链接"):
        return AMAZON_SERIES[3]
    return None


def amazon_sales_series_model(series: str) -> str:
    """Return the target-dashboard model that owns a campaign series."""
    if series.startswith("TN10"):
        return "TN10"
    if series.startswith("TN20"):
        return "TN20"
    return series


def amazon_dashboard_selected_sites(values: list[str]) -> list[str]:
    """Parse site query values without silently widening their scope."""
    selected: list[str] = []
    supplied = False
    for raw in values:
        supplied = True
        for value in str(raw or "").split(","):
            site_name = value.strip()
            if not site_name:
                continue
            if site_name not in AMAZON_SITE_CODES:
                raise ValueError(f"不支持的 Amazon 站点：{site_name}")
            if site_name not in selected:
                selected.append(site_name)
    if not supplied or not selected:
        raise ValueError("站点参数无效")
    return selected


def amazon_dashboard_sites_or_all(values: list[str]) -> list[str]:
    """Keep endpoint defaults backward-compatible while rejecting bad input."""
    if not values:
        return list(AMAZON_SITE_CODES)
    return amazon_dashboard_selected_sites(values)


def amazon_sales_scope(model: str) -> tuple[set[str], set[str]]:
    """Map a sales-dashboard model to every currently known product variant."""
    if model == AMAZON_SALES_ALL_MODEL:
        products = set(AMAZON_PRODUCTS)
        series = {amazon_series(product) for product in products}
        series.discard(None)
        return products, series
    prefix = f"{model}-"
    products = {product for product in AMAZON_PRODUCTS if product.startswith(prefix)}
    series = {amazon_series(product) for product in products}
    series.discard(None)
    return products, series


def amazon_sales_selected_sites(site: str, include_regions: bool = False) -> list[str]:
    normalized = str(site or AMAZON_SALES_ALL_SITES).strip() or AMAZON_SALES_ALL_SITES
    if normalized == AMAZON_SALES_ALL_SITES:
        return list(AMAZON_SITE_CODES)
    if include_regions and normalized == AMAZON_SALES_EUROPE:
        return list(AMAZON_SALES_EUROPE_SITES)
    if normalized not in AMAZON_SITE_CODES:
        raise ValueError("销售看板站点无效")
    return [normalized]


def amazon_sales_actuals(rows: list[dict[str, Any]]) -> dict[str, float | None]:
    """Aggregate product rows from totals so derived ratios stay correct."""
    currencies = {str(row.get("currency") or "") for row in rows if row.get("currency")}
    if len(currencies) > 1:
        raise RuntimeError(f"销售看板实际值存在多币种，拒绝相加：{', '.join(sorted(currencies))}")
    additive = ("units", "net_sales", "clicks", "ad_cost", "ad_units", "ad_orders")
    totals: dict[str, float] = {key: 0.0 for key in additive}
    present = {key: False for key in additive}
    for row in rows:
        for key in additive:
            value = row.get(key)
            if value is not None:
                totals[key] += float(value)
                present[key] = True
    return {
        # Zero is a measured value, while None means the upstream field was
        # unavailable. Keeping the two states separate prevents valid zero
        # spend, sales, and orders from being displayed or compared as missing.
        "units": totals["units"] if present["units"] else None,
        "aov": totals["net_sales"] / totals["units"] if present["net_sales"] and present["units"] and totals["units"] != 0 else None,
        "net_sales": totals["net_sales"] if present["net_sales"] else None,
        "cpc": totals["ad_cost"] / totals["clicks"] if present["ad_cost"] and present["clicks"] and totals["clicks"] != 0 else None,
        "ad_sales_share": totals["ad_units"] / totals["units"] if present["ad_units"] and present["units"] and totals["units"] != 0 else None,
        "acoas": totals["ad_cost"] / totals["net_sales"] if present["ad_cost"] and present["net_sales"] and totals["net_sales"] != 0 else None,
        "ad_units": totals["ad_units"] if present["ad_units"] else None,
        "ad_cvr": totals["ad_orders"] / totals["clicks"] if present["ad_orders"] and present["clicks"] and totals["clicks"] != 0 else None,
        "ad_cost": totals["ad_cost"] if present["ad_cost"] else None,
        "clicks": totals["clicks"] if present["clicks"] else None,
    }


async def amazon_usd_exchange_rates(
    rate_date: date,
    currencies: set[str],
    client: httpx.AsyncClient | None = None,
) -> dict[str, float]:
    """Fetch once-per-day USD rates for an all-site advertising rollup."""
    currencies = {code for code in currencies if code and code != "USD"}
    if not currencies:
        return {"USD": 1.0}
    cache_key = ("amazon-usd-fx", rate_date.isoformat(), tuple(sorted(currencies)))
    cached = _amazon_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < 12 * 60 * 60:
        return cached[1]
    url = f"https://api.frankfurter.dev/v1/{rate_date.isoformat()}"

    async def fetch(client_value: httpx.AsyncClient) -> dict[str, float]:
        # httpx replaces an existing query string when ``params`` is supplied.
        # Keep ``base`` in the same params mapping so USD cannot be dropped.
        response = await client_value.get(
            url,
            params={"base": "USD", "symbols": ",".join(sorted(currencies))},
        )
        response.raise_for_status()
        payload = response.json()
        rates = payload.get("rates", {})
        result = {"USD": 1.0}
        for code in currencies:
            value = rates.get(code)
            if value is None or not float(value):
                raise RuntimeError(f"汇率服务缺少 {rate_date} 的 {code} 汇率")
            result[code] = 1.0 / float(value)
        return result

    if client is not None:
        result = await fetch(client)
    else:
        async with httpx.AsyncClient(timeout=15) as owned_client:
            result = await fetch(owned_client)
    _amazon_cache[cache_key] = (time.monotonic(), result)
    return result


async def amazon_sales_campaign_cost_by_series(
    start_date: date,
    end_date: date,
    selected_sites: list[str],
    selected_series: set[str],
    sid_map: dict[str, Any],
    store_rows: list[dict[str, Any]] | None,
    display_currency: str,
    refresh: bool,
) -> tuple[dict[tuple[str, str], float], dict[str, Any]]:
    """Read advertising money from the complete campaign inventory.

    LingXing's product-performance endpoint currently returns its generic
    advertising money in the marketplace settlement currency even when USD is
    requested.  The campaign report is the authoritative money source; product
    performance remains authoritative for non-money actuals.
    """
    payload = await amazon_strategy_board_payload(
        start_date,
        end_date,
        selected_sites,
        sid_map,
        store_rows,
        selected_series=selected_series,
        refresh=refresh,
    )
    quality = dict(payload.get("data_quality") or {})
    if not quality.get("complete"):
        errors = "; ".join(quality.get("errors") or [])
        raise RuntimeError(f"广告活动金额来源不完整：{errors or 'campaign inventory incomplete'}")
    unassigned_spend = quality.get("unassigned_campaign_spend") or {}
    unassigned_sites = {
        str(key).split("|", 1)[0]
        for key, value in unassigned_spend.items()
        if float(value or 0) > 0.01
    }
    authoritative_sites = [site for site in selected_sites if site not in unassigned_sites]
    fallback_sites = [site for site in selected_sites if site in unassigned_sites]
    quality["campaign_money_authoritative_sites"] = authoritative_sites
    quality["campaign_money_fallback_sites"] = fallback_sites
    quality["campaign_money_mode"] = (
        "campaign_report"
        if not fallback_sites
        else "product_performance_where_campaign_series_is_incomplete"
    )

    native_totals: dict[tuple[str, str, str], float] = {}
    for group in payload.get("strategies", []):
        site = str(group.get("site") or "")
        series = str(group.get("series") or "")
        currency = str(group.get("currency") or "USD")
        value = group.get("metrics", {}).get("ad_cost")
        if site in authoritative_sites and series and series in selected_series and value is not None:
            native_totals[(site, series, currency)] = native_totals.get((site, series, currency), 0.0) + float(value)
    # Keep an explicit zero for every authoritative site/series pair. Without
    # it, reconciliation could silently skip a series whose campaign inventory
    # was empty while product performance reported spend.
    for site in authoritative_sites:
        for series in selected_series:
            if not any(key[0] == site and key[1] == series for key in native_totals):
                native_totals[(site, series, AMAZON_CURRENCY_CODES.get(site, "USD"))] = 0.0

    requested_currency = str(display_currency or "original").upper()
    if requested_currency == "ORIGINAL":
        requested_currency = "USD" if len(selected_sites) > 1 else "original"
    rates: dict[str, float] | None = None
    if requested_currency != "original":
        needed = {currency for _, _, currency in native_totals}
        rates = await amazon_usd_exchange_rates(end_date, needed)

    totals: dict[tuple[str, str], float] = {}
    for (site, series, currency), value in native_totals.items():
        converted = value if requested_currency == "original" or currency == requested_currency else value * rates[currency]
        totals[(site, series)] = totals.get((site, series), 0.0) + converted
    return totals, quality


async def amazon_convert_sales_money_rows(
    rows: list[dict[str, Any]],
    target_currency: str,
    rate_date: date,
    rates: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Normalize product-performance settlement money to a display currency.

    LingXing may return marketplace money in CNY even when another currency is
    requested. Normalize at this boundary and recalculate money-derived fields
    so no downstream dashboard accidentally adds mixed currencies.
    """
    currencies = {
        str(row.get("currency") or "").strip().upper()
        for row in rows
        if str(row.get("currency") or "").strip()
    }
    needed_currencies = currencies | {target_currency}
    if not currencies or needed_currencies == {target_currency}:
        return rows
    rates = rates or await amazon_usd_exchange_rates(rate_date, needed_currencies)
    for row in rows:
        currency = str(row.get("currency") or target_currency).strip().upper()
        if currency == target_currency:
            continue
        rate = rates.get(currency)
        if rate is None:
            raise RuntimeError(f"销售看板金额缺少 {currency} 到 {target_currency} 的汇率")
        for key in ("net_sales", "ad_sales", "ad_cost"):
            value = row.get(key)
            if value is not None:
                row[key] = float(value) * rate
        if row.get("source_cpc") is not None:
            row["source_cpc"] = float(row["source_cpc"]) * rate
        row["currency"] = target_currency
        clicks = row.get("clicks")
        net_sales = row.get("net_sales")
        ad_sales = row.get("ad_sales")
        ad_orders = row.get("ad_orders")
        row["cpc"] = row["ad_cost"] / clicks if row.get("ad_cost") is not None and clicks is not None and clicks != 0 else None
        row["acoas"] = row["ad_cost"] / net_sales if row.get("ad_cost") is not None and net_sales is not None and net_sales != 0 else None
        row["acos"] = row["ad_cost"] / ad_sales if row.get("ad_cost") is not None and ad_sales is not None and ad_sales != 0 else None
        row["cpo"] = row["ad_cost"] / ad_orders if row.get("ad_cost") is not None and ad_orders is not None and ad_orders != 0 else None
    return rows


def amazon_sales_validate_money_reconciliation(
    rows: list[dict[str, Any]],
    campaign_costs: dict[tuple[str, str], float],
) -> dict[str, Any]:
    """Fail closed for authoritative campaign/site pairs when sources disagree."""
    currencies = {str(row.get("currency") or "") for row in rows if row.get("currency")}
    if len(currencies) > 1:
        raise RuntimeError(f"销售看板实际值存在多币种，拒绝相加：{', '.join(sorted(currencies))}")

    # Target dashboards merge primary and small variants into TN10/TN20.
    # Campaign assignments identify a variant series while product performance
    # attributes marketplace spend by ASIN; those variant allocations can
    # legitimately differ. Reconcile the merged model total that the dashboard
    # actually consumes, rather than failing on a variant-level attribution
    # shift between the two upstream sources.
    product_costs: dict[tuple[str, str], float] = {}
    for row in rows:
        key = (
            str(row.get("site") or ""),
            amazon_sales_series_model(str(row.get("series") or "")),
        )
        value = row.get("ad_cost")
        if value is not None:
            product_costs[key] = product_costs.get(key, 0.0) + float(value)

    checks: list[dict[str, Any]] = []
    # Only campaign-authoritative site/series pairs are checked. Sites with
    # unclassified campaign spend remain on product-performance money because
    # forcing those campaigns into a series would create a worse attribution
    # error; that fallback is exposed through data_quality instead.
    campaign_model_costs: dict[tuple[str, str], float] = {}
    for (site_name, series), value in campaign_costs.items():
        model_key = (site_name, amazon_sales_series_model(series))
        campaign_model_costs[model_key] = (
            campaign_model_costs.get(model_key, 0.0) + float(value or 0.0)
        )

    for key in sorted(campaign_model_costs):
        campaign_value = float(campaign_model_costs.get(key, 0.0))
        model = key[1]
        product_key = key
        product_value = float(product_costs.get(product_key, 0.0))
        difference = abs(campaign_value - product_value)
        # Campaign-series attribution and ASIN performance attribution can
        # legitimately drift by more than ten percent in a short week.  Keep
        # that visible as a warning, but only block order-of-magnitude gaps
        # that indicate conversion or source-grain corruption.  Campaign money
        # remains authoritative whenever this check passes.
        basis = max(abs(campaign_value), abs(product_value))
        warning_tolerance = max(50.0, 0.1 * basis)
        block_tolerance = max(100.0, 0.5 * basis)
        status = (
            "failure" if difference > block_tolerance
            else "warning" if difference > warning_tolerance
            else "pass"
        )
        passed = status != "failure"
        checks.append({
            "site": key[0],
            "model": model,
            "campaign_report": campaign_value,
            "product_performance": product_value,
            "difference": difference,
            "warning_tolerance": warning_tolerance,
            "block_tolerance": block_tolerance,
            "status": status,
            "passed": passed,
        })
        if not passed:
            raise RuntimeError(
                f"广告金额交叉校验失败：{key[0]} {model} "
                f"广告活动 {campaign_value:.2f} / 产品表现 {product_value:.2f}"
            )
    return {
        "source": "campaign_report_vs_product_performance",
        "complete": True,
        "warnings": [check for check in checks if check["status"] == "warning"],
        "checks": checks,
    }


def amazon_sales_apply_campaign_ad_cost(
    rows: list[dict[str, Any]],
    campaign_costs: dict[tuple[str, str], float],
) -> list[dict[str, Any]]:
    """Replace product-performance spend with campaign-report spend.

    Campaign assignments identify a series, not an individual ASIN.  Allocate
    each series total across its product rows only so the model-level metrics
    used by the target dashboard remain exact; the allocation weights do not
    affect the final model total.
    """
    groups: dict[tuple[str, str], list[int]] = {}
    for index, row in enumerate(rows):
        groups.setdefault((str(row.get("site") or ""), str(row.get("series") or "")), []).append(index)

    for key, indexes in groups.items():
        if key not in campaign_costs:
            continue
        authoritative_cost = float(campaign_costs.get(key, 0.0))
        current_values = [float(rows[index].get("ad_cost") or 0.0) for index in indexes]
        weight_sum = sum(current_values)
        if weight_sum <= 0:
            weights = [float(rows[index].get("clicks") or 0.0) for index in indexes]
            weight_sum = sum(weights)
            if weight_sum <= 0:
                weights = [1.0] * len(indexes)
                weight_sum = float(len(indexes))
        else:
            weights = current_values
        for index, weight in zip(indexes, weights):
            rows[index]["ad_cost"] = authoritative_cost * weight / weight_sum
            rows[index]["ad_cost_source"] = "campaign_report"
            clicks = rows[index].get("clicks")
            rows[index]["cpc"] = rows[index]["ad_cost"] / clicks if clicks is not None and clicks != 0 else None
    return rows


def amazon_sales_target_values(item: Any | None) -> dict[str, float | None]:
    """Read the five manually entered targets.

    Older rows did not store AOV directly. When both a legacy sales target and
    a unit target exist, recover AOV from them so existing data remains useful.
    Derived targets are intentionally not persisted; they are calculated from
    the manual values every time the dashboard is rendered.
    """
    if item is None:
        return {key: None for key in AMAZON_SALES_TARGET_FIELDS}
    values = {
        key: float(value) if (value := getattr(item, f"target_{key}")) is not None else None
        for key in AMAZON_SALES_TARGET_FIELDS
    }
    legacy_sales = getattr(item, "target_net_sales", None)
    if values.get("aov") is None and legacy_sales is not None and values.get("units"):
        values["aov"] = float(legacy_sales) / float(values["units"])
    return values


def amazon_sales_country_target_payload(items: list[Any]) -> list[dict[str, Any]]:
    """Return one target slot per country, including countries with no target."""
    by_site = {str(item.site): item for item in items}
    return [
        {
            "site": site,
            "targets": {
                key: float(value) if (value := getattr(by_site.get(site), f"target_{key}", None)) is not None else None
                for key in AMAZON_SALES_TARGET_FIELDS
            },
            **edit_metadata(by_site.get(site)),
        }
        for site in AMAZON_SITE_CODES
    ]


def amazon_sales_europe_target_values(items: list[Any]) -> dict[str, float | None]:
    """Aggregate only unit targets for the virtual Europe scope.

    Ratios and money-per-click targets are not additive across marketplaces.
    They intentionally stay unset at the Europe scope; operators continue to
    maintain them country by country.
    """
    by_site = {str(item.site): item for item in items}
    values = {key: None for key in AMAZON_SALES_TARGET_FIELDS}
    # A missing country target is deliberately zero for the Europe rollup.  The
    # explicit zero keeps the API, progress bar, and target table on one rule
    # even when only part of the region has entered a target.
    values["units"] = sum(
        float(item.target_units)
        if (item := by_site.get(site)) is not None and item.target_units is not None
        else 0.0
        for site in AMAZON_SALES_EUROPE_SITES
    )
    return values


def amazon_sales_bulk_unit_targets(raw_items: Any) -> dict[str, float | None]:
    """Validate a quick-entry country/unit-target payload."""
    if not isinstance(raw_items, list):
        raise ValueError("销量目标批量保存格式无效")
    output: dict[str, float | None] = {}
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise ValueError("销量目标批量保存行格式无效")
        site = str(raw.get("site") or "").strip()
        if site not in AMAZON_SITE_CODES:
            raise ValueError(f"销量目标批量保存站点无效：{site or '空'}")
        if site in output:
            raise ValueError(f"销量目标批量保存站点重复：{site}")
        output[site] = amazon_sales_target_number(raw.get("target_units"))
    missing = [site for site in AMAZON_SITE_CODES if site not in output]
    if missing:
        raise ValueError(f"销量目标批量保存缺少站点：{'、'.join(missing)}")
    return output


def amazon_sales_week_time_progress(week_start: date, week_end: date, site_today: date) -> float:
    """Return elapsed-day progress for a Monday-based week."""
    if site_today < week_start:
        return 0.0
    if site_today > week_end:
        return 1.0
    return ((site_today - week_start).days + 1) / 7


async def amazon_sales_actual_rows(
    period_start: date,
    actual_end: date | None,
    comparison: str,
    selected_sites: list[str],
    selected_series: set[str],
    products: set[str],
    refresh: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch sales-dashboard product rows through the existing LingXing path."""
    no_data = {"source": "not_requested", "complete": True, "errors": []}
    if actual_end is None or actual_end < period_start:
        return [], no_data
    if not os.environ.get("LINGXING_APP_ID") or not os.environ.get("LINGXING_APP_SECRET"):
        raise HTTPException(status_code=503, detail="领星 API 尚未配置")
    try:
        sid_map = json.loads(os.environ.get("LINGXING_SIDS_JSON", "{}"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=503, detail="LINGXING_SIDS_JSON 配置格式错误") from exc
    if not sid_map:
        try:
            for store_item in await lingxing_store_rows():
                country = str(store_item.get("country") or "")
                sid = store_item.get("sid")
                if country and sid and int(store_item.get("status") or 0) == 1:
                    sid_map.setdefault(country, {"sid": sid})
        except Exception as exc:
            raise HTTPException(status_code=502, detail="领星店铺列表获取失败") from exc
    try:
        # Japan has two seller accounts, so the authoritative store list is
        # used to supplement configured sids. A configured sid map still
        # allows the dashboard to render if this secondary call fails.
        store_rows = await lingxing_store_rows()
    except Exception as exc:
        if not sid_map:
            raise HTTPException(status_code=502, detail="领星店铺列表获取失败") from exc
        store_rows = []
    if refresh:
        _amazon_cache.clear_current_namespace()
    # A single site reports in that site's native marketplace currency.  For
    # all sites, fetch product performance in native currency and convert it
    # with the same FX source/date used by the campaign report.
    requested_currency = amazon_sales_currency(selected_sites)
    # "native" is an internal multi-site mode: keep each site in its own
    # marketplace currency first, then convert all rows once below. The public
    # original-currency mode intentionally rejects a multi-site rollup.
    performance_currency = "native" if len(selected_sites) > 1 else requested_currency
    periodic = await amazon_dashboard_periodic(
        comparison,
        period_start,
        actual_end,
        selected_sites,
        selected_series,
        products,
        sid_map,
        store_rows,
        performance_currency,
    )
    periodic_rows = await amazon_convert_sales_money_rows(
        periodic["rows"], requested_currency, actual_end,
    )
    campaign_costs, campaign_quality = await amazon_sales_campaign_cost_by_series(
        period_start,
        actual_end,
        selected_sites,
        selected_series,
        sid_map,
        store_rows,
        requested_currency,
        refresh,
    )
    reconciliation = amazon_sales_validate_money_reconciliation(periodic_rows, campaign_costs)
    rows = amazon_sales_apply_campaign_ad_cost(periodic_rows, campaign_costs)
    data_quality = dict(periodic.get("data_quality") or {})
    data_quality["ad_money_source"] = str(campaign_quality.get("campaign_money_mode") or "campaign_report")
    data_quality["campaign_money_quality"] = campaign_quality
    data_quality["money_reconciliation"] = reconciliation
    return rows, data_quality


def amazon_sales_currency(selected_sites: list[str]) -> str:
    """Use the native currency for one site and USD for all-site rollups."""
    if len(selected_sites) == 1:
        return AMAZON_CURRENCY_CODES.get(selected_sites[0], "USD")
    return "USD"


def amazon_sales_derived_targets(targets: dict[str, float | None]) -> dict[str, float | None]:
    """Calculate non-manual monthly targets from the five manual inputs."""
    units = targets.get("units")
    aov = targets.get("aov")
    cpc = targets.get("cpc")
    ad_sales_share = targets.get("ad_sales_share")
    ad_cvr = targets.get("ad_cvr")

    net_sales = units * aov if units is not None and aov is not None else None
    clicks = (
        units * ad_sales_share / ad_cvr
        if units is not None and ad_sales_share is not None and ad_cvr
        else None
    )
    ad_units = clicks * ad_cvr if clicks is not None and ad_cvr is not None else None
    ad_cost = cpc * clicks if cpc is not None and clicks is not None else None
    acoas = ad_cost / net_sales if ad_cost is not None and net_sales is not None and net_sales != 0 else None
    return {
        "net_sales": net_sales,
        "clicks": clicks,
        "ad_units": ad_units,
        "ad_cost": ad_cost,
        "acoas": acoas,
    }


def amazon_sales_completion(
    metric_key: str,
    target: float | None,
    actual: float | None,
) -> dict[str, Any]:
    """Apply the dashboard's explicit threshold and color rules.

    The signs here intentionally follow the requested business rules rather
    than the usual higher-is-better convention.
    """
    definition = next(item for item in AMAZON_SALES_METRICS if item["key"] == metric_key)
    if target is None or actual is None:
        return {"value": None, "status": ""}
    epsilon = 1e-9
    if definition["completion"] == "ratio":
        if abs(float(target)) <= epsilon:
            return {"value": None, "status": ""}
        value = float(actual) / float(target)
        status = "gray" if abs(value - 1) <= epsilon else ("green" if value < 1 else "red")
        return {"value": value, "status": status}
    value = float(actual) - float(target)
    if abs(value) <= epsilon:
        status = "gray"
    elif definition["difference_rule"] == "lower_is_red":
        status = "red" if value < 0 else "green"
    else:
        status = "red" if value > 0 else "green"
    return {"value": value, "status": status}


def amazon_sales_period_change(
    metric_key: str,
    current: float | None,
    previous: float | None,
) -> dict[str, Any]:
    """Calculate an absolute period-over-period difference.

    Ratio metrics keep their native unit and are formatted as percentage
    points in the frontend. Red/green follows each metric's business direction:
    for example, CPC falling is red, AOV and ad CVR rising are red, while ad
    spend rising is green.
    """
    definition = next(item for item in AMAZON_SALES_METRICS if item["key"] == metric_key)
    if current is None or previous is None:
        return {"value": None, "status": ""}
    value = float(current) - float(previous)
    epsilon = 1e-9
    if abs(value) <= epsilon:
        status = "gray"
    elif metric_key in AMAZON_SALES_COMPARISON_LOWER_IS_BETTER:
        status = "red" if value < 0 else "green"
    else:
        status = "red" if value > 0 else "green"
    return {"value": value, "status": status}


def amazon_sales_metric_rows(
    targets: dict[str, float | None],
    actuals: dict[str, float | None],
    previous_actuals: dict[str, float | None] | None = None,
) -> list[dict[str, Any]]:
    output = []
    derived_targets = amazon_sales_derived_targets(targets)
    previous_actuals = previous_actuals or {}
    for definition in AMAZON_SALES_METRICS:
        key = definition["key"]
        target = targets.get(key) if definition.get("target_input") else derived_targets.get(key)
        actual = actuals.get(key)
        previous_actual = previous_actuals.get(key)
        output.append({
            **definition,
            "target": float(target) if target is not None else None,
            "actual": float(actual) if actual is not None else None,
            "period_comparison": amazon_sales_period_change(key, actual, previous_actual),
            "completion": amazon_sales_completion(key, target, actual),
        })
    return output


def amazon_previous_month_comparison_period(
    month_start: date,
    month_end: date,
    actual_end: date | None,
) -> tuple[date, date, date | None]:
    """Return the prior-month period used by month-over-month comparison.

    A complete selected month compares with the complete previous month. An
    in-progress month compares with the same month-to-date segment. Short prior
    months are capped at their final day so the comparison never leaks into
    another month.
    """
    previous_year = month_start.year - (1 if month_start.month == 1 else 0)
    previous_month = 12 if month_start.month == 1 else month_start.month - 1
    previous_start = date(previous_year, previous_month, 1)
    previous_end = date(
        previous_year,
        previous_month,
        calendar.monthrange(previous_year, previous_month)[1],
    )
    if actual_end is None:
        return previous_start, previous_end, None
    if actual_end >= month_end:
        return previous_start, previous_end, previous_end
    elapsed_days = (actual_end - month_start).days + 1
    previous_actual_end = min(
        previous_end,
        previous_start + timedelta(days=elapsed_days - 1),
    )
    return previous_start, previous_end, previous_actual_end


def amazon_sales_target_number(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        text = raw.strip()
        is_percent = text.endswith("%")
        if is_percent:
            text = text[:-1].strip()
    else:
        text = raw
        is_percent = False
    try:
        value = float(text)
    except (TypeError, ValueError) as exc:
        raise ValueError("目标必须是数字") from exc
    if is_percent:
        value /= 100
    if not math.isfinite(value) or value < 0:
        raise ValueError("目标必须是不小于 0 的数字")
    return value


async def lingxing_store_rows() -> list[dict[str, Any]]:
    cache_key = ("lingxing-stores",)
    cached = _amazon_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < LINGXING_STORE_CACHE_TTL_SECONDS:
        return list(cached[1])
    async with _lingxing_store_lock:
        cached = _amazon_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < LINGXING_STORE_CACHE_TTL_SECONDS:
            return list(cached[1])
        last_error: RuntimeError | None = None
        for attempt in range(5):
            try:
                body = await lingxing_get("/erp/sc/data/seller/lists")
                rows = lingxing_rows(body)
                _amazon_cache[cache_key] = (time.monotonic(), rows)
                return rows
            except RuntimeError as exc:
                last_error = exc
                message = str(exc).lower()
                if "频繁" not in str(exc) and "too frequent" not in message:
                    raise
                if attempt < 4:
                    await asyncio.sleep(2 ** attempt)
        if last_error:
            raise last_error
        return []


def amazon_sid_accounts(
    site_name: str,
    sid_map: dict[str, Any],
    store_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return every LingXing shop that contributes to an Amazon site.

    Japan is intentionally a multi-shop site: Comu-JP owns TN20 while
    Comulytic-JP owns TN10.  The dashboard must query both sids and let the
    normal period/ASIN aggregation merge them into one JP result.
    """
    site_code = AMAZON_SITE_CODES.get(site_name, site_name)
    raw = sid_map.get(site_name) or sid_map.get(site_code)
    candidates: list[dict[str, Any]] = []

    def add(value: Any, name: str = "") -> None:
        if isinstance(value, (list, tuple)):
            for item in value:
                add(item, name)
            return
        if isinstance(value, dict):
            sid = value.get("sid")
            if sid is not None:
                candidates.append({"sid": sid, "name": str(value.get("name") or value.get("account_name") or name)})
            return
        if value not in (None, ""):
            candidates.append({"sid": value, "name": name})

    add(raw)
    # Supplement configured values with the authoritative store list. This
    # also makes a missing LINGXING_SIDS_JSON entry recoverable for every site,
    # while retaining the two-shop Japan behavior.
    for row in store_rows or []:
        country = str(row.get("country") or "")
        name = str(row.get("name") or row.get("account_name") or "")
        if country in {site_name, site_code}:
            add(row.get("sid"), name)

    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in candidates:
        key = str(item.get("sid") or "")
        if key and key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _normalized_field_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _nested_field_value(value: Any, wanted: set[str]) -> Any:
    """Find a scalar field in LingXing's occasionally nested response objects."""
    if isinstance(value, dict):
        for key, nested in value.items():
            if _normalized_field_name(key) in wanted and nested not in (None, "", [], {}):
                if not isinstance(nested, (dict, list, tuple, set)):
                    return nested
                found = _nested_field_value(nested, wanted)
                if found not in (None, ""):
                    return found
        for nested in value.values():
            found = _nested_field_value(nested, wanted)
            if found not in (None, ""):
                return found
    elif isinstance(value, (list, tuple, set)):
        for nested in value:
            found = _nested_field_value(nested, wanted)
            if found not in (None, ""):
                return found
    return None


def optional_value(row: dict[str, Any], *names: str) -> float | None:
    """Read a metric from top-level or nested LingXing response fields."""
    for name in names:
        value = _nested_field_value(row, {_normalized_field_name(name)})
        if value not in (None, ""):
            try:
                return float(value or 0)
            except (TypeError, ValueError):
                return None
    return None


def _first_nested_field_value(value: Any, names: tuple[str, ...]) -> Any:
    for name in names:
        found = _nested_field_value(value, {_normalized_field_name(name)})
        if found not in (None, ""):
            return found
    return None


def _campaign_name_from_nested(value: Any) -> Any:
    """Find a human campaign name in versioned/nested LingXing ad rows."""
    exact_names = {_normalized_field_name(name) for name in (
        "campaign_name", "campaignName", "campaign_name_cn", "campaignNameCn",
        "campaign_name_en", "campaignNameEn", "campaign_title", "campaignTitle",
        "ads_name", "adsName", "ads_campaign_name", "adsCampaignName",
        "ad_name", "adName", "display_name", "displayName", "title", "label",
    )}
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = _normalized_field_name(key)
            if any(token in normalized for token in ("campaign", "advert", "ads")):
                found = _campaign_name_from_nested(nested)
                if found not in (None, "") and not isinstance(found, (dict, list, tuple, set)):
                    return found
        for key, nested in value.items():
            if _normalized_field_name(key) in exact_names and nested not in (None, "") and not isinstance(nested, (dict, list, tuple, set)):
                return nested
        for nested in value.values():
            found = _campaign_name_from_nested(nested)
            if found not in (None, ""):
                return found
    elif isinstance(value, (list, tuple, set)):
        for nested in value:
            found = _campaign_name_from_nested(nested)
            if found not in (None, ""):
                return found
    return None


def lingxing_rows(body: dict[str, Any]) -> list[dict[str, Any]]:
    data = body.get("data") or []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("list", "rows", "data", "records", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


async def fetch_ad_report(
    sid: int,
    report_date: date,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    offset: int = 0,
    *,
    show_detail: int = 1,
) -> list[dict[str, Any]]:
    """Read LingXing's dated advertising report for one store."""
    cache_key = ("ads-v3", sid, report_date.isoformat(), offset, show_detail)
    cached = _amazon_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < AMAZON_CACHE_TTL_SECONDS:
        # Sales-dashboard enrichment reallocates rows in-place after this call.
        # Return a private object so those edits cannot pollute the cached raw
        # periodic response for another viewer or filter invocation.
        return copy.deepcopy(cached[1])
    global _lingxing_ad_report_last_call
    async with semaphore:
        # LingXing advertising endpoints use a one-token bucket. Keep a
        # minimum spacing between requests even when several dashboard
        # periods are being loaded back-to-back.
        wait_for = 1.2 - (time.monotonic() - _lingxing_ad_report_last_call)
        if wait_for > 0:
            await asyncio.sleep(wait_for)
        body = await lingxing_post(
            "/pb/openapi/newad/spProductAdReports",
            {
                "sid": sid,
                "report_date": report_date.isoformat(),
                "show_detail": show_detail,
                "offset": offset,
                "length": 500,
            },
            client=client,
        )
        _lingxing_ad_report_last_call = time.monotonic()
    data = body.get("data") or []
    rows = data if isinstance(data, list) else (
        data.get("list") or data.get("rows") or data.get("data") or []
        if isinstance(data, dict) else []
    )
    rows = rows if isinstance(rows, list) else []
    _amazon_cache[cache_key] = (time.monotonic(), rows)
    return rows


async def fetch_ad_reports_range(
    sid: int,
    start_date: date,
    end_date: date,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    *,
    show_detail: int = 1,
) -> list[dict[str, Any]]:
    """Read dated advertising reports, serializing requests for LingXing's rate limit."""
    periodic: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    cursor = start_date
    while cursor <= end_date:
        async with _lingxing_ad_report_lock:
            try:
                daily = await fetch_ad_report(sid, cursor, client, semaphore, 0, show_detail=show_detail)
            except RuntimeError as exc:
                if "频繁" not in str(exc) and "too frequent" not in str(exc).lower():
                    raise
                daily = None
                for attempt in range(5):
                    await asyncio.sleep(2 ** attempt)
                    try:
                        daily = await fetch_ad_report(sid, cursor, client, semaphore, 0, show_detail=show_detail)
                        break
                    except RuntimeError as retry_exc:
                        if "频繁" not in str(retry_exc) and "too frequent" not in str(retry_exc).lower():
                            raise
                if daily is None:
                    raise
        offset = 0
        page_signatures: set[str] = set()
        while daily:
            signature = json.dumps(daily[:3], ensure_ascii=False, sort_keys=True, default=str)
            if signature in page_signatures:
                break
            page_signatures.add(signature)
            for row in daily:
                if isinstance(row, dict):
                    tagged = dict(row)
                    tagged["_source"] = "ad_report"
                    tagged["_dashboard_date"] = cursor.isoformat()
                    tagged["_store_sid"] = str(sid)
                    rows.append(tagged)
            if len(daily) < 500:
                break
            offset += len(daily)
            async with _lingxing_ad_report_lock:
                daily = await fetch_ad_report(sid, cursor, client, semaphore, offset, show_detail=show_detail)
        cursor += timedelta(days=1)
    return rows


def ad_report_campaign_id(row: dict[str, Any]) -> str:
    for key in ("campaign_id", "campaignId", "campaignID", "ads_id", "adsId", "id"):
        value = row.get(key)
        if value not in (None, "") and not isinstance(value, (dict, list)):
            return str(value).strip()
    return ""


def ad_report_store_sid(row: dict[str, Any]) -> str:
    for key in ("_store_sid", "store_sid", "storeSid", "sid", "store_id", "storeId", "seller_id", "sellerId"):
        value = row.get(key)
        if value not in (None, "") and not isinstance(value, (dict, list)):
            return str(value).strip()
    return str(row.get("_store_sid") or "").strip()


def ad_report_store_name(row: dict[str, Any]) -> str:
    for key in ("store_name", "storeName", "shop_name", "shopName", "seller_name", "sellerName", "account_name", "accountName"):
        value = row.get(key)
        if value not in (None, "") and not isinstance(value, (dict, list)):
            return str(value).strip()
    for key in ("store", "shop", "account", "profile"):
        value = row.get(key)
        if isinstance(value, dict):
            for nested_key in ("name", "store_name", "storeName", "shop_name", "shopName", "account_name", "accountName"):
                nested = value.get(nested_key)
                if nested not in (None, "") and not isinstance(nested, (dict, list)):
                    return str(nested).strip()
    return ""


def ad_report_type(row: dict[str, Any]) -> str:
    def classify(value: Any) -> str:
        text = str(value or "").strip().upper().replace("-", "_")
        if not text: return ""
        if text in {"SBV", "VIDEO", "SPONSORED_BRANDS_VIDEO"} or text.startswith("SBV") or "VIDEO" in text: return "SBV"
        if text in {"SP", "SPONSORED_PRODUCTS"} or text.startswith("SP"): return "SP"
        if text in {"SB", "SB2", "HSA", "SPONSORED_BRANDS", "PRODUCT_COLLECTION"} or text.startswith("SB"): return "SB"
        if text in {"SD", "SPONSORED_DISPLAY"} or text.startswith("SD"): return "SD"
        return ""
    # SB and SBV share the sponsored type (SB2/HSA). The creative format must
    # win before the generic key scan maps SB2/HSA to plain SB.
    sponsored = str(_first_nested_field_value(row, ("sponsored_type", "sponsoredType")) or "").strip().upper()
    if sponsored in {"HSA", "SB2", "SPONSORED_BRANDS"}:
        creative = str(_first_nested_field_value(row, ("creative_type", "creativeType")) or "").upper()
        if "VIDEO" in creative:
            return "SBV"
        # Some LingXing campaign-report versions omit creative_type. Campaign
        # names in this account use an explicit SBV token, so use it only as
        # the last fallback for an already-identified Sponsored Brands row.
        campaign_name = ad_report_campaign_name(row).upper()
        return "SBV" if "SBV" in campaign_name else "SB"
    keys = ("ad_type", "adType", "ads_type", "adsType", "advertising_type", "advertisingType", "type", "product_type", "productType", "campaign_type", "campaignType", "ad_product", "adProduct", "campaign_type_name", "campaignTypeName", "ad_format", "adFormat", "ad_type_name", "adTypeName", "sponsored_type", "sponsoredType")
    for key in keys:
        value = _first_nested_field_value(row, (key,))
        classified = classify(value)
        if classified: return classified
    return ""


def ad_report_campaign_name(row: dict[str, Any]) -> str:
    value = _campaign_name_from_nested(row)
    if value in (None, ""):
        value = _first_nested_field_value(row, ("campaign_name", "campaignName", "campaign_name_cn", "campaignNameCn", "ads_name", "adsName", "ad_name", "name", "campaign"))
    return str(value).strip() if value not in (None, "") else ""


def ad_report_number(row: dict[str, Any], *names: str) -> float:
    value = optional_value(row, *names)
    return float(value or 0)


def normalize_strategy(value: Any) -> str:
    strategy = str(value or "/").strip()
    return strategy or "/"


def validate_strategy_series(strategy: str, series: str) -> None:
    """Keep campaign classification atomic: a real strategy requires a series."""
    if strategy == "/" and series:
        raise HTTPException(status_code=422, detail="策略为“/”时不能选择系列")
    if strategy != "/" and not series:
        raise HTTPException(status_code=422, detail="已选择策略时必须同时选择系列")


def strategy_metrics(row: dict[str, Any]) -> dict[str, float]:
    clicks = ad_report_number(row, "clicks", "click", "bn_total_ad_clicks", "ad_clicks", "total_clicks")
    impressions = ad_report_number(row, "impressions", "impression", "bn_total_ad_impressions", "ad_impressions", "total_impressions")
    ad_cost = ad_report_number(row, "spends", "spend", "ad_cost", "advertising_spend", "cost")
    ad_sales = ad_report_number(row, "sales", "bn_total_ad_sales", "ad_sales", "advertising_sales", "sales_amount")
    ad_units = ad_report_number(row, "ad_units", "bn_total_ad_units", "ads_sales_volume_quantity", "ad_sales_volume_quantity", "sales_volume_quantity")
    ad_orders = ad_report_number(row, "orders", "bn_total_ad_orders", "ad_orders", "ad_order_quantity", "order_quantity")
    return {
        "impressions": impressions,
        "clicks": clicks,
        "ad_cost": ad_cost,
        "ad_sales": ad_sales,
        "ad_units": ad_units,
        "ad_orders": ad_orders,
    }


def ad_report_currency(row: dict[str, Any], default_currency: str) -> str:
    return str(row.get("currency") or row.get("currency_code") or default_currency).strip().upper() or default_currency


def strategy_campaign_id(row: dict[str, Any]) -> str:
    value = ad_report_campaign_id(row)
    return value or f"name:{ad_report_store_sid(row)}:{strategy_campaign_name(row)}"


def strategy_campaign_name(row: dict[str, Any]) -> str:
    value = _campaign_name_from_nested(row)
    if value in (None, ""):
        value = _first_nested_field_value(row, ("campaign_name", "campaignName", "campaign_name_cn", "campaignNameCn", "ads_name", "adsName", "ad_name", "name", "campaign"))
    return str(value).strip() if value not in (None, "") else "未命名广告活动"


def strategy_site_code(value: str) -> str:
    return AMAZON_SITE_CODES.get(value, value)


def strategy_date_range(start_date: date | None, end_date: date | None) -> tuple[date, date]:
    if (start_date is None) != (end_date is None):
        raise HTTPException(status_code=422, detail="开始日期和结束日期需要同时提供")
    if start_date is None:
        today = datetime.now(timezone.utc).date()
        end_date = today - timedelta(days=(today.weekday() + 1) % 7)
        start_date = end_date - timedelta(days=6)
    if start_date > end_date:
        raise HTTPException(status_code=422, detail="开始日期必须早于或等于结束日期")
    if (end_date - start_date).days > AMAZON_MAX_DATE_RANGE_DAYS:
        raise HTTPException(status_code=422, detail=f"日期范围最多支持 {AMAZON_MAX_DATE_RANGE_DAYS} 天")
    return start_date, end_date


def finalize_strategy_metrics(total: dict[str, float]) -> dict[str, float | None]:
    clicks = total.get("clicks", 0)
    impressions = total.get("impressions", 0)
    ad_cost = total.get("ad_cost", 0)
    ad_sales = total.get("ad_sales", 0)
    ad_orders = total.get("ad_orders", 0)
    return {
        "clicks": int(clicks),
        "cpc": ad_cost / clicks if clicks is not None and clicks != 0 else None,
        "ad_cost": ad_cost,
        "ad_sales": ad_sales,
        "ad_orders": int(ad_orders),
        "ad_units": int(total.get("ad_units", 0)),
        "acos": ad_cost / ad_sales if ad_sales is not None and ad_sales != 0 else None,
        "roas": ad_sales / ad_cost if ad_cost is not None and ad_cost != 0 else None,
        "ad_cvr": ad_orders / clicks if clicks is not None and clicks != 0 else None,
    }


async def lingxing_post(path: str, payload: dict[str, Any], client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    token = await lingxing_access_token()
    params = lingxing_auth_params(token, payload)
    request_params = {key: params[key] for key in ("access_token", "app_key", "timestamp", "sign")}
    owns_client = client is None
    request_client = client or httpx.AsyncClient(timeout=45)
    body: dict[str, Any] = {}
    try:
        response = await request_client.post(
            f"{LINGXING_API_BASE}{path}",
            params=request_params,
            headers={"X-API-VERSION": "2"},
            json=payload,
        )
        response.raise_for_status()
        body = response.json()
    finally:
        if owns_client:
            await request_client.aclose()
    if str(body.get("code", "200")) not in {"200", "0"}:
        raise RuntimeError(f"LingXing API failed: {body.get('msg', 'unknown error')}")
    return body


async def lingxing_get(path: str) -> dict[str, Any]:
    token = await lingxing_access_token()
    params = lingxing_auth_params(token, {})
    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.get(
            f"{LINGXING_API_BASE}{path}",
            params=params,
            headers={"X-API-VERSION": "2"},
        )
        response.raise_for_status()
        body = response.json()
    if str(body.get("code", "200")) not in {"200", "0"}:
        raise RuntimeError(f"LingXing API failed: {body.get('message') or body.get('msg', 'unknown error')}")
    return body


def lingxing_auth_params(token: str, business: dict[str, Any]) -> dict[str, Any]:
    app_id = os.environ.get("LINGXING_APP_ID", "").strip()
    params = {k: v for k, v in business.items() if v is not None and v != ""}
    params.update({"access_token": token, "app_key": app_id, "timestamp": int(time.time())})
    def signing_value(value: Any) -> str:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)
    signing = "&".join(f"{key}={signing_value(params[key])}" for key in sorted(params))
    digest = hashlib.md5(signing.encode("utf-8")).hexdigest().upper().encode("utf-8")
    key = app_id.encode("utf-8")
    if len(key) not in {16, 24, 32}:
        key = hashlib.md5(key).digest()
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    padded = digest + bytes([16 - len(digest) % 16]) * (16 - len(digest) % 16)
    params["sign"] = base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode("ascii")
    return params


def migrate_amazon_monthly_targets(engine) -> None:
    """Migrate monthly-target rows created before this release."""
    table = AmazonMonthlyTarget.__tablename__
    unique_name = "uq_amazon_monthly_target_scope_site"
    with engine.begin() as connection:
        inspector = sql_inspect(connection)
        if table not in inspector.get_table_names():
            return
        columns = {column["name"] for column in inspector.get_columns(table)}
        is_mysql = engine.dialect.name == "mysql"
        if "target_aov" not in columns:
            if is_mysql:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN target_aov DECIMAL(18, 4) NULL"))
            else:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN target_aov NUMERIC(18, 4) NULL"))
        site_added = False
        if "site" not in columns:
            site_added = True
            if is_mysql:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN site VARCHAR(32) NULL"))
            else:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN site VARCHAR(32) NOT NULL DEFAULT '{AMAZON_SALES_ALL_SITES}'"))
        connection.execute(
            text(f"UPDATE {table} SET site = :site WHERE site IS NULL OR site = ''"),
            {"site": AMAZON_SALES_ALL_SITES},
        )
        if is_mysql:
            site_column = next(column for column in sql_inspect(connection).get_columns(table) if column["name"] == "site")
            if site_added or site_column.get("nullable", True):
                connection.execute(text(f"ALTER TABLE {table} MODIFY site VARCHAR(32) NOT NULL"))

        indexes = sql_inspect(connection).get_indexes(table)
        wanted = {"year", "month", "model"}
        for index in indexes:
            index_columns = set(index.get("column_names") or [])
            if index.get("unique") and index.get("name") and wanted.issubset(index_columns) and "site" not in index_columns:
                connection.execute(text(f"ALTER TABLE {table} DROP INDEX {index['name']}"))

        indexes = sql_inspect(connection).get_indexes(table)
        if not any(index.get("name") == unique_name for index in indexes):
            if is_mysql:
                connection.execute(text(f"ALTER TABLE {table} ADD CONSTRAINT {unique_name} UNIQUE (year, month, model, site)"))
            else:
                connection.execute(text(f"CREATE UNIQUE INDEX {unique_name} ON {table} (year, month, model, site)"))


def migrate_shared_edit_metadata(engine) -> None:
    """Add editor metadata without rewriting existing dashboard content."""

    tables = (
        AmazonCampaignStrategy.__tablename__,
        AmazonCampaignAssignment.__tablename__,
        AmazonAdPlan.__tablename__,
        AmazonOperationPlan.__tablename__,
        AmazonStrategyNote.__tablename__,
        AmazonMonthlyTarget.__tablename__,
        AmazonWeeklyTarget.__tablename__,
        KeywordDashboardTerm.__tablename__,
    )
    with engine.begin() as connection:
        inspector = sql_inspect(connection)
        existing_tables = set(inspector.get_table_names())
        for table in tables:
            if table not in existing_tables:
                continue
            columns = {column["name"] for column in inspector.get_columns(table)}
            if "updated_by" not in columns:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN updated_by VARCHAR(80) NULL"))
            existing_tables.add(table)


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL missing")
    mysql_prefix = "mysql" + "://"
    if url.startswith(mysql_prefix):
        return "mysql+" + "pymysql" + "://" + url[len(mysql_prefix) :]
    return url


def engine():
    global _engine, _session_factory
    if _engine is None:
        url = database_url()
        _engine = create_engine(url, pool_pre_ping=True, pool_recycle=300)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
        Base.metadata.create_all(_engine)
        migrate_amazon_monthly_targets(_engine)
        migrate_shared_edit_metadata(_engine)
    return _engine


def session_factory():
    engine()
    return _session_factory


def normalized_sku(value: Any) -> str:
    return str(value or "").strip().upper() or "(无 SKU)"


def title_matches(title: Any, keywords: tuple[str, ...]) -> bool:
    normalized = str(title or "").lower()
    return any(keyword in normalized for keyword in keywords)


def sku_unit_value(raw_sku: Any) -> int:
    sku = normalized_sku(raw_sku)
    if sku in SINGLE_VALUE_SKUS:
        return 1
    match = re.match(r"^(TN10P011|TN10P012|TN10P013)-(\d+)$", sku)
    if not match:
        return 1
    value = int(match.group(2))
    return value if value in ALLOWED_SUFFIX_VALUES else 1


def sku_color(raw_sku: Any, title: Any = "") -> str:
    sku = normalized_sku(raw_sku)
    if title_matches(title, WARRANTY_KEYWORDS):
        return "延保服务"
    if not title_matches(title, FINAL_PAYMENT_KEYWORDS) and title_matches(title, PRESALE_KEYWORDS):
        return "预售"
    if sku in {"X0051AFG1N", "TN10P011", "TN10P051", "TN20P011"} or sku.startswith("TN10P011-"):
        return "黑色"
    if sku in {"TN10P012", "TN10P052", "TN20P012"} or sku.startswith("TN10P012-"):
        return "银色"
    if sku in {"TN10P013", "TN10P053", "TN20P014"} or sku.startswith("TN10P013-"):
        return "樱桃红" if sku == "TN20P014" else "橙色"
    return "未分类"


def classify_item(item: dict[str, Any]) -> dict[str, Any]:
    title = item.get("title") or ""
    sku = normalized_sku(item.get("sku"))
    raw_quantity = int(item.get("quantity") or 0)
    multiplier = sku_unit_value(item.get("sku"))
    if title_matches(title, WARRANTY_KEYWORDS):
        item_type = "warranty"
        effective_units = 0
    elif title_matches(title, FINAL_PAYMENT_KEYWORDS):
        item_type = "final_payment"
        effective_units = 0 if sku == "(无 SKU)" else raw_quantity * multiplier
    elif title_matches(title, PRESALE_KEYWORDS):
        item_type = "presale"
        effective_units = 0
    else:
        item_type = "product"
        effective_units = raw_quantity * multiplier
    return {
        "shopify_line_item_id": item.get("id") or f"generated-{sku}-{title}",
        "title": title,
        "sku": sku,
        "color": sku_color(item.get("sku"), title),
        "raw_quantity": raw_quantity,
        "unit_multiplier": multiplier,
        "effective_units": effective_units,
        "item_type": item_type,
    }


def read_stores() -> list[dict[str, str]]:
    raw = os.environ.get("SHOPIFY_STORES_JSON", "").strip()
    if raw:
        try:
            stores = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"SHOPIFY_STORES_JSON invalid: {exc.msg}") from exc
    else:
        stores = [
            {
                "name": os.environ.get("SHOPIFY_STORE", ""),
                "store": os.environ.get("SHOPIFY_STORE", ""),
                "clientId": os.environ.get("SHOPIFY_CLIENT_ID", ""),
                "clientSecret": os.environ.get("SHOPIFY_CLIENT_SECRET", ""),
                "timezone": os.environ.get("SHOPIFY_TIMEZONE", DEFAULT_TIMEZONE),
                "apiVersion": os.environ.get("SHOPIFY_API_VERSION", DEFAULT_API_VERSION),
            }
        ]
    normalized = []
    for index, config in enumerate(stores):
        store = str(config.get("store") or config.get("shopDomain") or "").replace("https://", "").replace("http://", "").rstrip("/")
        client_id = str(config.get("clientId") or config.get("apiKey") or "")
        client_secret = str(config.get("clientSecret") or config.get("apiSecret") or "")
        if not store or not client_id or not client_secret:
            raise RuntimeError(f"Store config {index + 1} requires store, clientId and clientSecret")
        normalized.append(
            {
                "name": str(config.get("name") or store),
                "store": store,
                "client_id": client_id,
                "client_secret": client_secret,
                "timezone": str(config.get("timezone") or DEFAULT_TIMEZONE),
                "api_version": str(config.get("apiVersion") or DEFAULT_API_VERSION),
            }
        )
    return normalized


async def get_access_token(client: httpx.AsyncClient, store: dict[str, str]) -> str:
    response = await client.post(
        f"https://{store['store']}/admin/oauth/access_token",
        data={
            "grant_type": "client_credentials",
            "client_id": store["client_id"],
            "client_secret": store["client_secret"],
        },
    )
    response.raise_for_status()
    body = response.json()
    token = body.get("access_token")
    if not token:
        raise RuntimeError("Shopify token response did not include access_token")
    return token


async def fetch_orders(store: dict[str, str], updated_since: date) -> list[dict[str, Any]]:
    search = f"updated_at:>={updated_since.isoformat()}"
    orders: list[dict[str, Any]] = []
    after = None
    timeout = httpx.Timeout(30.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        token = await get_access_token(client, store)
        while True:
            response = await client.post(
                f"https://{store['store']}/admin/api/{store['api_version']}/graphql.json",
                headers={"x-shopify-access-token": token, "content-type": "application/json"},
                json={
                    "query": SHOPIFY_ORDERS_QUERY,
                    "variables": {"first": 250, "after": after, "search": search},
                },
            )
            response.raise_for_status()
            body = response.json()
            if body.get("errors"):
                raise RuntimeError(f"Shopify GraphQL error: {body['errors'][0].get('message', 'unknown error')}")
            data = body["data"]["orders"]
            orders.extend(edge["node"] for edge in data.get("edges", []))
            page_info = data.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
    return orders


def order_local_date(created_at: datetime, _timezone_name: str) -> date:
    # Shopify timestamps are absolute. The platform image has no extra timezone
    # dependency, so UTC is used for durable storage; UI labels make this clear.
    return created_at.date()


def upsert_order(db: Session, store: dict[str, str], payload: dict[str, Any], synced_at: datetime) -> None:
    shopify_order_id = payload["id"]
    order = db.scalar(
        select(Order).where(
            Order.store_domain == store["store"],
            Order.shopify_order_id == shopify_order_id,
        )
    )
    if order is None:
        order = Order(store_domain=store["store"], shopify_order_id=shopify_order_id)
        db.add(order)

    created_at = parse_datetime(payload.get("createdAt")) or synced_at
    updated_at = parse_datetime(payload.get("updatedAt")) or created_at
    classified_items = [classify_item(edge["node"]) for edge in (payload.get("lineItems") or {}).get("edges", [])]
    current_total = payload.get("currentTotalPriceSet") or payload.get("totalPriceSet") or {}
    refunded = payload.get("totalRefundedSet") or {}

    order.store_name = store["name"]
    order.order_name = payload.get("name") or shopify_order_id
    order.order_date = order_local_date(created_at, store["timezone"])
    order.shopify_created_at = created_at
    order.shopify_updated_at = updated_at
    order.cancelled_at = parse_datetime(payload.get("cancelledAt"))
    order.is_test = bool(payload.get("test"))
    order.financial_status = payload.get("displayFinancialStatus") or ""
    order.fulfillment_status = payload.get("displayFulfillmentStatus") or ""
    order.risk_level = payload.get("riskLevel") or ""
    order.currency = payload.get("currencyCode") or "USD"
    order.sales_amount = decimal_value((current_total.get("shopMoney") or {}).get("amount"))
    order.refund_amount = decimal_value((refunded.get("shopMoney") or {}).get("amount"))
    order.unit_count = sum(item["effective_units"] for item in classified_items)
    order.presale_unit_count = sum(
        item["raw_quantity"] * item["unit_multiplier"] for item in classified_items if item["item_type"] == "presale"
    )
    order.synced_at = synced_at

    # Flush only after every non-null order field has been assigned. New
    # orders need their generated id before line items can be replaced.
    db.flush()
    db.execute(delete(OrderItem).where(OrderItem.order_id == order.id))
    for item in classified_items:
        db.add(OrderItem(order_id=order.id, **item))


def latest_success(db: Session) -> SyncRun | None:
    return db.scalar(
        select(SyncRun).where(SyncRun.status == "success").order_by(SyncRun.finished_at.desc()).limit(1)
    )


def sync_is_fresh(db: Session) -> bool:
    latest = latest_success(db)
    if not latest or not latest.finished_at:
        return False
    cooldown = int(os.environ.get("SYNC_COOLDOWN_SECONDS", DEFAULT_SYNC_COOLDOWN_SECONDS))
    return latest.finished_at >= utcnow() - timedelta(seconds=cooldown)


async def run_sync(trigger: str) -> dict[str, Any]:
    factory = session_factory()
    with factory() as db:
        if trigger == "dashboard" and sync_is_fresh(db):
            latest = latest_success(db)
            return {
                "status": "cached",
                "message": "数据刚刚同步过，已使用最新缓存。",
                "last_sync_at": latest.finished_at.isoformat() if latest and latest.finished_at else None,
            }

        lock_acquired = db.execute(text("SELECT GET_LOCK('shopify_bi_sync', 0)")).scalar()
        if lock_acquired != 1:
            return {"status": "running", "message": "已有同步任务正在执行。"}

        run = SyncRun(status="running", started_at=utcnow(), trigger=trigger)
        db.add(run)
        db.commit()
        try:
            stores = read_stores()
            last_order_update = db.scalar(select(func.max(Order.shopify_updated_at)))
            if last_order_update:
                updated_since = (last_order_update - timedelta(days=3)).date()
            else:
                initial_days = int(os.environ.get("SHOPIFY_INITIAL_SYNC_DAYS", DEFAULT_INITIAL_SYNC_DAYS))
                updated_since = (utcnow() - timedelta(days=initial_days)).date()

            synced_at = utcnow()
            total_orders = 0
            for store in stores:
                payloads = await fetch_orders(store, updated_since)
                total_orders += len(payloads)
                for payload in payloads:
                    upsert_order(db, store, payload, synced_at)
                db.commit()

            run.status = "success"
            run.finished_at = utcnow()
            run.store_count = len(stores)
            run.order_count = total_orders
            run.message = f"同步完成：{len(stores)} 个店铺，{total_orders} 个更新订单。"
            db.commit()
            return {
                "status": "success",
                "message": run.message,
                "last_sync_at": run.finished_at.isoformat(),
                "store_count": run.store_count,
                "order_count": run.order_count,
            }
        except Exception as exc:
            db.rollback()
            failed_run = db.get(SyncRun, run.id)
            if failed_run:
                failed_run.status = "failed"
                failed_run.finished_at = utcnow()
                failed_run.message = "同步失败，请检查服务日志"
                db.commit()
            raise
        finally:
            db.execute(text("SELECT RELEASE_LOCK('shopify_bi_sync')"))
            db.commit()


def percentage_change(current: Decimal | int, previous: Decimal | int) -> float | None:
    current_value = float(current or 0)
    previous_value = float(previous or 0)
    if previous_value == 0:
        return 0.0 if current_value == 0 else None
    return round((current_value - previous_value) / previous_value * 100, 1)


def totals_for(db: Session, start: date, end: date, store: str | None) -> dict[str, Any]:
    conditions = [Order.order_date >= start, Order.order_date < end, Order.cancelled_at.is_(None), Order.is_test.is_(False)]
    if store:
        conditions.append(Order.store_domain == store)
    row = db.execute(
        select(
            func.count(Order.id),
            func.coalesce(func.sum(Order.sales_amount), 0),
            func.coalesce(func.sum(Order.refund_amount), 0),
            func.coalesce(func.sum(Order.unit_count), 0),
            func.coalesce(func.sum(Order.presale_unit_count), 0),
        ).where(*conditions)
    ).one()
    return {
        "orders": int(row[0] or 0),
        "sales": float(row[1] or 0),
        "refunds": float(row[2] or 0),
        "net_sales": float((row[1] or 0) - (row[2] or 0)),
        "units": int(row[3] or 0),
        "presale_units": int(row[4] or 0),
    }


def dashboard_payload(db: Session, start: date, end: date, store: str | None) -> dict[str, Any]:
    days = (end - start).days
    previous_start = start - timedelta(days=days)
    current = totals_for(db, start, end, store)
    previous = totals_for(db, previous_start, start, store)
    current["changes"] = {
        key: percentage_change(current[key], previous[key])
        for key in ("orders", "sales", "refunds", "net_sales", "units", "presale_units")
    }

    base_conditions = [Order.order_date >= start, Order.order_date < end, Order.cancelled_at.is_(None), Order.is_test.is_(False)]
    if store:
        base_conditions.append(Order.store_domain == store)

    daily_rows = db.execute(
        select(
            Order.order_date,
            func.count(Order.id),
            func.coalesce(func.sum(Order.sales_amount), 0),
            func.coalesce(func.sum(Order.refund_amount), 0),
            func.coalesce(func.sum(Order.unit_count), 0),
        )
        .where(*base_conditions)
        .group_by(Order.order_date)
        .order_by(Order.order_date)
    ).all()
    daily_map = {
        row[0]: {
            "date": row[0].isoformat(),
            "orders": int(row[1]),
            "sales": float(row[2]),
            "refunds": float(row[3]),
            "units": int(row[4]),
        }
        for row in daily_rows
    }
    daily = []
    cursor = start
    while cursor < end:
        daily.append(daily_map.get(cursor, {"date": cursor.isoformat(), "orders": 0, "sales": 0, "refunds": 0, "units": 0}))
        cursor += timedelta(days=1)

    item_conditions = list(base_conditions)
    sku_rows = db.execute(
        select(
            OrderItem.sku,
            OrderItem.color,
            func.coalesce(func.sum(OrderItem.effective_units), 0),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            *item_conditions,
            OrderItem.item_type.in_(["product", "final_payment"]),
            OrderItem.sku != "(无 SKU)",
        )
        .group_by(OrderItem.sku, OrderItem.color)
        .order_by(func.sum(OrderItem.effective_units).desc())
        .limit(12)
    ).all()

    abnormal_order_rows = db.execute(
        select(Order.order_name)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .where(*item_conditions, OrderItem.sku == "(无 SKU)")
        .distinct()
        .order_by(Order.order_date.desc(), Order.order_name.desc())
    ).all()

    fulfillment_rows = db.execute(
        select(Order.fulfillment_status, func.count(Order.id))
        .where(*base_conditions)
        .group_by(Order.fulfillment_status)
        .order_by(func.count(Order.id).desc())
    ).all()

    stores = db.execute(
        select(Order.store_domain, Order.store_name)
        .distinct()
        .order_by(Order.store_domain, Order.store_name)
    ).all()
    last = latest_success(db)
    currency = db.scalar(select(Order.currency).where(*base_conditions).limit(1)) or "USD"

    return {
        "period": {"days": days, "start": start.isoformat(), "end": (end - timedelta(days=1)).isoformat()},
        "store": store or "all",
        "currency": currency,
        "totals": current,
        "daily": daily,
        "sku_breakdown": [
            {"sku": row[0], "color": row[1], "units": int(row[2] or 0)} for row in sku_rows
        ],
        "abnormal_orders": [row[0] for row in abnormal_order_rows],
        "fulfillment": [
            {"status": row[0] or "UNKNOWN", "orders": int(row[1])} for row in fulfillment_rows
        ],
        "stores": [{"domain": row[0], "name": row[1]} for row in stores],
        "last_sync": {
            "at": last.finished_at.isoformat() if last and last.finished_at else None,
            "message": last.message if last else "尚未同步",
        },
        "timezone_note": "订单日期按 UTC 保存；店铺本地时区将在后续版本支持。",
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/dashboard/write-token")
def dashboard_write_token(request: Request, editor: str | None = Query(default=None)):
    """Issue a short-lived browser write credential without exposing SYNC_API_KEY."""

    issued = issue_dashboard_write_token(
        request.headers.get("X-Dashboard-Editor") or editor,
        request.headers.get("Origin"),
    )
    return JSONResponse(
        content=issued,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
            "Vary": "Origin",
        },
    )


@app.get("/__ideadock/verify/mysql")
def verify_mysql():
    try:
        url = os.environ.get("DATABASE_URL", "").strip()
        if not url:
            return {"ok": False, "reason": "DATABASE_URL missing"}
        mysql_prefix = "mysql" + "://"
        if url.startswith(mysql_prefix):
            url = "mysql+" + "pymysql" + "://" + url[len(mysql_prefix) :]
        with create_engine(url).connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"ok": True}
    except Exception:
        return {"ok": False, "reason": "database check failed"}


@app.get("/api/status")
def status():
    configured = bool(os.environ.get("SHOPIFY_STORES_JSON") or os.environ.get("SHOPIFY_STORE"))
    database_configured = bool(os.environ.get("DATABASE_URL", "").strip())
    amazon_configured = bool(os.environ.get("LINGXING_APP_ID") and os.environ.get("LINGXING_APP_SECRET"))
    return {
        "configured": configured and database_configured,
        "shopify_configured": configured,
        "database_configured": database_configured,
        "amazon_configured": amazon_configured,
        "sync_mode": "on_demand",
    }


def amazon_empty_row(series: str, product: str | None = None) -> dict[str, Any]:
    return {"series": series, "product": product or "", "acoas": None, "ad_sales_share": None, "ad_order_share": None, "units": None, "net_sales": None, "orders": None, "b2b_units": None, "b2b_orders": None, "ctr": None, "clicks": 0, "cpc": None, "cpo": None, "ad_cost": 0, "ad_cvr": None, "ad_units": 0, "ad_orders": 0, "cvr": None, "acos": None, "sessions": None, "page_views": None}


def amazon_periods(start_date: date, end_date: date, comparison: str) -> list[tuple[str, date, date]]:
    periods: list[tuple[str, date, date]] = []
    if comparison == "日":
        cursor = start_date
        while cursor <= end_date:
            periods.append((cursor.isoformat(), cursor, cursor))
            cursor += timedelta(days=1)
        return periods
    if comparison == "周":
        cursor = start_date - timedelta(days=start_date.weekday())
        while cursor <= end_date:
            natural_end = cursor + timedelta(days=6)
            period_start = max(cursor, start_date)
            period_end = min(natural_end, end_date)
            periods.append((f"{period_start.isoformat()}~{period_end.isoformat()}", period_start, period_end))
            cursor += timedelta(days=7)
        return periods
    cursor = start_date.replace(day=1)
    while cursor <= end_date:
        next_month = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        natural_end = next_month - timedelta(days=1)
        period_start = max(cursor, start_date)
        period_end = min(natural_end, end_date)
        periods.append((cursor.strftime("%Y-%m"), period_start, period_end))
        cursor = next_month
    return periods


async def fetch_product_performance(
    sid: int,
    start_date: date,
    end_date: date,
    comparison: str,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    asin_list: list[str] | None = None,
    currency_code: str | None = None,
    quality: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Read LingXing's product-performance endpoint for operating metrics."""
    # An explicitly empty list means the selected products have no ASINs
    # mapped for this site; avoid issuing an unfiltered request in that case.
    if asin_list == []:
        return []
    if lingxing_mcp_key():
        try:
            rows = await fetch_mcp_product_performance(sid, start_date, end_date, client, asin_list, currency_code)
            # LingXing defaults its product-performance currency to CNY. The
            # explicit request above makes money follow currency_code; preserve
            # that contract even when a row omits or mislabels the currency.
            if currency_code:
                for row in rows:
                    row["currency_code"] = currency_code
                    row.pop("currencyCode", None)
            if quality is not None:
                _record_performance_quality(quality, "mcp", True, None, rows)
            return rows
        except (RuntimeError, httpx.HTTPError) as exc:
            # The OpenAPI endpoint is only an explicit degraded mode. It does
            # not expose the same typed click dimensions, so never present its
            # result as a complete MCP dataset.
            if quality is not None:
                _record_performance_quality(quality, "openapi_fallback", False, str(exc))
    # LingXing limits this endpoint to a maximum 92-day date range.  The
    # dashboard allows a wider range for quick presets such as "去年", so split
    # longer requests into bounded chunks and merge the returned rows.  Chunk
    # responses are kept in the
    # normal cache, which also prevents repeated filter changes from issuing
    # the same upstream request again.
    rows: list[dict[str, Any]] = []
    cursor = start_date
    while cursor <= end_date:
        chunk_end = min(cursor + timedelta(days=91), end_date)
        cache_key = ("product-performance-v3", sid, cursor.isoformat(), chunk_end.isoformat(), tuple(asin_list or ()), currency_code or "")
        rate_limited = False
        cached = _amazon_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < AMAZON_CACHE_TTL_SECONDS:
            # Currency normalization below mutates each returned row. Keep the
            # cached upstream payload immutable across viewers and currencies.
            chunk_rows = copy.deepcopy(cached[1])
        else:
            payload = {
                "offset": 0,
                "length": 10000,
                "sort_field": "volume",
                "sort_type": "desc",
                # LingXing documents a scalar string for a single-shop query;
                # arrays are reserved for multi-shop aggregation.
                "sid": str(sid),
                "start_date": cursor.isoformat(),
                "end_date": chunk_end.isoformat(),
                "summary_field": "asin",
                "is_recently_enum": False,
                "purchase_status": 0,
                # Without this flag LingXing may return generic totals while
                # leaving the SP/SB/SBV/SD dimensions as zero placeholders.
                # The LingXing product-performance API treats this as a
                # boolean flag.  Sending JSON true enables the typed SP/SB/
                # SBV/SD fields, while numeric 1 can leave them as zeros.
                "query_order_profit": True,
            }
            if currency_code:
                payload["currency_code"] = currency_code
            # The endpoint supports native ASIN filtering through
            # search_field/search_value (up to 50 ASINs per request).
            if asin_list is not None:
                payload["search_field"] = "asin"
                payload["search_value"] = asin_list
            # LingXing may briefly reject consecutive requests for the same
            # account even when each request is within the documented range.
            # Retry only that specific upstream response with bounded backoff;
            # all other errors still fail fast and remain visible to the UI.
            for attempt in range(6):
                try:
                    # The documented token bucket for this endpoint is 1.
                    # Serialize product-performance calls across periods so a
                    # week/month query cannot self-rate-limit its own requests.
                    global _lingxing_performance_last_call
                    async with _lingxing_performance_lock:
                        # LingXing documents a one-request token bucket for
                        # this endpoint when querying a single shop. Keep a
                        # small spacing between period requests so later
                        # periods are not silently rate-limited.
                        wait_for = 1.05 - (time.monotonic() - _lingxing_performance_last_call)
                        if wait_for > 0:
                            await asyncio.sleep(wait_for)
                        async with semaphore:
                            body = await lingxing_post("/bd/productPerformance/openApi/asinList", payload, client=client)
                        _lingxing_performance_last_call = time.monotonic()
                    break
                except RuntimeError as exc:
                    error_text = str(exc).lower()
                    if "too frequent" in error_text or "request later" in error_text:
                        if attempt == 5:
                            rate_limited = True
                            break
                        await asyncio.sleep(2 * (attempt + 1))
                        continue
                    if "频繁" not in str(exc):
                        raise
                    if attempt == 4:
                        rate_limited = True
                        break
                    await asyncio.sleep(2 * (attempt + 1))
            if rate_limited:
                # A rate-limited later chunk must not discard the data already
                # fetched for earlier chunks or turn the whole dashboard into
                # HTTP 500.  Return the successful prefix; the normal cache
                # keeps it available while a subsequent request retries the
                # missing chunk after LingXing's cooldown.
                break
            data = body.get("data") or {}
            if isinstance(data, list):
                chunk_rows = data
            elif isinstance(data, dict):
                chunk_rows = data.get("list") or data.get("rows") or data.get("records") or data.get("items")
                if not isinstance(chunk_rows, list) and isinstance(data.get("data"), dict):
                    nested = data["data"]
                    chunk_rows = nested.get("list") or nested.get("rows") or nested.get("records") or nested.get("items")
            else:
                chunk_rows = []
            chunk_rows = chunk_rows if isinstance(chunk_rows, list) else []
            _amazon_cache[cache_key] = (time.monotonic(), copy.deepcopy(chunk_rows))
        if quality is not None:
            _record_performance_quality(
                quality,
                "openapi_fallback",
                False,
                "product OpenAPI rate limited" if rate_limited else None,
                chunk_rows,
            )
        rows.extend(chunk_rows)
        cursor = chunk_end + timedelta(days=1)
    return rows


def _record_performance_quality(
    quality: dict[str, Any],
    source: str,
    mcp_ok: bool,
    error: str | None,
    rows: list[dict[str, Any]] | None = None,
) -> None:
    quality.setdefault("sources", set()).add(source)
    quality.setdefault("errors", [])
    quality["mcp_ok"] = bool(quality.get("mcp_ok", True)) and mcp_ok
    if error:
        quality["errors"].append(str(error))
    if rows is not None:
        quality["raw_rows"] = quality.get("raw_rows", 0) + len(rows)
        quality["typed_rows"] = quality.get("typed_rows", 0) + sum(
            product_performance_typed_metrics_present(row) for row in rows if isinstance(row, dict)
        )


def product_performance_typed_metrics_present(raw: dict[str, Any]) -> bool:
    """Return whether every non-money product ad dimension is available."""

    try:
        breakdown = product_performance_ad_breakdown(raw)
        return all(
            breakdown[ad_type][metric] is not None
            for ad_type in AMAZON_AD_BREAKDOWN_FIELDS
            for metric in AMAZON_AD_BREAKDOWN_FIELDS[ad_type]
            if metric not in PRODUCT_PERFORMANCE_TYPED_MONEY_FIELDS
        )
    except (KeyError, TypeError, ValueError):
        return False


# Compatibility name retained for existing API callers and tests. It now means
# the full non-money typed matrix is present, not only the four click fields.
product_performance_typed_clicks_present = product_performance_typed_metrics_present


def _performance_data_quality(quality: dict[str, Any]) -> dict[str, Any]:
    sources = {str(source) for source in quality.get("sources", set())}
    raw_rows = int(quality.get("raw_rows", 0) or 0)
    typed_rows = int(quality.get("typed_rows", 0) or 0)
    errors = [str(error) for error in quality.get("errors", [])]
    typed_present = raw_rows > 0 and raw_rows == typed_rows
    mcp_ok = bool(quality.get("mcp_ok", False))
    if raw_rows > 0 and not typed_present:
        errors.append("产品表现缺少完整 SP/SB/SBV/SD 分类字段")
    return {
        "source": "openapi_fallback" if "openapi_fallback" in sources else ("mcp" if sources else "unknown"),
        "mcp_ok": mcp_ok,
        "raw_rows": raw_rows,
        "typed_rows": typed_rows,
        "typed_clicks_present": typed_present,
        "typed_metrics_present": typed_present,
        "complete": mcp_ok and not errors and (raw_rows == 0 or typed_present),
        "errors": errors,
    }


def optional_metric(row: dict[str, Any], *names: str) -> float | None:
    return optional_value(row, *names)


AMAZON_SOURCE_FIELDS = {
    "performance": {
        "units": ("volume", "totalSalesQuantity"),
        "net_sales": ("net_amount", "netAmount", "net_sales", "netSales"),
        "orders": ("order_items", "orderItems", "totalOrderQuantity"),
        "b2b_units": ("b2b_volume", "b2bVolume", "totalB2bSalesQuantity"),
        "b2b_orders": ("b2b_order_items", "b2bOrderItems", "totalB2bOrderQuantity"),
        "sessions": ("sessions_total", "sessionsTotal", "sessionTotal", "trafficSessionTotal"),
        "page_views": ("page_views_total", "pageViewsTotal", "page_view_total", "pageViewTotal", "pageviews_total", "pageviewsTotal", "trafficPVTotal", "trafficPvTotal", "traffic_pv_total", "trafficPageViewsTotal", "pv_total", "pvTotal", "page_views", "pageViews", "page_view", "pageView", "pv"),
        "impressions": ("impressions", "ad_impressions", "adImpressions", "total_ad_impressions", "totalAdImpressions"),
        "clicks": ("clicks", "ad_clicks", "adClicks", "ads_clicks", "adsClicks", "total_ad_clicks", "totalAdClicks", "ad_click_quantity", "adClickQuantity", "ad_clicks_total"),
        "ad_sales": ("ad_sales_amount", "ads_sales_amount", "adSalesAmount"),
        "ad_cost": ("spend", "ad_cost", "advertising_spend"),
        "ad_units": ("ads_sales_volume_quantity", "ad_sales_volume_quantity", "adUnits"),
        "ad_orders": ("ad_order_quantity", "ad_orders", "adOrders", "ads_orders", "adsOrders", "ad_order_num", "adOrderNum", "ad_order_quantity_total", "adOrderQuantityTotal"),
    },
}
AMAZON_SOURCE_RATIOS = {
    "performance": {"source_cvr": ("cvr", "conversion_rate", "conversionRate")},
}
AMAZON_METRIC_SOURCES = {
    "performance": ["units", "net_sales", "orders", "b2b_units", "b2b_orders", "sessions", "page_views", "cvr", "impressions", "clicks", "ad_sales", "ad_cost", "ad_units", "ad_orders", "ctr", "cpc", "ad_cvr", "acos"],
    "calculated": ["acoas", "ad_sales_share", "ad_order_share"],
}

AMAZON_AD_BREAKDOWN_FIELDS = {
    "sp": {
        "impressions": ("ad_impressions_sp", "adImpressionsSp", "impressions_sp", "sp_impressions", "spImpressions", "ads_sp_impressions"),
        "clicks": ("ad_clicks_sp", "adClicksSp", "clicks_sp", "sp_clicks", "spClicks", "ads_sp_clicks"),
        "ad_cost": ("ads_sp_cost", "adSpendSp", "spend_sp"),
        "ad_units": ("ads_sp_sales_volume_quantity", "adSalesVolumeQuantitySp", "ad_units_sp"),
        "ad_orders": ("ad_order_quantity_sp", "adOrderQuantitySp", "ad_orders_sp", "sp_orders", "spOrders", "ad_order_num_sp", "adOrderNumSp"),
        "ad_sales": ("ads_sp_sales", "adsSpSales", "ad_sales_sp", "ad_direct_sales_amount_sp"),
    },
    "sb": {
        "impressions": ("shared_ad_impressions_sb", "sharedAdImpressionsSb", "ad_impressions_sb", "sb_impressions", "sbImpressions", "shared_ads_sb_impressions"),
        "clicks": ("shared_ad_clicks_sb", "sharedAdClicksSb", "ad_clicks_sb", "sb_clicks", "sbClicks", "shared_ad_clicks_brand", "ad_clicks_brand", "shared_ads_sb_clicks"),
        "ad_cost": ("shared_ads_sb_cost", "sharedAdsSbCost", "ad_spend_sb", "shared_cost_of_advertising_sb"),
        "ad_units": ("shared_ads_sb_sales_volume_quantity", "sharedAdsSbSalesVolumeQuantity", "ad_units_sb"),
        "ad_orders": ("shared_ad_order_quantity_sb", "sharedAdOrderQuantitySb", "ad_orders_sb", "sb_orders", "sbOrders", "ad_order_num_sb", "adOrderNumSb"),
        "ad_sales": ("shared_ads_sb_sales", "sharedAdsSbSales", "ad_sales_sb", "shared_ad_direct_sales_amount_sb"),
    },
    "sbv": {
        "impressions": ("shared_ad_impressions_sbv", "sharedAdImpressionsSbv", "ad_impressions_sbv", "sbv_impressions", "sbvImpressions", "shared_ads_sbv_impressions"),
        "clicks": ("shared_ad_clicks_sbv", "sharedAdClicksSbv", "ad_clicks_sbv", "sbv_clicks", "sbvClicks", "shared_ad_clicks_video", "ad_clicks_video", "shared_ads_sbv_clicks"),
        "ad_cost": ("shared_ads_sbv_cost", "sharedAdsSbvCost", "ad_spend_sbv", "shared_cost_of_advertising_sbv"),
        "ad_units": ("shared_ads_sbv_sales_volume_quantity", "sharedAdsSbvSalesVolumeQuantity", "ad_units_sbv"),
        "ad_orders": ("shared_ad_order_quantity_sbv", "sharedAdOrderQuantitySbv", "ad_orders_sbv", "sbv_orders", "sbvOrders", "ad_order_num_sbv", "adOrderNumSbv"),
        "ad_sales": ("shared_ads_sbv_sales", "sharedAdsSbvSales", "ad_sales_sbv", "shared_ad_direct_sales_amount_sbv"),
    },
    "sd": {
        "impressions": ("ad_impressions_sd", "adImpressionsSd", "impressions_sd", "sd_impressions", "sdImpressions", "ads_sd_impressions"),
        "clicks": ("ad_clicks_sd", "adClicksSd", "clicks_sd", "sd_clicks", "sdClicks", "ads_sd_clicks"),
        "ad_cost": ("ads_sd_cost", "adSpendSd", "spend_sd"),
        "ad_units": ("ads_sd_sales_volume_quantity", "adSalesVolumeQuantitySd", "ad_units_sd"),
        "ad_orders": ("ad_order_quantity_sd", "adOrderQuantitySd", "ad_orders_sd", "sd_orders", "sdOrders", "ad_order_num_sd", "adOrderNumSd"),
        "ad_sales": ("ads_sd_sales", "adsSdSales", "ad_sales_sd", "ad_direct_sales_amount_sd"),
    },
}

# LingXing's typed product-performance money fields are returned in the store's
# settlement currency even when the generic product-performance money fields are
# requested in USD (or another site currency).  Reconcile a complete typed money
# matrix against the authoritative generic total instead of adding the raw typed
# values.  This keeps the four ad-type details in the dashboard currency while
# preventing a US spend of about $76k from being displayed as roughly CNY 387k.
PRODUCT_PERFORMANCE_TYPED_MONEY_FIELDS = {"ad_cost", "ad_sales"}


def product_performance_ad_breakdown(raw: dict[str, Any]) -> dict[str, dict[str, float | None]]:
    """Normalize LingXing's product-performance advertising dimensions."""
    result: dict[str, dict[str, float]] = {}
    for ad_type, fields in AMAZON_AD_BREAKDOWN_FIELDS.items():
        result[ad_type] = {}
        for metric, names in fields.items():
            # Keep an absent upstream field as null. Filling missing fields
            # with zero makes the frontend mistake an incomplete breakdown for
            # a complete zero-valued breakdown and overwrite the generic total.
            result[ad_type][metric] = optional_metric(raw, *names)

    # Typed money values can use a different currency from the requested
    # generic total.  Scale a complete four-type matrix so its sum exactly
    # matches the authoritative total.  Incomplete, zero-but-positive, or
    # unavailable totals stay null instead of presenting an invented split.
    for metric in PRODUCT_PERFORMANCE_TYPED_MONEY_FIELDS:
        typed_values = [
            optional_metric(raw, *AMAZON_AD_BREAKDOWN_FIELDS[ad_type][metric])
            for ad_type in AMAZON_AD_BREAKDOWN_FIELDS
        ]
        generic_total = optional_metric(
            raw,
            *AMAZON_SOURCE_FIELDS["performance"][metric],
        )
        typed_total = sum(typed_values) if all(value is not None for value in typed_values) else None
        if (
            typed_total is not None
            and generic_total is not None
            and typed_total > 0
            and generic_total > 0
        ):
            scale = generic_total / typed_total
            for ad_type, value in zip(AMAZON_AD_BREAKDOWN_FIELDS, typed_values):
                result[ad_type][metric] = value * scale
        elif typed_total == 0 and generic_total == 0:
            for ad_type in AMAZON_AD_BREAKDOWN_FIELDS:
                result[ad_type][metric] = 0
        else:
            for ad_type in AMAZON_AD_BREAKDOWN_FIELDS:
                result[ad_type][metric] = None

    generic_fields = {
        "clicks": AMAZON_SOURCE_FIELDS["performance"]["clicks"],
        "ad_units": AMAZON_SOURCE_FIELDS["performance"]["ad_units"],
        "ad_orders": AMAZON_SOURCE_FIELDS["performance"]["ad_orders"],
    }
    # A zero-filled dimension block is not the same as four real zero values.
    # When a generic total is positive but every typed value is zero, expose
    # nulls so the UI shows an unavailable breakdown instead of false zeros.
    for metric, names in generic_fields.items():
        generic_total = optional_metric(raw, *names)
        typed_values = [result[ad_type].get(metric) for ad_type in AMAZON_AD_BREAKDOWN_FIELDS]
        if generic_total and all(value == 0 for value in typed_values if value is not None) and any(value is not None for value in typed_values):
            for ad_type in result:
                result[ad_type][metric] = None
    return result


def product_performance_ad_totals(raw: dict[str, Any]) -> dict[str, float]:
    """Sum the four product-performance ad types when those fields are present.

    LingXing's generic ``clicks``/``spend`` fields can be incomplete for some
    accounts.  Typed fields are authoritative for additive counts such as
    clicks and orders.  They are intentionally not used for money: those typed
    values can be in a different currency from the requested generic totals.
    """
    totals: dict[str, float] = {}
    generic_fields = {
        "clicks": AMAZON_SOURCE_FIELDS["performance"]["clicks"],
        "ad_cost": AMAZON_SOURCE_FIELDS["performance"]["ad_cost"],
        "ad_units": AMAZON_SOURCE_FIELDS["performance"]["ad_units"],
        "ad_orders": AMAZON_SOURCE_FIELDS["performance"]["ad_orders"],
    }
    for metric, _ in next(iter(AMAZON_AD_BREAKDOWN_FIELDS.values())).items():
        if metric in PRODUCT_PERFORMANCE_TYPED_MONEY_FIELDS:
            continue
        total = 0.0
        typed_values: list[float] = []
        for fields in AMAZON_AD_BREAKDOWN_FIELDS.values():
            names = fields[metric]
            value = optional_metric(raw, *names)
            if value is not None:
                typed_values.append(value)
                total += value
        generic_total = optional_metric(raw, *generic_fields.get(metric, ()))
        complete = len(typed_values) == len(AMAZON_AD_BREAKDOWN_FIELDS)
        if complete and not (total == 0 and generic_total and metric in generic_fields):
            totals[metric] = total
    return totals


async def amazon_dashboard_periodic(
    comparison: str,
    start_date: date,
    end_date: date,
    site: str | list[str] | None,
    selected_series: set[str],
    selected_products: set[str],
    sid_map: dict[str, Any],
    store_rows: list[dict[str, Any]] | None = None,
    display_currency: str = "original",
) -> dict[str, Any]:
    periods = amazon_periods(start_date, end_date, comparison)
    if site is None or site == "" or site == []:
        selected_sites = list(AMAZON_SITE_CODES)
    else:
        selected_sites = amazon_dashboard_selected_sites(
            [site] if isinstance(site, str) else [str(value) for value in site]
        )
    is_multi_site = len(selected_sites) > 1
    requested_currency = str(display_currency or "original").upper()
    currency_mode = requested_currency
    if requested_currency == "NATIVE":
        # Internal sales-dashboard mode. It keeps site rows separate while the
        # caller performs one final conversion into its rollup currency.
        requested_currency = "native"
    if requested_currency == "ORIGINAL":
        requested_currency = "original"
    if is_multi_site and requested_currency in ("original",):
        raise ValueError("多站点汇总必须选择统一货币，不能直接相加原币种")
    if requested_currency not in ("original", "native") and requested_currency not in AMAZON_SUPPORTED_CURRENCIES:
        raise ValueError(f"不支持的货币：{display_currency}")
    semaphore = asyncio.Semaphore(AMAZON_UPSTREAM_CONCURRENCY)
    performance_quality: dict[str, Any] = {}
    cache_key = ("periodic-dashboard-v10-explicit-mcp-currency", comparison, start_date.isoformat(), end_date.isoformat(), tuple(selected_sites), requested_currency, tuple(sorted(selected_series)), tuple(sorted(selected_products)))
    cached = _amazon_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < AMAZON_CACHE_TTL_SECONDS:
        # Sales-dashboard enrichment reallocates rows in-place after this call.
        # Return a private object so those edits cannot pollute the cached raw
        # periodic response for another viewer or filter invocation.
        return copy.deepcopy(cached[1])

    async with httpx.AsyncClient(timeout=45) as client:
        async def fetch_site(site_name: str):
            site_code = AMAZON_SITE_CODES.get(site_name, site_name)
            accounts = amazon_sid_accounts(site_name, sid_map, store_rows)
            if not accounts:
                return site_name, site_code, AMAZON_CURRENCY_CODES.get(site_name, "USD"), []
            native_currency = AMAZON_CURRENCY_CODES.get(site_name, "USD")
            query_currency = native_currency if requested_currency in ("original", "native") else requested_currency
            # Translate the UI's product selection back to the site's mapped
            # ASINs so LingXing can filter at the source. Keep a full request
            # for "all products"; an empty mapped result intentionally yields
            # no performance rows for this site.
            asin_filter: list[str] | None
            if selected_products == set(AMAZON_PRODUCTS):
                asin_filter = None
            else:
                site_mapping = ASIN_MAPPING.get(site_code, {})
                asin_filter = [asin for asin, product_name in site_mapping.items() if product_name in selected_products]
            all_rows: list[dict[str, Any]] = []
            for account in accounts:
                sid_value = account["sid"]
                performance_rows = []
                try:
                    # The product-performance endpoint has a token bucket of 1.
                    # Query each natural dashboard period separately so every
                    # day/week/month receives the metrics belonging to it.
                    for period_label, period_start, period_end in periods:
                        period_rows = await fetch_product_performance(
                            int(sid_value), period_start, period_end, comparison,
                            client, semaphore, asin_filter, query_currency, performance_quality,
                        )
                        for period_row in period_rows:
                            if isinstance(period_row, dict):
                                tagged = dict(period_row)
                                tagged["_dashboard_period"] = period_label
                                tagged["_source"] = "performance"
                                performance_rows.append(tagged)
                except RuntimeError as exc:
                    _record_performance_quality(performance_quality, "openapi_fallback", False, str(exc))
                    if "ip not permit" in str(exc).lower() or "白名单" in str(exc):
                        performance_rows = []
                    else:
                        raise
                for row in performance_rows:
                    row["_source"] = "performance"
                all_rows.extend(performance_rows)
            return site_name, site_code, native_currency, all_rows

        results = await asyncio.gather(*(fetch_site(site_name) for site_name in selected_sites))

    aggregate: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    def row_date(raw: dict[str, Any]) -> date | None:
        if raw.get("_dashboard_date"):
            try:
                return date.fromisoformat(str(raw["_dashboard_date"])[:10])
            except ValueError:
                pass
        for key in ("r_date", "rDate", "data_date", "dataDate", "date", "report_date", "reportDate", "stat_date", "statDate"):
            value = raw.get(key)
            if value:
                text = str(value).strip().replace("/", "-")
                for candidate in (text[:10], text.split(" ", 1)[0]):
                    try:
                        return datetime.fromisoformat(candidate).date()
                    except ValueError:
                        pass
                for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                    try:
                        return datetime.strptime(text, fmt).date()
                    except ValueError:
                        pass
        return None

    def period_for(value: date | None) -> tuple[str, date, date] | None:
        if value is None:
            # 产品表现接口返回的是所选区间汇总，没有日期列，归入当前请求对应的周期。
            return periods[-1] if periods else None
        return next((p for p in periods if p[1] <= value <= p[2]), None)

    for site_name, site_code, default_currency, raw_rows in results:
        for raw in raw_rows:
            forced_period = raw.get("_dashboard_period") if isinstance(raw, dict) else None
            period_info = next((p for p in periods if p[0] == forced_period), None) if forced_period else period_for(row_date(raw))
            if period_info is None:
                continue
            period_label, period_start, period_end = period_info
            product = amazon_product(
                site_code,
                raw.get("asin") or raw.get("ASIN") or raw.get("asins") or raw.get("ASINs") or raw,
            )
            group = amazon_series(product)
            if not group or group not in selected_series or product not in selected_products:
                continue
            row_currency = str(raw.get("currency_code") or raw.get("currencyCode") or default_currency).strip() or default_currency
            key = (period_label, site_name, group, product)
            item = aggregate.setdefault(key, {"period": period_label, "site": site_name, "site_code": site_code, "period_start": period_start.isoformat(), "period_end": period_end.isoformat(), "currency": row_currency, "asins": set(), "ad_breakdown": {ad_type: {metric: None for metric in fields} for ad_type, fields in AMAZON_AD_BREAKDOWN_FIELDS.items()}})
            if str(item.get("currency") or "") != row_currency:
                raise RuntimeError(
                    f"产品表现同一分组返回混合币种，拒绝相加：{period_label} {site_name} {group} "
                    f"{item.get('currency')} / {row_currency}"
                )
            item["asins"].update(amazon_asins_for_product(site_code, product))
            source = raw.get("_source", "performance")
            if source not in AMAZON_SOURCE_FIELDS:
                continue
            fields = AMAZON_SOURCE_FIELDS[source]
            source_fields = AMAZON_SOURCE_RATIOS[source]
            breakdown = product_performance_ad_breakdown(raw)
            breakdown_totals = product_performance_ad_totals(raw)
            for field, names in fields.items():
                value = optional_metric(raw, *names)
                if field in breakdown_totals and (breakdown_totals[field] != 0 or value in (None, 0)):
                    value = breakdown_totals[field]
                if value is not None:
                    item[field] = (item.get(field) or 0) + value
            for field, names in source_fields.items():
                value = optional_metric(raw, *names)
                if value is not None:
                    item[field] = value
            for ad_type, metrics in breakdown.items():
                for metric, value in metrics.items():
                    if value is not None:
                        current = item["ad_breakdown"][ad_type].get(metric)
                        item["ad_breakdown"][ad_type][metric] = (current or 0) + value

    rows: list[dict[str, Any]] = []
    for (period_label, site_name, group, product), item in aggregate.items():
        units = item.get("units")
        net_sales = item.get("net_sales")
        orders = item.get("orders")
        clicks = item.get("clicks")
        impressions = item.get("impressions")
        ad_sales = item.get("ad_sales")
        ad_cost = item.get("ad_cost")
        ad_orders = item.get("ad_orders")
        sessions = item.get("sessions")
        page_views = item.get("page_views")
        ad_units = item.get("ad_units")
        calculated_cvr = (
            orders / sessions
            if orders is not None and sessions is not None and sessions != 0
            else None
        )
        ad_sales_share = (ad_units / units) if ad_units is not None and units is not None and units != 0 else None
        ad_order_share = (ad_orders / orders) if ad_orders is not None and orders is not None and orders != 0 else None
        # ACoAS is defined by the dashboard requirement as ad spend divided
        # by net sales. Recalculate it from the period totals instead of
        # trusting a range-level/source value that may use another denominator.
        calculated_acoas = (ad_cost / net_sales) if ad_cost is not None and net_sales is not None and net_sales != 0 else None
        rows.append({
            "period": item["period"], "period_start": item["period_start"], "period_end": item["period_end"],
            "site": site_name, "site_code": item.get("site_code"), "series": group, "product": product, "asin": ", ".join(sorted(item.get("asins") or [])) or None, "currency": item.get("currency", "USD"),
            "units": int(units) if units is not None else None, "net_sales": net_sales, "orders": int(orders) if orders is not None else None,
            "b2b_units": int(item["b2b_units"]) if item.get("b2b_units") is not None else None, "b2b_orders": int(item["b2b_orders"]) if item.get("b2b_orders") is not None else None,
            "ctr": clicks / impressions if clicks is not None and impressions is not None and impressions != 0 else (None if impressions is not None else item.get("source_ctr")), "clicks": int(clicks) if clicks is not None else None,
            "impressions": int(impressions) if impressions is not None else None, "cpc": ad_cost / clicks if ad_cost is not None and clicks is not None and clicks != 0 else (None if clicks is not None else item.get("source_cpc")),
            "ad_cost": ad_cost, "ad_cvr": ad_orders / clicks if ad_orders is not None and clicks is not None and clicks != 0 else (None if clicks is not None else item.get("source_ad_cvr")),
            "cpo": ad_cost / ad_orders if ad_cost is not None and ad_orders is not None and ad_orders != 0 else None,
            "ad_units": int(ad_units) if ad_units is not None else None, "ad_orders": int(ad_orders) if ad_orders is not None else None,
            "cvr": calculated_cvr, "acos": ad_cost / ad_sales if ad_cost is not None and ad_sales is not None and ad_sales != 0 else (None if ad_sales is not None else item.get("source_acos")),
            "acoas": calculated_acoas, "ad_sales_share": ad_sales_share, "ad_order_share": ad_order_share, "ad_sales": ad_sales,
            "sessions": int(sessions) if sessions is not None else None,
            "page_views": int(page_views) if page_views is not None else None,
            "ad_breakdown": item.get("ad_breakdown", {}),
        })
    # Product performance can label all marketplace money as CNY regardless of
    # the requested currency. Normalize from that settlement currency to the
    # requested/native currency before any endpoint aggregates rows.
    row_rates: dict[str, float] | None = None
    if requested_currency == "native":
        by_site: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_site.setdefault(str(row.get("site") or ""), []).append(row)
        needed = {
            str(row.get("currency") or "").strip().upper()
            for row in rows
            if str(row.get("currency") or "").strip()
        } | {AMAZON_CURRENCY_CODES.get(site, "USD") for site in by_site}
        row_rates = await amazon_usd_exchange_rates(end_date, needed) if needed else None
        for site, site_rows in by_site.items():
            rows_for_site = site_rows
            await amazon_convert_sales_money_rows(
                rows_for_site,
                AMAZON_CURRENCY_CODES.get(site, "USD"),
                end_date,
                row_rates,
            )
    else:
        target_currency = requested_currency if requested_currency != "original" else AMAZON_CURRENCY_CODES.get(selected_sites[0], "USD")
        needed = {
            str(row.get("currency") or "").strip().upper()
            for row in rows
            if str(row.get("currency") or "").strip()
        } | ({target_currency} if rows else set())
        row_rates = await amazon_usd_exchange_rates(end_date, needed) if needed else None
        await amazon_convert_sales_money_rows(rows, target_currency, end_date, row_rates)

    output_currency = (
        "native"
        if requested_currency == "native"
        else requested_currency if requested_currency != "original" else AMAZON_CURRENCY_CODES.get(selected_sites[0], "USD")
    )
    response = {
        "period": {"comparison": comparison, "start": start_date.isoformat(), "end": end_date.isoformat()},
        "currency": output_currency,
        "currency_mode": currency_mode.lower(),
        "selected_sites": selected_sites,
        "filters": {"site": selected_sites, "series": list(selected_series), "products": list(selected_products)},
        "periods": [{"label": label, "start": p_start.isoformat(), "end": p_end.isoformat()} for label, p_start, p_end in periods],
        "rows": rows,
        "data_quality": _performance_data_quality(performance_quality),
        "mapping": {
            "key": "site+asin",
            "sites": list(AMAZON_SITE_CODES),
            "sources": {
                **AMAZON_METRIC_SOURCES,
            },
            "net_sales_field": "net_amount",
        },
    }
    _amazon_cache[cache_key] = (time.monotonic(), copy.deepcopy(response))
    return response


def lingxing_mcp_key() -> str:
    return os.environ.get("LINGXING_MCP_KEY", "").strip()


def _reset_lingxing_mcp_metadata_cache() -> None:
    """Clear dynamically resolved MCP metadata; used by tests and refreshes."""

    global _lingxing_mcp_catalog_version
    _lingxing_mcp_metadata_cache.clear()
    _lingxing_mcp_catalog_version = ""


def _normalized_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key or "").lower())


def _mcp_scalar(node: Any, names: tuple[str, ...]) -> str | int | None:
    wanted = {_normalized_key(name) for name in names}
    stack = [node]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            for key, item in value.items():
                if _normalized_key(key) in wanted and item not in (None, "") and not isinstance(item, (dict, list, bool)):
                    return item
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return None


def _mcp_tool_record(node: Any, tool_id: str) -> dict[str, Any] | None:
    id_keys = {_normalized_key(key) for key in ("toolId", "tool_id", "name", "id")}
    stack = [node]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            if any(_normalized_key(key) in id_keys and str(item) == tool_id for key, item in value.items()):
                return value
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return None


def _lingxing_mcp_business_error(business: dict[str, Any] | None) -> str | None:
    if not isinstance(business, dict):
        return None
    containers = [business]
    nested = business.get("data")
    if isinstance(nested, dict):
        containers.append(nested)
    for container in containers:
        if container.get("success") is False or container.get("code") == 0:
            return str(
                container.get("msg")
                or container.get("message")
                or container.get("error")
                or container.get("error_message")
                or "request failed"
            )
    return None


def _lingxing_mcp_content_text(result: dict[str, Any]) -> str:
    content = result.get("content") or []
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(item.get("text") or "")
        for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    )


def _lingxing_mcp_json_body(response: httpx.Response) -> dict[str, Any]:
    """Decode a JSON or SSE MCP response without exposing auth headers."""
    text_body = response.text.strip()
    if not text_body:
        raise RuntimeError("LingXing MCP returned an empty response")
    candidates = [text_body]
    candidates.extend(
        line[5:].strip()
        for line in text_body.splitlines()
        if line.startswith("data:") and line[5:].strip()
    )
    for candidate in reversed(candidates):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise RuntimeError("LingXing MCP returned invalid JSON")


def _lingxing_mcp_result(payload: dict[str, Any]) -> Any:
    protocol_error = payload.get("error")
    if isinstance(protocol_error, dict):
        message = protocol_error.get("message") or protocol_error.get("error") or "request failed"
        raise RuntimeError(f"LingXing MCP request failed: {message}")
    result = payload.get("result")
    if isinstance(result, dict):
        if result.get("isError"):
            text = _lingxing_mcp_content_text(result)
            business = None
            try:
                decoded = json.loads(text)
                business = decoded if isinstance(decoded, dict) else None
            except (json.JSONDecodeError, TypeError):
                business = None
            message = (_lingxing_mcp_business_error(business) if business else None) or text or "request failed"
            raise RuntimeError(f"LingXing MCP request failed: {message}")
        content = result.get("content") or []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                try:
                    business = json.loads(str(item.get("text") or ""))
                except json.JSONDecodeError as exc:
                    raise RuntimeError("LingXing MCP returned invalid business JSON") from exc
                break
        else:
            business = result.get("structuredContent")
    else:
        business = payload
    if not isinstance(business, dict):
        raise RuntimeError("LingXing MCP returned an invalid result")
    business_error = _lingxing_mcp_business_error(business)
    if business_error:
        message = business_error
        raise RuntimeError(f"LingXing MCP request failed: {message}")
    nested = business.get("data")
    if isinstance(nested, dict) and ("code" in nested or "success" in nested) and "data" in nested:
        if nested.get("success") is False or nested.get("code") == 0:
            message = nested.get("msg") or nested.get("message") or "request failed"
            raise RuntimeError(f"LingXing MCP request failed: {message}")
        return nested.get("data")
    return nested if nested is not None else business


async def lingxing_mcp_raw(
    operation: str,
    arguments: dict[str, Any],
    client: httpx.AsyncClient,
) -> Any:
    """Call a low-level LingXing MCP operation without hard-coded versions."""

    key = lingxing_mcp_key()
    if not key:
        raise RuntimeError("LINGXING_MCP_KEY missing")
    payload = {
        "jsonrpc": "2.0",
        "id": f"bi-dashboard-{time.time_ns()}",
        "method": "tools/call",
        "params": {"name": operation, "arguments": arguments},
    }
    response = await client.post(
        LINGXING_MCP_URL,
        json=payload,
        headers={"X-Mcp-Key": key, "Accept": "application/json, text/event-stream"},
    )
    response.raise_for_status()
    return _lingxing_mcp_result(_lingxing_mcp_json_body(response))


async def lingxing_mcp_metadata(
    tool_id: str,
    client: httpx.AsyncClient,
    force_refresh: bool = False,
) -> LingXingMCPMetadata:
    """Resolve a tool from the live catalog, with a verified version fallback.

    LingXing's catalog responses have changed shape at least once: ``help`` and
    ``search`` can describe a tool without returning the three version fields
    required by ``action``.  Prefer fresh catalog values whenever they exist,
    but keep the last production-verified tuple as a fallback instead of
    silently dropping the authoritative MCP data source.
    """

    async with _lingxing_mcp_metadata_lock:
        cached = _lingxing_mcp_metadata_cache.get(tool_id)
        if cached and not force_refresh and time.monotonic() - cached[0] < LINGXING_MCP_METADATA_TTL_SECONDS:
            return cached[1]
        if force_refresh:
            _lingxing_mcp_metadata_cache.clear()

        help_result: Any = None
        search_result: Any = None
        discovery_errors: list[str] = []
        try:
            help_result = await lingxing_mcp_raw("help", {"query": tool_id, "limit": 20}, client)
        except (RuntimeError, httpx.HTTPError) as exc:
            discovery_errors.append(f"help: {exc}")
        try:
            search_result = await lingxing_mcp_raw("search", {"toolId": tool_id}, client)
        except (RuntimeError, httpx.HTTPError) as exc:
            discovery_errors.append(f"search: {exc}")

        catalog_value = (
            _mcp_scalar(help_result, ("catalogVersion", "catalog_version"))
            or _mcp_scalar(search_result, ("catalogVersion", "catalog_version"))
        )
        tool_record = _mcp_tool_record(search_result, tool_id) or _mcp_tool_record(help_result, tool_id)
        version_source = tool_record or (search_result if isinstance(search_result, dict) else {})
        schema_value = _mcp_scalar(
            version_source,
            ("schemaVersion", "schema_version", "inputSchemaVersion", "input_schema_version"),
        )
        version_id_value = _mcp_scalar(
            version_source,
            ("toolVersionId", "tool_version_id", "latestToolVersionId", "latest_tool_version_id"),
        )
        missing_fields = [
            name
            for name, value in (
                ("catalogVersion", catalog_value),
                ("schemaVersion", schema_value),
                ("toolVersionId", version_id_value),
            )
            if value in (None, "")
        ]

        if not missing_fields:
            try:
                version_id = int(version_id_value)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"LingXing MCP metadata for {tool_id} has invalid toolVersionId"
                ) from exc
            metadata = LingXingMCPMetadata(
                tool_id=tool_id,
                schema_version=str(schema_value),
                tool_version_id=version_id,
                catalog_version=str(catalog_value),
            )
        else:
            known_version = LINGXING_MCP_KNOWN_VERSIONS.get(tool_id)
            if known_version is None:
                detail = "; ".join([*discovery_errors, f"missing {', '.join(missing_fields)}"])
                raise RuntimeError(f"LingXing MCP metadata for {tool_id} is incomplete: {detail}")
            print(
                f"LingXing MCP metadata fallback for {tool_id}: missing "
                f"{', '.join(missing_fields)}",
                flush=True,
            )
            metadata = LingXingMCPMetadata(
                tool_id=tool_id,
                catalog_version=known_version[0],
                schema_version=known_version[1],
                tool_version_id=known_version[2],
            )

        global _lingxing_mcp_catalog_version
        catalog_text = metadata.catalog_version
        if _lingxing_mcp_catalog_version and _lingxing_mcp_catalog_version != catalog_text:
            _lingxing_mcp_metadata_cache.clear()
        _lingxing_mcp_catalog_version = catalog_text
        _lingxing_mcp_metadata_cache[tool_id] = (time.monotonic(), metadata)
        return metadata


def _is_lingxing_catalog_update_error(error: RuntimeError) -> bool:
    message = str(error).lower()
    return "catalog" in message or "版本" in str(error) or "version" in message or "updated" in message or "expired" in message


async def lingxing_mcp_call(
    tool_id: str,
    params: dict[str, Any],
    client: httpx.AsyncClient,
) -> Any:
    metadata = await lingxing_mcp_metadata(tool_id, client)
    try:
        return await lingxing_mcp_raw(
            "action",
            {
                "catalogVersion": metadata.catalog_version,
                "schemaVersion": metadata.schema_version,
                "toolId": metadata.tool_id,
                "toolVersionId": metadata.tool_version_id,
                "params": params,
            },
            client,
        )
    except RuntimeError as exc:
        if not _is_lingxing_catalog_update_error(exc):
            raise

    # Catalog changes invalidate every cached tool version together, then the
    # failed action receives exactly one refreshed retry.
    metadata = await lingxing_mcp_metadata(tool_id, client, force_refresh=True)
    return await lingxing_mcp_raw(
        "action",
        {
            "catalogVersion": metadata.catalog_version,
            "schemaVersion": metadata.schema_version,
            "toolId": metadata.tool_id,
            "toolVersionId": metadata.tool_version_id,
            "params": params,
        },
        client,
    )


async def fetch_mcp_product_performance(
    sid: int,
    start_date: date,
    end_date: date,
    client: httpx.AsyncClient,
    asin_list: list[str] | None = None,
    currency_code: str | None = None,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "offset": 0,
        "length": 10000,
        "sort_field": "volume",
        "sort_type": "desc",
        "sids": str(sid),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "summary_field": "asin",
        "date_type": "purchase",
        "turn_on_summary": 1,
        "query_order_profit": True,
    }
    if currency_code:
        # Omitting this parameter silently requests LingXing's default CNY.
        params["currency_code"] = currency_code
    if asin_list is not None:
        params["search_field"] = "asin"
        params["search_value"] = asin_list
    data = await lingxing_mcp_call(
        LINGXING_MCP_PRODUCT_TOOL,
        params,
        client,
    )
    if isinstance(data, dict):
        rows = data.get("list") or data.get("rows") or data.get("records") or data.get("items") or []
    else:
        rows = data or []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


async def lingxing_mcp_shop_rows(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    cache_key = ("lingxing-mcp-ad-shops",)
    cached = _amazon_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < LINGXING_STORE_CACHE_TTL_SECONDS:
        return list(cached[1])
    data = await lingxing_mcp_call(
        LINGXING_MCP_SHOPS_TOOL,
        {},
        client,
    )
    rows = data if isinstance(data, list) else []
    _amazon_cache[cache_key] = (time.monotonic(), rows)
    return rows


@dataclass
class CampaignReportResult:
    rows: list[dict[str, Any]]
    expected: int | None
    pages: int
    profile_found: bool
    complete: bool
    error: str = ""


def _mcp_profile_id(row: dict[str, Any]) -> str:
    value = _mcp_scalar(row, ("profileId", "profile_id", "profileID", "amazonProfileId", "amazon_profile_id"))
    return str(value or "").strip()


def _campaign_report_page(data: Any) -> tuple[list[dict[str, Any]], int | None]:
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)], None
    if not isinstance(data, dict):
        return [], None
    batch = next((data[key] for key in ("data", "list", "rows", "records", "items") if isinstance(data.get(key), list)), None)
    expected_value = _mcp_scalar(data, ("recordsFiltered", "records_filtered", "recordsTotal", "records_total", "total", "totalCount", "total_count", "count"))
    try:
        expected = int(expected_value) if expected_value is not None else None
    except (TypeError, ValueError):
        expected = None
    return ([row for row in batch if isinstance(row, dict)] if isinstance(batch, list) else []), expected


async def fetch_mcp_campaign_report(
    sid: int,
    start_date: date,
    end_date: date,
    client: httpx.AsyncClient,
) -> CampaignReportResult:
    shops = await lingxing_mcp_shop_rows(client)
    profile_id = next((_mcp_profile_id(row) for row in shops if str(row.get("sid") or "") == str(sid) and _mcp_profile_id(row)), "")
    if not profile_id:
        return CampaignReportResult([], None, 0, False, False, f"LingXing MCP profile_id not found for sid {sid}")
    rows: list[dict[str, Any]] = []
    expected: int | None = None
    pages = 0
    errors: list[str] = []
    ended_with_short_page = False
    page = 1
    while True:
        try:
            data = await lingxing_mcp_call(
                LINGXING_MCP_CAMPAIGN_TOOL,
                {
                    "report_date": f"{start_date.isoformat()} - {end_date.isoformat()}",
                    "profile_ids": [profile_id],
                    "page": page,
                    "length": 100,
                    "sort_field": "clicks",
                    "sort_type": "desc",
                },
                client,
            )
        except (RuntimeError, httpx.HTTPError) as exc:
            errors.append(str(exc))
            break
        batch, page_expected = _campaign_report_page(data)
        if page_expected is not None:
            expected = page_expected
        pages += 1
        ended_with_short_page = len(batch) < 100
        if not batch:
            if expected is not None and len(rows) != expected:
                errors.append(f"LingXing MCP campaign report returned {len(rows)} of {expected} campaigns")
            break
        for row in batch:
            # The campaign report prepends an aggregate row with metrics but
            # no campaign id or name. It must not become a fake campaign.
            if ad_report_campaign_id(row) or ad_report_campaign_name(row):
                rows.append(row)
        if expected is not None and len(rows) >= expected:
            if len(rows) > expected:
                errors.append(f"LingXing MCP campaign report returned {len(rows)} campaigns; expected {expected}")
            break
        if len(batch) < 100:
            if expected is not None and len(rows) != expected:
                errors.append(f"LingXing MCP campaign report returned {len(rows)} of {expected} campaigns")
            break
        if page >= 50:
            errors.append("LingXing MCP campaign report exceeded the pagination safety limit")
            break
        page += 1
    # Newer MCP responses may omit recordsFiltered. A full page followed by a
    # short final page is still an exhaustive traversal; retain the explicit
    # count check whenever the upstream provides one.
    exhausted_without_total = expected is None and pages > 0 and ended_with_short_page
    complete = (
        (expected is not None and len(rows) == expected or exhausted_without_total)
        and not errors
    )
    return CampaignReportResult(rows, expected, pages, True, complete, "; ".join(errors))


def amazon_strategy_board_groups(
    aggregate: dict[tuple[str, str, str], dict[str, Any]],
    assignments: dict[tuple[str, str, str], dict[str, str]],
    notes: dict[tuple[date, str, str, str], str],
    selected_sites: list[str],
    selected_series: set[str] | None,
    week_scope: date,
) -> list[dict[str, Any]]:
    """Build strategy groups from campaigns, without seeded placeholder rows."""
    metric_names = ("impressions", "clicks", "ad_cost", "ad_sales", "ad_units", "ad_orders")
    strategies: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for (site_code, store_sid, campaign_id), item in aggregate.items():
        # Keep campaigns with zero clicks so the board remains a complete activity inventory.
        assignment = assignments.get((site_code, store_sid, campaign_id)) or assignments.get((site_code, "", campaign_id)) or {}
        strategy = assignment.get("strategy", "/")
        if strategy not in AMAZON_STRATEGY_OPTIONS:
            strategy = "/"
        series = assignment.get("series", "") if assignment.get("series", "") in AMAZON_SERIES else ""
        # Unassigned campaigns remain visible under the all-series view.
        if selected_series is not None and series not in selected_series:
            continue
        group_key = (site_code, strategy, series, "")
        group = strategies.get(group_key)
        if group is None:
            group = {
                "strategy": strategy,
                "series": series,
                "product": "",
                "note": notes.get((week_scope, site_code, series, strategy), ""),
                "metrics": {name: 0.0 for name in metric_names},
                "campaigns": [],
                "sites": [],
            }
            strategies[group_key] = group
        if item["site"] not in group["sites"]:
            group["sites"].append(item["site"])
        for name, value in item["metrics"].items():
            group["metrics"][name] += value
        group["campaigns"].append({
            "campaign_id": campaign_id,
            "campaign_name": item["campaign_name"],
            "site": item["site"],
            "site_code": site_code,
            "store_sid": store_sid,
            "store_name": item["store_name"],
            "ad_type": item["ad_type"],
            "series": series,
            "product": "",
            "strategy": strategy,
            "currency": item["currency"],
            "updated_at": item.get("updated_at"),
            "updated_by": item.get("updated_by"),
            **finalize_strategy_metrics(item["metrics"]),
        })

    output: list[dict[str, Any]] = []
    for site_name in selected_sites:
        site_code = strategy_site_code(site_name)
        for group in (group for key, group in strategies.items() if key[0] == site_code):
            group["campaigns"].sort(key=lambda campaign: (-campaign["clicks"], campaign["campaign_name"]))
            group["metrics"] = finalize_strategy_metrics(group["metrics"])
            group["site"] = site_name
            group["site_code"] = site_code
            group["currency"] = AMAZON_CURRENCY_CODES.get(site_name, "USD")
            output.append(group)
    output.sort(key=lambda group: (
        selected_sites.index(group["site"]),
        AMAZON_STRATEGY_OPTIONS.index(group["strategy"]),
        group.get("series") or "",
        group.get("product") or "",
    ))
    return output


def _strategy_campaign_data_quality(
    fetched: list[tuple[str, str, list[dict[str, Any]], list[dict[str, Any]]]],
) -> dict[str, Any]:
    qualities = [quality for _, _, _, account_quality in fetched for quality in account_quality]
    errors = [error for quality in qualities for error in quality.get("errors", [])]
    expected_values = [quality.get("campaign_expected") for quality in qualities]
    expected = sum(value for value in expected_values if value is not None) if qualities and all(value is not None for value in expected_values) else None
    type_counts = {ad_type: 0 for ad_type in ("SP", "SB", "SBV", "SD")}
    missing_type_rows = 0
    for _, _, raw_rows, _ in fetched:
        for raw in raw_rows:
            ad_type = ad_report_type(raw)
            if ad_type in type_counts:
                type_counts[ad_type] += 1
            else:
                missing_type_rows += 1
    if missing_type_rows:
        errors.insert(0, f"{missing_type_rows} campaign rows have no recognizable ad type")
    complete = (
        bool(qualities)
        and all(bool(quality.get("complete")) for quality in qualities)
        and missing_type_rows == 0
    )
    cacheable = complete and not errors and bool(qualities) and all(
        quality.get("source") == "mcp" for quality in qualities
    )
    return {
        "source": "mcp" if qualities and all(quality.get("source") == "mcp" for quality in qualities) else "openapi_fallback",
        "mcp_ok": bool(qualities) and all(bool(quality.get("mcp_ok")) for quality in qualities),
        "campaign_pages": sum(int(quality.get("campaign_pages") or 0) for quality in qualities),
        "campaign_rows": sum(int(quality.get("campaign_rows") or 0) for quality in qualities),
        "campaign_expected": expected,
        "profile_found": bool(qualities) and all(bool(quality.get("profile_found")) for quality in qualities),
        "types_present": all(type_counts[ad_type] > 0 for ad_type in type_counts),
        "type_counts": type_counts,
        "complete": complete,
        "errors": errors,
        "campaign_inventory_complete": complete,
        "cacheable": cacheable,
        "unassigned_campaigns_included": True,
    }


async def amazon_strategy_board_payload(
    start_date: date,
    end_date: date,
    selected_sites: list[str],
    sid_map: dict[str, Any],
    store_rows: list[dict[str, Any]] | None = None,
    selected_store_sids: set[str] | None = None,
    selected_series: set[str] | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Aggregate campaigns from LingXing's advertising backend by strategy.

    Strategy assignments are keyed by site and campaign id and therefore live
    independently from the selected date range.  The report itself remains a
    date-scoped snapshot, and campaigns with zero clicks are omitted.
    """
    selected_sites = amazon_dashboard_selected_sites(
        [str(value) for value in selected_sites]
    )
    selected_store_sids = {str(value) for value in (selected_store_sids or set()) if str(value).strip()}
    series_filter = None if selected_series is None else set(selected_series)
    cache_key = ("amazon-strategy-board-v5-currency-integrity", start_date.isoformat(), end_date.isoformat(), tuple(selected_sites), tuple(sorted(selected_store_sids)), tuple(sorted(series_filter or set())))
    if refresh:
        _amazon_cache.pop(cache_key, None)
    cached = _amazon_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < AMAZON_CACHE_TTL_SECONDS:
        # Sales endpoints enrich and reallocate rows after this function. A
        # cache hit must therefore return a private object; otherwise those
        # endpoint-level edits pollute the next viewer's raw periodic cache.
        return copy.deepcopy(cached[1])

    semaphore = asyncio.Semaphore(AMAZON_UPSTREAM_CONCURRENCY)
    async with httpx.AsyncClient(timeout=45) as client:
        async def fetch_site(site_name: str):
            site_code = strategy_site_code(site_name)
            accounts = amazon_sid_accounts(site_name, sid_map, store_rows)
            native_currency = AMAZON_CURRENCY_CODES.get(site_name, "USD")
            rows: list[dict[str, Any]] = []
            account_quality: list[dict[str, Any]] = []
            for account in accounts:
                account_sid = str(account["sid"])
                if selected_store_sids and account_sid not in selected_store_sids:
                    continue
                errors: list[str] = []
                mcp_result: CampaignReportResult | None = None
                if lingxing_mcp_key():
                    try:
                        mcp_result = await fetch_mcp_campaign_report(
                            int(account["sid"]), start_date, end_date, client
                        )
                    except (RuntimeError, httpx.HTTPError) as exc:
                        errors.append(str(exc))
                    if mcp_result and mcp_result.error:
                        errors.append(mcp_result.error)

                # OpenAPI is only an explicit degraded source. Its SP-detail
                # endpoint can enrich names/types and preserve visible rows,
                # but it can never turn an incomplete MCP inventory into a
                # complete SB/SBV/SD campaign list.
                fallback_rows: list[dict[str, Any]] = []
                metadata_rows: list[dict[str, Any]] = []
                if mcp_result is None or not mcp_result.complete:
                    try:
                        fallback_rows = await fetch_ad_reports_range(int(account["sid"]), start_date, end_date, client, semaphore)
                        metadata_rows = await fetch_ad_reports_range(int(account["sid"]), start_date, end_date, client, semaphore, show_detail=0)
                    except (RuntimeError, httpx.HTTPError) as exc:
                        errors.append(str(exc))
                metadata_by_id: dict[str, dict[str, Any]] = {}
                for metadata in metadata_rows:
                    metadata_id = ad_report_campaign_id(metadata)
                    if metadata_id and (ad_report_campaign_name(metadata) or ad_report_type(metadata)):
                        metadata_by_id.setdefault(metadata_id, metadata)

                combined_by_campaign: dict[str, dict[str, Any]] = {}
                source_rows = list(mcp_result.rows if mcp_result else []) + fallback_rows
                for row in source_rows:
                    campaign_key = ad_report_campaign_id(row) or ad_report_campaign_name(row)
                    if not campaign_key:
                        continue
                    metadata = metadata_by_id.get(ad_report_campaign_id(row))
                    merged = dict(metadata or {})
                    merged.update({key: value for key, value in row.items() if value not in (None, "", [], {})})
                    previous = combined_by_campaign.get(campaign_key)
                    if previous is not None:
                        previous_currency = ad_report_currency(previous, native_currency)
                        row_report_currency = ad_report_currency(row, native_currency)
                        if previous_currency != row_report_currency:
                            raise RuntimeError(
                                f"广告活动同一分组返回混合币种，拒绝相加：{site_name} "
                                f"{ad_report_campaign_name(row) or campaign_key} {previous_currency} / {row_report_currency}"
                            )
                    if previous is None or (mcp_result and row in mcp_result.rows):
                        combined_by_campaign[campaign_key] = merged
                account_rows = list(combined_by_campaign.values())
                for row in account_rows:
                    row["_store_sid"] = str(row.get("_store_sid") or account_sid)
                    row["_store_name"] = str(ad_report_store_name(row) or account.get("name") or account.get("account_name") or "未命名店铺")
                    row["_site_name"] = site_name
                    row["_ad_type"] = ad_report_type(row)
                rows.extend(account_rows)
                used_fallback = mcp_result is None or not mcp_result.complete
                account_quality.append({
                    "sid": account_sid,
                    "source": "openapi_fallback" if used_fallback else "mcp",
                    "mcp_ok": bool(mcp_result and mcp_result.complete),
                    "campaign_pages": mcp_result.pages if mcp_result else 0,
                    "campaign_rows": len(account_rows),
                    "campaign_expected": mcp_result.expected if mcp_result else None,
                    "profile_found": bool(mcp_result and mcp_result.profile_found),
                    "complete": bool(mcp_result and mcp_result.complete),
                    "errors": errors,
                })
            store_lookup = {str(item.get("sid")): item for item in (store_rows or []) if item.get("sid") is not None}
            for row in rows:
                row["_store_sid"] = str(row.get("_store_sid") or "")
                store = store_lookup.get(row["_store_sid"], {})
                row["_store_name"] = str(row.get("_store_name") or ad_report_store_name(row) or store.get("name") or store.get("account_name") or "未命名店铺")
                row["_site_name"] = site_name
                row["_ad_type"] = ad_report_type(row)
            if selected_store_sids:
                rows = [row for row in rows if str(row.get("_store_sid") or "") in selected_store_sids]
            return site_name, site_code, rows, account_quality

        fetched = await asyncio.gather(*(fetch_site(site_name) for site_name in selected_sites))

    assignments: dict[tuple[str, str, str], dict[str, str]] = {}
    assignment_metadata: dict[tuple[str, str, str], dict[str, str | None]] = {}
    notes: dict[tuple[date, str, str, str], str] = {}
    note_metadata: dict[tuple[date, str, str, str], dict[str, str | None]] = {}
    with session_factory()() as db:
        for item in db.scalars(select(AmazonCampaignAssignment).where(AmazonCampaignAssignment.site_code.in_([strategy_site_code(s) for s in selected_sites]))):
            exact_key = (item.site_code, str(item.store_sid), item.campaign_id)
            assignments[exact_key] = {
                "strategy": normalize_strategy(item.strategy),
                "series": item.series or "",
                "product": item.product or "",
                "campaign_name": item.campaign_name or "",
                "store_name": item.store_name or "",
            }
            assignment_metadata[exact_key] = edit_metadata(item)
        for item in db.scalars(select(AmazonCampaignStrategy).where(AmazonCampaignStrategy.site_code.in_([strategy_site_code(s) for s in selected_sites]))):
            assignments.setdefault((item.site_code, "", item.campaign_id), {
                "strategy": normalize_strategy(item.strategy), "series": "", "product": "", "campaign_name": item.campaign_name or "", "store_name": "",
            })
        for item in db.scalars(select(AmazonStrategyNote).where(AmazonStrategyNote.site_code.in_([strategy_site_code(s) for s in selected_sites]))):
            note_key = (item.week_start, item.site_code, item.series, normalize_strategy(item.strategy))
            notes[note_key] = item.note
            note_metadata[note_key] = edit_metadata(item)

    # A store-specific assignment must override a legacy site-wide assignment,
    # but an empty field on the specific row should not hide the useful legacy
    # value. Merge non-empty fields so old classifications continue to cover
    # new store accounts instead of creating false "unassigned" campaigns.
    for exact_key in list(assignments):
        _, store_sid, campaign_id = exact_key
        if not store_sid:
            continue
        generic = assignments.get((exact_key[0], "", campaign_id))
        if not generic:
            continue
        merged = dict(generic)
        merged.update({key: value for key, value in assignments[exact_key].items() if value not in ("", None)})
        assignments[exact_key] = merged

    aggregate: dict[tuple[str, str, str], dict[str, Any]] = {}
    for site_name, site_code, raw_rows, _ in fetched:
        for raw in raw_rows:
            campaign_id = strategy_campaign_id(raw)
            campaign_name = strategy_campaign_name(raw)
            store_sid = ad_report_store_sid(raw)
            assignment = assignments.get((site_code, store_sid, campaign_id)) or assignments.get((site_code, "", campaign_id)) or {}
            assignment_version = assignment_metadata.get((site_code, store_sid, campaign_id)) or assignment_metadata.get((site_code, "", campaign_id)) or {"updated_at": None, "updated_by": None}
            if campaign_name == "未命名广告活动" and assignment.get("campaign_name"):
                campaign_name = str(assignment["campaign_name"]).strip()
            if not campaign_id or not campaign_name:
                continue
            if campaign_name == "未命名广告活动":
                campaign_name = f"未命名广告活动 · {campaign_id}"
            metrics = strategy_metrics(raw)
            store_name = str(raw.get("_store_name") or "未命名店铺")
            key = (site_code, store_sid, campaign_id)
            default_currency = AMAZON_CURRENCY_CODES.get(site_name, "USD")
            row_currency = ad_report_currency(raw, default_currency)
            item = aggregate.setdefault(key, {"site": site_name, "site_code": site_code, "store_sid": store_sid, "store_name": store_name, "campaign_id": campaign_id, "campaign_name": campaign_name, "ad_type": ad_report_type(raw), "metrics": {name: 0.0 for name in ("impressions", "clicks", "ad_cost", "ad_sales", "ad_units", "ad_orders")}, "currency": row_currency})
            item.update(assignment_version)
            if str(item.get("currency") or "") != row_currency:
                raise RuntimeError(
                    f"广告活动同一分组返回混合币种，拒绝相加：{site_name} {campaign_name} "
                    f"{item.get('currency')} / {row_currency}"
                )
            if campaign_name != "未命名广告活动":
                item["campaign_name"] = campaign_name
            if item["ad_type"] == "" and ad_report_type(raw):
                item["ad_type"] = ad_report_type(raw)
            for name, value in metrics.items():
                item["metrics"][name] += value

    week_scope = normalize_week_start(start_date)
    unassigned_campaign_spend: dict[tuple[str, str], float] = {}
    for (site_code, store_sid, campaign_id), item in aggregate.items():
        assignment = assignments.get((site_code, store_sid, campaign_id)) or assignments.get((site_code, "", campaign_id)) or {}
        series = assignment.get("series", "") if assignment.get("series", "") in AMAZON_SERIES else ""
        if not series:
            key = (item["site"], str(item.get("currency") or "USD"))
            value = float(item.get("metrics", {}).get("ad_cost") or 0)
            if value:
                unassigned_campaign_spend[key] = unassigned_campaign_spend.get(key, 0.0) + value
    output = amazon_strategy_board_groups(aggregate, assignments, notes, selected_sites, series_filter, week_scope)
    data_quality = _strategy_campaign_data_quality(fetched)
    data_quality["unassigned_campaign_spend"] = {
        f"{site}|{currency}": value
        for (site, currency), value in unassigned_campaign_spend.items()
        if value > 0.01
    }
    campaign_version = max(
        assignment_metadata.values(),
        key=lambda value: parse_datetime(value.get("updated_at") or "") or datetime.min,
        default={"updated_at": None, "updated_by": None},
    )
    note_version = max(
        [metadata for key, metadata in note_metadata.items() if key[0] == week_scope],
        key=lambda value: parse_datetime(value.get("updated_at") or "") or datetime.min,
        default={"updated_at": None, "updated_by": None},
    )
    response = {"period": {"start": start_date.isoformat(), "end": end_date.isoformat()}, "strategies": output, "strategy_options": list(AMAZON_STRATEGY_OPTIONS), "series_options": list(AMAZON_SERIES), "product_options": list(AMAZON_PRODUCTS), "selected_sites": selected_sites, "data_quality": data_quality, "edit_versions": {"campaigns": campaign_version, "notes": note_version}}
    # A degraded OpenAPI response must not occupy the normal 10-minute slot.
    # Leaving it uncached makes the next request retry the authoritative MCP
    # inventory immediately after a catalog or upstream metadata failure.
    if data_quality.get("cacheable"):
        _amazon_cache[cache_key] = (time.monotonic(), response)
    return response


@app.get("/api/amazon/strategy-board")
async def amazon_strategy_board(
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    site: list[str] = Query(default=[]),
    store_sid: list[str] = Query(default=[]),
    series: list[str] = Query(default=[]),
    refresh: bool = Query(default=False),
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
):
    require_business_access(x_sync_key, allow_public=True)
    start_date, end_date = strategy_date_range(start_date, end_date)
    try:
        selected_sites = amazon_dashboard_sites_or_all(site)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if any(value not in AMAZON_SERIES for value in series):
        raise HTTPException(status_code=422, detail="广告策略看板系列参数无效")
    if not os.environ.get("LINGXING_APP_ID") or not os.environ.get("LINGXING_APP_SECRET"):
        raise HTTPException(status_code=503, detail="领星 API 尚未配置")
    try:
        sid_map = json.loads(os.environ.get("LINGXING_SIDS_JSON", "{}"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=503, detail="LINGXING_SIDS_JSON 配置格式错误") from exc
    store_rows = await lingxing_store_rows()
    try:
        return await amazon_strategy_board_payload(start_date, end_date, selected_sites, sid_map, store_rows, set(store_sid), set(series) if series else None, refresh)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"广告报表请求失败：HTTP {exc.response.status_code}") from exc
    except RuntimeError as exc:
        message = str(exc)
        if "频繁" in message or "too frequent" in message.lower():
            raise HTTPException(status_code=429, detail="领星广告接口限流，请稍后重试") from exc
        raise HTTPException(status_code=502, detail=f"广告策略数据获取失败：{message}") from exc
    except Exception as exc:
        print(f"strategy-board error: {type(exc).__name__}: {exc}", flush=True)
        raise HTTPException(status_code=502, detail=f"广告策略数据获取失败：{type(exc).__name__}") from exc


@app.post("/api/amazon/strategy-board/campaign-strategy")
def save_campaign_strategy(
    payload: dict[str, Any] = Body(...),
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
    x_dashboard_editor: str | None = Header(default=None, alias="X-Dashboard-Editor"),
):
    require_business_access(x_sync_key, allow_public=False)
    site_code = str(payload.get("site_code") or "").strip().upper()
    store_sid = str(payload.get("store_sid") or "").strip()
    campaign_id = str(payload.get("campaign_id") or "").strip()
    strategy = normalize_strategy(payload.get("strategy"))
    series = str(payload.get("series") or "").strip()
    product = str(payload.get("product") or "").strip()
    if site_code not in AMAZON_SITE_CODES.values() or not store_sid or not campaign_id or strategy not in AMAZON_STRATEGY_OPTIONS:
        raise HTTPException(status_code=422, detail="广告活动策略参数无效")
    if series and series not in AMAZON_SERIES:
        raise HTTPException(status_code=422, detail="广告活动系列参数无效")
    validate_strategy_series(strategy, series)
    if product and product not in AMAZON_PRODUCTS:
        raise HTTPException(status_code=422, detail="广告活动产品参数无效")
    with session_factory()() as db:
        item = db.scalar(select(AmazonCampaignAssignment).where(AmazonCampaignAssignment.site_code == site_code, AmazonCampaignAssignment.store_sid == store_sid, AmazonCampaignAssignment.campaign_id == campaign_id))
        ensure_edit_freshness(item, payload.get("base_updated_at"), force=bool(payload.get("force")))
        if item is None:
            item = AmazonCampaignAssignment(site_code=site_code, store_sid=store_sid, campaign_id=campaign_id)
            db.add(item)
        item.campaign_name = str(payload.get("campaign_name") or "")[:500]
        item.store_name = str(payload.get("store_name") or "")[:255]
        item.strategy = strategy
        item.series = series
        item.product = product
        item.updated_at = utcnow()
        item.updated_by = dashboard_request_editor(x_dashboard_editor)
        db.commit()
    _amazon_cache.clear_namespaces("amazon-strategy-board", "amazon-sales-targets")
    return {"ok": True, "site_code": site_code, "store_sid": store_sid, "campaign_id": campaign_id, "strategy": strategy, "series": series, "product": product, **edit_metadata(item)}


@app.post("/api/amazon/strategy-board/campaign-strategy/batch")
def save_campaign_strategies(
    payload: dict[str, Any] = Body(...),
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
    x_dashboard_editor: str | None = Header(default=None, alias="X-Dashboard-Editor"),
):
    """Persist all visible campaign classifications in one transaction."""
    require_business_access(x_sync_key, allow_public=False)
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise HTTPException(status_code=422, detail="至少需要一条广告活动分类")
    normalized: dict[tuple[str, str, str], dict[str, str]] = {}
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise HTTPException(status_code=422, detail="广告活动分类格式无效")
        site_code = str(raw.get("site_code") or "").strip().upper()
        store_sid = str(raw.get("store_sid") or "").strip()
        campaign_id = str(raw.get("campaign_id") or "").strip()
        strategy = normalize_strategy(raw.get("strategy"))
        series = str(raw.get("series") or "").strip()
        product = str(raw.get("product") or "").strip()
        if site_code not in AMAZON_SITE_CODES.values() or not store_sid or not campaign_id or strategy not in AMAZON_STRATEGY_OPTIONS:
            raise HTTPException(status_code=422, detail="广告活动策略参数无效")
        if series and series not in AMAZON_SERIES:
            raise HTTPException(status_code=422, detail="广告活动系列参数无效")
        validate_strategy_series(strategy, series)
        if product and product not in AMAZON_PRODUCTS:
            raise HTTPException(status_code=422, detail="广告活动产品参数无效")
        normalized[(site_code, store_sid, campaign_id)] = {
            "site_code": site_code,
            "store_sid": store_sid,
            "campaign_id": campaign_id,
            "campaign_name": str(raw.get("campaign_name") or "")[:500],
            "store_name": str(raw.get("store_name") or "")[:255],
            "strategy": strategy,
            "series": series,
            "product": product,
        }
    with session_factory()() as db:
        site_codes = {item["site_code"] for item in normalized.values()}
        existing = db.scalars(select(AmazonCampaignAssignment).where(AmazonCampaignAssignment.site_code.in_(site_codes))).all()
        by_key = {(item.site_code, str(item.store_sid), item.campaign_id): item for item in existing}
        force = bool(payload.get("force"))
        scope_base_updated_at = payload.get("base_updated_at")
        for values in normalized.values():
            key = (values["site_code"], values["store_sid"], values["campaign_id"])
            item = by_key.get(key)
            ensure_edit_freshness(item, values.get("base_updated_at") or scope_base_updated_at, force=force)
            if item is None:
                item = AmazonCampaignAssignment(site_code=values["site_code"], store_sid=values["store_sid"], campaign_id=values["campaign_id"])
                db.add(item)
                by_key[key] = item
            if values["campaign_name"]:
                item.campaign_name = values["campaign_name"]
            if values["store_name"]:
                item.store_name = values["store_name"]
            item.strategy = values["strategy"]
            item.series = values["series"]
            item.product = values["product"]
            item.updated_at = utcnow()
            item.updated_by = dashboard_request_editor(x_dashboard_editor)
        db.commit()
    _amazon_cache.clear_namespaces("amazon-strategy-board", "amazon-sales-targets")
    latest = max(by_key.values(), key=lambda item: item.updated_at or datetime.min, default=None)
    return {"ok": True, "saved": len(normalized), **edit_metadata(latest)}


@app.post("/api/amazon/strategy-board/notes/batch")
def save_strategy_notes_batch(
    payload: dict[str, Any] = Body(...),
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
    x_dashboard_editor: str | None = Header(default=None, alias="X-Dashboard-Editor"),
):
    require_business_access(x_sync_key, allow_public=False)
    try:
        week_start = normalize_week_start(date.fromisoformat(str(payload.get("week_start") or "")))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="策略备注周格式无效") from exc
    items = payload.get("items")
    if not isinstance(items, list) or len(items) > 500:
        raise HTTPException(status_code=422, detail="策略备注批量参数无效")
    if not items:
        raise HTTPException(status_code=422, detail="策略备注批量保存内容不能为空")
    normalized = []
    normalized_keys: set[tuple[str, str, str]] = set()
    for raw in items:
        if not isinstance(raw, dict):
            raise HTTPException(status_code=422, detail="策略备注批量参数无效")
        site_code = str(raw.get("site_code") or "").strip().upper()
        series = str(raw.get("series") or "").strip()
        strategy = normalize_strategy(raw.get("strategy"))
        if site_code not in AMAZON_SITE_CODES.values() or series not in ("", *AMAZON_SERIES) or strategy not in AMAZON_STRATEGY_OPTIONS:
            raise HTTPException(status_code=422, detail="策略备注参数无效")
        key = (site_code, series, strategy)
        if key in normalized_keys:
            raise HTTPException(status_code=422, detail="策略备注存在重复的策略/系列保存键")
        normalized_keys.add(key)
        normalized.append((site_code, series, strategy, str(raw.get("note") or "")))
    with session_factory()() as db:
        force = bool(payload.get("force"))
        scope_base_updated_at = payload.get("base_updated_at")
        saved_items: list[AmazonStrategyNote] = []
        for site_code, series, strategy, note in normalized:
            item = db.scalar(select(AmazonStrategyNote).where(AmazonStrategyNote.week_start == week_start, AmazonStrategyNote.site_code == site_code, AmazonStrategyNote.series == series, AmazonStrategyNote.strategy == strategy))
            ensure_edit_freshness(item, scope_base_updated_at, force=force)
            if item is None:
                item = AmazonStrategyNote(week_start=week_start, site_code=site_code, series=series, strategy=strategy)
                db.add(item)
            item.note = note
            item.updated_at = utcnow()
            item.updated_by = dashboard_request_editor(x_dashboard_editor)
            saved_items.append(item)
        db.commit()
    _amazon_cache.clear_namespaces("amazon-strategy-board", "amazon-sales-targets")
    latest = max(saved_items, key=lambda item: item.updated_at or datetime.min, default=None)
    return {"ok": True, "saved": len(normalized), "week_start": week_start.isoformat(), **edit_metadata(latest)}


@app.post("/api/amazon/strategy-board/note")
def save_strategy_note(
    payload: dict[str, Any] = Body(...),
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
    x_dashboard_editor: str | None = Header(default=None, alias="X-Dashboard-Editor"),
):
    require_business_access(x_sync_key, allow_public=False)
    site_code = str(payload.get("site_code") or "").strip().upper()
    try:
        week_start = normalize_week_start(date.fromisoformat(str(payload.get("week_start") or "")))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="策略备注周格式无效") from exc
    series = str(payload.get("series") or "").strip()
    strategy = normalize_strategy(payload.get("strategy"))
    note = str(payload.get("note") or "")
    if site_code not in AMAZON_SITE_CODES.values() or series not in ("", *AMAZON_SERIES) or strategy not in AMAZON_STRATEGY_OPTIONS:
        raise HTTPException(status_code=422, detail="策略备注参数无效")
    with session_factory()() as db:
        item = db.scalar(select(AmazonStrategyNote).where(AmazonStrategyNote.week_start == week_start, AmazonStrategyNote.site_code == site_code, AmazonStrategyNote.series == series, AmazonStrategyNote.strategy == strategy))
        ensure_edit_freshness(item, payload.get("base_updated_at"), force=bool(payload.get("force")))
        if item is None:
            item = AmazonStrategyNote(week_start=week_start, site_code=site_code, series=series, strategy=strategy)
            db.add(item)
        item.note = note
        item.updated_at = utcnow()
        item.updated_by = dashboard_request_editor(x_dashboard_editor)
        db.commit()
    _amazon_cache.clear_namespaces("amazon-strategy-board", "amazon-sales-targets")
    return {"ok": True, "week_start": week_start.isoformat(), "site_code": site_code, "series": series, "strategy": strategy, "note": note, **edit_metadata(item)}


def normalize_week_start(value: date | None) -> date:
    selected = value or datetime.now(timezone.utc).date()
    return selected - timedelta(days=selected.weekday())


def normalize_keyword_week_start(value: date | None) -> date:
    """Return the Sunday-based week required by Xiyou's weekly ABA API."""

    selected = value or datetime.now(timezone.utc).date()
    return selected - timedelta(days=(selected.weekday() + 1) % 7)


def amazon_week_label(period_start: str) -> str | None:
    """Return the ISO week label used consistently by the ad charts."""

    try:
        week = date.fromisoformat(period_start).isocalendar()[1]
    except ValueError:
        return None
    return f"W{week:02d}"


def normalize_keyword(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def normalize_keyword_lookup(value: Any) -> str:
    return normalize_keyword(value).casefold()


def keyword_dashboard_site(site: Any) -> str:
    normalized = str(site or "").strip()
    if normalized not in AMAZON_SITE_CODES:
        raise ValueError("关键词看板站点无效")
    return normalized


def keyword_week_columns(start: date, end: date) -> list[dict[str, str]]:
    """Build Sunday-to-Saturday columns and label them by their ISO week."""

    return [
        {
            "start": (start + timedelta(days=offset * 7)).isoformat(),
            "end": (start + timedelta(days=offset * 7 + 6)).isoformat(),
            "label": amazon_week_label((start + timedelta(days=offset * 7 + 6)).isoformat()) or "",
        }
        for offset in range((end - start).days // 7 + 1)
    ]


def latest_completed_keyword_week_start(selected: date | datetime | None = None) -> date:
    """Return the most recent complete Sunday-to-Saturday Xiyou week.

    Xiyou rejects a range containing the in-progress week with
    ``InvalidTrendsRange``.  The keyword dashboard deliberately treats a week as
    complete only after its Saturday has passed.
    """

    selected = selected.date() if isinstance(selected, datetime) else (selected or datetime.now(timezone.utc).date())
    current_start = normalize_keyword_week_start(selected)
    return current_start - timedelta(days=7)


class XiyouInvalidTrendRangeError(RuntimeError):
    """Xiyou rejected a range because one of its weeks is unavailable."""


@dataclass(frozen=True)
class XiyouWeeklyFetchOutcome:
    """Describe exactly which cells can safely be persisted."""

    records: list[dict[str, Any]]
    successful_weeks: tuple[date, ...]
    unavailable_weeks: tuple[date, ...]
    request_count: int
    errors: tuple[str, ...] = ()


def keyword_unavailable_weeks(
    site_code: str,
    weeks: list[dict[str, str]],
    *,
    now: datetime | None = None,
) -> set[date]:
    """Return recent Xiyou week failures without repeatedly probing them."""

    current = now or datetime.now(timezone.utc)
    selected = {date.fromisoformat(week["start"]) for week in weeks}
    with _keyword_unavailable_weeks_lock:
        expired = [
            key for key, failed_at in _keyword_unavailable_weeks.items()
            if current - failed_at >= timedelta(seconds=KEYWORD_UNAVAILABLE_WEEK_TTL_SECONDS)
        ]
        for key in expired:
            _keyword_unavailable_weeks.pop(key, None)
        return {
            week for site, week in _keyword_unavailable_weeks
            if site == site_code and week in selected
        }


def remember_keyword_unavailable_weeks(
    site_code: str,
    weeks: list[date],
    *,
    now: datetime | None = None,
) -> None:
    """Remember unavailable weeks briefly; refresh can explicitly clear this."""

    failed_at = now or datetime.now(timezone.utc)
    with _keyword_unavailable_weeks_lock:
        for week in weeks:
            _keyword_unavailable_weeks[(site_code, week)] = failed_at


def clear_keyword_unavailable_weeks(site_code: str) -> None:
    with _keyword_unavailable_weeks_lock:
        for site, week in list(_keyword_unavailable_weeks):
            if site == site_code:
                del _keyword_unavailable_weeks[(site, week)]


def keyword_date_value(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()[:10].replace(".", "-").replace("/", "-")
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def xiyou_business_payload(payload: dict[str, Any]) -> Any:
    """Return Xiyou's business data while turning API errors into text."""

    code = payload.get("code")
    success = payload.get("success")
    successful_code = code in (None, 0, 200, "0", "200", "success", "SUCCESS")
    if success is False or (code is not None and not successful_code):
        message = payload.get("msg") or payload.get("message") or f"错误码 {code}"
        raise RuntimeError(f"西柚接口返回错误：{message}")
    data = payload.get("data", payload.get("entities", payload))
    if isinstance(data, dict):
        for key in ("entities", "list", "records", "rows", "items", "data"):
            if key in data:
                return data[key]
    return data


def xiyou_weekly_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse Xiyou's weekly ABA response without guessing a single schema.

    The endpoint documentation exposes ``searchFrequencyRank`` and
    ``weeklySearchVolume``.  Its nesting, however, has changed between API
    revisions, so walk the business payload and keep every dated record that
    carries either field.
    """

    records: list[dict[str, Any]] = []

    def first_value(source: dict[str, Any], keys: tuple[str, ...]) -> Any:
        for key in keys:
            if key in source and source[key] not in (None, ""):
                return source[key]
        return None

    def walk(value: Any, term_hint: str | None = None) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item, term_hint)
            return
        if not isinstance(value, dict):
            return
        term = first_value(value, ("searchTerm", "searchTerms", "keyword", "keywordName")) or term_hint
        term = normalize_keyword(term) if term else term_hint
        nested = False
        for key in ("weeklyData", "weekly_data", "trends", "list", "records", "rows", "items", "data"):
            child = value.get(key)
            if isinstance(child, (list, dict)):
                nested = True
                walk(child, term)
        rank = first_value(value, ("searchFrequencyRank", "searchFrequencyRankWeekly", "rank"))
        volume = first_value(value, ("weeklySearchVolume", "searchVolumeWeekly", "searchVolume"))
        report_date = keyword_date_value(first_value(value, ("reportFromDate", "startDate", "reportStart", "weekStart")))
        if report_date and (rank is not None or volume is not None):
            try:
                rank_number = int(rank) if rank is not None else None
            except (TypeError, ValueError):
                rank_number = None
            try:
                volume_number = int(volume) if volume is not None else None
            except (TypeError, ValueError):
                volume_number = None
            if rank_number is not None or volume_number is not None:
                records.append({
                    "keyword": normalize_keyword(term or ""),
                    "week_start": normalize_keyword_week_start(report_date),
                    "rank": rank_number,
                    "volume": volume_number,
                })
        if not nested:
            for key in ("searchFrequencyRankList", "weeklySearchVolumeList", "weeklyList"):
                if isinstance(value.get(key), list):
                    walk(value[key], term)

    walk(xiyou_business_payload(payload))
    return records


def keyword_dashboard_rows(
    terms: list[KeywordDashboardTerm],
    weeks: list[dict[str, str]],
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_term: dict[str, dict[date, dict[str, int | None]]] = {
        normalize_keyword_lookup(term.keyword): {} for term in terms
    }
    for record in records:
        term = by_term.get(normalize_keyword_lookup(record.get("keyword")))
        if term is None:
            continue
        # Prefer a rank and later a volume; overwrite only when the new record
        # supplies a value that the previous one did not have.
        current = term.setdefault(record["week_start"], {"rank": None, "volume": None})
        for metric in ("rank", "volume"):
            value = record.get(metric)
            if value is not None:
                current[metric] = value

    output: list[dict[str, Any]] = []
    for term in terms:
        values = by_term[normalize_keyword_lookup(term.keyword)]
        weekly = [
            {
                "week_start": week["start"],
                "week_end": week["end"],
                "label": week["label"],
                "search_volume": values.get(date.fromisoformat(week["start"]), {}).get("volume"),
                "search_rank": values.get(date.fromisoformat(week["start"]), {}).get("rank"),
            }
            for week in weeks
        ]
        present_ranks = [item["search_rank"] for item in weekly if item["search_rank"] is not None]
        rank_change = None
        if len(present_ranks) >= 2:
            rank_change = present_ranks[-1] - present_ranks[-2]
        latest_rank = present_ranks[-1] if present_ranks else None
        output.append({
            "id": term.id,
            "site_code": term.site_code,
            "category": term.category,
            "keyword": term.keyword,
            "sort_order": term.sort_order,
            "weekly": weekly,
            "rank_change": rank_change,
            "latest_search_rank": latest_rank,
        })
    category_order = {category: index for index, category in enumerate(KEYWORD_CATEGORIES)}
    output.sort(key=lambda row: (
        category_order.get(row["category"], len(KEYWORD_CATEGORIES)),
        row["latest_search_rank"] is None,
        row["latest_search_rank"] if row["latest_search_rank"] is not None else 0,
        normalize_keyword_lookup(row["keyword"]),
    ))
    return output


def keyword_history_index(
    rows: list[KeywordDashboardWeeklyRecord],
) -> dict[str, dict[date, KeywordDashboardWeeklyRecord]]:
    history: dict[str, dict[date, KeywordDashboardWeeklyRecord]] = {}
    for row in rows:
        history.setdefault(normalize_keyword_lookup(row.keyword), {})[row.week_start] = row
    return history


@contextmanager
def keyword_dashboard_cross_process_lock():
    """Prevent duplicate Xiyou credit use across deployment workers.

    The in-process asyncio lock cannot coordinate multiple IdeaDock workers.
    MySQL named locks follow the underlying connection, so hold this dedicated
    connection until all missing cells in the request have been persisted.
    A zero-second timeout lets another viewer return cached rows immediately
    instead of stacking requests behind a potentially slow API fetch.
    """

    with engine().connect() as connection:
        acquired = connection.execute(
            text("SELECT GET_LOCK(:lock_name, 0)"),
            {"lock_name": KEYWORD_DASHBOARD_FETCH_LOCK_NAME},
        ).scalar()
        if acquired != 1:
            yield False
            return
        try:
            yield True
        finally:
            connection.execute(
                text("SELECT RELEASE_LOCK(:lock_name)"),
                {"lock_name": KEYWORD_DASHBOARD_FETCH_LOCK_NAME},
            )


def keyword_fetch_groups(
    terms: list[KeywordDashboardTerm],
    weeks: list[dict[str, str]],
    history: dict[str, dict[date, KeywordDashboardWeeklyRecord]],
    *,
    refresh: bool,
    unavailable_weeks: set[date] | None = None,
) -> list[tuple[date, date, list[str]]]:
    """Build exact consecutive missing-week runs, chunked for Xiyou limits."""

    by_run: dict[tuple[date, date], set[str]] = {}
    skipped_weeks = set() if refresh else (unavailable_weeks or set())
    week_dates = [
        date.fromisoformat(week["start"]) for week in weeks
        if date.fromisoformat(week["start"]) not in skipped_weeks
    ]
    for term in terms:
        keyword = term.keyword
        missing = [
            week
            for week in week_dates
            if refresh or week not in history.get(normalize_keyword_lookup(keyword), {})
        ]
        if not missing:
            continue
        run_start = missing[0]
        previous = missing[0]
        runs: list[tuple[date, date]] = []
        for week in missing[1:]:
            if (week - previous).days != 7:
                runs.append((run_start, previous))
                run_start = week
            previous = week
        runs.append((run_start, previous))

        for run_end_start, run_end in runs:
            cursor = run_end_start
            while cursor <= run_end:
                chunk_end = min(cursor + timedelta(days=7 * (KEYWORD_FETCH_WEEK_CHUNK - 1)), run_end)
                by_run.setdefault((cursor, chunk_end), set()).add(keyword)
                cursor = chunk_end + timedelta(days=7)

    groups: list[tuple[date, date, list[str]]] = []
    for (range_start, range_end), keyword_set in sorted(by_run.items()):
        keywords = sorted(keyword_set, key=normalize_keyword_lookup)
        for index in range(0, len(keywords), KEYWORD_FETCH_TERM_BATCH_SIZE):
            groups.append((range_start, range_end, keywords[index:index + KEYWORD_FETCH_TERM_BATCH_SIZE]))
    return groups


def persist_keyword_weekly_records(
    db: Session,
    site_code: str,
    terms: list[str],
    weeks: list[dict[str, str]],
    records: list[dict[str, Any]],
    *,
    fetched_at: datetime,
) -> None:
    """Upsert actual values and mark successful no-data keyword/week cells."""

    keyword_set = {normalize_keyword_lookup(term) for term in terms}
    week_set = {date.fromisoformat(week["start"]) for week in weeks}
    existing = db.scalars(
        select(KeywordDashboardWeeklyRecord)
        .where(KeywordDashboardWeeklyRecord.site_code == site_code)
        .where(KeywordDashboardWeeklyRecord.week_start >= min(week_set))
        .where(KeywordDashboardWeeklyRecord.week_start <= max(week_set))
    ).all()
    existing_index = keyword_history_index(existing)
    actual: set[tuple[str, date]] = set()

    def row_for(keyword: str, week: date) -> KeywordDashboardWeeklyRecord | None:
        return existing_index.get(normalize_keyword_lookup(keyword), {}).get(week)

    for record in records:
        keyword = normalize_keyword_lookup(record.get("keyword"))
        week = record.get("week_start")
        if keyword not in keyword_set or week not in week_set:
            continue
        actual.add((keyword, week))
        stored = row_for(keyword, week)
        if stored is None:
            stored = KeywordDashboardWeeklyRecord(
                site_code=site_code,
                keyword=keyword,
                week_start=week,
                fetched_at=fetched_at,
            )
            db.add(stored)
            existing_index.setdefault(keyword, {})[week] = stored
        stored.keyword = keyword
        stored.search_rank = record.get("rank")
        stored.search_volume = record.get("volume")
        stored.fetched_at = fetched_at

    # A successful response without a term/week value is itself history: keep a
    # null marker so later views do not spend another credit on the same cell.
    for keyword in keyword_set:
        for week in week_set:
            if (keyword, week) in actual or row_for(keyword, week) is not None:
                continue
            db.add(KeywordDashboardWeeklyRecord(
                site_code=site_code,
                keyword=keyword,
                week_start=week,
                search_rank=None,
                search_volume=None,
                fetched_at=fetched_at,
            ))
    db.commit()


async def fetch_xiyou_weekly_records(
    country: str,
    terms: list[str],
    start: date,
    end: date,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    if not _xiyou_keyword_dashboard_scope.get():
        raise RuntimeError("西柚接口仅允许搜索词看板调用")
    api_key = os.environ.get("XIYOU_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("西柚 API Key 尚未配置")

    async def request(client_value: httpx.AsyncClient) -> list[dict[str, Any]]:
        response = await client_value.post(
            f"{XIYOU_API_BASE}/v1/searchTerms/abaReport/trends/weekly",
            headers={"X-Auth-Version": "2.0", "X-Api-Key": api_key},
            json={
                "country": country,
                "searchTerms": terms,
                "startWeek": {"startDate": start.isoformat(), "endDate": (start + timedelta(days=6)).isoformat()},
                "endWeek": {"startDate": end.isoformat(), "endDate": (end + timedelta(days=6)).isoformat()},
            },
        )
        if response.status_code == 401:
            raise RuntimeError("西柚 API Key 无效或未授权")
        if response.status_code == 402:
            raise RuntimeError("西柚接口余额不足或超出扣费限制")
        if response.status_code == 429:
            raise RuntimeError("西柚接口请求过于频繁，请稍后刷新")
        try:
            payload = response.json()
        except ValueError as exc:
            response.raise_for_status()
            raise RuntimeError("西柚接口返回了非 JSON 数据") from exc
        if response.status_code >= 400:
            error_code = str(payload.get("code") or "")
            if error_code == "InvalidTrendsRange":
                raise XiyouInvalidTrendRangeError("西柚接口暂不支持所选周范围")
            message = payload.get("msg") or payload.get("message") or response.text[:200]
            raise RuntimeError(f"西柚接口请求失败：HTTP {response.status_code} {message}")
        return xiyou_weekly_records(payload)

    if client is not None:
        return await request(client)
    async with httpx.AsyncClient(timeout=45) as created:
        return await request(created)


async def fetch_xiyou_weekly_records_with_recovery(
    country: str,
    terms: list[str],
    start: date,
    end: date,
    client: httpx.AsyncClient | None = None,
) -> XiyouWeeklyFetchOutcome:
    """Fetch a batch, then split an invalid multi-week range into single weeks.

    Xiyou returns ``InvalidTrendsRange`` when any week in a range is not
    published yet. Retrying the same range as individual weeks preserves every
    available week and identifies the exact week that must be skipped for a
    short period. Billing is based on keywords multiplied by weeks, so this
    fallback does not increase the theoretical Credit cost.
    """

    try:
        records = await fetch_xiyou_weekly_records(country, terms, start, end, client)
        successful_weeks = tuple(
            start + timedelta(days=7 * offset)
            for offset in range((end - start).days // 7 + 1)
        )
        return XiyouWeeklyFetchOutcome(records, successful_weeks, (), 1)
    except XiyouInvalidTrendRangeError:
        if start == end:
            return XiyouWeeklyFetchOutcome([], (), (start,), 1)

    records: list[dict[str, Any]] = []
    successful_weeks: list[date] = []
    unavailable_weeks: list[date] = []
    errors: list[str] = []
    request_count = 1
    cursor = start
    while cursor <= end:
        try:
            records.extend(await fetch_xiyou_weekly_records(country, terms, cursor, cursor, client))
            successful_weeks.append(cursor)
        except XiyouInvalidTrendRangeError:
            unavailable_weeks.append(cursor)
        except (httpx.HTTPError, RuntimeError) as exc:
            message = str(exc) or "西柚关键词数据获取失败"
            if message not in errors:
                errors.append(message)
        request_count += 1
        cursor += timedelta(days=7)
    return XiyouWeeklyFetchOutcome(
        records,
        tuple(successful_weeks),
        tuple(unavailable_weeks),
        request_count,
        tuple(errors),
    )


def keyword_terms_payload(rows: list[KeywordDashboardTerm]) -> list[dict[str, Any]]:
    return [
        {
            "id": row.id,
            "category": row.category,
            "keyword": row.keyword,
            "sort_order": row.sort_order,
            "enabled": row.enabled,
            **edit_metadata(row),
        }
        for row in rows
    ]


@app.get("/api/keyword-dashboard/terms")
def get_keyword_dashboard_terms(
    site: str = Query(default="美国"),
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
):
    require_business_access(x_sync_key, allow_public=True)
    try:
        site_name = keyword_dashboard_site(site)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    site_code = AMAZON_SITE_CODES[site_name]
    with session_factory()() as db:
        rows = db.scalars(
            select(KeywordDashboardTerm)
            .where(KeywordDashboardTerm.site_code == site_code)
            .order_by(KeywordDashboardTerm.sort_order, KeywordDashboardTerm.id)
        ).all()
        payload = keyword_terms_payload(rows)
        latest = max(rows, key=lambda row: row.updated_at or datetime.min, default=None)
    return {"site": site_name, "site_code": site_code, "terms": payload, **edit_metadata(latest)}


@app.post("/api/keyword-dashboard/terms")
def save_keyword_dashboard_terms(
    payload: dict[str, Any] = Body(...),
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
    x_dashboard_editor: str | None = Header(default=None, alias="X-Dashboard-Editor"),
):
    """Replace all keywords for one site so additions/deletes stay explicit."""

    require_business_access(x_sync_key, allow_public=False)
    global _amazon_cache
    try:
        site_name = keyword_dashboard_site(payload.get("site"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    site_code = AMAZON_SITE_CODES[site_name]
    raw_terms = payload.get("terms")
    if not isinstance(raw_terms, list):
        raise HTTPException(status_code=422, detail="关键词列表格式无效")
    if len(raw_terms) > 100:
        raise HTTPException(status_code=422, detail="每个站点最多支持 100 个关键词")

    normalized: list[tuple[str, str, int]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_terms):
        if not isinstance(raw, dict):
            raise HTTPException(status_code=422, detail="关键词格式无效")
        keyword = normalize_keyword(raw.get("keyword"))
        category = normalize_keyword(raw.get("category")) or "未分类"
        if not keyword:
            raise HTTPException(status_code=422, detail="关键词不能为空")
        if category not in KEYWORD_CATEGORIES:
            raise HTTPException(status_code=422, detail=f"关键词分类无效：{category}")
        if len(keyword) > 255 or len(category) > 80:
            raise HTTPException(status_code=422, detail="关键词或分类长度超出限制")
        lookup = normalize_keyword_lookup(keyword)
        if lookup in seen:
            raise HTTPException(status_code=422, detail=f"关键词重复：{keyword}")
        seen.add(lookup)
        try:
            sort_order = int(raw.get("sort_order", index))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="关键词排序无效") from None
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=422, detail="关键词启用状态无效")
        if enabled:
            normalized.append((category, keyword, sort_order))

    now = utcnow()
    with session_factory()() as db:
        current_rows = db.scalars(
            select(KeywordDashboardTerm).where(KeywordDashboardTerm.site_code == site_code)
        ).all()
        latest = max(current_rows, key=lambda row: row.updated_at or datetime.min, default=None)
        ensure_edit_freshness(latest, payload.get("base_updated_at"), force=bool(payload.get("force")))
        db.execute(delete(KeywordDashboardTerm).where(KeywordDashboardTerm.site_code == site_code))
        for category, keyword, sort_order in normalized:
            db.add(KeywordDashboardTerm(
                site_code=site_code,
                category=category,
                keyword=keyword,
                sort_order=sort_order,
                enabled=True,
                updated_at=now,
                updated_by=dashboard_request_editor(x_dashboard_editor),
            ))
        db.commit()
        rows = db.scalars(
            select(KeywordDashboardTerm)
            .where(KeywordDashboardTerm.site_code == site_code)
            .order_by(KeywordDashboardTerm.sort_order, KeywordDashboardTerm.id)
        ).all()
        saved = keyword_terms_payload(rows)

    latest = max(rows, key=lambda row: row.updated_at or datetime.min, default=None)
    return {"ok": True, "site": site_name, "site_code": site_code, "terms": saved, "saved": len(saved), **edit_metadata(latest)}


@app.get("/api/keyword-dashboard")
async def keyword_dashboard(
    site: str = Query(default="美国"),
    start_week: date | None = Query(default=None),
    end_week: date | None = Query(default=None),
    refresh: bool = Query(default=False),
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
):
    require_business_access(x_sync_key, allow_public=True)
    try:
        site_name = keyword_dashboard_site(site)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    site_code = AMAZON_SITE_CODES[site_name]
    try:
        start = normalize_keyword_week_start(start_week)
        end = normalize_keyword_week_start(end_week)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="关键词看板周格式无效") from exc
    if not start_week or not end_week or start > end:
        raise HTTPException(status_code=422, detail="关键词看板周范围无效")
    week_count = (end - start).days // 7 + 1
    if week_count > 52:
        raise HTTPException(status_code=422, detail="关键词看板最多支持 52 周")
    latest_completed_start = latest_completed_keyword_week_start()
    requested_start, requested_end = start, end
    # Xiyou rejects ranges containing an in-progress week. Clamping keeps API
    # callers and a page left open across a week boundary from repeatedly
    # spending credits on a range that can never return data.
    start = min(start, latest_completed_start)
    end = min(end, latest_completed_start)
    range_adjusted = (start, end) != (requested_start, requested_end)
    latest_completed_end = latest_completed_start + timedelta(days=6)
    requested_period = {
        "start": requested_start.isoformat(),
        "end": requested_end.isoformat(),
    }
    period = {"start": start.isoformat(), "end": end.isoformat()}
    latest_completed_week = {
        "start": latest_completed_start.isoformat(),
        "end": latest_completed_end.isoformat(),
    }
    range_adjustment = {
        "adjusted": range_adjusted,
        "requested_period": requested_period,
        "period": period,
        "latest_completed_week": latest_completed_week,
    }

    weeks = keyword_week_columns(start, end)
    with session_factory()() as db:
        terms = db.scalars(
            select(KeywordDashboardTerm)
            .where(KeywordDashboardTerm.site_code == site_code)
            .order_by(KeywordDashboardTerm.sort_order, KeywordDashboardTerm.id)
        ).all()
        term_rows = list(terms)
    term_payload = keyword_terms_payload(term_rows)
    if not term_rows:
        return {
            "site": site_name,
            "site_code": site_code,
            "period": period,
            "requested_period": requested_period,
            "latest_completed_week": latest_completed_week,
            "range_adjustment": range_adjustment,
            "warnings": [
                f"西柚当前最新可用周为 {latest_completed_start.isoformat()}~{latest_completed_end.isoformat()}，已自动切换；已抓取历史不会重复消耗 Credit。"
            ] if range_adjusted else [],
            "weeks": weeks,
            "terms": term_payload,
            "rows": [],
            "cached": False,
            "source": "xiyou",
            "field_availability": {"rank": False, "volume": False},
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
        }

    warnings: list[str] = []
    requested_fetch_count = 0
    fetched_record_count = 0
    fetch_in_progress_elsewhere = False
    if refresh:
        clear_keyword_unavailable_weeks(site_code)

    # Compute missing ranges while holding the fetch lock. A completed request
    # persists its history before the next viewer can compute its own gaps. The
    # MySQL named lock extends that guarantee to a future multi-worker deploy.
    with keyword_dashboard_cross_process_lock() as lock_acquired:
        if not lock_acquired:
            fetch_in_progress_elsewhere = True
            warnings.append("另一个请求正在抓取西柚数据，本次先展示已缓存数据。")
        else:
            async with _keyword_dashboard_fetch_lock:
                with session_factory()() as db:
                    history_rows = db.scalars(
                        select(KeywordDashboardWeeklyRecord)
                        .where(KeywordDashboardWeeklyRecord.site_code == site_code)
                        .where(KeywordDashboardWeeklyRecord.week_start >= start)
                        .where(KeywordDashboardWeeklyRecord.week_start <= end)
                    ).all()
                    history = keyword_history_index(list(history_rows))
                    unavailable_weeks = keyword_unavailable_weeks(site_code, weeks)
                    groups = keyword_fetch_groups(
                        term_rows,
                        weeks,
                        history,
                        refresh=refresh,
                        unavailable_weeks=unavailable_weeks,
                    )
                if unavailable_weeks:
                    unavailable_text = "、".join(week.isoformat() for week in sorted(unavailable_weeks))
                    warnings.append(
                        f"以下周西柚暂未发布或暂不支持：{unavailable_text}；已展示可用历史，点击刷新可重新检测。"
                    )
                for range_start, range_end, keywords in groups:
                    group_weeks = [
                        week for week in weeks
                        if range_start <= date.fromisoformat(week["start"]) <= range_end
                    ]
                    try:
                        outcome = await fetch_xiyou_weekly_records_with_recovery(
                            site_code,
                            keywords,
                            range_start,
                            range_end,
                        )
                        requested_fetch_count += outcome.request_count
                        fetched_record_count += len(outcome.records)
                        for message in outcome.errors:
                            if message not in warnings:
                                warnings.append(message)
                        if outcome.unavailable_weeks:
                            remember_keyword_unavailable_weeks(
                                site_code,
                                list(outcome.unavailable_weeks),
                            )
                            unavailable_text = "、".join(
                                week.isoformat() for week in outcome.unavailable_weeks
                            )
                            warning = (
                                f"以下周西柚暂未发布或暂不支持：{unavailable_text}；"
                                "已展示可用历史，点击刷新可重新检测。"
                            )
                            if warning not in warnings:
                                warnings.append(warning)
                        successful_weeks = {
                            date.fromisoformat(week["start"])
                            for week in group_weeks
                            if date.fromisoformat(week["start"]) in outcome.successful_weeks
                        }
                        if successful_weeks:
                            persist_weeks = [
                                week for week in group_weeks
                                if date.fromisoformat(week["start"]) in successful_weeks
                            ]
                            fetched_at = datetime.now(timezone.utc)
                            with session_factory()() as db:
                                persist_keyword_weekly_records(
                                    db,
                                    site_code,
                                    keywords,
                                    persist_weeks,
                                    outcome.records,
                                    fetched_at=fetched_at,
                                )
                    except (httpx.HTTPError, RuntimeError) as exc:
                        message = str(exc) or "西柚关键词数据获取失败"
                        if message not in warnings:
                            warnings.append(message)

    with session_factory()() as db:
        history_rows = db.scalars(
            select(KeywordDashboardWeeklyRecord)
            .where(KeywordDashboardWeeklyRecord.site_code == site_code)
            .where(KeywordDashboardWeeklyRecord.week_start >= start)
            .where(KeywordDashboardWeeklyRecord.week_start <= end)
        ).all()
    records = [
        {
            "keyword": row.keyword,
            "week_start": row.week_start,
            "rank": row.search_rank,
            "volume": row.search_volume,
        }
        for row in history_rows
        if row.search_rank is not None or row.search_volume is not None
    ]
    rows = keyword_dashboard_rows(term_rows, weeks, records)
    clamp_warning = (
        f"西柚当前最新可用周为 {latest_completed_start.isoformat()}~{latest_completed_end.isoformat()}，"
        "已自动切换；已抓取历史不会重复消耗 Credit。"
    )
    if range_adjusted and clamp_warning not in warnings:
        warnings.insert(0, clamp_warning)
    return {
        "site": site_name,
        "site_code": site_code,
        "period": period,
        "requested_period": requested_period,
        "latest_completed_week": latest_completed_week,
        "range_adjustment": range_adjustment,
        "weeks": weeks,
        "terms": term_payload,
        "rows": rows,
        "cached": requested_fetch_count == 0 and not fetch_in_progress_elsewhere,
        "fetch_in_progress": fetch_in_progress_elsewhere,
        "history_cell_count": len(history_rows),
        "requested_fetch_count": requested_fetch_count,
        "fetched_record_count": fetched_record_count,
        "warnings": warnings,
        "source": "xiyou",
        "field_availability": {
            "rank": any(item["search_rank"] is not None for row in rows for item in row["weekly"]),
            "volume": any(item["search_volume"] is not None for row in rows for item in row["weekly"]),
        },
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
    }


def amazon_ads_chart_rows(
    periodic: dict[str, Any],
    range_start: date | None = None,
    range_end: date | None = None,
) -> list[dict[str, Any]]:
    """Collapse product/site rows into the three weekly ad-chart datasets."""

    additive = ("net_sales", "ad_sales", "ad_cost", "clicks", "ad_orders", "sessions", "page_views")
    currencies = {
        str(source.get("currency") or "").strip().upper()
        for source in periodic.get("rows", [])
        if str(source.get("currency") or "").strip()
    }
    if len(currencies) > 1:
        raise ValueError(f"广告周度图表存在多币种，拒绝相加：{', '.join(sorted(currencies))}")
    grouped: dict[str, dict[str, Any]] = {}
    for source in periodic.get("rows", []):
        period_start = str(source.get("period_start") or "")
        period_end = str(source.get("period_end") or "")
        if not period_start or not period_end:
            continue
        item = grouped.setdefault(source["period_start"], {
            "period": source.get("period") or period_start,
            "period_start": period_start,
            "period_end": period_end,
            **{key: 0.0 for key in additive},
            **{f"{key}_present": False for key in additive},
        })
        for key in additive:
            value = source.get(key)
            if value is not None:
                item[key] += float(value)
                item[f"{key}_present"] = True

    requested_weeks: list[date] | None = None
    if range_start is not None and range_end is not None:
        normalized_start = normalize_week_start(range_start)
        normalized_end = normalize_week_start(range_end)
        if normalized_start > normalized_end:
            raise ValueError("广告周度图表周范围无效")
        requested_weeks = [
            normalized_start + timedelta(days=offset * 7)
            for offset in range((normalized_end - normalized_start).days // 7 + 1)
        ]

    output: list[dict[str, Any]] = []
    grouped_items = (
        [grouped.get(week.isoformat()) for week in requested_weeks]
        if requested_weeks is not None
        else sorted(grouped.values(), key=lambda row: row["period_start"])
    )
    for index, item in enumerate(grouped_items):
        if item is None:
            week_start_value = requested_weeks[index] if requested_weeks is not None else None
            if week_start_value is None:
                continue
            week_end_value = week_start_value + timedelta(days=6)
            output.append({
                "period": f"{week_start_value.isoformat()}~{week_end_value.isoformat()}",
                "period_start": week_start_value.isoformat(),
                "period_end": week_end_value.isoformat(),
                "week_label": amazon_week_label(week_start_value.isoformat()),
                "net_sales": None,
                "ad_sales": None,
                "ad_cost": None,
                "fee_ratio": None,
                "clicks": None,
                "ad_orders": None,
                "ad_cvr": None,
                "sessions": None,
                "page_views": None,
            })
            continue
        net_sales = item["net_sales"] if item["net_sales_present"] else None
        ad_sales = item["ad_sales"] if item["ad_sales_present"] else None
        ad_cost = item["ad_cost"] if item["ad_cost_present"] else None
        clicks = item["clicks"] if item["clicks_present"] else None
        ad_orders = item["ad_orders"] if item["ad_orders_present"] else None
        sessions = item["sessions"] if item["sessions_present"] else None
        page_views = item["page_views"] if item["page_views_present"] else None
        output.append({
            "period": item["period"],
            "period_start": item["period_start"],
            "period_end": item["period_end"],
            "week_label": amazon_week_label(item["period_start"]),
            "net_sales": net_sales,
            "ad_sales": ad_sales,
            "ad_cost": ad_cost,
            "fee_ratio": ad_cost / net_sales if ad_cost is not None and net_sales is not None and net_sales != 0 else None,
            "clicks": int(clicks) if clicks is not None else None,
            "ad_orders": int(ad_orders) if ad_orders is not None else None,
            "ad_cvr": ad_orders / clicks if ad_orders is not None and clicks is not None and clicks != 0 else None,
            "sessions": int(sessions) if sessions is not None else None,
            "page_views": int(page_views) if page_views is not None else None,
        })
    return output


@app.get("/api/amazon/ads-charts")
async def amazon_ads_charts(
    start_week: date | None = Query(default=None),
    end_week: date | None = Query(default=None),
    site: str = Query(default=AMAZON_SALES_ALL_SITES),
    model: str = Query(default="TN10"),
    refresh: bool = Query(default=False),
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
):
    """Return weekly sales, conversion, and traffic charts for one model."""

    require_business_access(x_sync_key, allow_public=True)
    try:
        start = normalize_week_start(start_week)
        end = normalize_week_start(end_week)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="广告图表周格式无效") from exc
    if not start_week or not end_week or start > end:
        raise HTTPException(status_code=422, detail="广告图表周范围无效")
    if (end - start).days + 7 > AMAZON_MAX_DATE_RANGE_DAYS:
        raise HTTPException(status_code=422, detail=f"广告图表周范围无效，最多支持 {AMAZON_MAX_DATE_RANGE_DAYS} 天")
    model = model.strip().upper()
    if model not in AMAZON_SALES_MODEL_CHOICES:
        raise HTTPException(status_code=422, detail="广告图表型号无效")
    try:
        selected_sites = amazon_sales_selected_sites(site)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not os.environ.get("LINGXING_APP_ID") or not os.environ.get("LINGXING_APP_SECRET"):
        raise HTTPException(status_code=503, detail="领星 API 尚未配置")

    products, selected_series = amazon_sales_scope(model)
    if not products or not selected_series:
        raise HTTPException(status_code=422, detail="广告图表型号暂无产品映射")
    try:
        sid_map = json.loads(os.environ.get("LINGXING_SIDS_JSON", "{}"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=503, detail="LINGXING_SIDS_JSON 配置格式错误") from exc
    if not sid_map:
        for store_item in await lingxing_store_rows():
            country = str(store_item.get("country") or "")
            sid = store_item.get("sid")
            if country and sid and int(store_item.get("status") or 0) == 1:
                sid_map.setdefault(country, {"sid": sid})
    store_rows = []
    if any(site_name == "日本" for site_name in selected_sites) or not sid_map:
        store_rows = await lingxing_store_rows()
    if refresh:
        _amazon_cache.clear_current_namespace()

    site_today = min(
        datetime.now(ZoneInfo(AMAZON_SITE_TIMEZONES.get(site_name, DEFAULT_TIMEZONE))).date()
        for site_name in selected_sites
    )
    fetch_end = min(end + timedelta(days=6), site_today)
    # Future-only ranges should render empty weeks instead of raising because
    # the upstream periodic payload was never requested.
    periodic: dict[str, Any] = {"rows": []}
    rows: list[dict[str, Any]] = []
    data_quality: dict[str, Any] = {"source": "not_requested", "complete": True, "errors": []}
    if start <= fetch_end:
        try:
            periodic = await amazon_dashboard_periodic(
                "周",
                start,
                fetch_end,
                selected_sites,
                selected_series,
                products,
                sid_map,
                store_rows,
                "USD" if len(selected_sites) > 1 else "original",
            )
            data_quality = dict(periodic.get("data_quality") or {})
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="领星产品表现数据获取失败") from exc
        except RuntimeError as exc:
                raise HTTPException(status_code=502, detail=f"领星产品表现数据获取失败：{exc}") from exc

    rows = amazon_ads_chart_rows(periodic, start, end)
    sessions_present = any(row.get("sessions") is not None for row in rows)
    page_views_present = any(row.get("page_views") is not None for row in rows)
    return {
        "period": {"start": start.isoformat(), "end": (end + timedelta(days=6)).isoformat()},
        "site": site,
        "selected_sites": selected_sites,
        "model": model,
        "currency": "USD" if len(selected_sites) > 1 else AMAZON_CURRENCY_CODES.get(selected_sites[0], "USD"),
        "currency_mode": "USD" if len(selected_sites) > 1 else "original",
        "rows": rows,
        "field_availability": {
            "sessions_field": "Sessions-Total",
            "sessions_present": sessions_present,
            "page_views_field": "PV-Total",
            "page_views_present": page_views_present,
        },
        "data_quality": data_quality,
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/amazon/ad-plan")
def get_ad_plan(
    week_start: date | None = Query(default=None),
    site: str = Query(default="美国"),
    series: str = Query(default=""),
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
):
    require_business_access(x_sync_key, allow_public=True)
    site_code = AMAZON_SITE_CODES.get(site, site.strip().upper())
    if site_code not in AMAZON_SITE_CODES.values() or series not in AMAZON_SERIES:
        raise HTTPException(status_code=422, detail="广告计划筛选条件无效")
    week = normalize_week_start(week_start)
    with session_factory()() as db:
        item = db.scalar(select(AmazonAdPlan).where(AmazonAdPlan.week_start == week, AmazonAdPlan.site_code == site_code, AmazonAdPlan.series == series))
        if item is None:
            return {"week_start": week.isoformat(), "site": site, "site_code": site_code, "series": series, "review": "", "plan": "", "updated_at": None, "updated_by": None}
        return {"week_start": week.isoformat(), "site": site, "site_code": site_code, "series": series, "review": item.review, "plan": item.plan, **edit_metadata(item)}


@app.post("/api/amazon/ad-plan")
def save_ad_plan(
    payload: dict[str, Any] = Body(...),
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
    x_dashboard_editor: str | None = Header(default=None, alias="X-Dashboard-Editor"),
):
    require_business_access(x_sync_key, allow_public=False)
    site = str(payload.get("site") or "美国").strip()
    site_code = AMAZON_SITE_CODES.get(site, str(payload.get("site_code") or "").strip().upper())
    series = str(payload.get("series") or "").strip()
    if site_code not in AMAZON_SITE_CODES.values() or series not in AMAZON_SERIES:
        raise HTTPException(status_code=422, detail="广告计划筛选条件无效")
    try:
        week_start = normalize_week_start(date.fromisoformat(str(payload.get("week_start") or "")))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="广告计划周格式无效") from exc
    review = str(payload.get("review") or "")[:20000]
    plan = str(payload.get("plan") or "")[:20000]
    with session_factory()() as db:
        item = db.scalar(select(AmazonAdPlan).where(AmazonAdPlan.week_start == week_start, AmazonAdPlan.site_code == site_code, AmazonAdPlan.series == series))
        ensure_edit_freshness(item, payload.get("base_updated_at"), force=bool(payload.get("force")))
        if item is None:
            item = AmazonAdPlan(week_start=week_start, site_code=site_code, series=series)
            db.add(item)
        item.review = review
        item.plan = plan
        item.updated_at = utcnow()
        item.updated_by = dashboard_request_editor(x_dashboard_editor)
        db.commit()
    return {"ok": True, "week_start": week_start.isoformat(), "site": site, "site_code": site_code, "series": series, "review": review, "plan": plan, **edit_metadata(item)}


@app.get("/api/amazon/operation-plan")
def get_operation_plan(
    week_start: date | None = Query(default=None),
    site: str = Query(default="美国"),
    series: str = Query(default=""),
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
):
    require_business_access(x_sync_key, allow_public=True)
    site_code = AMAZON_SITE_CODES.get(site, site.strip().upper())
    if site_code not in AMAZON_SITE_CODES.values() or series not in AMAZON_SERIES:
        raise HTTPException(status_code=422, detail="运营计划筛选条件无效")
    week = normalize_week_start(week_start)
    with session_factory()() as db:
        item = db.scalar(select(AmazonOperationPlan).where(AmazonOperationPlan.week_start == week, AmazonOperationPlan.site_code == site_code, AmazonOperationPlan.series == series))
        if item is None:
            return {"week_start": week.isoformat(), "site": site, "site_code": site_code, "series": series, "review": "", "plan": "", "updated_at": None, "updated_by": None}
        return {"week_start": week.isoformat(), "site": site, "site_code": site_code, "series": series, "review": item.review, "plan": item.plan, **edit_metadata(item)}


@app.post("/api/amazon/operation-plan")
def save_operation_plan(
    payload: dict[str, Any] = Body(...),
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
    x_dashboard_editor: str | None = Header(default=None, alias="X-Dashboard-Editor"),
):
    require_business_access(x_sync_key, allow_public=False)
    site = str(payload.get("site") or "美国").strip()
    site_code = AMAZON_SITE_CODES.get(site, str(payload.get("site_code") or "").strip().upper())
    series = str(payload.get("series") or "").strip()
    if site_code not in AMAZON_SITE_CODES.values() or series not in AMAZON_SERIES:
        raise HTTPException(status_code=422, detail="运营计划筛选条件无效")
    try:
        week_start = normalize_week_start(date.fromisoformat(str(payload.get("week_start") or "")))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="运营计划周格式无效") from exc
    review = str(payload.get("review") or "")[:20000]
    plan = str(payload.get("plan") or "")[:20000]
    with session_factory()() as db:
        item = db.scalar(select(AmazonOperationPlan).where(AmazonOperationPlan.week_start == week_start, AmazonOperationPlan.site_code == site_code, AmazonOperationPlan.series == series))
        ensure_edit_freshness(item, payload.get("base_updated_at"), force=bool(payload.get("force")))
        if item is None:
            item = AmazonOperationPlan(week_start=week_start, site_code=site_code, series=series)
            db.add(item)
        item.review = review
        item.plan = plan
        item.updated_at = utcnow()
        item.updated_by = dashboard_request_editor(x_dashboard_editor)
        db.commit()
    return {"ok": True, "week_start": week_start.isoformat(), "site": site, "site_code": site_code, "series": series, "review": review, "plan": plan, **edit_metadata(item)}


@app.get("/api/amazon/dashboard")
async def amazon_dashboard(
    comparison: str = Query(default="周", pattern="^(日|周|月)$"),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    site: list[str] = Query(default=[]),
    currency: str = Query(default="original"),
    refresh: bool = Query(default=False),
    series: list[str] = Query(default=[]),
    products: list[str] = Query(default=[]),
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
):
    """Return read-only Amazon dashboard data; credentials stay server-side."""
    require_business_access(x_sync_key, allow_public=True)
    try:
        selected_sites = amazon_dashboard_sites_or_all(site)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    today = min(
        datetime.now(ZoneInfo(AMAZON_SITE_TIMEZONES.get(site_name, DEFAULT_TIMEZONE))).date()
        for site_name in selected_sites
    )
    requested_currency = str(currency or "original").strip().upper()
    if requested_currency == "ORIGINAL":
        requested_currency = "original"
    if requested_currency != "original" and requested_currency not in AMAZON_SUPPORTED_CURRENCIES:
        raise HTTPException(status_code=422, detail="不支持的货币")
    if len(selected_sites) > 1 and requested_currency == "original":
        raise HTTPException(status_code=422, detail="多站点汇总必须选择统一货币，不能直接相加原币种")
    if (start_date is None) != (end_date is None):
        raise HTTPException(status_code=422, detail="开始日期和结束日期需要同时提供")
    if start_date is None:
        if comparison == "日":
            start_date = end_date = today
        elif comparison == "周":
            end_date = today - timedelta(days=(today.weekday() + 1) % 7)
            start_date = end_date - timedelta(days=6)
        else:
            end_date = today.replace(day=1) - timedelta(days=1)
            start_date = end_date.replace(day=1)
    if start_date > today or end_date > today:
        raise HTTPException(status_code=422, detail="日期不能晚于今天")
    if start_date > end_date or (end_date - start_date).days > AMAZON_MAX_DATE_RANGE_DAYS:
        raise HTTPException(status_code=422, detail=f"日期范围无效，最多支持 {AMAZON_MAX_DATE_RANGE_DAYS} 天")
    if not os.environ.get("LINGXING_APP_ID") or not os.environ.get("LINGXING_APP_SECRET"):
        raise HTTPException(status_code=503, detail="领星 API 尚未配置")
    selected_series = set(series or AMAZON_SERIES)
    selected_products = {re.sub(r"-(黑|银|橙)$", r"-\1色", str(value)) for value in (products or AMAZON_PRODUCTS)}
    if not selected_series or any(value not in AMAZON_SERIES for value in selected_series):
        raise HTTPException(status_code=422, detail="产品表现看板系列参数无效")
    if not selected_products or any(value not in AMAZON_PRODUCTS for value in selected_products):
        raise HTTPException(status_code=422, detail="产品表现看板产品参数无效")
    try:
        sid_map = json.loads(os.environ.get("LINGXING_SIDS_JSON", "{}"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=503, detail="LINGXING_SIDS_JSON 配置格式错误") from exc
    if not sid_map:
        for store_item in await lingxing_store_rows():
            country = str(store_item.get("country") or "")
            sid = store_item.get("sid")
            if country and sid and int(store_item.get("status") or 0) == 1:
                sid_map.setdefault(country, {"sid": sid})
    store_rows = []
    if any(site_name == "日本" for site_name in selected_sites) or not sid_map:
        store_rows = await lingxing_store_rows()
    if refresh:
        _amazon_cache.clear_current_namespace()
    return await amazon_dashboard_periodic(
        comparison,
        start_date,
        end_date,
        selected_sites,
        selected_series,
        selected_products,
        sid_map,
        store_rows,
        requested_currency,
    )


@app.get("/api/amazon/sales-dashboard")
async def amazon_sales_dashboard(
    year: int = Query(default=datetime.now(timezone.utc).year),
    month: int = Query(default=datetime.now(timezone.utc).month),
    model: str = Query(default="TN10"),
    site: str = Query(default=AMAZON_SALES_ALL_SITES),
    refresh: bool = Query(default=False),
    include_comparison: bool = Query(default=False),
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
):
    """Return monthly target completion for one merged product model."""
    require_business_access(x_sync_key, allow_public=True)
    if year < 2000 or year > 2100 or month < 1 or month > 12:
        raise HTTPException(status_code=422, detail="年月参数无效")
    model = model.strip().upper()
    if model not in AMAZON_SALES_MODEL_CHOICES:
        raise HTTPException(status_code=422, detail="销售看板型号无效")
    try:
        selected_sites = amazon_sales_selected_sites(site, include_regions=True)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    products, selected_series = amazon_sales_scope(model)
    if not products or not selected_series:
        raise HTTPException(status_code=422, detail="销售看板型号暂无产品映射")

    month_start = date(year, month, 1)
    month_end = date(year, month, calendar.monthrange(year, month)[1])
    # A specific site uses that site's local date. “All sites” uses the minimum
    # site-local date so a future site day cannot leak into other sites.
    site_today = min(
        datetime.now(ZoneInfo(AMAZON_SITE_TIMEZONES.get(site_name, DEFAULT_TIMEZONE))).date()
        for site_name in selected_sites
    )
    timezone_basis = (
        selected_sites[0]
        if len(selected_sites) == 1
        else ("欧洲站点日期的最小值" if site == AMAZON_SALES_EUROPE else "全部站点站点日期的最小值")
    )
    selected_month = (year, month)
    current_month = (site_today.year, site_today.month)
    if selected_month < current_month:
        time_progress = 1.0
    elif selected_month > current_month:
        time_progress = 0.0
    else:
        time_progress = site_today.day / month_end.day

    with session_factory()() as db:
        country_items = list(db.scalars(
            select(AmazonMonthlyTarget).where(
                AmazonMonthlyTarget.year == year,
                AmazonMonthlyTarget.month == month,
                AmazonMonthlyTarget.model == model,
                AmazonMonthlyTarget.site.in_([AMAZON_SALES_ALL_SITES, *AMAZON_SITE_ORDER]),
            )
        ))
        country_target_payload = amazon_sales_country_target_payload(country_items)
        if site == AMAZON_SALES_EUROPE:
            targets = amazon_sales_europe_target_values(country_items)
            target_edit = latest_edit_metadata([item for item in country_items if item.site in AMAZON_SITE_ORDER])
        else:
            item = next((entry for entry in country_items if entry.site == site), None)
            targets = amazon_sales_target_values(item)
            target_edit = edit_metadata(item)

    actual_end = min(month_end, site_today) if month_start <= site_today else None
    rows: list[dict[str, Any]] = []
    data_quality: dict[str, Any] = {"source": "not_requested", "complete": True, "errors": []}
    if month_start <= site_today:
        try:
            rows, data_quality = await amazon_sales_actual_rows(
                month_start,
                actual_end,
                "月",
                selected_sites,
                selected_series,
                products,
                refresh,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="领星产品表现数据获取失败") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=f"领星产品表现数据获取失败：{exc}") from exc

    actuals = amazon_sales_actuals(rows)
    previous_actuals: dict[str, float | None] = {}
    comparison_payload: dict[str, Any] | None = None
    if include_comparison and actual_end is not None:
        previous_start, previous_period_end, previous_actual_end = amazon_previous_month_comparison_period(
            month_start,
            month_end,
            actual_end,
        )
        if previous_actual_end is not None:
            try:
                previous_rows, previous_quality = await amazon_sales_actual_rows(
                    previous_start,
                    previous_actual_end,
                    "月",
                    selected_sites,
                    selected_series,
                    products,
                    refresh,
                )
            except httpx.HTTPError as exc:
                raise HTTPException(status_code=502, detail="领星环比产品表现数据获取失败") from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=502, detail=f"领星环比产品表现数据获取失败：{exc}") from exc
            previous_actuals = amazon_sales_actuals(previous_rows)
            comparison_payload = {
                "basis": "previous_month_same_elapsed_scope",
                "period": {
                    "start": previous_start.isoformat(),
                    "end": previous_period_end.isoformat(),
                    "actual_end": previous_actual_end.isoformat(),
                },
                "actuals": {key: float(value) if value is not None else None for key, value in previous_actuals.items()},
                "data_quality": previous_quality,
            }
    target_units = targets.get("units")
    actual_units = actuals.get("units")
    sales_rate = actual_units / target_units if target_units and actual_units is not None else None
    return {
        "year": year,
        "month": month,
        "model": model,
        "models": list(AMAZON_SALES_MODEL_CHOICES),
        "site": site,
        "sites": [AMAZON_SALES_ALL_SITES, AMAZON_SALES_EUROPE, *AMAZON_SITE_ORDER],
        "currency": amazon_sales_currency(selected_sites),
        "period": {
            "start": month_start.isoformat(),
            "end": month_end.isoformat(),
            "actual_end": actual_end.isoformat() if month_start <= site_today else None,
        },
        "scope": {
            "products": sorted(products),
            "series": sorted(selected_series),
            "sites": selected_sites,
        },
        "targets": {key: float(value) if value is not None else None for key, value in targets.items()},
        "target_edit": target_edit,
        "country_targets": country_target_payload,
        "metrics": amazon_sales_metric_rows(targets, actuals, previous_actuals),
        "comparison": comparison_payload,
        "progress": {
            "sales": {
                "target": float(target_units) if target_units is not None else None,
                "actual": float(actual_units) if actual_units is not None else None,
                "rate": sales_rate,
            },
            "time": {
                "rate": time_progress,
                "current_day": site_today.day if selected_month == current_month else None,
                "days_in_month": month_end.day,
                "site_date": site_today.isoformat(),
                "timezone_basis": timezone_basis,
            },
        },
        "data_quality": data_quality,
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/amazon/sales-dashboard/weekly")
async def amazon_sales_weekly_dashboard(
    week_start: date | None = Query(default=None),
    model: str = Query(default="TN10"),
    site: str = Query(default=AMAZON_SALES_ALL_SITES),
    refresh: bool = Query(default=False),
    include_comparison: bool = Query(default=False),
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
):
    """Return weekly target completion for one merged product model."""
    require_business_access(x_sync_key, allow_public=True)
    try:
        week = normalize_week_start(week_start)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="销售看板周格式无效") from exc
    model = model.strip().upper()
    if model not in AMAZON_SALES_MODEL_CHOICES:
        raise HTTPException(status_code=422, detail="销售看板型号无效")
    try:
        selected_sites = amazon_sales_selected_sites(site, include_regions=True)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    products, selected_series = amazon_sales_scope(model)
    if not products or not selected_series:
        raise HTTPException(status_code=422, detail="销售看板型号暂无产品映射")

    week_end = week + timedelta(days=6)
    site_today = min(
        datetime.now(ZoneInfo(AMAZON_SITE_TIMEZONES.get(site_name, DEFAULT_TIMEZONE))).date()
        for site_name in selected_sites
    )
    timezone_basis = (
        selected_sites[0]
        if len(selected_sites) == 1
        else ("欧洲站点日期的最小值" if site == AMAZON_SALES_EUROPE else "全部站点站点日期的最小值")
    )
    time_progress = amazon_sales_week_time_progress(week, week_end, site_today)

    with session_factory()() as db:
        country_items = list(db.scalars(
            select(AmazonWeeklyTarget).where(
                AmazonWeeklyTarget.week_start == week,
                AmazonWeeklyTarget.model == model,
                AmazonWeeklyTarget.site.in_([AMAZON_SALES_ALL_SITES, *AMAZON_SITE_ORDER]),
            )
        ))
        country_target_payload = amazon_sales_country_target_payload(country_items)
        if site == AMAZON_SALES_EUROPE:
            targets = amazon_sales_europe_target_values(country_items)
            target_edit = latest_edit_metadata([item for item in country_items if item.site in AMAZON_SITE_ORDER])
        else:
            item = next((entry for entry in country_items if entry.site == site), None)
            targets = amazon_sales_target_values(item)
            target_edit = edit_metadata(item)

    rows: list[dict[str, Any]] = []
    data_quality: dict[str, Any] = {"source": "not_requested", "complete": True, "errors": []}
    try:
        rows, data_quality = await amazon_sales_actual_rows(
            week,
            min(week_end, site_today),
            "周",
            selected_sites,
            selected_series,
            products,
            refresh,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="领星产品表现数据获取失败") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"领星产品表现数据获取失败：{exc}") from exc

    actuals = amazon_sales_actuals(rows)
    previous_actuals: dict[str, float | None] = {}
    comparison_payload: dict[str, Any] | None = None
    actual_end = min(week_end, site_today) if week <= site_today else None
    if include_comparison and actual_end is not None:
        previous_start = week - timedelta(days=7)
        previous_period_end = week_end - timedelta(days=7)
        previous_actual_end = actual_end - timedelta(days=7)
        try:
            previous_rows, previous_quality = await amazon_sales_actual_rows(
                previous_start,
                previous_actual_end,
                "周",
                selected_sites,
                selected_series,
                products,
                refresh,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="领星环比产品表现数据获取失败") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=f"领星环比产品表现数据获取失败：{exc}") from exc
        previous_actuals = amazon_sales_actuals(previous_rows)
        comparison_payload = {
            "basis": "previous_week_same_elapsed_scope",
            "period": {
                "start": previous_start.isoformat(),
                "end": previous_period_end.isoformat(),
                "actual_end": previous_actual_end.isoformat(),
            },
            "actuals": {key: float(value) if value is not None else None for key, value in previous_actuals.items()},
            "data_quality": previous_quality,
        }
    target_units = targets.get("units")
    actual_units = actuals.get("units")
    sales_rate = actual_units / target_units if target_units and actual_units is not None else None
    current_day = (site_today - week).days + 1 if week <= site_today <= week_end else None
    return {
        "dimension": "week",
        "week_start": week.isoformat(),
        "week_end": week_end.isoformat(),
        "model": model,
        "models": list(AMAZON_SALES_MODEL_CHOICES),
        "site": site,
        "sites": [AMAZON_SALES_ALL_SITES, AMAZON_SALES_EUROPE, *AMAZON_SITE_ORDER],
        "currency": amazon_sales_currency(selected_sites),
        "period": {
            "start": week.isoformat(),
            "end": week_end.isoformat(),
            "actual_end": actual_end.isoformat() if actual_end else None,
        },
        "scope": {
            "products": sorted(products),
            "series": sorted(selected_series),
            "sites": selected_sites,
        },
        "targets": {key: float(value) if value is not None else None for key, value in targets.items()},
        "target_edit": target_edit,
        "country_targets": country_target_payload,
        "metrics": amazon_sales_metric_rows(targets, actuals, previous_actuals),
        "comparison": comparison_payload,
        "progress": {
            "sales": {
                "target": float(target_units) if target_units is not None else None,
                "actual": float(actual_units) if actual_units is not None else None,
                "rate": sales_rate,
            },
            "time": {
                "rate": time_progress,
                "current_day": current_day,
                "days_in_week": 7,
                "site_date": site_today.isoformat(),
                "timezone_basis": timezone_basis,
            },
        },
        "data_quality": data_quality,
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/api/amazon/sales-dashboard/weekly/targets")
def save_amazon_sales_weekly_targets(
    payload: dict[str, Any] = Body(...),
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
    x_dashboard_editor: str | None = Header(default=None, alias="X-Dashboard-Editor"),
):
    """Upsert all editable weekly targets for one week/model/site scope."""
    require_business_access(x_sync_key, allow_public=False)
    try:
        week = normalize_week_start(date.fromisoformat(str(payload.get("week_start") or "")))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="周度目标保存周格式无效") from exc
    model = str(payload.get("model") or "").strip().upper()
    site = str(payload.get("site") or AMAZON_SALES_ALL_SITES).strip() or AMAZON_SALES_ALL_SITES
    if model not in AMAZON_SALES_MODEL_CHOICES:
        raise HTTPException(status_code=422, detail="周度目标保存参数无效")
    try:
        amazon_sales_selected_sites(site, include_regions=True)
        if site == AMAZON_SALES_EUROPE:
            raise ValueError("欧洲销量目标由各国目标汇总，请使用快速写入目标")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raw_targets = payload.get("targets")
    if not isinstance(raw_targets, dict):
        raise HTTPException(status_code=422, detail="周度目标格式无效")
    unknown_keys = set(raw_targets) - set(AMAZON_SALES_TARGET_FIELDS)
    if unknown_keys:
        raise HTTPException(status_code=422, detail="周度目标包含未知指标")
    try:
        normalized = {
            key: amazon_sales_target_number(raw_targets.get(key))
            for key in AMAZON_SALES_TARGET_FIELDS
        }
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    with session_factory()() as db:
        item = db.scalar(
            select(AmazonWeeklyTarget).where(
                AmazonWeeklyTarget.week_start == week,
                AmazonWeeklyTarget.model == model,
                AmazonWeeklyTarget.site == site,
            )
        )
        if item is None:
            item = AmazonWeeklyTarget(week_start=week, model=model, site=site)
            db.add(item)
        ensure_edit_freshness(item, payload.get("base_updated_at"), force=bool(payload.get("force")))
        for key, value in normalized.items():
            setattr(item, f"target_{key}", value)
        item.updated_at = utcnow()
        item.updated_by = dashboard_request_editor(x_dashboard_editor)
        db.commit()
    _amazon_cache.clear_namespaces("amazon-sales-targets")

    return {
        "ok": True,
        "week_start": week.isoformat(),
        "model": model,
        "site": site,
        "targets": {key: float(value) if value is not None else None for key, value in normalized.items()},
        **edit_metadata(item),
    }


@app.post("/api/amazon/sales-dashboard/weekly/targets/bulk")
def save_amazon_sales_weekly_targets_bulk(
    payload: dict[str, Any] = Body(...),
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
    x_dashboard_editor: str | None = Header(default=None, alias="X-Dashboard-Editor"),
):
    """Quick-enter weekly unit targets for every country in one transaction."""
    require_business_access(x_sync_key, allow_public=False)
    try:
        week = normalize_week_start(date.fromisoformat(str(payload.get("week_start") or "")))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="周度销量目标批量保存周格式无效") from exc
    model = str(payload.get("model") or "").strip().upper()
    if model not in AMAZON_SALES_MODEL_CHOICES:
        raise HTTPException(status_code=422, detail="周度销量目标批量保存参数无效")
    try:
        normalized = amazon_sales_bulk_unit_targets(payload.get("items"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    with session_factory()() as db:
        items = {
            str(item.site): item
            for item in db.scalars(
                select(AmazonWeeklyTarget).where(
                    AmazonWeeklyTarget.week_start == week,
                    AmazonWeeklyTarget.model == model,
                    AmazonWeeklyTarget.site.in_(AMAZON_SITE_ORDER),
                )
            )
        }
        ensure_edit_freshness(
            max(items.values(), key=lambda item: item.updated_at or datetime.min, default=None),
            payload.get("base_updated_at"),
            force=bool(payload.get("force")),
        )
        for site_name, target_units in normalized.items():
            item = items.get(site_name)
            if item is None:
                item = AmazonWeeklyTarget(week_start=week, model=model, site=site_name)
                db.add(item)
            item.target_units = target_units
            item.updated_at = utcnow()
            item.updated_by = dashboard_request_editor(x_dashboard_editor)
        db.commit()
        _amazon_cache.clear_namespaces("amazon-sales-targets")
        saved_items = list(db.scalars(
            select(AmazonWeeklyTarget).where(
                AmazonWeeklyTarget.week_start == week,
                AmazonWeeklyTarget.model == model,
                AmazonWeeklyTarget.site.in_(AMAZON_SITE_ORDER),
            )
        ))

    return {
        "ok": True,
        "week_start": week.isoformat(),
        "model": model,
        "country_targets": amazon_sales_country_target_payload(saved_items),
        **latest_edit_metadata(saved_items),
    }


@app.post("/api/amazon/sales-dashboard/targets")
def save_amazon_sales_targets(
    payload: dict[str, Any] = Body(...),
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
    x_dashboard_editor: str | None = Header(default=None, alias="X-Dashboard-Editor"),
):
    """Upsert all editable monthly targets for one year/month/model/site scope."""
    require_business_access(x_sync_key, allow_public=False)
    try:
        year = int(payload.get("year"))
        month = int(payload.get("month"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="年月参数无效") from exc
    model = str(payload.get("model") or "").strip().upper()
    site = str(payload.get("site") or AMAZON_SALES_ALL_SITES).strip() or AMAZON_SALES_ALL_SITES
    if year < 2000 or year > 2100 or month < 1 or month > 12 or model not in AMAZON_SALES_MODEL_CHOICES:
        raise HTTPException(status_code=422, detail="月度目标保存参数无效")
    try:
        amazon_sales_selected_sites(site, include_regions=True)
        if site == AMAZON_SALES_EUROPE:
            raise ValueError("欧洲销量目标由各国目标汇总，请使用快速写入目标")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raw_targets = payload.get("targets")
    if not isinstance(raw_targets, dict):
        raise HTTPException(status_code=422, detail="月度目标格式无效")
    unknown_keys = set(raw_targets) - set(AMAZON_SALES_TARGET_FIELDS)
    if unknown_keys:
        raise HTTPException(status_code=422, detail="月度目标包含未知指标")
    try:
        normalized = {key: amazon_sales_target_number(raw_targets.get(key)) for key in AMAZON_SALES_TARGET_FIELDS}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    with session_factory()() as db:
        item = db.scalar(
            select(AmazonMonthlyTarget).where(
                AmazonMonthlyTarget.year == year,
                AmazonMonthlyTarget.month == month,
                AmazonMonthlyTarget.model == model,
                AmazonMonthlyTarget.site == site,
            )
        )
        if item is None:
            item = AmazonMonthlyTarget(year=year, month=month, model=model, site=site)
            db.add(item)
        ensure_edit_freshness(item, payload.get("base_updated_at"), force=bool(payload.get("force")))
        for key, value in normalized.items():
            setattr(item, f"target_{key}", value)
        item.updated_at = utcnow()
        item.updated_by = dashboard_request_editor(x_dashboard_editor)
        db.commit()
    _amazon_cache.clear_namespaces("amazon-sales-targets")

    return {
        "ok": True,
        "year": year,
        "month": month,
        "model": model,
        "site": site,
        "targets": {key: float(value) if value is not None else None for key, value in normalized.items()},
        **edit_metadata(item),
    }


@app.post("/api/amazon/sales-dashboard/targets/bulk")
def save_amazon_sales_targets_bulk(
    payload: dict[str, Any] = Body(...),
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
    x_dashboard_editor: str | None = Header(default=None, alias="X-Dashboard-Editor"),
):
    """Quick-enter monthly unit targets for every country in one transaction."""
    require_business_access(x_sync_key, allow_public=False)
    try:
        year = int(payload.get("year"))
        month = int(payload.get("month"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="月度销量目标批量保存年月无效") from exc
    model = str(payload.get("model") or "").strip().upper()
    if year < 2000 or year > 2100 or month < 1 or month > 12 or model not in AMAZON_SALES_MODEL_CHOICES:
        raise HTTPException(status_code=422, detail="月度销量目标批量保存参数无效")
    try:
        normalized = amazon_sales_bulk_unit_targets(payload.get("items"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    with session_factory()() as db:
        items = {
            str(item.site): item
            for item in db.scalars(
                select(AmazonMonthlyTarget).where(
                    AmazonMonthlyTarget.year == year,
                    AmazonMonthlyTarget.month == month,
                    AmazonMonthlyTarget.model == model,
                    AmazonMonthlyTarget.site.in_(AMAZON_SITE_ORDER),
                )
            )
        }
        ensure_edit_freshness(
            max(items.values(), key=lambda item: item.updated_at or datetime.min, default=None),
            payload.get("base_updated_at"),
            force=bool(payload.get("force")),
        )
        for site_name, target_units in normalized.items():
            item = items.get(site_name)
            if item is None:
                item = AmazonMonthlyTarget(year=year, month=month, model=model, site=site_name)
                db.add(item)
            item.target_units = target_units
            item.updated_at = utcnow()
            item.updated_by = dashboard_request_editor(x_dashboard_editor)
        db.commit()
        _amazon_cache.clear_namespaces("amazon-sales-targets")
        saved_items = list(db.scalars(
            select(AmazonMonthlyTarget).where(
                AmazonMonthlyTarget.year == year,
                AmazonMonthlyTarget.month == month,
                AmazonMonthlyTarget.model == model,
                AmazonMonthlyTarget.site.in_(AMAZON_SITE_ORDER),
            )
        ))

    return {
        "ok": True,
        "year": year,
        "month": month,
        "model": model,
        "country_targets": amazon_sales_country_target_payload(saved_items),
        **latest_edit_metadata(saved_items),
    }


@app.get("/api/amazon/stores")
async def amazon_stores(
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
):
    require_business_access(x_sync_key, allow_public=True)
    """Return read-only Amazon stores without exposing credentials."""
    if not os.environ.get("LINGXING_APP_ID") or not os.environ.get("LINGXING_APP_SECRET"):
        raise HTTPException(status_code=503, detail="领星 API 尚未配置")
    try:
        raw_stores = await lingxing_store_rows()
        stores = []
        for item in raw_stores:
            country = str(item.get("country") or "未知站点")
            stores.append({
                "sid": int(item.get("sid")) if item.get("sid") is not None else None,
                "name": str(item.get("name") or item.get("account_name") or "未命名店铺"),
                "country": country,
                "site_code": AMAZON_SITE_CODES.get(country, ""),
                "region": str(item.get("region") or ""),
                "seller_id": str(item.get("seller_id") or ""),
                "has_ads_setting": int(item.get("has_ads_setting") or 0),
                "status": int(item.get("status") or 0),
            })
        stores.sort(key=lambda item: (AMAZON_SITE_ORDER.index(item["country"]) if item["country"] in AMAZON_SITE_ORDER else len(AMAZON_SITE_ORDER), item["name"], str(item["sid"] or "")))
        return {"stores": stores, "count": len(stores)}
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"领星店铺列表请求失败：HTTP {exc.response.status_code}") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="领星店铺列表获取失败") from exc


@app.get("/api/amazon/date-context")
async def amazon_date_context(
    site: list[str] = Query(default=["美国"]),
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
):
    """Return the current calendar date for the selected marketplace site."""
    require_business_access(x_sync_key, allow_public=True)
    try:
        selected_sites = amazon_dashboard_selected_sites(site)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    dates = {
        site_name: datetime.now(ZoneInfo(AMAZON_SITE_TIMEZONES.get(site_name, DEFAULT_TIMEZONE))).date().isoformat()
        for site_name in selected_sites
    }
    return {
        "site": selected_sites,
        "timezone": {site_name: AMAZON_SITE_TIMEZONES.get(site_name, DEFAULT_TIMEZONE) for site_name in selected_sites},
        "today": min(dates.values()),
        "today_by_site": dates,
    }


@app.post("/api/sync")
async def sync(
    trigger: str = Query(default="button", pattern="^(button|dashboard)$"),
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
):
    require_business_access(x_sync_key, allow_public=False)
    try:
        return await run_sync(trigger)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="同步服务暂不可用") from exc
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Shopify request failed: HTTP {exc.response.status_code}") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="同步失败，请稍后重试") from exc


@app.get("/api/dashboard")
async def dashboard(
    days: int = Query(default=30, ge=1, le=180),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    store: str | None = Query(default=None, max_length=255),
    auto_sync: bool = Query(default=False),
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
):
    try:
        require_business_access(x_sync_key, allow_public=True)
        if (start_date is None) != (end_date is None):
            raise HTTPException(status_code=422, detail="自定义日期需要同时提供开始日期和结束日期")
        if start_date is not None and end_date is not None:
            if start_date > end_date:
                raise HTTPException(status_code=422, detail="开始日期不能晚于结束日期")
            selected_days = (end_date - start_date).days + 1
            if selected_days > 180:
                raise HTTPException(status_code=422, detail="自定义日期范围不能超过 180 天")
            period_start = start_date
            period_end = end_date + timedelta(days=1)
        else:
            period_end = utcnow().date() + timedelta(days=1)
            period_start = period_end - timedelta(days=days)
        factory = session_factory()
        if auto_sync:
            await run_sync("dashboard")
        with factory() as db:
            return dashboard_payload(db, period_start, period_end, store)
    except HTTPException:
        raise
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="看板服务暂不可用") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Dashboard query failed") from exc
