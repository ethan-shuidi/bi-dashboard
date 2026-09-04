import asyncio
import json
import os
import re
import base64
import hashlib
import hmac
import time
from urllib.parse import quote
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
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
    allow_headers=["Content-Type", "X-Sync-Key"],
)


def require_business_access(
    x_sync_key: str | None,
) -> None:
    """Require the configured dashboard key for every business API request."""
    configured_key = (
        os.environ.get("DASHBOARD_API_KEY", "").strip()
        or os.environ.get("SYNC_API_KEY", "").strip()
    )
    if configured_key and x_sync_key and hmac.compare_digest(x_sync_key, configured_key):
        return
    raise HTTPException(status_code=401, detail="看板接口需要有效的 X-Sync-Key")

Base = declarative_base()
_engine = None
_session_factory = None
_lingxing_token: dict[str, Any] = {}
_lingxing_token_lock = asyncio.Lock()
_lingxing_performance_lock = asyncio.Lock()
_lingxing_performance_last_call = 0.0
_lingxing_ad_report_lock = asyncio.Lock()
_amazon_cache: dict[tuple[Any, ...], tuple[float, Any]] = {}
AMAZON_CACHE_TTL_SECONDS = 600
AMAZON_UPSTREAM_CONCURRENCY = 5
AMAZON_CURRENCY_CODES = {
    "美国": "USD", "日本": "JPY", "加拿大": "CAD", "澳洲": "AUD",
    "英国": "GBP", "德国": "EUR", "法国": "EUR", "意大利": "EUR",
    "西班牙": "EUR", "荷兰": "EUR", "比利时": "EUR", "瑞典": "SEK",
}

DEFAULT_TIMEZONE = "America/Los_Angeles"
DEFAULT_API_VERSION = "2026-07"
DEFAULT_INITIAL_SYNC_DAYS = 90
DEFAULT_SYNC_COOLDOWN_SECONDS = 600

LINGXING_API_BASE = "https://openapi.lingxing.com"
AMAZON_SERIES = [
    "TN10系列（主链接）汇总",
    "TN10系列（小链接）汇总",
    "TN20系列（主链接）汇总",
]
AMAZON_PRODUCTS = [
    "TN10-主链接-黑色", "TN10-主链接-银色", "TN10-主链接-橙色",
    "TN10-小链接-黑色", "TN10-小链接-银色", "TN10-小链接-橙色",
    "TN20-主链接-黑色", "TN20-主链接-银色", "TN20-主链接-红",
    "TN20-小链接-黑色", "TN20-小链接-银色", "TN20-小链接-樱桃红",
]
AMAZON_SITE_CODES = {"美国": "US", "日本": "JP", "加拿大": "CA", "澳洲": "AU", "德国": "DE", "法国": "FR", "意大利": "IT", "西班牙": "ES", "英国": "UK", "荷兰": "NL", "比利时": "BE", "瑞典": "SE"}

