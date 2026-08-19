import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
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
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)

Base = declarative_base()
_engine = None
_session_factory = None

DEFAULT_TIMEZONE = "America/Los_Angeles"
DEFAULT_API_VERSION = "2026-07"
DEFAULT_INITIAL_SYNC_DAYS = 90
DEFAULT_SYNC_COOLDOWN_SECONDS = 600

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
                failed_run.message = str(exc)[:1000]
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
        select(Order.store_domain, Order.store_name).distinct().order_by(Order.store_name)
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
    return {
        "configured": configured and database_configured,
        "shopify_configured": configured,
        "database_configured": database_configured,
        "sync_mode": "on_demand",
    }


@app.post("/api/sync")
async def sync(trigger: str = Query(default="button", pattern="^(button|dashboard)$")):
    try:
        return await run_sync(trigger)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Shopify request failed: HTTP {exc.response.status_code}") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Sync failed: {str(exc)[:300]}") from exc


@app.get("/api/dashboard")
async def dashboard(
    days: int = Query(default=30, ge=1, le=180),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    store: str | None = Query(default=None, max_length=255),
    auto_sync: bool = Query(default=True),
):
    try:
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
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Dashboard query failed: {str(exc)[:300]}") from exc
