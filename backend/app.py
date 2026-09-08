from __future__ import annotations

import asyncio
import json
import os
import re
import base64
import hashlib
import time
from urllib.parse import quote
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import Body, FastAPI, Header, HTTPException, Query
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
    """Company-internal deployment: business APIs do not require a key."""
    return None

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
AMAZON_SUPPORTED_CURRENCIES = ("USD", "CNY", "JPY", "EUR", "GBP", "CAD", "AUD", "SEK")

DEFAULT_TIMEZONE = "America/Los_Angeles"
DEFAULT_API_VERSION = "2026-07"
DEFAULT_INITIAL_SYNC_DAYS = 90
DEFAULT_SYNC_COOLDOWN_SECONDS = 600

AMAZON_STRATEGY_OPTIONS = ("品类词", "品牌防御", "竞品词", "自动", "SB/SBV", "SD", "B2B", "/")

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
    "瑞典": "Europe/Stockholm",
}
AMAZON_MAX_DATE_RANGE_DAYS = 400

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


class AmazonStrategyNote(Base):
    __tablename__ = "amazon_strategy_notes"
    __table_args__ = (
        UniqueConstraint("site_code", "strategy", name="uq_amazon_strategy_note"),
        Index("ix_amazon_strategy_note_site", "site_code"),
    )

    id = Column(Integer, primary_key=True)
    site_code = Column(String(12), nullable=False)
    strategy = Column(String(80), nullable=False)
    note = Column(Text, nullable=False, default="")
    updated_at = Column(DateTime(timezone=True), nullable=False)


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


def ad_report_campaign_id(row: dict[str, Any]) -> str:
    for key in ("campaign_id", "campaignId", "campaignID", "ads_id", "adsId", "id"):
        value = row.get(key)
        if value not in (None, "") and not isinstance(value, (dict, list)):
            return str(value).strip()
    return ""


def ad_report_campaign_name(row: dict[str, Any]) -> str:
    for key in ("name", "campaign_name", "campaignName", "campaign"):
        value = row.get(key)
        if value not in (None, "") and not isinstance(value, (dict, list)):
            return str(value).strip()
    return ""


def ad_report_number(row: dict[str, Any], *names: str) -> float:
    value = optional_value(row, *names)
    return float(value or 0)


def normalize_strategy(value: Any) -> str:
    strategy = str(value or "/").strip()
    return strategy or "/"


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


def strategy_campaign_id(row: dict[str, Any]) -> str:
    value = ad_report_campaign_id(row)
    return value or f"name:{strategy_campaign_name(row)}"


def strategy_campaign_name(row: dict[str, Any]) -> str:
    for key in ("campaign_name", "campaignName", "name", "campaign", "ads_name", "adsName", "ad_name"):
        value = row.get(key)
        if value not in (None, "") and not isinstance(value, (dict, list)):
            return str(value).strip()
    return "未命名广告活动"


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
        "cpc": ad_cost / clicks if clicks else None,
        "ad_cost": ad_cost,
        "ad_sales": ad_sales,
        "acos": ad_cost / ad_sales if ad_sales else None,
        "roas": ad_sales / ad_cost if ad_cost else None,
        "ad_cvr": ad_orders / clicks if clicks else None,
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
) -> list[dict[str, Any]]:
    """Read LingXing's product-performance endpoint for operating metrics."""
    # An explicitly empty list means the selected products have no ASINs
    # mapped for this site; avoid issuing an unfiltered request in that case.
    if asin_list == []:
        return []
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
        "impressions": ("impressions",),
        "clicks": ("clicks",),
        "ad_sales": ("ad_sales_amount", "ads_sales_amount", "adSalesAmount"),
        "ad_cost": ("spend", "ad_cost", "advertising_spend"),
        "ad_units": ("ads_sales_volume_quantity", "ad_sales_volume_quantity", "adUnits"),
        "ad_orders": ("ad_order_quantity", "ad_orders", "adOrders"),
    },
}
AMAZON_SOURCE_RATIOS = {
    "performance": {"source_cvr": ("cvr", "conversion_rate", "conversionRate")},
}
AMAZON_METRIC_SOURCES = {
    "performance": ["units", "net_sales", "orders", "b2b_units", "b2b_orders", "sessions", "cvr", "impressions", "clicks", "ad_sales", "ad_cost", "ad_units", "ad_orders", "ctr", "cpc", "ad_cvr", "acos"],
    "calculated": ["acoas", "ad_sales_share", "ad_order_share"],
}