# The Feishu mapping is keyed by site + ASIN.  Site-specific ASIN lists are
# intentionally kept as data, so a later refresh can replace this block
# without changing aggregation logic.
ASIN_MAPPING = {
    "US": {"B0G1XQ3H4H":"TN10-主链接-黑色","B0G1YMLFSZ":"TN10-小链接-银色","B0GMGP9B1D":"TN10-小链接-橙色","B0GSJMTSMQ":"TN10-小链接-黑色","B0GZNNL72W":"TN10-主链接-橙色","B0GR9CDQYG":"TN10-主链接-银色","B0H8SF6N61":"TN20-主链接-黑色","B0H8S9M43Y":"TN20-主链接-银色","B0H8SZZN8X":"TN20-主链接-红","B0H8MRQW7Q":"TN20-小链接-黑色","B0H8CKG9P5":"TN20-小链接-银色","B0H8NP9TVK":"TN20-小链接-樱桃红"},
    "JP": {"B0G4M5QMNG":"TN10-主链接-黑色","B0G4M4YMHZ":"TN10-主链接-银色","B0G4M4KZ5S":"TN10-主链接-橙色","B0HC6V88K5":"TN20-主链接-黑色","B0HC75XJ3D":"TN20-主链接-银色","B0HC78T99S":"TN20-主链接-红","B0HD7GRRL5":"TN20-小链接-黑色","B0HD77JKX5":"TN20-小链接-银色","B0HD7QJ1XJ":"TN20-小链接-樱桃红"},
}
for _site, _asins, _series in [
    ("CA", ["B0G1XQ3H4H","B0G1YMLFSZ","B0G1YCTVJG","B0H8NCJLMD","B0H8RSZHB3","B0H8S2TK5K","B0H94CHVCN","B0H94MYQP3","B0H94QM3TZ"], None),
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


def amazon_series(product: str | None) -> str | None:
    if not product:
        return None
    if product.startswith("TN10-主链接"):
        return AMAZON_SERIES[0]
    if product.startswith("TN10-小链接"):
        return AMAZON_SERIES[1]
    if product.startswith("TN20-主链接"):
        return AMAZON_SERIES[2]
    return None


async def lingxing_store_rows() -> list[dict[str, Any]]:
    body = await lingxing_get("/erp/sc/data/seller/lists")
    return list(body.get("data") or [])


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
    if site_code == "JP":
        # Always supplement configured values with the authoritative store
        # list, because older configurations may contain only one JP sid.
        for row in store_rows or []:
            country = str(row.get("country") or "")
            name = str(row.get("name") or row.get("account_name") or "")
            if country in {"日本", "JP"} and name in {"Comu-JP", "Comulytic-JP"}:
                add(row.get("sid"), name)

    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in candidates:
        key = str(item.get("sid") or "")
        if key and key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def optional_value(row: dict[str, Any], *names: str) -> float | None:
    """Read a metric only when the upstream response actually contains it."""
    for name in names:
        if name in row and row.get(name) is not None and row.get(name) != "":
            try:
                return float(row.get(name) or 0)
            except (TypeError, ValueError):
                return None
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
) -> list[dict[str, Any]]:
    """Read LingXing's dated advertising report for one store."""
    cache_key = ("ads", sid, report_date.isoformat())
    cached = _amazon_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < AMAZON_CACHE_TTL_SECONDS:
        return cached[1]
    async with semaphore:
        body = await lingxing_post(
            "/pb/openapi/newad/spProductAdReports",
            {
                "sid": sid,
                "report_date": report_date.isoformat(),
                "show_detail": 1,
                "offset": 0,
                "length": 500,
            },
            client=client,
        )
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
) -> list[dict[str, Any]]:
    """Read dated advertising reports, serializing requests for LingXing's rate limit."""
    rows: list[dict[str, Any]] = []
    cursor = start_date
    while cursor <= end_date:
        async with _lingxing_ad_report_lock:
            try:
                daily = await fetch_ad_report(sid, cursor, client, semaphore)
            except RuntimeError as exc:
                if "频繁" in str(exc) or "too frequent" in str(exc).lower():
                    await asyncio.sleep(2)
                    daily = await fetch_ad_report(sid, cursor, client, semaphore)
                else:
                    raise
        for row in daily:
            if isinstance(row, dict):
                tagged = dict(row)
                tagged["_source"] = "ad_report"
                tagged["_dashboard_date"] = cursor.isoformat()
                rows.append(tagged)
        cursor += timedelta(days=1)
    return rows


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
    return {"series": series, "product": product or "", "acoas": None, "ad_sales_share": None, "ad_order_share": None, "units": None, "net_sales": None, "orders": None, "b2b_units": None, "b2b_orders": None, "ctr": None, "clicks": 0, "cpc": None, "ad_cost": 0, "ad_cvr": None, "ad_units": 0, "ad_orders": 0, "cvr": None, "acos": None, "sessions": None}


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
            period_end = cursor + timedelta(days=6)
            periods.append((f"{cursor.isoformat()}~{period_end.isoformat()}", cursor, period_end))
            cursor += timedelta(days=7)
        return periods
    cursor = start_date.replace(day=1)
    while cursor <= end_date:
        next_month = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        period_end = next_month - timedelta(days=1)
        periods.append((cursor.strftime("%Y-%m"), cursor, period_end))
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
) -> list[dict[str, Any]]:
    """Read LingXing's product-performance endpoint for operating metrics."""
    # An explicitly empty list means the selected products have no ASINs
    # mapped for this site; avoid issuing an unfiltered request in that case.
    if asin_list == []:
        return []
    # LingXing limits this endpoint to a maximum 92-day date range.  The
    # dashboard allows up to 180 days, so split longer requests into bounded
    # chunks and merge the returned rows.  Chunk responses are kept in the
    # normal cache, which also prevents repeated filter changes from issuing
    # the same upstream request again.
    rows: list[dict[str, Any]] = []
    cursor = start_date
    while cursor <= end_date:
        chunk_end = min(cursor + timedelta(days=91), end_date)
        cache_key = ("product-performance-v2", sid, cursor.isoformat(), chunk_end.isoformat(), tuple(asin_list or ()))
        cached = _amazon_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < AMAZON_CACHE_TTL_SECONDS:
            chunk_rows = cached[1]
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
            }
            # The endpoint supports native ASIN filtering through
            # search_field/search_value (up to 50 ASINs per request).
            if asin_list is not None:
                payload["search_field"] = "asin"
                payload["search_value"] = asin_list
            # LingXing may briefly reject consecutive requests for the same
            # account even when each request is within the documented range.
            # Retry only that specific upstream response with bounded backoff;
            # all other errors still fail fast and remain visible to the UI.
            rate_limited = False
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
            _amazon_cache[cache_key] = (time.monotonic(), chunk_rows)
        rows.extend(chunk_rows)
        cursor = chunk_end + timedelta(days=1)
    return rows


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
    },
    "ad_report": {
        "impressions": ("impressions",),
        "clicks": ("clicks",),
        "ad_sales": ("sales", "sales_14d", "ad_sales", "adSales"),
        "ad_cost": ("spends", "cost", "spend"),
        "ad_units": ("ad_units", "units", "units_14d", "adUnits"),
        "ad_orders": ("orders", "orders_14d", "order_count", "ad_orders", "adOrders"),
    },
}
AMAZON_SOURCE_RATIOS = {
    "performance": {"source_cvr": ("cvr", "conversion_rate", "conversionRate")},
    "ad_report": {
        "source_ctr": ("ctr", "click_through_rate", "clickThroughRate"),
        "source_cpc": ("cpc", "cost_per_click", "costPerClick"),
        "source_ad_cvr": ("ad_cvr", "adCvr", "ad_conversion_rate", "adConversionRate"),
        "source_acos": ("acos", "ACOS"),
    },
}
AMAZON_METRIC_SOURCES = {
    "performance": ["units", "net_sales", "orders", "b2b_units", "b2b_orders", "sessions", "cvr"],
    "ad_report": ["impressions", "clicks", "ad_sales", "ad_cost", "ad_units", "ad_orders", "ctr", "cpc", "ad_cvr", "acos"],
    "calculated": ["acoas", "ad_sales_share", "ad_order_share"],
}


