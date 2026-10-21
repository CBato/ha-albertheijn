"""Offline test of the suggestions (suggest.py) and fill_list (history.py + db.py) with stubbed HA and fake AH."""

from datetime import date, datetime, timedelta
import asyncio
import os
import sqlite3

from _harness import tmp, ConfigEntries  # sets up the Home Assistant stubs

import importlib
history = importlib.import_module("albert_heijn.history")
suggest = importlib.import_module("albert_heijn.suggest")
from albert_heijn.api import OrderItem

today = date.today()
monday = suggest.week_start(today)


class Hass:
    config_entries = ConfigEntries()  # no Picnic integration

    class config:
        @staticmethod
        def path(name): return os.path.join(tmp, name)

    async def async_add_executor_job(self, func, *args):
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)


# 8 weekly orders; the newest 10 days ago so weekly products are due
weeks = [today - timedelta(days=10 + 7 * i) for i in range(8)][::-1]
ORDERS = {}
for n, d in enumerate(weeks):
    items = [("Appelsap", 1, 3), ("Melk", 4, 2)]
    if n % 2 == 0:  # chips every 2 weeks, last time 17 days ago -> due
        items.append(("Chips", 2, 1))
    items.append(("Koffie", 3, 1)) if n % 2 == 1 else None
    ORDERS[100 + n] = (d.isoformat(), items)


class FakeApi:
    async def get_product_gtins(self, product_id):
        return []

    on_list = [{"product_id": 3, "title": "Koffie", "quantity": 1, "checked": False}, {"product_id": None, "title": "bananen"}]
    written = []

    async def get_closed_fulfillments(self):
        return [{"order_id": oid, "date": d, "status": "Bezorgd", "cancelled": False} for oid, (d, _) in ORDERS.items()]

    async def get_order_items(self, oid):
        return [OrderItem(pid, t, "AH", q, 2.0, "", "", None, 2.0, None, None) for t, pid, q in ORDERS[oid][1]]

    async def get_order_summaries(self, page=0, size=25):
        return []

    async def get_bonus_periods(self):
        nm = monday + timedelta(days=7)
        return [{"start": monday.isoformat(), "end": (monday + timedelta(days=6)).isoformat()},
                {"start": nm.isoformat(), "end": (nm + timedelta(days=6)).isoformat()}]

    async def get_bonus_section(self, kind, start):
        if kind != "previously_bought" or start == monday.isoformat():
            return []
        return [{"product_id": 1, "title": "Appelsap", "mechanism": "1 + 1 gratis", "price": 2.0, "price_was": 2.0, "image_url": None, "group": None, "source": kind},
                {"product_id": 4, "title": "Melk", "mechanism": "25% korting", "price": 0.9, "price_was": 1.2, "image_url": None, "group": None, "source": kind}]

    async def get_receipts(self, offset=0, limit=50):
        return []

    async def get_receipt_lines(self, rid):
        return []

    async def convert_pos_ids(self, ids):
        return {}

    async def get_previously_bought(self, size, page):
        return [], 0

    async def get_prices(self, ids):
        # this week: appelsap 1+1 at normal price (real), chips 2e halve prijs but price raised (fake), melk no bonus
        return [{"product_id": i, "price": {1: 2.0, 2: 2.6, 3: 5.0, 4: 1.2}[i], "price_before_bonus": {1: 2.0, 2: 2.6, 3: 5.0, 4: 1.2}[i],
                 "bonus": {1: "1+1 gratis", 2: "2e halve prijs"}.get(i), "unit_price": ""} for i in ids]

    async def get_shopping_list(self):
        return list(self.on_list)

    async def set_list_items(self, items):
        FakeApi.written.append([(i["product_id"], i["quantity"]) for i in items])


def seed_prices(path):
    """8 weeks of normal prices before this week: appelsap 2.00, chips 2.00 (then raised to 2.60 with the bonus)."""
    con = sqlite3.connect(path)
    for w in range(1, 9):
        d = (monday - timedelta(days=7 * w)).isoformat()
        con.executemany("INSERT OR REPLACE INTO prices(product_id, date, price, price_before_bonus, bonus) VALUES(?,?,?,?,NULL)",
                        [(1, d, 2.0, 2.0), (2, d, 2.0, 2.0), (4, d, 1.2, 1.2)])
    con.commit(); con.close()


