"""Order history for Albert Heijn: delivered orders kept in HA storage, frequently ordered products."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import logging
from statistics import median
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .api import AlbertHeijnApi, AlbertHeijnApiError, Fulfillment, OrderItem
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
# Recent orders count more: an order from HALF_LIFE_DAYS ago weighs half as much as one from today
HALF_LIFE_DAYS = 180
CATALOG_MAX_PAGES = 10
CATALOG_PAGE_SIZE = 100
REQUEST_PAUSE = 0.3  # seconds between order detail requests during a sync
PRICE_EVERY_DAYS = 7  # weekly price check
PRICE_PRODUCTS = 300  # how many products (most ordered first, then "Eerder gekocht")
PRICE_BATCH = 30
PRICE_KEEP = 156  # weekly points per product (3 years)


def _item_dict(item: OrderItem) -> dict[str, Any]:
    d = asdict(item)
    return {k: v for k, v in d.items() if v not in (None, "")}


class OrderHistory:
    """Delivered orders stored locally, so history keeps growing even if AH forgets old orders.

    Stored per order: delivery date, status ("open" snapshot or "delivered") and its products.
    Also keeps AH's own "Eerder gekocht" list as a catalog of everything ever bought.
    """

    def __init__(self, hass: HomeAssistant, api: AlbertHeijnApi, entry_id: str) -> None:
        self._api = api
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.history.{entry_id}")
        self._lock = asyncio.Lock()
        self.data: dict[str, Any] = {
            "orders": {}, "catalog": [], "catalog_at": None, "synced_at": None, "prices": {}, "prices_at": None,
        }
        self.closed_supported: bool | None = None
        self.last_error: str | None = None

    async def async_load(self) -> None:
        if stored := await self._store.async_load():
            self.data.update(stored)

    @property
    def orders(self) -> dict[str, dict[str, Any]]:
        return self.data["orders"]

    def delivered(self) -> list[dict[str, Any]]:
        return [o for o in self.orders.values() if o.get("status") == "delivered"]

    async def async_sync(self, open_orders: list[Fulfillment], refresh_catalog: bool = False) -> bool:
        """Fetch new delivered orders (and the catalog once a day). Returns True if anything changed."""
        if self._lock.locked():
            return False
        async with self._lock:
            changed = False
            today = date.today().isoformat()
            self.last_error = None

            # 1. Delivered orders straight from AH (undocumented CLOSED status)
            try:
                closed = await self._api.get_closed_fulfillments()
                self.closed_supported = True
            except AlbertHeijnApiError as err:
                _LOGGER.debug("Closed orders not available: %s", err)
                self.closed_supported = False
                closed = []
            for order_id, day in closed:
                key = str(order_id)
                if self.orders.get(key, {}).get("status") == "delivered":
                    continue
                if await self._fetch_order(key, day, "delivered"):
                    changed = True
                await asyncio.sleep(REQUEST_PAUSE)

            # 2. Snapshot of open orders with products; one that is gone after its delivery date was delivered
            open_ids = {str(f.order_id) for f in open_orders}
            for f in open_orders:
                if f.total_price > 0 and await self._fetch_order(str(f.order_id), f.delivery_slot.date, "open"):
                    changed = True
                await asyncio.sleep(REQUEST_PAUSE)
            for key, order in self.orders.items():
                if order.get("status") == "open" and key not in open_ids and order.get("date", "9999") < today:
                    order["status"] = "delivered"
                    changed = True

            # 3. AH "Eerder gekocht" catalog, once a day
            if refresh_catalog or (self.data.get("catalog_at") or "")[:10] != today:
                try:
                    catalog: list[dict[str, Any]] = []
                    page, pages = 0, 1
                    while page < min(pages, CATALOG_MAX_PAGES):
                        products, pages = await self._api.get_previously_bought(CATALOG_PAGE_SIZE, page)
                        catalog.extend(products)
                        page += 1
                    self.data["catalog"] = catalog
                    self.data["catalog_at"] = datetime.now(timezone.utc).isoformat()
                    changed = True
                except AlbertHeijnApiError as err:
                    _LOGGER.debug("Previously bought not available: %s", err)
                    self.last_error = f"Eerder gekocht: {err}"

            # 4. Weekly price check (price, price without bonus, bonus) for the products we buy
            last = self.data.get("prices_at")
            if refresh_catalog or not last or date.fromisoformat(last[:10]) <= date.today() - timedelta(days=PRICE_EVERY_DAYS):
                if await self._fetch_prices():
                    changed = True

            self.data["synced_at"] = datetime.now(timezone.utc).isoformat()
            await self._store.async_save(self.data)
            return changed

    async def _fetch_prices(self) -> bool:
        ids: list[int] = []
        for p in self.frequent(PRICE_PRODUCTS, include_catalog=True):
            if p.get("product_id") and p["product_id"] not in ids:
                ids.append(int(p["product_id"]))
        if not ids:
            return False
        today = date.today().isoformat()
        prices = self.data.setdefault("prices", {})
        got = 0
        for i in range(0, len(ids), PRICE_BATCH):
            try:
                rows = await self._api.get_prices(ids[i : i + PRICE_BATCH])
            except AlbertHeijnApiError as err:
                _LOGGER.debug("Price check failed: %s", err)
                self.last_error = f"Prijzen: {err}"
                break
            for r in rows:
                if r.get("price") is None:
                    continue
                series = prices.setdefault(str(r["product_id"]), [])
                point = {"d": today, "p": r["price"], "b": r.get("price_before_bonus"), "bonus": r.get("bonus"), "u": r.get("unit_price")}
                if series and series[-1].get("d") == today:
                    series[-1] = point
                else:
                    series.append(point)
                del series[:-PRICE_KEEP]
                got += 1
            await asyncio.sleep(REQUEST_PAUSE)
        if got:
            self.data["prices_at"] = datetime.now(timezone.utc).isoformat()
        return got > 0

    def price_history(self, product_id: int) -> list[dict[str, Any]]:
        """Weekly prices of one product, oldest first, plus the prices paid in delivered orders."""
        weekly = self.data.get("prices", {}).get(str(product_id), [])
        paid = [
            {"d": o.get("date"), "p": i.get("price"), "b": i.get("price_before_bonus"), "bonus": i.get("bonus"), "order": True}
            for o in self.delivered()
            for i in o.get("items", [])
            if i.get("product_id") == product_id
        ]
        return sorted(weekly + paid, key=lambda x: x.get("d") or "")

    async def _fetch_order(self, key: str, day: str, status: str) -> bool:
        try:
            items = await self._api.get_order_items(int(key))
        except AlbertHeijnApiError as err:
            _LOGGER.debug("Could not fetch items of order %s: %s", key, err)
            return False
        if not items:
            return False
        new = {"date": day, "status": status, "items": [_item_dict(i) for i in items]}
        if self.orders.get(key) == new:
            return False
        self.orders[key] = new
        return True

    def frequent(self, limit: int = 100, include_catalog: bool = True) -> list[dict[str, Any]]:
        """Products sorted by how often (and how recently) they were delivered, with their order rhythm.

        Per product: count (orders), quantity (total), usual_qty (median per order), every_weeks (median gap),
        share (part of the orders since it was first ordered), per_week (average quantity per week),
        days_since, due (it's about time again), and the latest weekly price.
        """
        today = date.today()
        orders = []
        for order in self.delivered():
            try:
                orders.append((date.fromisoformat(order.get("date", "")), order))
            except ValueError:
                continue
        orders.sort(key=lambda x: x[0])
        order_days = [d for d, _ in orders]
        latest = order_days[-1] if order_days else today

        stats: dict[int, dict[str, Any]] = {}
        for day, order in orders:
            weight = 0.5 ** (max((today - day).days, 0) / HALF_LIFE_DAYS)
            for item in order.get("items", []):
                pid = item.get("product_id")
                if not pid:
                    continue
                s = stats.get(pid)
                if s is None:
                    s = stats[pid] = {
                        "product_id": pid, "title": item.get("title", ""), "brand": item.get("brand", ""),
                        "unit_size": item.get("unit_size", ""), "category": item.get("category", ""),
                        "image_url": item.get("image_url"), "count": 0, "quantity": 0, "score": 0.0,
                        "_days": [], "_qty": [],
                    }
                qty = int(item.get("quantity") or 1)
                if s["_days"] and s["_days"][-1] == day:
                    s["_qty"][-1] += qty
                else:
                    s["_days"].append(day)
                    s["_qty"].append(qty)
                    s["count"] += 1
                    s["score"] += weight
                s["quantity"] += qty
                s["title"] = item.get("title") or s["title"]
                s["image_url"] = item.get("image_url") or s["image_url"]

        prices = self.data.get("prices", {})
        result = sorted(stats.values(), key=lambda s: (-s["score"], -s["count"], s["title"]))
        for s in result:
            days, qtys = s.pop("_days"), s.pop("_qty")
            first, last = days[0], days[-1]
            since_first = sum(1 for d in order_days if d >= first)
            gaps = [(b - a).days for a, b in zip(days, days[1:])]
            gap = median(gaps) if gaps else None
            weeks = max(1.0, ((latest - first).days / 7) + 1)
            s.update({
                "first": first.isoformat(), "last": last.isoformat(),
                "days_since": (today - last).days,
                "usual_qty": int(median(qtys)),
                "every_weeks": max(1, round(gap / 7)) if gap else None,
                "interval_days": round(gap) if gap else None,
                "share": round(s["count"] / since_first, 2) if since_first else None,
                "per_week": round(s["quantity"] / weeks, 1),
                "due": bool(gap) and (today - last).days >= gap,
                "score": round(s["score"], 2),
            })
            if series := prices.get(str(s["product_id"])):
                s["price"], s["price_bonus"] = series[-1].get("p"), series[-1].get("bonus")

        if include_catalog:
            seen = set(stats)
            extra = [
                {**p, "count": 0, "quantity": 0, "score": 0, "first": None, "last": None, "interval_days": None,
                 "every_weeks": None, "usual_qty": None, "share": None, "per_week": None, "days_since": None, "due": False}
                for p in self.data.get("catalog") or []
                if p.get("product_id") not in seen
            ]
            result += sorted(extra, key=lambda p: p.get("title", "").lower())
        return result[:limit]
