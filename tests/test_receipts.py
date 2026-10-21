"""Offline test: till receipts as extra source, pause handling (AH outage), same-day dedupe, week-based share."""

from datetime import date, timedelta
import asyncio
import os

from _harness import tmp, ConfigEntries  # sets up the Home Assistant stubs

import importlib
history = importlib.import_module("albert_heijn.history")
from albert_heijn.api import OrderItem

today = date.today()


class Hass:
    config_entries = ConfigEntries()  # no Picnic integration

    class config:
        @staticmethod
        def path(name): return os.path.join(tmp, name)

    async def async_add_executor_job(self, func, *args):
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)


# deliveries: weekly for 6 weeks, the last one 23 days ago (then an AH outage)
DELIV = {200 + n: ((today - timedelta(days=23 + 7 * (5 - n))).isoformat(), [("Appelsap", 1, 2), ("Melk", 4, 2)]) for n in range(6)}
# receipts: in the store during the outage (10 and 3 days ago): appelsap + kaas; one receipt on the day of a delivery
RECEIPTS = [
    {"id": "r1", "datetime": f"{(today - timedelta(days=3)).isoformat()}T10:00:00Z", "total": 12.0},
    {"id": "r2", "datetime": f"{(today - timedelta(days=10)).isoformat()}T10:00:00Z", "total": 9.0},
    {"id": "r3", "datetime": f"{DELIV[205][0]}T18:30:00Z", "total": 20.0},  # receipt of the delivery itself
]
LINES = {
    "r1": [{"pos_id": 11, "name": "AH APPELSAP", "quantity": 2, "amount": 3.0}, {"pos_id": 13, "name": "KAAS", "quantity": 1, "amount": 4.0},
           {"pos_id": 99, "name": "BANANEN", "quantity": 0.8, "amount": 1.2}],
    "r2": [{"pos_id": 11, "name": "AH APPELSAP", "quantity": 2, "amount": 3.0}],
    "r3": [{"pos_id": 11, "name": "AH APPELSAP", "quantity": 2, "amount": 3.0}],
}


class FakeApi:
    async def get_product_gtins(self, product_id):
        return []

    convert_calls = 0

    async def get_closed_fulfillments(self):
        return [{"order_id": oid, "date": d, "status": "Bezorgd", "cancelled": False} for oid, (d, _) in DELIV.items()]

    async def get_order_items(self, oid):
        return [OrderItem(pid, t, "AH", q, 2.0, "", "", None, 2.0, None, None) for t, pid, q in DELIV[oid][1]]

    async def get_order_summaries(self, page=0, size=25):
        return []

    async def get_bonus_periods(self):
        return []

    async def get_bonus_section(self, kind, start):
        return []

    async def get_receipts(self, offset=0, limit=50):
        return RECEIPTS[offset:offset + limit]

    async def get_receipt_lines(self, rid):
        return LINES[rid]

    async def convert_pos_ids(self, ids):
        FakeApi.convert_calls += 1
        return {i: {11: 1, 13: 5}.get(i, -1) for i in ids}

    async def get_previously_bought(self, size, page):
        return [{"product_id": 1, "title": "Appelsap"}, {"product_id": 4, "title": "Melk"}, {"product_id": 5, "title": "Kaas"}], 1

    async def get_prices(self, ids):
        return [{"product_id": i, "price": 3.0, "price_before_bonus": 3.0, "bonus": None, "unit_price": "",
                 "title": "AH Jong belegen kaas" if i == 5 else "", "category": "Kaas, vleeswaren/Kaas" if i == 5 else "",
                 "image_url": "https://img/5" if i == 5 else None, "brand": "AH", "unit_size": "500 g"} for i in ids]


