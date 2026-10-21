"""Offline test of the SQLite history (db.py + history.py) with a stubbed Home Assistant and fake AH API."""

from datetime import date, timedelta
import asyncio
import os
import sqlite3
import tempfile
import time

from _harness import tmp, OLD_STORE, ConfigEntries  # sets up the Home Assistant stubs

import importlib
history = importlib.import_module("albert_heijn.history")
from albert_heijn.api import OrderItem, Fulfillment, DeliverySlot, AlbertHeijnApiError


class Hass:
    config_entries = ConfigEntries()  # no Picnic integration

    class config:
        @staticmethod
        def path(name): return os.path.join(tmp, name)

    async def async_add_executor_job(self, func, *args):
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)


today = date.today()
weeks = [today - timedelta(days=3 + 7 * i) for i in range(10)][::-1]
ORDERS = {}
for n, d in enumerate(weeks):
    items = [("Appelsap", 1, 3)]
    if n % 2 == 0:
        items.append(("Chips", 2, 1))
    if n == 0:
        items.append(("Koffie", 3, 1))
    ORDERS[100 + n] = (d.isoformat(), items)


class FakeApi:
    async def get_product_gtins(self, product_id):
        return []

    price_calls = 0
    closed_ok = True

    async def get_closed_fulfillments(self):
        if not self.closed_ok:
            raise AlbertHeijnApiError("unknown enum CLOSED")
        rows = [{"order_id": oid, "date": d, "status": "Bezorgd", "cancelled": False} for oid, (d, _) in ORDERS.items()]
        return rows + [{"order_id": 777, "date": (today - timedelta(days=2)).isoformat(), "status": "Geannuleerd", "cancelled": True}]

    async def get_order_items(self, oid):
        if oid >= 4999:
            return [OrderItem(1, "Appelsap", "AH", 2, 1.5, "1 L", "Sap", None, 1.9, None, None)]
        if oid == 999:
            return [OrderItem(1, "Appelsap", "AH", 2, 1.5, "1 L", "Sap", None, 1.9, "25% korting", "€ 1,50 per liter")]
        return [OrderItem(pid, t, "AH", q, 1.5, "1 L", "Cat", f"https://img/{pid}", 1.9, None, "€ 1,50 per liter") for t, pid, q in ORDERS[oid][1]]

    async def get_order_summaries(self, page=0, size=25):
        # 30 older weekly orders (beyond the 10 of GraphQL), paged by 25; the newest one cancelled; one future order
        rows = [{"order_id": 5000 + i, "date": (today - timedelta(days=80 + 7 * i)).isoformat(), "state": "DELIVERED", "cancelled": False} for i in range(30)]
        rows[0]["state"], rows[0]["cancelled"] = "CANCELLED", True
        rows.insert(0, {"order_id": 4999, "date": (today + timedelta(days=5)).isoformat(), "state": "CONFIRMED", "cancelled": False})
        return rows[page * size:(page + 1) * size]

    async def get_bonus_periods(self):
        return []

    async def get_bonus_section(self, kind, start):
        return []

    async def get_receipts(self, offset=0, limit=50):
        return []

    async def get_receipt_lines(self, rid):
        return []

    async def convert_pos_ids(self, ids):
        return {}

    async def get_previously_bought(self, size, page):
        # 582 catalog products over 6 pages (incl. the three ordered ones)
        names = {1: "Appelsap", 2: "Chips", 3: "Koffie"}
        allp = [{"product_id": i, "title": names.get(i, f"Product {i:03d}")} for i in range(1, 583)]
        return allp[page * size:(page + 1) * size], 6

    async def get_prices(self, ids):
        FakeApi.price_calls += 1
        assert len(ids) <= 30
        return [{"product_id": i, "price": 2.0, "price_before_bonus": 2.5, "bonus": "1+1 gratis" if i == 2 else None, "unit_price": ""} for i in ids]


