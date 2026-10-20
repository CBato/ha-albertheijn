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
from homeassistant.util import dt as dt_util

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
RECEIPTS_PAGE = 50
RECEIPTS_FIRST = 400  # the first sync imports at most this many till receipts (newest first)


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
    """Products sorted by how often (and how recently) they were bought, with their rhythm, followed by the rest
    of "Eerder gekocht". Purchases = delivered orders + till receipts (in-store with the bonus card).

    Per product: count (days bought), quantity (total), usual_qty (median per purchase), every_weeks (spacing),
    share (part of the weeks in which it was bought), per_week (average use per week),
    all counted over the weeks we have data for (a delivery or a receipt), so gaps in the data (deliveries AH no
    longer returns) don't make products look rarer than they are;
    days_since, due, sources, and the latest weekly price.

    due = the stock from the last purchase (last quantity / use per week) runs out within horizon_days
    (normally: until the delivery after the next one). So 4 pakken in de bonus last twice as long as 2.
    A pause (nothing bought for more than twice the usual interval, e.g. an AH outage or a holiday) is not counted:
    then "due" is judged as if one usual interval has passed since the last purchase.
    """
    today = today or date.today()
    days_all = sorted({date.fromisoformat(d) for d in snapshot["order_days"] + snapshot.get("receipt_days", [])})
    latest = days_all[-1] if days_all else today
    prices = snapshot["latest_prices"]
    hh_gaps = [(b - a).days for a, b in zip(days_all, days_all[1:])]
    hh_gap = median(hh_gaps) if hh_gaps else 7
    paused = (today - latest).days > max(14, 2 * hh_gap)
    # weeks we have data for (a delivery or a receipt): weeks without any data don't count as "not bought"
    epoch = date(2000, 1, 3)  # a Monday
    observed = sorted({(d - epoch).days // 7 for d in days_all})
    judge_day = latest + timedelta(days=round(hh_gap)) if paused else today

    stats: dict[int, dict[str, Any]] = {}
    for row in snapshot["items"]:
        day = date.fromisoformat(row["date"])
        pid = row["product_id"]
        s = stats.get(pid)
        if s is None:
            s = stats[pid] = {
                "product_id": pid, "title": row.get("title") or "", "brand": row.get("brand") or "",
                "unit_size": row.get("unit_size") or "", "category": row.get("category") or "",
                "image_url": row.get("image_url"), "_buy": {}, "_src": set(),
            }
        try:
            qty = max(1, round(float(row.get("quantity") or 1)))
        except (TypeError, ValueError):
            qty = 1
        # same day from two sources (delivery + receipt of that delivery) counts once: take the largest
        s["_buy"][day] = max(s["_buy"].get(day, 0), qty)
        s["_src"].add(row.get("source") or "bezorging")
        if not s["title"] and row.get("title"):
            s["title"] = row["title"]

    for s in stats.values():
        s["count"] = len(s["_buy"])
        s["quantity"] = sum(s["_buy"].values())
        s["score"] = sum(0.5 ** (max((today - d).days, 0) / HALF_LIFE_DAYS) for d in s["_buy"])

    result = sorted(stats.values(), key=lambda s: (-s["score"], -s["count"], s["title"]))
    for s in result:
        buy = s.pop("_buy")
        days = sorted(buy)
        qtys = [buy[d] for d in days]
        first, last = days[0], days[-1]
        gaps = [(b - a).days for a, b in zip(days, days[1:])]
        gap = median(gaps) if gaps else None
        first_week = (first - epoch).days // 7
        obs_weeks = max(1, sum(1 for w in observed if w >= first_week))
        weeks_bought = len({(d - epoch).days // 7 for d in days})
        per_week = s["quantity"] / obs_weeks
        spacing = obs_weeks / s["count"] if s["count"] > 1 else None  # bought every so many observed weeks
        covers = qtys[-1] / per_week * 7 if per_week else None  # days the last purchase lasts
        s.update({
            "first": first.isoformat(), "last": last.isoformat(),
            "days_since": (today - last).days,
            "usual_qty": int(median(qtys)),
            "every_weeks": max(1, round(spacing)) if spacing else None,
            "interval_days": round(gap) if gap else None,
            "share": round(min(1.0, weeks_bought / obs_weeks), 2),
            "per_week": round(per_week, 1),
            "covers_days": round(covers) if covers else None,
            "due": len(days) >= 2 and covers is not None and covers < (judge_day - last).days + horizon_days,
            "sources": sorted(s.pop("_src")),
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
        rdays = snap.get("receipt_days") or []
        self.summary = {
            "orders": len(days),
            "first_order": days[0] if days else None,
            "last_order": days[-1] if days else None,
            "receipts": len(rdays),
            "first_receipt": rdays[0] if rdays else None,
            "last_receipt": rdays[-1] if rdays else None,
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

            # 4. Till receipts (in-store purchases with the bonus card): new ones, the first time up to RECEIPTS_FIRST
            try:
                if await self._fetch_receipts(now):
                    changed = True
            except AlbertHeijnApiError as err:
                _LOGGER.debug("Receipts not available: %s", err)
                errors.append(f"Kassabonnen: {err}")

            # rebuild the cache first: the price check uses the product list
            await self._refresh_cache()

            # 5. Weekly price check for everything we buy (history first, then the catalog)
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

    async def _fetch_receipts(self, now: str) -> bool:
        """Import new till receipts and map their till product ids to webshop products."""
        known = await self._run(db.known_receipts)
        limit = RECEIPTS_PAGE
        max_new = RECEIPTS_FIRST if not known else RECEIPTS_PAGE * 4
        new: list[dict[str, Any]] = []
        offset = 0
        while len(new) < max_new:
            page = await self._api.get_receipts(offset, limit)
            fresh = [r for r in page if r["id"] not in known]
            new.extend(fresh)
            if len(page) < limit or len(fresh) < len(page):
                break  # last page, or we reached receipts we already have
            offset += limit
            await asyncio.sleep(REQUEST_PAUSE)
        tz = dt_util.get_default_time_zone()
        for r in new[:max_new]:
            lines = await self._api.get_receipt_lines(r["id"])
            moment = datetime.fromisoformat(r["datetime"].replace("Z", "+00:00"))
            day = (moment.astimezone(tz) if moment.tzinfo else moment).date().isoformat()
            await self._run(db.save_receipt, r["id"], day, r.get("total"), lines, now)
            await asyncio.sleep(REQUEST_PAUSE)
        unmapped = await self._run(db.unmapped_pos_ids)
        if unmapped:
            mapping = await self._api.convert_pos_ids(unmapped)
            await self._run(db.save_pos_map, mapping)
        return bool(new) or bool(unmapped)

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