async def amazon_dashboard_periodic(
    comparison: str,
    start_date: date,
    end_date: date,
    site: str | None,
    selected_series: set[str],
    selected_products: set[str],
    sid_map: dict[str, Any],
    store_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    periods = amazon_periods(start_date, end_date, comparison)
    selected_sites = [site] if site else list(AMAZON_SITE_CODES)
    semaphore = asyncio.Semaphore(AMAZON_UPSTREAM_CONCURRENCY)
    cache_key = ("periodic-dashboard-v3", comparison, start_date.isoformat(), end_date.isoformat(), site or "", tuple(sorted(selected_series)), tuple(sorted(selected_products)))
    cached = _amazon_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < AMAZON_CACHE_TTL_SECONDS:
        return cached[1]

    async with httpx.AsyncClient(timeout=45) as client:
        async def fetch_site(site_name: str):
            site_code = AMAZON_SITE_CODES.get(site_name, site_name)
            accounts = amazon_sid_accounts(site_name, sid_map, store_rows)
            if not accounts:
                return site_name, site_code, AMAZON_CURRENCY_CODES.get(site_name, "USD"), []
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
                            client, semaphore, asin_filter,
                        )
                        for period_row in period_rows:
                            if isinstance(period_row, dict):
                                tagged = dict(period_row)
                                tagged["_dashboard_period"] = period_label
                                tagged["_source"] = "performance"
                                performance_rows.append(tagged)
                except RuntimeError as exc:
                    if "ip not permit" in str(exc).lower() or "白名单" in str(exc):
                        performance_rows = []
                    else:
                        raise
                for row in performance_rows:
                    row["_source"] = "performance"
                ad_report_rows = await fetch_ad_reports_range(
                    int(sid_value), start_date, end_date, client, semaphore
                )
                all_rows.extend(performance_rows + ad_report_rows)
            return site_name, site_code, AMAZON_CURRENCY_CODES.get(site_name, "USD"), all_rows

        results = await asyncio.gather(*(fetch_site(site_name) for site_name in selected_sites))

    aggregate: dict[tuple[str, str, str], dict[str, Any]] = {}
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
            key = (period_label, group, product)
            item = aggregate.setdefault(key, {"period": period_label, "period_start": period_start.isoformat(), "period_end": period_end.isoformat(), "currency": str(raw.get("currency_code") or raw.get("currencyCode") or default_currency).strip() or default_currency})
            source = raw.get("_source", "performance")
            if source not in AMAZON_SOURCE_FIELDS:
                continue
            fields = AMAZON_SOURCE_FIELDS[source]
            source_fields = AMAZON_SOURCE_RATIOS[source]
            for field, names in fields.items():
                value = optional_metric(raw, *names)
                if value is not None:
                    item[field] = (item.get(field) or 0) + value
            for field, names in source_fields.items():
                value = optional_metric(raw, *names)
                if value is not None:
                    item[field] = value

    rows: list[dict[str, Any]] = []
    for (period_label, group, product), item in aggregate.items():
        units = item.get("units")
        net_sales = item.get("net_sales")
        orders = item.get("orders")
        clicks = item.get("clicks")
        impressions = item.get("impressions")
        ad_sales = item.get("ad_sales")
        ad_cost = item.get("ad_cost")
        ad_orders = item.get("ad_orders")
        sessions = item.get("sessions")
        ad_units = item.get("ad_units")
        ad_sales_share = (ad_units / units) if ad_units is not None and units else None
        ad_order_share = (ad_orders / orders) if ad_orders is not None and orders else None
        # ACoAS is defined by the dashboard requirement as ad spend divided
        # by net sales. Recalculate it from the period totals instead of
        # trusting a range-level/source value that may use another denominator.
        calculated_acoas = (ad_cost / net_sales) if ad_cost is not None and net_sales else None
        rows.append({
            "period": item["period"], "period_start": item["period_start"], "period_end": item["period_end"],
            "series": group, "product": product, "currency": item.get("currency", "USD"),
            "units": int(units) if units is not None else None, "net_sales": net_sales, "orders": int(orders) if orders is not None else None,
            "b2b_units": int(item["b2b_units"]) if item.get("b2b_units") is not None else None, "b2b_orders": int(item["b2b_orders"]) if item.get("b2b_orders") is not None else None,
            "ctr": clicks / impressions if clicks is not None and impressions else item.get("source_ctr"), "clicks": int(clicks) if clicks is not None else None,
            "impressions": int(impressions) if impressions is not None else None, "cpc": ad_cost / clicks if ad_cost is not None and clicks else item.get("source_cpc"),
            "ad_cost": ad_cost, "ad_cvr": ad_orders / clicks if ad_orders is not None and clicks else item.get("source_ad_cvr"),
            "ad_units": int(ad_units) if ad_units is not None else None, "ad_orders": int(ad_orders) if ad_orders is not None else None,
            "cvr": item.get("source_cvr"), "acos": ad_cost / ad_sales if ad_cost is not None and ad_sales else item.get("source_acos"),
            "acoas": calculated_acoas, "ad_sales_share": ad_sales_share, "ad_order_share": ad_order_share, "ad_sales": ad_sales,
            "sessions": int(sessions) if sessions is not None else None,
        })
    response = {
        "period": {"comparison": comparison, "start": start_date.isoformat(), "end": end_date.isoformat()},
        "currency": AMAZON_CURRENCY_CODES.get(site, "USD") if site else "MIXED",
        "filters": {"site": site or "全部站点", "series": list(selected_series), "products": list(selected_products)},
        "periods": [{"label": label, "start": p_start.isoformat(), "end": p_end.isoformat()} for label, p_start, p_end in periods],
        "rows": rows,
        "mapping": {
            "key": "site+asin",
            "sites": list(AMAZON_SITE_CODES),
            "sources": {
                **AMAZON_METRIC_SOURCES,
            },
            "net_sales_field": "net_amount",
        },
    }
    _amazon_cache[cache_key] = (time.monotonic(), response)
    return response