async def main():
    history.REQUEST_PAUSE = 0
    t0 = time.time()
    h = history.OrderHistory(Hass(), FakeApi(), "x")
    await h.async_load()
    changed = await h.async_sync([], refresh_catalog=True)
    print(f"first sync {time.time() - t0:.2f}s changed={changed} summary={h.summary} error={h.last_error}")
    assert changed and h.closed_supported
    print("orders incl. summaries:", h.summary["orders"], "list seen:", h.summary["order_list_seen"])
    assert h.summary["order_list_seen"] == 31
    assert h.summary["orders"] == 10 + 29 and h.summary["catalog"] == 582 and h.summary["products_ordered"] == 3
    assert FakeApi.price_calls == 20, FakeApi.price_calls  # 582 products in batches of 30
    assert h.summary["price_points"] == 582
    f = {p["title"]: p for p in h.frequent(1000)}
    a, c = f["Appelsap"], f["Chips"]
    print("Appelsap", {k: a[k] for k in ("count", "usual_qty", "every_weeks", "share", "per_week", "price")})
    print("Chips   ", {k: c[k] for k in ("count", "usual_qty", "every_weeks", "share", "per_week", "price_bonus")})
    # appelsap: every week in all 39 delivered orders (29 older with 2, 10 recent with 3)
    assert a["count"] == 39 and a["every_weeks"] == 1 and a["usual_qty"] == 2 and a["share"] == 1.0, a
    assert (c["every_weeks"], c["usual_qty"], c["per_week"], c["share"]) == (2, 1, 0.5, 0.56)
    assert c["price_bonus"] == "1+1 gratis"
    assert len(h.frequent(1000)) == 582 and len(h.frequent(1000, include_catalog=False)) == 3
    assert h.frequent(3)[0]["title"] == "Appelsap"

    # second sync: nothing new, no new price check within the week
    n = FakeApi.price_calls
    assert not await h.async_sync([])
    assert FakeApi.price_calls == n

    # open order snapshot -> delivered once it is gone after its date
    f = Fulfillment(999, "", "", "", 9.9, False, True, DeliverySlot((today + timedelta(days=4)).isoformat(), "", "", "", ""))
    assert await h.async_sync([f])
    con = sqlite3.connect(Hass.config.path("albert_heijn.db"))
    assert con.execute("SELECT status FROM orders WHERE order_id=999").fetchone()[0] == "open"
    con.execute("UPDATE orders SET date=? WHERE order_id=999", ((today - timedelta(days=1)).isoformat(),)); con.commit()
    await h.async_sync([])
    assert con.execute("SELECT status FROM orders WHERE order_id=999").fetchone()[0] == "delivered"
    row = con.execute("SELECT quantity, price_before_bonus, bonus, unit_price FROM order_items WHERE order_id=999").fetchone()
    assert row == (2, 1.9, "25% korting", "€ 1,50 per liter"), row
    ph = await h.async_product_history(1)
    assert any(x["source"] == "bezorging" and x["bonus"] == "25% korting" for x in ph["purchases"]) and ph["prices"]
    print("product history Appelsap:", len(ph["purchases"]), "purchases,", len(ph["prices"]), "weekly prices")
    # a cancelled order that was stored before (as open snapshot) is removed and never counted
    con2 = sqlite3.connect(Hass.config.path("albert_heijn.db"))
    con2.execute("INSERT INTO orders(order_id, date, status) VALUES(777, ?, 'open')", ((today - timedelta(days=2)).isoformat(),))
    con2.execute("INSERT INTO order_items(order_id, product_id, quantity) VALUES(777, 1, 5)"); con2.commit()
    await h.async_sync([])
    assert con2.execute("SELECT COUNT(*) FROM orders WHERE order_id=777").fetchone()[0] == 0
    assert con2.execute("SELECT COUNT(*) FROM order_items WHERE order_id=777").fetchone()[0] == 0
    assert h.summary["cancelled_skipped"] == 1
    con2.close()
    print("cancelled order removed ok")
    print("db size:", os.path.getsize(Hass.config.path("albert_heijn.db")) // 1024, "KB")
    con.close()

    # import of the old JSON store (0.6/0.7) into a fresh database
    OLD_STORE["data"] = {"orders": {"5": {"date": "2026-01-04", "status": "delivered", "items": [
        {"product_id": 77, "title": "Oud", "quantity": 2, "price": 1.0}]}},
        "catalog": [{"product_id": 78, "title": "Cat"}], "prices": {"77": [{"d": "2026-01-05", "p": 1.1}]}}
    tmp2 = tempfile.mkdtemp(dir=tmp)
    class Hass2(Hass):
        class config:
            @staticmethod
            def path(name): return os.path.join(tmp2, name)
    h2 = history.OrderHistory(Hass2(), FakeApi(), "y")
    await h2.async_load()
    assert OLD_STORE["removed"] and h2.summary["orders"] == 1 and h2.summary["catalog"] == 1 and h2.summary["price_points"] == 1, h2.summary
    print("old JSON import ok")

    # fallback without CLOSED: fresh db, only open snapshots
    OLD_STORE["data"] = None
    tmp3 = tempfile.mkdtemp(dir=tmp)
    class Hass3(Hass):
        class config:
            @staticmethod
            def path(name): return os.path.join(tmp3, name)
    api = FakeApi(); api.closed_ok = False
    h3 = history.OrderHistory(Hass3(), api, "z")
    await h3.async_load()
    await h3.async_sync([f])
    assert h3.closed_supported is False and h3.summary["orders"] == 29 and h3.summary["catalog"] == 582
    print("fallback without CLOSED ok")
    print("ALL OK")


asyncio.run(main())