async def main():
    history.REQUEST_PAUSE = 0
    h = history.OrderHistory(Hass(), FakeApi(), "x")
    await h.async_load()
    seed_prices(Hass.config.path("albert_heijn.db"))
    await h.async_sync([])  # orders + this week's prices
    week = (today + timedelta(days=5)).isoformat()

    # snoozed: chips not suggested; snooze cleared again afterwards
    until = await h.async_snooze(2, 7)
    assert until and next(p for p in h.frequent(10) if p["product_id"] == 2).get("snoozed_until") == until
    snoozed_dry = await h.async_fill_list([], {4}, week, dry_run=True)
    assert "Chips" not in [r["title"] for r in snoozed_dry["added"]], snoozed_dry["added"]
    assert await h.async_snooze(2, 0) is None and not next(p for p in h.frequent(10) if p["product_id"] == 2).get("snoozed_until")
    assert await h.async_snooze(2, -1) == "9999-12-31"
    await h.async_snooze(2, 0)
    print("snooze ok")
    await h.async_fill_list([], set(), week, dry_run=True)
    assert not FakeApi.written
    res = await h.async_fill_list([], {4}, week)  # melk is already in the order
    for r in res["added"]:
        print(r["title"], r["quantity"], r["kinds"], r["reasons"], r["bonus_check"])
    added = {r["title"]: r for r in res["added"]}
    assert set(added) == {"Appelsap", "Chips"}, added.keys()
    assert added["Appelsap"]["kinds"] == ["ritme", "bonus"] and added["Appelsap"]["quantity"] == 6 and added["Appelsap"]["bonus_check"] == "echt"
    assert added["Chips"]["kinds"] == ["ritme"] and "⚠" in added["Chips"]["reasons"][1], added["Chips"]  # fake bonus not used as reason
    assert res["added"][0]["title"] == "Appelsap"  # due + bonus first
    assert FakeApi.written == [[(1, 6), (2, 1)]], FakeApi.written
    assert "→ 6 genomen (normaal 3)" in added["Appelsap"]["reasons"][1] and "50% voordeel" in added["Appelsap"]["reasons"][1]
    assert {s["title"] for s in h.suggestions} == {"Appelsap", "Chips"} and h.suggestions[0]["reasons"]

    # second run in the same week: nothing again (also not what we removed from the list afterwards)
    res2 = await h.async_fill_list([], {4}, week)
    assert res2["added"] == [] and len(FakeApi.written) == 1
    nxt = await h.async_bonus("next")
    print("next week:", nxt["week_start"], [(o["title"], o["mechanism"], o["effective"], o["avg_paid"], o["saving"]) for o in nxt["offers"]])
    a = next(o for o in nxt["offers"] if o["title"] == "Appelsap")
    assert nxt["available"] and a["effective"] == 1.0 and a["saving"] == 0.5 and nxt["offers"][0]["title"] == "Melk"
    assert h.bonus_next.get("available") and (await h.async_bonus("current"))["offers"] == []
    # cleanup: make the suggestions 15 days old; appelsap still on the list with the suggested 6 -> removed,
    # chips changed by hand to 3 -> kept; koffie was not suggested -> untouched
    import sqlite3 as _sq
    _c = _sq.connect(Hass.config.path("albert_heijn.db"))
    _c.execute("UPDATE suggestions SET added_at = ?", ((datetime.now() - timedelta(days=14)).date().isoformat() + "T23:59:59+00:00",)); _c.execute("UPDATE orders SET date = date(date, '-30 days')")  # nothing bought since the suggestion
    _c.commit(); _c.close()
    FakeApi.on_list = [{"product_id": 1, "title": "Appelsap", "quantity": 6, "checked": False},
                       {"product_id": 2, "title": "Chips", "quantity": 3, "checked": False},
                       {"product_id": 3, "title": "Koffie", "quantity": 1, "checked": False}]
    FakeApi.written.clear()
    cl = await h.async_cleanup_suggestions(set(), 14)
    print("cleanup:", cl, FakeApi.written)
    assert cl["removed"] == ["Appelsap"] and FakeApi.written == [[(1, 0)]] and cl["kept"] == 0  # chips: changed by hand = handled
    assert 1 in h.snoozes  # removed -> not suggested again for 4 weeks
    FakeApi.on_list = [{"product_id": 3, "title": "Koffie", "quantity": 1, "checked": False}]
    assert all(r["product_id"] != 1 for r in (await h.async_fill_list([], set(), "2099-01-01", dry_run=True))["added"])
    assert (await h.async_cleanup_suggestions(set(), 14))["removed"] == []  # handled only once
    # bought since -> left alone; ticked / in the order -> kept
    _c = _sq.connect(Hass.config.path("albert_heijn.db"))
    _c.execute("UPDATE suggestions SET done_at = NULL"); _c.execute("UPDATE orders SET date = date('now')"); _c.commit(); _c.close()
    FakeApi.on_list = [{"product_id": 1, "title": "Appelsap", "quantity": 6, "checked": False}]
    FakeApi.written.clear()
    assert (await h.async_cleanup_suggestions(set(), 14))["removed"] == [] and not FakeApi.written
    # promo parsing
    P = suggest.parse_promo
    assert P("1 + 1 gratis") == {"group": 2, "pay": 1} and P("2+1 GRATIS") == {"group": 3, "pay": 2}
    assert P("2e halve prijs") == {"group": 2, "pay": 1.5} and P("2e gratis") == {"group": 2, "pay": 1}
    assert P("2 voor 5,00") == {"group": 2, "bundle": 5.0} and P("3 voor €10.00") == {"group": 3, "bundle": 10.0}
    assert P("25% KORTING") == {"group": 1, "percent": 25} and P("0,50 korting") == {"group": 1, "off": 0.5}
    assert P("2 voor 1") == {"group": 2, "pay": 1} and P("Bonus") is None
    E = suggest.effective_price
    assert E("1+1 gratis", 2.0, 2.0) == 1.0 and E("2e halve prijs", 2.0, 2.0) == 1.5 and E("2 voor 5,00", 3.0, 3.0) == 2.5
    assert E("25% korting", 1.5, 2.0) == 1.5 and E("onbekend", 1.8, 2.0) == 1.8
    assert suggest._round_up(3, 2) == 4 and suggest._round_up(1, 3) == 3 and suggest._round_up(2.5, 1) == 3
    # bonus_check unit cases
    s = [{"d": (monday - timedelta(days=7 * w)).isoformat(), "p": 3.0, "b": 3.0, "bonus": None} for w in (3, 2, 1)]
    B = lambda serie: suggest.bonus_check(serie, today)
    r = B(s + [{"d": monday.isoformat(), "p": 2.0, "b": 3.0, "bonus": "33% korting"}])
    assert r["status"] == "echt" and r["saving"] == 0.33, r
    assert B(s[:2] + [{"d": monday.isoformat(), "p": 2.0, "b": 3.0, "bonus": "x"}])["status"] == "onbekend"
    assert B(s + [{"d": (monday - timedelta(days=1)).isoformat(), "p": 2.0, "b": 3.0, "bonus": "x"}])["status"] == "geen"
    # 2e halve prijs while the normal price went up from 3,00 to 3,50: real saving only ~13%, flagged
    r = B(s + [{"d": monday.isoformat(), "p": 3.5, "b": 3.5, "bonus": "2e halve prijs"}])
    assert r["status"] == "let_op" and r["saving"] == 0.12, r
    # "1 euro korting" on 3,00 -> 2,00 = 33%
    r = B(s + [{"d": monday.isoformat(), "p": 2.0, "b": 3.0, "bonus": "1,00 korting"}])
    assert r["status"] == "echt" and r["effective"] == 2.0, r
    print("promo checks ok")
    print("ALL OK")


asyncio.run(main())
