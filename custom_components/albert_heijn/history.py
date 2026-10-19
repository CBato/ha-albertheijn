"""Order history for Albert Heijn: delivered orders and weekly prices in a SQLite database (db.py).

The database keeps growing even when AH stops returning old orders. Sensors and services read from an
in-memory cache that is rebuilt after each sync, so nothing blocks the event loop.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import logging
from statistics import median
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from . import db
from .api import AlbertHeijnApi, AlbertHeijnApiError, Fulfillment
from .suggest import REGULAR_DAYS, compute_suggestions, effective_price, week_start
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

DB_FILE = "albert_heijn.db"
# Recent orders count more: an order from HALF_LIFE_DAYS ago weighs half as much as one from today
HALF_LIFE_DAYS = 180
CATALOG_MAX_PAGES = 20  # 2000 products
CATALOG_PAGE_SIZE = 100
REQUEST_PAUSE = 0.3  # seconds between requests to AH during a sync
PRICE_EVERY_DAYS = 7  # weekly price check
PRICE_MAX_PRODUCTS = 2000
PRICE_BATCH = 30
CATALOG_EVERY_DAYS = 7


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _older_than(stamp: str | None, days: int) -> bool:
    if not stamp:
        return True
    try:
        return date.fromisoformat(stamp[:10]) <= date.today() - timedelta(days=days)
    except ValueError:
        return True


def compute_frequent(snapshot: dict[str, Any], today: date | None = None, horizon_days: int = 7) -> list[dict[str, Any]]:
    """Products sorted by how often (and how recently) they were delivered, with their order rhythm,
    followed by the rest of "Eerder gekocht".

    Per product: count (orders), quantity (total), usual_qty (median per order), every_weeks (median gap),
    share (part of the orders since it was first ordered), per_week (average quantity per week),
    days_since, due, and the latest weekly price.

    due = the stock from the last purchase (last quantity / use per week) runs out within horizon_days
    (normally: until the delivery after the next one). So 4 pakken in de bonus last twice as long as 2.
    """
    today = today or date.today()
    order_days = [date.fromisoformat(d) for d in snapshot["order_days"]]
    latest = order_days[-1] if order_days else today
    prices = snapshot["latest_prices"]

    stats: dict[int, dict[str, Any]] = {}
    for row in snapshot["items"]:
        day = date.fromisoformat(row["date"])
        pid = row["product_id"]
        s = stats.get(pid)
        if s is None:
            s = stats[pid] = {
                "product_id": pid, "title": row.get("title") or "", "brand": row.get("brand") or "",
                "unit_size": row.get("unit_size") or "", "category": row.get("category") or "",
                "image_url": row.get("image_url"), "count": 0, "quantity": 0, "score": 0.0, "_days": [], "_qty": [],
            }
        qty = int(row.get("quantity") or 1)
        if s["_days"] and s["_days"][-1] == day:
            s["_qty"][-1] += qty
        else:
            s["_days"].append(day)
            s["_qty"].append(qty)
            s["count"] += 1
            s["score"] += 0.5 ** (max((today - day).days, 0) / HALF_LIFE_DAYS)
        s["quantity"] += qty

    result = sorted(stats.values(), key=lambda s: (-s["score"], -s["count"], s["title"]))
    for s in result:
        days, qtys = s.pop("_days"), s.pop("_qty")
        first, last = days[0], days[-1]
        since_first = sum(1 for d in order_days if d >= first)
        gaps = [(b - a).days for a, b in zip(days, days[1:])]
        gap = median(gaps) if gaps else None
        weeks = max(1.0, ((latest - first).days / 7) + 1)
        per_week = s["quantity"] / weeks
        covers = qtys[-1] / per_week * 7 if per_week else None  # days the last purchase lasts
        s.update({
            "first": first.isoformat(), "last": last.isoformat(),
            "days_since": (today - last).days,
            "usual_qty": int(median(qtys)),
            "every_weeks": max(1, round(gap / 7)) if gap else None,
            "interval_days": round(gap) if gap else None,
            "share": round(s["count"] / since_first, 2) if since_first else None,
            "per_week": round(per_week, 1),
            "covers_days": round(covers) if covers else None,
            "due": len(days) >= 2 and covers is not None and covers < (today - last).days + horizon_days,
            "score": round(s["score"], 2),
        })
        if p := prices.get(s["product_id"]):
            s["price"], s["price_bonus"] = p["price"], p["bonus"]

    seen = set(stats)
    extra = []
    for p in snapshot["catalog"]:
        if p["product_id"] in seen:
            continue
        row = {**p, "count": 0, "quantity": 0, "score": 0, "first": None, "last": None, "interval_days": None,
               "every_weeks": None, "usual_qty": None, "share": None, "per_week": None, "days_since": None, "due": False}
        if lp := prices.get(p["product_id"]):
            row["price"], row["price_bonus"] = lp["price"], lp["bonus"]
        extra.append(row)
    return result + sorted(extra, key=lambda p: (p.get("title") or "").lower())


class OrderHistory:
    """Delivered orders, AH's "Eerder gekocht" catalog and weekly prices, stored in /config/albert_heijn.db."""

    def __init__(self, hass: HomeAssistant, api: AlbertHeijnApi, entry_id: str) -> None:
        self._hass = hass
        self._api = api
        self._entry_id = entry_id
        self._path = hass.config.path(DB_FILE)
        self._lock = asyncio.Lock()
        self.meta: dict[str, str] = {}
        self.summary: dict[str, Any] = {}
        self._frequent: list[dict[str, Any]] = []
        self.closed_supported: bool | None = None
        self.last_error: str | None = None
        self.suggestions: list[dict[str, Any]] = []
        self.next_delivery: str | None = None  # set by the coordinator

    async def _run(self, func, *args):
        return await self._hass.async_add_executor_job(func, self._path, *args)

    async def async_load(self) -> None:
        await self._run(db.init)
        await self._import_old_store()
        self.meta = await self._run(db.get_meta)
        await self._refresh_cache()
        if week := self.meta.get("suggest_week"):
            self.suggestions = await self._run(db.get_suggestions, week)

    async def _import_old_store(self) -> None:
        """One-time import of the JSON history of fork 0.6/0.7 (.storage/albert_heijn.history.<entry>)."""
        store: Store[dict[str, Any]] = Store(self._hass, 1, f"{DOMAIN}.history.{self._entry_id}")
        old = await store.async_load()
        if not old:
            return
        now = _now()
        for key, order in (old.get("orders") or {}).items():
            await self._run(db.save_order, int(key), order.get("date", ""), order.get("status", "open"), order.get("items") or [], now)
        if old.get("catalog"):
            await self._run(db.save_catalog, old["catalog"], now)
        for pid, series in (old.get("prices") or {}).items():
            for pt in series:
                await self._run(db.save_prices, pt["d"], [{"product_id": int(pid), "price": pt.get("p"),
                                "price_before_bonus": pt.get("b"), "bonus": pt.get("bonus"), "unit_price": pt.get("u")}])
        await store.async_remove()
        _LOGGER.info("Albert Heijn: old JSON history imported into %s", DB_FILE)

    async def _refresh_cache(self) -> None:
        snap = await self._run(db.load_for_frequent)
        self._frequent = await self._hass.async_add_executor_job(compute_frequent, snap, None, self.horizon_days())
        days = snap["order_days"]
        self.summary = {
            "orders": len(days),
            "first_order": days[0] if days else None,
            "last_order": days[-1] if days else None,
            "products_ordered": sum(1 for p in self._frequent if p["count"]),
            "catalog": len(snap["catalog"]),
            "products": snap["products"],
            "price_points": snap["price_points"],
            "synced_at": self.meta.get("synced_at"),
            "prices_at": self.meta.get("prices_at"),
            "catalog_at": self.meta.get("catalog_at"),
        }

    def horizon_days(self) -> int:
        """Days ahead a product must last: until the delivery after the next one."""
        try:
            until = (date.fromisoformat(self.next_delivery or "") - date.today()).days
        except ValueError:
            until = 7
        return max(0, until) + 7

    def frequent(self, limit: int = 100, include_catalog: bool = True) -> list[dict[str, Any]]:
        rows = self._frequent if include_catalog else [p for p in self._frequent if p["count"]]
        return [dict(p) for p in rows[:limit]]

    async def async_price_history(self, product_id: int) -> list[dict[str, Any]]:
        return await self._run(db.price_history, product_id)

    async def async_fill_list(
        self,
        open_orders: list[Fulfillment],
        in_order: set[int],
        week: str,
        dry_run: bool = False,
        min_count: int = 3,
        min_share: float = 0.3,
        max_items: int = 25,
    ) -> dict[str, Any]:
        """Put this week's suggestions (rhythm + bonus) on the AH shopping list, and remember why."""
        today = date.today()
        # bonus information must be from this bonus week
        if (self.meta.get("prices_at") or "")[:10] < week_start(today).isoformat():
            await self.async_sync(open_orders, refresh_prices=True)
        await self._refresh_cache()  # "due" depends on today and the next delivery
        on_list = {i["product_id"] for i in await self._api.get_shopping_list() if i.get("product_id")}
        before = {s["product_id"] for s in await self._run(db.get_suggestions, week)}
        frequent = [p for p in self._frequent if p.get("count")]
        since = (today - timedelta(days=REGULAR_DAYS + 7)).isoformat()
        series = await self._run(db.price_series, [p["product_id"] for p in frequent], since)
        rows = await self._hass.async_add_executor_job(
            compute_suggestions, frequent, series, on_list | set(in_order) | before, today, min_count, min_share, max_items
        )
        if rows and not dry_run:
            await self._api.set_list_items(rows)
            await self._run(db.save_suggestions, week, rows, _now())
            self.meta["suggest_week"] = week
            await self._run(db.set_meta, {"suggest_week": week})
            self.suggestions = await self._run(db.get_suggestions, week)
        return {
            "week": week,
            "dry_run": dry_run,
            "added": rows,
            "skipped": {"on_list": len(on_list), "in_order": len(in_order), "earlier_this_week": len(before)},
        }

    async def async_sync(
        self, open_orders: list[Fulfillment], refresh_catalog: bool = False, refresh_prices: bool = False
    ) -> bool:
        """Fetch new delivered orders, the catalog and prices (each when due). Returns True if anything changed."""
        if self._lock.locked():
            return False
        async with self._lock:
            changed = False
            errors: list[str] = []
            now, today = _now(), date.today().isoformat()
            known = await self._run(db.order_status)

            # 1. Delivered orders straight from AH (undocumented CLOSED status)
            try:
                closed = await self._api.get_closed_fulfillments()
                self.closed_supported = True
            except AlbertHeijnApiError as err:
                _LOGGER.debug("Closed orders not available: %s", err)
                self.closed_supported = False
                closed = []
            for order_id, day in closed:
                if known.get(order_id) == "delivered":
                    continue
                if await self._fetch_order(order_id, day, "delivered", now):
                    changed = True
                await asyncio.sleep(REQUEST_PAUSE)

            # 2. Snapshot of open orders with products; gone after their delivery date = delivered
            for f in open_orders:
                if f.total_price > 0 and await self._fetch_order(f.order_id, f.delivery_slot.date, "open", now):
                    changed = True
                await asyncio.sleep(REQUEST_PAUSE)
            if await self._run(db.mark_delivered, {f.order_id for f in open_orders}, today):
                changed = True

            # 3. AH "Eerder gekocht" catalog, weekly
            if refresh_catalog or _older_than(self.meta.get("catalog_at"), CATALOG_EVERY_DAYS):
                try:
                    catalog: list[dict[str, Any]] = []
                    page, pages = 0, 1
                    while page < min(pages, CATALOG_MAX_PAGES):
                        products, pages = await self._api.get_previously_bought(CATALOG_PAGE_SIZE, page)
                        catalog.extend(products)
                        page += 1
                        await asyncio.sleep(REQUEST_PAUSE)
                    if catalog:
                        await self._run(db.save_catalog, catalog, now)
                        self.meta["catalog_at"] = now
                        changed = True
                except AlbertHeijnApiError as err:
                    _LOGGER.debug("Previously bought not available: %s", err)
                    errors.append(f"Eerder gekocht: {err}")

            # rebuild the cache first: the price check uses the product list
            await self._refresh_cache()

            # 4. Weekly price check for everything we buy (history first, then the catalog)
            if refresh_catalog or refresh_prices or _older_than(self.meta.get("prices_at"), PRICE_EVERY_DAYS):
                got, err = await self._fetch_prices(today)
                if err:
                    errors.append(f"Prijzen: {err}")
                if got:
                    self.meta["prices_at"] = now
                    changed = True

            self.meta["synced_at"] = now
            await self._run(db.set_meta, self.meta)
            self.last_error = "; ".join(errors) or None
            await self._refresh_cache()
            return changed

    async def _fetch_order(self, order_id: int, day: str, status: str, now: str) -> bool:
        try:
            items = await self._api.get_order_items(int(order_id))
        except AlbertHeijnApiError as err:
            _LOGGER.debug("Could not fetch items of order %s: %s", order_id, err)
            return False
        if not items:
            return False
        return await self._run(db.save_order, int(order_id), day, status, [asdict(i) for i in items], now)

    async def _fetch_prices(self, today: str) -> tuple[int, str | None]:
        ids = [int(p["product_id"]) for p in self._frequent[:PRICE_MAX_PRODUCTS] if p.get("product_id")]
        got = 0
        for i in range(0, len(ids), PRICE_BATCH):
            try:
                rows = await self._api.get_prices(ids[i : i + PRICE_BATCH])
            except AlbertHeijnApiError as err:
                _LOGGER.debug("Price check failed: %s", err)
                return got, str(err)
            for r in rows:
                r["effective"] = effective_price(r.get("bonus"), r.get("price"), r.get("price_before_bonus"))
            got += await self._run(db.save_prices, today, rows)
            await asyncio.sleep(REQUEST_PAUSE)
        return got, None