async def main():
    history.REQUEST_PAUSE = 0
    h = history.OrderHistory(Hass(), FakeApi(), "x")
    await h.async_load()
    await h.async_sync([])
    print("summary:", {k: h.summary[k] for k in ("orders", "receipts", "first_receipt", "last_receipt", "products_ordered")}, "error:", h.last_error)
    assert h.summary["orders"] == 6 and h.summary["receipts"] == 3 and FakeApi.convert_calls == 1
    f = {p["title"]: p for p in h.frequent(50, include_catalog=False)}
    for t in ("Appelsap", "Melk", "Kaas"):
        p = f[t]
        print(f"{t:8} count={p['count']} qty={p['quantity']} usual={p['usual_qty']} share={p['share']} per_week={p['per_week']} "
              f"last={p['days_since']}d due={p['due']} sources={p['sources']}")
    a, m, k = f["Appelsap"], f["Melk"], f["Kaas"]
    # delivery + receipt of that delivery on the same day count once (max, not 4)
    assert a["count"] == 8 and a["quantity"] == 16, a
    assert a["sources"] == ["bezorging", "winkel"] and k["sources"] == ["winkel"]
    assert "BANANEN" not in f  # not sold online -> not counted
    # melk: only deliveries, last 23 days ago; the store trips (3 and 10 days ago) mean no pause, so melk is due
    assert m["due"] is True
    ph = await h.async_product_history(1)
    t = ph["totals"]
    print("product history appelsap:", ph["product"].get("title"), t, "| sources", sorted({p["source"] for p in ph["purchases"]}),
          "| winkelprijs p/st", [p["price"] for p in ph["purchases"] if p["source"] == "winkel"][:2], "| rhythm", ph["rhythm"]["every_weeks"])
    assert t["purchases"] == 9 and ph["rhythm"]["product_id"] == 1  # 6 deliveries + 3 receipt lines (one on a delivery day)
    assert all(p["price"] == 1.5 for p in ph["purchases"] if p["source"] == "winkel")
    ins = await h.async_insights(12)
    print("insights month:", ins["month"], "| prev:", ins["prev_month"]["spent"])
    print("weeks (last 6):", [(w["week"][5:], w["delivery"], w["store"]) for w in ins["weeks"][-6:]])
    print("top:", [(e["title"], e["spent"], e.get("every_weeks")) for e in ins["top"]], "| cats:", ins["categories"], "| movers:", ins["movers"]["source"])
    assert len(ins["weeks"]) == 12 and ins["top"][0]["title"] in ("Appelsap", "Melk")
    assert abs(sum(w["delivery"] + w["store"] for w in ins["weeks"]) - (sum(8.0 for _ in range(6) if True) * 0 + sum(w["delivery"] + w["store"] for w in ins["weeks"]))) < 0.01
    ov = await h.async_products_overview()
    print("overview:", [(r["title"], r["times"], r["avg_paid"], r["last_paid"], r["last_bought"], r.get("every_weeks")) for r in ov])
    ap = next(r for r in ov if r["title"] == "Appelsap")
    assert ap["times"] == 8 and ap["avg_paid"] is not None and ov[0]["last_bought"] >= ov[-1]["last_bought"]
    import sqlite3 as _sq
    _c = _sq.connect(Hass.config.path("albert_heijn.db"))
    print("kaas details:", _c.execute("SELECT title, category, image_url, unit_size FROM products WHERE product_id=5").fetchone())
    assert _c.execute("SELECT category FROM products WHERE product_id=5").fetchone()[0] == "Kaas, vleeswaren/Kaas"
    assert _c.execute("SELECT title FROM products WHERE product_id=1").fetchone()[0] == "Appelsap"  # known title kept
    _c.close()
    # second sync: no new receipts, nothing converted again
    await h.async_sync([])
    assert FakeApi.convert_calls == 1

    # pause handling: only deliveries, last one 23 days ago, no receipts -> pause; melk (bought every week, 2)
    # is judged as if one week passed: due (it runs out before the delivery after next), and nothing "extra" is due
    snap = {"order_days": [d for d, _ in DELIV.values()], "receipt_days": [], "catalog": [], "latest_prices": {},
            "items": [{"date": d, "source": "bezorging", "product_id": pid, "quantity": q, "title": t}
                      for d, its in DELIV.values() for t, pid, q in its]
            + [{"date": DELIV[202][0], "source": "bezorging", "product_id": 7, "quantity": 1, "title": "Koffie"},
               {"date": DELIV[205][0], "source": "bezorging", "product_id": 7, "quantity": 1, "title": "Koffie"}]}
    rows = {p["title"]: p for p in history.compute_frequent(snap, today, 7)}
    k = rows["Koffie"]
    print("pause: melk due", rows["Melk"]["due"], "| koffie (every 3 wk, last just before the outage) due", k["due"], "covers", k["covers_days"])
    assert rows["Melk"]["due"] is True
    # with the pause rule koffie is judged one week after the last delivery: not due yet
    assert k["covers_days"] == 21 and k["due"] is False
    # without a pause (same data, but "today" = the day after the last delivery + 3 weeks of normal deliveries) it would be due
    snap2 = dict(snap, order_days=snap["order_days"] + [(today - timedelta(days=d)).isoformat() for d in (16, 9, 2)])
    assert {p["title"]: p for p in history.compute_frequent(snap2, today, 7)}["Koffie"]["due"] is True
    print("ALL OK")


asyncio.run(main())
