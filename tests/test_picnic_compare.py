"""Offline test: Picnic matching (barcode, else name and size), confirm/reject, and the list compared."""

import asyncio
import os
from types import SimpleNamespace as NS

from _harness import tmp, ConfigEntries  # sets up the Home Assistant stubs

import importlib

history = importlib.import_module("albert_heijn.history")
compare = importlib.import_module("albert_heijn.compare")
db = importlib.import_module("albert_heijn.db")
history.STORE_PAUSE = 0

# your products: 1 branded with EAN, 2 Calvé without EAN at Picnic (other size there), 3 store brand
PRODUCTS = [(1, "Coca-Cola Regular", "Coca-Cola", "1,5 l"), (2, "Calvé Pindakaas", "Calvé", "450 g"), (3, "AH Halfvolle melk", "AH", "1 l")]
GTINS = {1: ["8710400000011"], 2: ["8710400000028"], 3: []}
ARTICLES = {
    "s1": NS(id="s1", name="Coca-Cola Regular", price=219, original_price=None, unit_quantity="1,5 liter"),
    "s2": NS(id="s2", name="Calvé Pindakaas", price=379, original_price=None, unit_quantity="650 gram", image_id="img2"),
    "s3": NS(id="s3", name="Picnic halfvolle melk", price=109, original_price=None, unit_quantity="1 liter"),
    "s4": NS(id="s4", name="Campina halfvolle melk", price=139, original_price=None, unit_quantity="1 liter"),
}
BY_EAN = {"8710400000011": "s1"}  # Picnic has the Calvé, but in another size (other EAN)
SEARCH = {"calve pindakaas": ["s2"], "halfvolle melk": ["s3", "s4"]}


class Hass:
    config_entries = ConfigEntries([NS(domain="picnic", data={"access_token": "t", "country_code": "NL"})])

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
                {"product_id": 2, "title": "Calvé Pindakaas", "quantity": 1, "checked": False},
                {"product_id": 3, "title": "AH Halfvolle melk", "quantity": 2, "checked": False},
                {"product_id": None, "title": "bananen", "quantity": 1, "checked": False}]


class FakePicnic:
    calls = 0

    def get_article_by_gtin(self, gtin):
        FakePicnic.calls += 1
        return ARTICLES.get(BY_EAN.get(gtin))

    def get_article(self, article_id):
        return ARTICLES.get(article_id)

    def search(self, query):
        FakePicnic.calls += 1
        return NS(items=[NS(id=a, name=ARTICLES[a].name, display_price=ARTICLES[a].price, unit_quantity=ARTICLES[a].unit_quantity,
                            image_id=getattr(ARTICLES[a], "image_id", None))
                         for a in SEARCH.get(query, [])])


compare.picnic_client = lambda credentials: FakePicnic()


async def main():
    h = history.OrderHistory(Hass(), FakeApi(), "e1")
    await h._run(db.init)
    now, today = "2026-10-22T04:15:00+00:00", "2026-10-22"
    await h._run(db.save_order, 10, "2026-10-18", "delivered",
                 [{"product_id": p, "title": t, "brand": b, "unit_size": u, "quantity": 1, "price": 2.0} for p, t, b, u in PRODUCTS], now)
    await h._run(db.save_prices, today, [{"product_id": 1, "price": 2.49}, {"product_id": 2, "price": 2.99}, {"product_id": 3, "price": 1.19}])

    assert await h._fetch_gtins(now) == 2
    assert await h._fetch_picnic(today, now) is None
    n = FakePicnic.calls
    await h._fetch_picnic(today, now)
    assert FakePicnic.calls == n  # everything checked this week

    got = await h._run(db.store_prices_for, [1, 2, 3])
    cola, calve, melk = (got[p]["picnic"] for p in (1, 2, 3))
    assert cola["match"] == "ean" and cola["score"] == 100
    assert calve["match"] == "size" and calve["score"] >= 90, calve  # same product, 650 g instead of 450 g
    assert calve["image_url"] == "https://storefront-prod.nl.picnicinternational.com/static/images/img2/small.png", calve
    assert melk["article_id"] == "s3" and melk["match"] == "same", melk  # store brand vs store brand
    counts = await h._run(db.store_counts)
    assert counts == {"gtin_known": 2, "picnic_matched": 3}, counts

    c = await h.async_compare_list()
    rows = {r["product_id"]: r for r in c["items"]}
    # Calvé: 650 g for 3,79 -> 450 g costs 2,62 at Picnic
    assert rows[2]["picnic_equiv"] == 2.62 and rows[2]["picnic_per_unit"] == 5.831, rows[2]
    assert c["totals"] == {"n": 1, "ah": 4.98, "picnic": 4.38}, c["totals"]  # sure: only the barcode match
    assert c["likely"] == {"n": 3, "ah": 10.35, "picnic": 9.18}, c["likely"]

    # reject the Picnic milk -> the next best (Campina) right away; confirm it -> sure
    now_match = await h.async_confirm_match(3, "s3", False)
    assert now_match["article_id"] == "s4" and now_match["match"] == "same", now_match
    now_match = await h.async_confirm_match(3, "s4", True)
    assert now_match["match"] == "confirmed" and now_match["score"] == 100
    c = await h.async_compare_list()
    assert c["totals"]["n"] == 2 and c["totals"]["picnic"] == 4.38 + 2.78, c["totals"]

    # product page shows it
    assert (await h._run(db.product_history, 3))["stores"]["picnic"]["article_id"] == "s4"

    # without the Picnic integration: skipped, no error
    Hass.config_entries = ConfigEntries()
    n = FakePicnic.calls
    assert await h._fetch_picnic("2026-10-30", now) is None and FakePicnic.calls == n
    print("PICNIC OK")


asyncio.run(main())