AMAZON_AD_BREAKDOWN_FIELDS = {
    "sp": {
        "impressions": ("ad_impressions_sp", "adImpressionsSp", "impressions_sp"),
        "clicks": ("ad_clicks_sp", "adClicksSp", "clicks_sp"),
        "ad_cost": ("ads_sp_cost", "adSpendSp", "spend_sp"),
        "ad_units": ("ads_sp_sales_volume_quantity", "adSalesVolumeQuantitySp", "ad_units_sp"),
        "ad_orders": ("ad_order_quantity_sp", "adOrderQuantitySp", "ad_orders_sp"),
        "ad_sales": ("ads_sp_sales", "adsSpSales", "ad_sales_sp"),
    },
    "sb": {
        "impressions": ("shared_ad_impressions_sb", "sharedAdImpressionsSb", "ad_impressions_sb"),
        "clicks": ("shared_ad_clicks_sb", "sharedAdClicksSb", "ad_clicks_sb"),
        "ad_cost": ("shared_ads_sb_cost", "sharedAdsSbCost", "ad_spend_sb"),
        "ad_units": ("shared_ads_sb_sales_volume_quantity", "sharedAdsSbSalesVolumeQuantity", "ad_units_sb"),
        "ad_orders": ("shared_ad_order_quantity_sb", "sharedAdOrderQuantitySb", "ad_orders_sb"),
        "ad_sales": ("shared_ads_sb_sales", "sharedAdsSbSales", "ad_sales_sb"),
    },
    "sbv": {
        "impressions": ("shared_ad_impressions_sbv", "sharedAdImpressionsSbv", "ad_impressions_sbv"),
        "clicks": ("shared_ad_clicks_sbv", "sharedAdClicksSbv", "ad_clicks_sbv"),
        "ad_cost": ("shared_ads_sbv_cost", "sharedAdsSbvCost", "ad_spend_sbv"),
        "ad_units": ("shared_ads_sbv_sales_volume_quantity", "sharedAdsSbvSalesVolumeQuantity", "ad_units_sbv"),
        "ad_orders": ("shared_ad_order_quantity_sbv", "sharedAdOrderQuantitySbv", "ad_orders_sbv"),
        "ad_sales": ("shared_ads_sbv_sales", "sharedAdsSbvSales", "ad_sales_sbv"),
    },
    "sd": {
        "impressions": ("ad_impressions_sd", "adImpressionsSd", "impressions_sd"),
        "clicks": ("ad_clicks_sd", "adClicksSd", "clicks_sd"),
        "ad_cost": ("ads_sd_cost", "adSpendSd", "spend_sd"),
        "ad_units": ("ads_sd_sales_volume_quantity", "adSalesVolumeQuantitySd", "ad_units_sd"),
        "ad_orders": ("ad_order_quantity_sd", "adOrderQuantitySd", "ad_orders_sd"),
        "ad_sales": ("ads_sd_sales", "adsSdSales", "ad_sales_sd"),
    },
}