@app.get("/api/amazon/dashboard")
async def amazon_dashboard(
    comparison: str = Query(default="周", pattern="^(日|周|月)$"),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    site: str | None = Query(default=None),
    series: list[str] = Query(default=[]),
    products: list[str] = Query(default=[]),
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
):
    require_business_access(x_sync_key)
    today = utcnow().date()
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
    if comparison == "周" and (start_date.weekday() != 0 or end_date.weekday() != 6):
        raise HTTPException(status_code=422, detail="周维度只能选择自然周的周一至周日")
    if comparison == "月":
        next_month = (end_date.replace(day=28) + timedelta(days=4)).replace(day=1)
        if start_date.day != 1 or end_date != next_month - timedelta(days=1):
            raise HTTPException(status_code=422, detail="月维度只能选择自然月的月初至月末")
    if start_date > end_date or (end_date - start_date).days > 180:
        raise HTTPException(status_code=422, detail="日期范围无效，最多支持 180 天")
    if not os.environ.get("LINGXING_APP_ID") or not os.environ.get("LINGXING_APP_SECRET"):
        raise HTTPException(status_code=503, detail="领星 API 尚未配置")
    selected_sites = [site] if site else list(AMAZON_SITE_CODES)
    selected_series = set(series or AMAZON_SERIES)
    selected_products = {re.sub(r"-(黑|银|橙)$", r"-\1色", str(value)) for value in (products or AMAZON_PRODUCTS)}
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
    if site == "日本" or not sid_map:
        store_rows = await lingxing_store_rows()
    return await amazon_dashboard_periodic(comparison, start_date, end_date, site, selected_series, selected_products, sid_map, store_rows)


