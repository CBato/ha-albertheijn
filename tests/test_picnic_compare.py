"""Offline test: EAN codes from AH, Picnic prices matched on EAN, and the list compared (fake AH, fake Picnic)."""

import asyncio
import os
from types import SimpleNamespace

from _harness import tmp, ConfigEntries  # sets up the Home Assistant stubs

import importlib

history = importlib.import_module("albert_heijn.history")
compare = importlib.import_module("albert_heijn.compare")
db = importlib.import_module("albert_heijn.db")

GTINS = {1: ["8710400000011"], 2: ["8710400000028"], 3: []}  # 3: AH gives no EAN
PICNIC = {"8710400000011": SimpleNamespace(id="s100", name="Coca-Cola 1,5L", price=219, original_price=None, unit_quantity="1,5 l")}
# 2 has an EAN but Picnic doesn't sell it


class Hass:
    config_entries = ConfigEntries([SimpleNamespace(domain="picnic", data={"access_token": "t", "country_code": "NL"})])

    class config:
        @staticmethod
        def path(name):
            return os.path.join(tmp, "picnic-" + name)

    async def async_add_executor_job(self, func, *args):
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)


class FakeApi:
    async def get_product_gtins(self, pid):
        return GTINS.get(pid, [])

    async def get_shopping_list(self):
        return [{"product_id": 1, "title": "Coca-Cola", "quantity": 2, "checked": False},
                {"product_id": 2, "title": "AH Pindakaas", "quantity": 1, "checked": False},
                {"product_id": None, "title": "bananen", "quantity": 1, "checked": False}]


class FakePicnic:
    calls = 0

    def get_article_by_gtin(self, gtin):
        FakePicnic.calls += 1
        return PICNIC.get(gtin)


compare.picnic_client = lambda credentials: FakePicnic()


async def main():
    h = history.OrderHistory(Hass(), FakeApi(), "e1")
    await h._run(db.init)
    now, today = "2026-10-22T04:15:00+00:00", "2026-10-22"
    await h._run(db.save_order, 10, "2026-10-18", "delivered",
                 [{"product_id": p, "title": t, "quantity": 1, "price": 2.5} for p, t in ((1, "Coca-Cola"), (2, "AH Pindakaas"), (3, "AH Brood"))], now)
    await h._run(db.save_prices, today, [{"product_id": 1, "price": 2.49}, {"product_id": 2, "price": 2.99}])

    assert await h._fetch_gtins(now) == 2
    assert await h._fetch_gtins(now) == 0  # 3 (no EAN) is not asked again this week
    assert await h._fetch_picnic(today, now) is None and FakePicnic.calls == 2
    await h._fetch_picnic(today, now)
    assert FakePicnic.calls == 2  # checked this week, also "not sold" (2)
    counts = await h._run(db.store_counts)
    assert counts == {"gtin_known": 2, "picnic_matched": 1}, counts

    c = await h.async_compare_list()
    print(c)
    assert c["picnic_connected"] and c["on_list"] == 2 and c["compared"] == 1
    assert c["totals"] == {"ah": 4.98, "picnic": 4.38}, c["totals"]
    cola = next(r for r in c["items"] if r["product_id"] == 1)
    assert cola["picnic"] == 2.19 and cola["picnic_title"] == "Coca-Cola 1,5L"

    over = {p["product_id"]: p for p in await h._run(db.products_overview)}
    assert over[1]["stores"]["picnic"]["price"] == 2.19 and over[2]["stores"] == {}

    # without the Picnic integration: skipped, no error
    Hass.config_entries = ConfigEntries()
    assert await h._fetch_picnic("2026-10-30", now) is None and FakePicnic.calls == 2
    print("PICNIC OK")


asyncio.run(main())
