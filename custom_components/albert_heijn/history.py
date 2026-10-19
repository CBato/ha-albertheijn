"""Order history for Albert Heijn: delivered orders kept in HA storage, frequently ordered products."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import date, datetime, timezone
import logging
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
        self.data: dict[str, Any] = {"orders": {}, "catalog": [], "catalog_at": None, "synced_at": None}
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

            self.data["synced_at"] = datetime.now(timezone.utc).isoformat()
            await self._store.async_save(self.data)
            return changed

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
        """Products sorted by how often (and how recently) they were delivered."""
        today = date.today()
        stats: dict[int, dict[str, Any]] = {}
        for order in self.delivered():
            try:
                day = date.fromisoformat(order.get("date", ""))
            except ValueError:
                continue
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
                        "image_url": item.get("image_url"), "count": 0, "quantity": 0,
                        "first": day, "last": day, "score": 0.0,
                    }
                s["count"] += 1
                s["quantity"] += int(item.get("quantity") or 1)
                s["score"] += weight
                if day > s["last"]:
                    s["last"] = day
                    s["title"] = item.get("title") or s["title"]
                    s["image_url"] = item.get("image_url") or s["image_url"]
                s["first"] = min(s["first"], day)

        result = sorted(stats.values(), key=lambda s: (-s["score"], -s["count"], s["title"]))
        for s in result:
            s["interval_days"] = round((s["last"] - s["first"]).days / (s["count"] - 1)) if s["count"] > 1 else None
            s["days_since"] = (today - s["last"]).days
            s["first"], s["last"] = s["first"].isoformat(), s["last"].isoformat()
            s["score"] = round(s["score"], 2)

        if include_catalog:
            seen = set(stats)
            extra = [
                {**p, "count": 0, "quantity": 0, "score": 0, "first": None, "last": None,
                 "interval_days": None, "days_since": None}
                for p in self.data.get("catalog") or []
                if p.get("product_id") not in seen
            ]
            result += sorted(extra, key=lambda p: p.get("title", "").lower())
        return result[:limit]