@app.get("/api/amazon/stores")
async def amazon_stores(
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
):
    """Return the authorized Amazon stores without exposing credentials."""
    require_business_access(x_sync_key)
    if not os.environ.get("LINGXING_APP_ID") or not os.environ.get("LINGXING_APP_SECRET"):
        raise HTTPException(status_code=503, detail="领星 API 尚未配置")
    try:
        raw_stores = await lingxing_store_rows()
        stores = []
        for item in raw_stores:
            stores.append({
                "sid": int(item.get("sid")) if item.get("sid") is not None else None,
                "name": str(item.get("name") or item.get("account_name") or "未命名店铺"),
                "country": str(item.get("country") or "未知站点"),
                "region": str(item.get("region") or ""),
                "seller_id": str(item.get("seller_id") or ""),
                "has_ads_setting": int(item.get("has_ads_setting") or 0),
                "status": int(item.get("status") or 0),
            })
        return {"stores": stores, "count": len(stores)}
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"领星店铺列表请求失败：HTTP {exc.response.status_code}") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="领星店铺列表获取失败") from exc


@app.post("/api/sync")
async def sync(
    trigger: str = Query(default="button", pattern="^(button|dashboard)$"),
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
):
    configured_key = os.environ.get("SYNC_API_KEY", "").strip()
    if not configured_key or x_sync_key != configured_key:
        raise HTTPException(status_code=401, detail="同步接口需要有效的 X-Sync-Key")
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
        require_business_access(x_sync_key)
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
            configured_key = os.environ.get("SYNC_API_KEY", "").strip()
            if not configured_key or x_sync_key != configured_key:
                raise HTTPException(status_code=401, detail="自动同步需要有效的 X-Sync-Key")
            await run_sync("dashboard")
        with factory() as db:
            return dashboard_payload(db, period_start, period_end, store)
    except HTTPException:
        raise
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="看板服务暂不可用") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Dashboard query failed") from exc
