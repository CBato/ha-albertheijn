"""Offline test: Picnic matching (barcode, else name and size), confirm/reject, and the list compared."""

import asyncio
import sqlite3
from datetime import date, timedelta
import os
from types import SimpleNamespace as NS

from _harness import tmp, ConfigEntries  # sets up the Home Assistant stubs

import importlib

history = importlib.import_module("albert_heijn.history")
compare = importlib.import_module("albert_heijn.compare")
db = importlib.import_module("albert_heijn.db")
history.STORE_PAUSE = 0
history.AUTO_CONFIRM_SCORE = 101  # first without auto-confirm; checked separately at the end

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
    article_calls = 0

    def get_article_by_gtin(self, gtin):
        FakePicnic.calls += 1
        return ARTICLES.get(BY_EAN.get(gtin))

    def get_article(self, article_id):
        FakePicnic.article_calls += 1
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
    assert calve["match"] == "size" and calve["score"] == 85, calve  # same product, 650 g instead of 450 g
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

    # search by hand: results with score, best first; choosing one = confirm
    res = await h.async_search_store(3)
    assert res["query"] == "halfvolle melk" and [i["article_id"] for i in res["items"]][:1] == ["s3"] and res["items"][0]["score"] >= 90, res
    res = await h.async_search_store(2, "calve pindakaas")
    assert res["items"][0]["kind"] == "size"
    ph = await h._run(db.product_history, 2)
    assert ph["store_checked"]["picnic"] == today

    # product page shows it
    assert (await h._run(db.product_history, 3))["stores"]["picnic"]["article_id"] == "s4"

    # a week later: barcode and confirmed matches only get the price of their article; the Calvé (another size) is
    # looked for again, a better match (the same size) may exist
    n, a = FakePicnic.calls, FakePicnic.article_calls
    next_week = (date.fromisoformat(today) + timedelta(days=7)).isoformat()
    history.date = type("D", (date,), {"today": staticmethod(lambda: date.fromisoformat(next_week))})
    assert await h._fetch_picnic(next_week, now) is None
    history.date = date
    assert FakePicnic.calls == n + 2 and FakePicnic.article_calls == a + 2, (FakePicnic.calls - n, FakePicnic.article_calls - a)
    assert (await h._run(db.store_prices_for, [2]))[2]["picnic"]["match"] == "size"  # kind and score kept

    # stored matches scored with old rules are scored again (no search): an old 95 for another size becomes 85
    con = sqlite3.connect(Hass.config.path("albert_heijn.db"))
    con.execute("UPDATE store_prices SET score = 95 WHERE store = 'picnic' AND product_id = 2"); con.commit(); con.close()
    assert await h._run(db.rescore_name_matches, "picnic") == 1
    assert (await h._run(db.store_prices_for, [2]))[2]["picnic"]["score"] == 85

    # only name matches of the same size count as confirmed automatically; the Calvé (other size, 85) does not
    assert await h._run(db.confirm_high_scores, "picnic", 95, now) == 0
    assert (await h._run(db.store_prices_for, [2]))[2]["picnic"]["match"] == "size"
    # 0.28.0 did confirm other sizes: undone once, and the product is looked for again
    await h._run(db.add_confirmed_match, "picnic", 2, "s2", now)
    rows = lambda: sqlite3.connect(Hass.config.path("albert_heijn.db")).execute(  # noqa: E731
        "SELECT COUNT(*) FROM store_prices WHERE store = 'picnic' AND product_id = 2").fetchone()[0]
    before = rows()
    assert await h._run(db.unconfirm_other_sizes, "picnic") == 1
    assert rows() == before - 1  # the latest check is removed (older prices stay as history)
    assert (await h._run(db.store_matches, "picnic")).get(2, {}).get("confirmed") is None

    # not found: the best guess below the match score is kept, to confirm by hand
    await h._run(db.save_store_price, "picnic", 9, today, None, {"article_id": "g1", "title": "Picnic kookroom", "price": 1.1, "score": 45})
    assert (await h._run(db.store_guesses, "picnic", [9, 1]))[9]["article_id"] == "g1"

    # Picnic answers nonsense now and then: wait and go on; 3 in a row: stop, the rest stays due
    history.STORE_BACKOFF = 0
    orig = FakePicnic.get_article_by_gtin
    flaky = {"n": 0}

    def once_broken(self, gtin):
        flaky["n"] += 1
        if flaky["n"] == 1:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return orig(self, gtin)
    FakePicnic.get_article_by_gtin = once_broken
    await h._run(db.save_order, 11, "2026-10-19", "delivered", [{"product_id": 7, "title": "Coca-Cola Light", "brand": "Coca-Cola",
                                                                  "unit_size": "1,5 l", "quantity": 1, "price": 2.0}], now)
    await h._run(db.save_gtins, 7, ["8710400000077"], now)
    assert await h._fetch_picnic("2026-11-30", now) is None  # one error, then on
    for pid in (21, 22, 23):  # three new products, Picnic keeps failing
        await h._run(db.save_order, 100 + pid, "2026-10-19", "delivered", [{"product_id": pid, "title": f"Merk product {pid}", "brand": "Merk",
                                                                             "unit_size": "1 l", "quantity": 1, "price": 1.0}], now)
        await h._run(db.save_gtins, pid, [f"87104000000{pid}"], now)
    FakePicnic.get_article_by_gtin = lambda self, gtin: (_ for _ in ()).throw(ValueError("Expecting value"))
    err = await h._fetch_picnic("2026-12-30", now)
    assert err and "gestopt na" in err, err
    # after a stopped round Picnic is left alone for a day
    again = await h._fetch_picnic("2026-12-30", now)
    assert again and "gepauzeerd" in again, again
    h.meta.pop("picnic_paused_until")
    assert {21, 22, 23} <= {p["product_id"] for p in await h._run(db.store_due, "picnic", "2026-10-22", 100)}  # still to do
    FakePicnic.get_article_by_gtin = orig

    # the client of the loaded Picnic integration is used (one session and token); not loaded: leave Picnic alone
    shared = FakePicnic()
    entry = Hass.config_entries.entries[0]
    entry.runtime_data = NS(picnic_api_client=shared)
    assert (await h._picnic())[0] is shared
    entry.state = NS(value="setup_retry")
    client, why = await h._picnic()
    assert client is None and "niet geladen" in why
    del entry.state, entry.runtime_data

    # confirmations are scored again with the current rules: an automatic one that no longer reaches 95 loses it,
    # one you decided (with a rejection) stays
    await h._run(db.add_confirmed_match, "picnic", 2, "s2", now)  # Calvé 650 g for your 450 g: 'size', 85
    assert await h._run(db.recheck_confirmed, "picnic") == 1
    assert (await h._run(db.store_matches, "picnic")).get(2, {}).get("confirmed") is None
    assert (await h._run(db.store_prices_for, [2]))[2]["picnic"]["match"] == "size"

    # an automatic confirmation shows as 'auto', yours as 'confirmed'
    await h._run(db.add_confirmed_match, "picnic", 1, "s1", now)
    con = sqlite3.connect(Hass.config.path("albert_heijn.db"))
    con.execute("UPDATE store_prices SET match = 'confirmed' WHERE store = 'picnic' AND product_id = 1"); con.commit(); con.close()
    assert await h._run(db.mark_auto_matches, "picnic") >= 1
    assert (await h._run(db.store_prices_for, [1]))[1]["picnic"]["match"] == "auto"

    # without the Picnic integration: skipped, no error
    Hass.config_entries = ConfigEntries()
    n = FakePicnic.calls
    assert await h._fetch_picnic("2026-10-30", now) is None and FakePicnic.calls == n
    print("PICNIC OK")


asyncio.run(main())