def product_performance_ad_breakdown(raw: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Normalize LingXing's product-performance advertising dimensions."""
    result: dict[str, dict[str, float]] = {}
    for ad_type, fields in AMAZON_AD_BREAKDOWN_FIELDS.items():
        result[ad_type] = {}
        for metric, names in fields.items():
            result[ad_type][metric] = optional_metric(raw, *names) or 0.0
    return result


def product_performance_ad_totals(raw: dict[str, Any]) -> dict[str, float]:
    """Sum the four product-performance ad types when those fields are present.

    LingXing's generic ``clicks``/``spend`` fields can be incomplete for some
    accounts.  The typed fields are the authoritative product-performance
    dimensions for this dashboard, so prefer their sum and let the caller
    fall back to the generic field only when no typed field was returned.
    """
    totals: dict[str, float] = {}
    for metric, _ in next(iter(AMAZON_AD_BREAKDOWN_FIELDS.values())).items():
        present = False
        total = 0.0
        for fields in AMAZON_AD_BREAKDOWN_FIELDS.values():
            names = fields[metric]
            if any(name in raw and raw.get(name) not in (None, "") for name in names):
                present = True
            total += optional_metric(raw, *names) or 0.0
        if present:
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
    if isinstance(site, str):
        selected_sites = [site] if site else list(AMAZON_SITE_CODES)
    else:
        selected_sites = list(site or AMAZON_SITE_CODES)
    selected_sites = list(dict.fromkeys(item for item in selected_sites if item in AMAZON_SITE_CODES)) or list(AMAZON_SITE_CODES)
    is_multi_site = len(selected_sites) > 1
    requested_currency = str(display_currency or "original").upper()
    if requested_currency == "ORIGINAL":
        requested_currency = "original"
    if requested_currency != "original" and requested_currency not in AMAZON_SUPPORTED_CURRENCIES:
        raise ValueError(f"不支持的货币：{display_currency}")
    semaphore = asyncio.Semaphore(AMAZON_UPSTREAM_CONCURRENCY)
    cache_key = ("periodic-dashboard-v4", comparison, start_date.isoformat(), end_date.isoformat(), tuple(selected_sites), requested_currency, tuple(sorted(selected_series)), tuple(sorted(selected_products)))
    cached = _amazon_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < AMAZON_CACHE_TTL_SECONDS:
        return cached[1]

    async with httpx.AsyncClient(timeout=45) as client:
        async def fetch_site(site_name: str):
            site_code = AMAZON_SITE_CODES.get(site_name, site_name)
            accounts = amazon_sid_accounts(site_name, sid_map, store_rows)
            if not accounts:
                return site_name, site_code, AMAZON_CURRENCY_CODES.get(site_name, "USD"), []
            native_currency = AMAZON_CURRENCY_CODES.get(site_name, "USD")
            query_currency = native_currency if requested_currency == "original" else requested_currency
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
                            client, semaphore, asin_filter, query_currency,
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
                all_rows.extend(performance_rows)
            return site_name, site_code, query_currency, all_rows

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
            item = aggregate.setdefault(key, {"period": period_label, "site": site_name, "site_code": site_code, "period_start": period_start.isoformat(), "period_end": period_end.isoformat(), "currency": row_currency, "asins": set(), "ad_breakdown": {ad_type: {metric: 0.0 for metric in fields} for ad_type, fields in AMAZON_AD_BREAKDOWN_FIELDS.items()}})
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
                if field in breakdown_totals:
                    value = breakdown_totals[field]
                if value is not None:
                    item[field] = (item.get(field) or 0) + value
            for field, names in source_fields.items():
                value = optional_metric(raw, *names)
                if value is not None:
                    item[field] = value
            for ad_type, metrics in breakdown.items():
                for metric, value in metrics.items():
                    item["ad_breakdown"][ad_type][metric] += value

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
        ad_units = item.get("ad_units")
        ad_sales_share = (ad_units / units) if ad_units is not None and units else None
        ad_order_share = (ad_orders / orders) if ad_orders is not None and orders else None
        # ACoAS is defined by the dashboard requirement as ad spend divided
        # by net sales. Recalculate it from the period totals instead of
        # trusting a range-level/source value that may use another denominator.
        calculated_acoas = (ad_cost / net_sales) if ad_cost is not None and net_sales else None
        rows.append({
            "period": item["period"], "period_start": item["period_start"], "period_end": item["period_end"],
            "site": site_name, "site_code": item.get("site_code"), "series": group, "product": product, "asin": ", ".join(sorted(item.get("asins") or [])) or None, "currency": item.get("currency", "USD"),
            "units": int(units) if units is not None else None, "net_sales": net_sales, "orders": int(orders) if orders is not None else None,
            "b2b_units": int(item["b2b_units"]) if item.get("b2b_units") is not None else None, "b2b_orders": int(item["b2b_orders"]) if item.get("b2b_orders") is not None else None,
            "ctr": clicks / impressions if clicks is not None and impressions else item.get("source_ctr"), "clicks": int(clicks) if clicks is not None else None,
            "impressions": int(impressions) if impressions is not None else None, "cpc": ad_cost / clicks if ad_cost is not None and clicks else item.get("source_cpc"),
            "ad_cost": ad_cost, "ad_cvr": ad_orders / clicks if ad_orders is not None and clicks else item.get("source_ad_cvr"),
            "ad_units": int(ad_units) if ad_units is not None else None, "ad_orders": int(ad_orders) if ad_orders is not None else None,
            "cvr": item.get("source_cvr"), "acos": ad_cost / ad_sales if ad_cost is not None and ad_sales else item.get("source_acos"),
            "acoas": calculated_acoas, "ad_sales_share": ad_sales_share, "ad_order_share": ad_order_share, "ad_sales": ad_sales,
            "sessions": int(sessions) if sessions is not None else None, "ad_breakdown": item.get("ad_breakdown", {}),
        })
    output_currency = requested_currency if requested_currency != "original" else (AMAZON_CURRENCY_CODES.get(selected_sites[0], "USD") if len(selected_sites) == 1 else "original")
    response = {
        "period": {"comparison": comparison, "start": start_date.isoformat(), "end": end_date.isoformat()},
        "currency": output_currency,
        "currency_mode": requested_currency,
        "selected_sites": selected_sites,
        "filters": {"site": selected_sites, "series": list(selected_series), "products": list(selected_products)},
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


async def amazon_strategy_board_payload(
    start_date: date,
    end_date: date,
    selected_sites: list[str],
    sid_map: dict[str, Any],
    store_rows: list[dict[str, Any]] | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Aggregate campaigns from LingXing's advertising backend by strategy.

    Strategy assignments are keyed by site and campaign id and therefore live
    independently from the selected date range.  The report itself remains a
    date-scoped snapshot, and campaigns with zero clicks are omitted.
    """
    selected_sites = list(dict.fromkeys(selected_sites))
    cache_key = ("amazon-strategy-board-v1", start_date.isoformat(), end_date.isoformat(), tuple(selected_sites))
    if refresh:
        _amazon_cache.pop(cache_key, None)
    cached = _amazon_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < AMAZON_CACHE_TTL_SECONDS:
        return cached[1]

    semaphore = asyncio.Semaphore(AMAZON_UPSTREAM_CONCURRENCY)
    async with httpx.AsyncClient(timeout=45) as client:
        async def fetch_site(site_name: str):
            site_code = strategy_site_code(site_name)
            accounts = amazon_sid_accounts(site_name, sid_map, store_rows)
            rows: list[dict[str, Any]] = []
            for account in accounts:
                try:
                    rows.extend(await fetch_ad_reports_range(int(account["sid"]), start_date, end_date, client, semaphore))
                except RuntimeError as exc:
                    if "白名单" in str(exc) or "ip not permit" in str(exc).lower():
                        continue
                    raise
            return site_name, site_code, rows

        fetched = await asyncio.gather(*(fetch_site(site_name) for site_name in selected_sites))

    assignments: dict[tuple[str, str], str] = {}
    notes: dict[tuple[str, str], str] = {}
    with session_factory() as db:
        for item in db.scalars(select(AmazonCampaignStrategy).where(AmazonCampaignStrategy.site_code.in_([strategy_site_code(s) for s in selected_sites]))):
            assignments[(item.site_code, item.campaign_id)] = normalize_strategy(item.strategy)
        for item in db.scalars(select(AmazonStrategyNote).where(AmazonStrategyNote.site_code.in_([strategy_site_code(s) for s in selected_sites]))):
            notes[(item.site_code, normalize_strategy(item.strategy))] = item.note

    aggregate: dict[tuple[str, str], dict[str, Any]] = {}
    for site_name, site_code, raw_rows in fetched:
        for raw in raw_rows:
            campaign_id = strategy_campaign_id(raw)
            campaign_name = strategy_campaign_name(raw)
            if not campaign_id or not campaign_name:
                continue
            metrics = strategy_metrics(raw)
            key = (site_code, campaign_id)
            item = aggregate.setdefault(key, {"site": site_name, "site_code": site_code, "campaign_id": campaign_id, "campaign_name": campaign_name, "metrics": {name: 0.0 for name in ("impressions", "clicks", "ad_cost", "ad_sales", "ad_units", "ad_orders")}, "currency": str(raw.get("currency") or raw.get("currency_code") or AMAZON_CURRENCY_CODES.get(site_name, "USD"))})
            if campaign_name != "未命名广告活动":
                item["campaign_name"] = campaign_name
            for name, value in metrics.items():
                item["metrics"][name] += value

    metric_names = ("impressions", "clicks", "ad_cost", "ad_sales", "ad_units", "ad_orders")
    strategies: dict[tuple[str, str], dict[str, Any]] = {
        (strategy_site_code(site), name): {"strategy": name, "note": notes.get((strategy_site_code(site), name), ""), "metrics": {metric: 0.0 for metric in metric_names}, "campaigns": [], "sites": []}
        for site in selected_sites for name in AMAZON_STRATEGY_OPTIONS
    }
    for (site_code, campaign_id), item in aggregate.items():
        if item["metrics"]["clicks"] <= 0:
            continue
        strategy = assignments.get((site_code, campaign_id), "/")
        if strategy not in AMAZON_STRATEGY_OPTIONS:
            strategy = "/"
        # Keep a separate site row when multiple marketplaces are selected, so
        # native currencies never get added together.
        group_key = (site_code, strategy)
        group = strategies.get(group_key)
        if group is None:
            group = {"strategy": strategy, "note": notes.get((site_code, strategy), ""), "metrics": {name: 0.0 for name in metric_names}, "campaigns": [], "sites": []}
            strategies[group_key] = group
        group["sites"] = [item["site"]]
        for name, value in item["metrics"].items():
            group["metrics"][name] += value
        group["campaigns"].append({"campaign_id": campaign_id, "campaign_name": item["campaign_name"], "site": item["site"], "site_code": site_code, "currency": item["currency"], **finalize_strategy_metrics(item["metrics"])})

    output = []
    for site_name in selected_sites:
        site_code = strategy_site_code(site_name)
        for strategy in AMAZON_STRATEGY_OPTIONS:
            group = strategies[(site_code, strategy)]
            group["campaigns"].sort(key=lambda campaign: (-campaign["clicks"], campaign["campaign_name"]))
            group["metrics"] = finalize_strategy_metrics(group["metrics"])
            group["site"] = site_name
            group["site_code"] = site_code
            group["currency"] = AMAZON_CURRENCY_CODES.get(site_name, "USD")
            output.append(group)
    response = {"period": {"start": start_date.isoformat(), "end": end_date.isoformat()}, "strategies": output, "strategy_options": list(AMAZON_STRATEGY_OPTIONS), "selected_sites": selected_sites}
    _amazon_cache[cache_key] = (time.monotonic(), response)
    return response


@app.get("/api/amazon/strategy-board")
async def amazon_strategy_board(
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    site: list[str] = Query(default=[]),
    refresh: bool = Query(default=False),
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
):
    require_business_access(x_sync_key)
    start_date, end_date = strategy_date_range(start_date, end_date)
    selected_sites = list(dict.fromkeys(value for raw in site for value in str(raw).split(",") if value in AMAZON_SITE_CODES)) or list(AMAZON_SITE_CODES)
    if not os.environ.get("LINGXING_APP_ID") or not os.environ.get("LINGXING_APP_SECRET"):
        raise HTTPException(status_code=503, detail="领星 API 尚未配置")
    try:
        sid_map = json.loads(os.environ.get("LINGXING_SIDS_JSON", "{}"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=503, detail="LINGXING_SIDS_JSON 配置格式错误") from exc
    store_rows = await lingxing_store_rows() if any(site_name == "日本" for site_name in selected_sites) or not sid_map else []
    try:
        return await amazon_strategy_board_payload(start_date, end_date, selected_sites, sid_map, store_rows, refresh)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"广告报表请求失败：HTTP {exc.response.status_code}") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="广告策略数据获取失败") from exc


@app.post("/api/amazon/strategy-board/campaign-strategy")
def save_campaign_strategy(
    payload: dict[str, Any] = Body(...),
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
):
    require_business_access(x_sync_key)
    site_code = str(payload.get("site_code") or "").strip().upper()
    campaign_id = str(payload.get("campaign_id") or "").strip()
    strategy = normalize_strategy(payload.get("strategy"))
    if site_code not in AMAZON_SITE_CODES.values() or not campaign_id or strategy not in AMAZON_STRATEGY_OPTIONS:
        raise HTTPException(status_code=422, detail="广告活动策略参数无效")
    with session_factory() as db:
        item = db.scalar(select(AmazonCampaignStrategy).where(AmazonCampaignStrategy.site_code == site_code, AmazonCampaignStrategy.campaign_id == campaign_id))
        if item is None:
            item = AmazonCampaignStrategy(site_code=site_code, campaign_id=campaign_id)
            db.add(item)
        item.campaign_name = str(payload.get("campaign_name") or "")[:500]
        item.strategy = strategy
        item.updated_at = utcnow()
        db.commit()
    _amazon_cache.clear()
    return {"ok": True, "site_code": site_code, "campaign_id": campaign_id, "strategy": strategy}


@app.post("/api/amazon/strategy-board/note")
def save_strategy_note(
    payload: dict[str, Any] = Body(...),
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
):
    require_business_access(x_sync_key)
    site_code = str(payload.get("site_code") or "").strip().upper()
    strategy = normalize_strategy(payload.get("strategy"))
    note = str(payload.get("note") or "")
    if site_code not in AMAZON_SITE_CODES.values() or strategy not in AMAZON_STRATEGY_OPTIONS:
        raise HTTPException(status_code=422, detail="策略备注参数无效")
    with session_factory() as db:
        item = db.scalar(select(AmazonStrategyNote).where(AmazonStrategyNote.site_code == site_code, AmazonStrategyNote.strategy == strategy))
        if item is None:
            item = AmazonStrategyNote(site_code=site_code, strategy=strategy)
            db.add(item)
        item.note = note
        item.updated_at = utcnow()
        db.commit()
    _amazon_cache.clear()
    return {"ok": True, "site_code": site_code, "strategy": strategy, "note": note}


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
    require_business_access(x_sync_key)
    selected_sites = list(dict.fromkeys(value for raw in site for value in str(raw).split(",") if value in AMAZON_SITE_CODES))
    if not selected_sites:
        selected_sites = list(AMAZON_SITE_CODES)
    today = min(
        datetime.now(ZoneInfo(AMAZON_SITE_TIMEZONES.get(site_name, DEFAULT_TIMEZONE))).date()
        for site_name in selected_sites
    )
    requested_currency = str(currency or "original").strip().upper()
    if requested_currency == "ORIGINAL":
        requested_currency = "original"
    if requested_currency != "original" and requested_currency not in AMAZON_SUPPORTED_CURRENCIES:
        raise HTTPException(status_code=422, detail="不支持的货币")
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
        _amazon_cache.clear()
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


@app.get("/api/amazon/stores")
async def amazon_stores(
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
):
    require_business_access(x_sync_key)
    """Return read-only Amazon stores without exposing credentials."""
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


@app.get("/api/amazon/date-context")
async def amazon_date_context(
    site: list[str] = Query(default=["美国"]),
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
):
    """Return the current calendar date for the selected marketplace site."""
    require_business_access(x_sync_key)
    selected_sites = list(dict.fromkeys(value for raw in site for value in str(raw).split(",") if value in AMAZON_SITE_CODES))
    if not selected_sites:
        raise HTTPException(status_code=422, detail="不支持的 Amazon 站点")
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
            await run_sync("dashboard")
        with factory() as db:
            return dashboard_payload(db, period_start, period_end, store)
    except HTTPException:
        raise
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="看板服务暂不可用") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Dashboard query failed") from exc
