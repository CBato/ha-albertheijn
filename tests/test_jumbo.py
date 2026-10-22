"""Offline test: Jumbo through the GraphQL API of its website (faked), matched like Picnic, and the list compared
with two stores."""

import asyncio
import json
import os
import unicodedata
from types import SimpleNamespace as NS

from _harness import tmp, ConfigEntries  # sets up the Home Assistant stubs

import importlib

history = importlib.import_module("albert_heijn.history")
compare = importlib.import_module("albert_heijn.compare")
db = importlib.import_module("albert_heijn.db")
history.STORE_PAUSE = 0
history.REQUEST_PAUSE = 0
compare.STORES = (compare.PICNIC, compare.JUMBO)

JUMBO = {
    "216435STK": {"id": "216435STK", "title": "Calvé Pindakaas Regular 650 g", "subtitle": "650 g", "ean": "8711200431632",
                  "image": "https://www.jumbo.com/img/calve.png", "price": {"price": 475, "promoPrice": None}},
    "216438STK": {"id": "216438STK", "title": "Calvé Pindakaas Regular 350 g", "subtitle": "350 g", "ean": "8711200428953",
                  "image": None, "price": {"price": 349, "promoPrice": 299}},
    "100PAP": {"id": "100PAP", "title": "Jumbo Paprika Rood", "subtitle": None, "ean": "2000000000001", "image": None,
               "price": {"price": 79, "promoPrice": None}},
    "539046PAK": {"id": "539046PAK", "title": "Heineken Premium Pilsener 0.0 Bier Blik 6 x 330ml", "subtitle": "6 x 330 ml",
                  "ean": "8712000058678", "image": None, "price": {"price": 649, "promoPrice": None}},
    "539042PAK": {"id": "539042PAK", "title": "Heineken Premium Pilsener Bier Blik 6 x 330ml", "subtitle": "6 x 330 ml",
                  "ean": "8712000025649", "image": None, "price": {"price": 699, "promoPrice": None}},
}


class Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        return self._body

    async def json(self, content_type=None):
        return json.loads(self._body)


class FakeSession:
    """jumbo.com: the home page with the client version, and /api/graphql (search and product)."""
    posts: list = []
    down = False

    def get(self, url, headers=None):
        return Resp(200, '<script>window.x={apollo:{name:"JUMBO_WEB",version:"master-v40.0.0-web"},ssr:true}</script>')

    def post(self, url, json=None, headers=None):
        FakeSession.posts.append((json, headers))
        if FakeSession.down:
            return Resp(403, "<HTML>Access Denied</HTML>")
        if headers.get("apollographql-client-version") != "master-v40.0.0-web":
            return Resp(200, '{"errors":[{"message":"No client headers set"}]}')
        v = json["variables"]
        if "sku" in v:
            return Resp(200, globals()["json"].dumps({"data": {"product": JUMBO.get(v["sku"])}}))
        term = v["input"]["searchTerms"].lower()
        plain = lambda t: unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()  # noqa: E731
        hits = [p for p in JUMBO.values() if term == p["ean"] or all(w in plain(p["title"]) for w in plain(term).split())]
        return Resp(200, globals()["json"].dumps({"data": {"searchProducts": {"products": hits[: v["input"]["limit"]]}}}))


class Hass:
    config_entries = ConfigEntries([])  # no Picnic: only Jumbo

    class config:
        @staticmethod
        def path(name):
            return os.path.join(tmp, "jumbo-" + name)

    async def async_add_executor_job(self, func, *args):
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)


class FakeApi:
    async def get_shopping_list(self):
        return [{"product_id": 1, "title": "Calvé", "quantity": 1, "checked": False},
                {"product_id": 2, "title": "AH Paprika rood", "quantity": 3, "checked": False},
                {"product_id": 3, "title": "Heineken", "quantity": 1, "checked": False}]


async def main():
    # the client: reads the website's version, offer price, EAN must be exactly the same
    j = compare.JumboStore(FakeSession())
    hits = await j.search("calve pindakaas")
    assert [h["article_id"] for h in hits] == ["216435STK", "216438STK"], hits
    assert hits[1]["price"] == 2.99 and hits[1]["original_price"] == 3.49, hits[1]  # in the offer
    assert hits[0]["price"] == 4.75 and hits[0]["original_price"] is None and hits[0]["unit"] == "650 g"
    assert FakeSession.posts[-1][1]["apollographql-client-version"] == "master-v40.0.0-web"
    assert (await j.lookup("8711200431632"))["article_id"] == "216435STK"
    assert await j.lookup("08711200431632") is not None  # leading zero (GTIN-14 / EAN-13)
    assert await j.lookup("8711200000000") is None
    assert (await j.article("539046PAK"))["title"].startswith("Heineken")
    FakeSession.down = True
    try:
        await j.search("calve")
        raise AssertionError("an error page must raise")
    except compare.JumboError as err:
        assert "403" in str(err)
    assert j._version is None  # read the version again next time
    FakeSession.down = False

    # a round: Calvé on barcode, paprika by name (loose, no size there: one), Heineken never the 0.0
    h = history.OrderHistory(Hass(), FakeApi(), "e1")
    h._jumbo = compare.JumboStore(FakeSession())
    await h._run(db.init)
    assert h.stores() == ["jumbo"]  # Picnic not set up
    now, today = "2026-10-22T04:15:00+00:00", "2026-10-22"
    await h._run(db.save_order, 10, "2026-10-18", "delivered", [
        {"product_id": 1, "title": "Calvé Pindakaas", "brand": "Calvé", "unit_size": "650 g", "quantity": 1, "price": 4.99},
        {"product_id": 2, "title": "AH Paprika rood", "brand": "AH", "unit_size": "per stuk", "quantity": 2, "price": 0.95},
        {"product_id": 3, "title": "Heineken Premium pilsener 6-pack", "brand": "Heineken", "unit_size": "6 x 0,33 l",
         "quantity": 1, "price": 7.49}], now)
    await h._run(db.save_gtins, 1, ["8711200431632"], now)
    await h._run(db.save_prices, today, [{"product_id": 1, "price": 4.99}, {"product_id": 2, "price": 0.95},
                                         {"product_id": 3, "price": 7.49}])
    assert await h._fetch_store("jumbo", today) is None
    got = {p: (await h._run(db.store_prices_for, [p]))[p]["jumbo"] for p in (1, 2, 3)}
    assert got[1]["match"] == "ean" and got[1]["article_id"] == "216435STK", got[1]
    assert got[2]["article_id"] == "100PAP" and got[2]["match"] == "auto", got[2]  # same, 95: confirmed automatically
    assert got[3]["article_id"] == "539042PAK", got[3]  # the beer, not the 0.0

    # the list compared, per store
    c = await h.async_compare_list()
    assert c["stores"] == [{"id": "jumbo", "name": "Jumbo"}]
    rows = {r["product_id"]: r for r in c["items"]}
    assert rows[2]["stores"]["jumbo"]["price"] == 0.79 and rows[2]["picnic"] is None
    sure = c["by_store"]["jumbo"]["sure"]
    assert sure == {"n": 3, "ah": round(4.99 + 0.95 * 3 + 7.49, 2), "store": round(4.75 + 0.79 * 3 + 6.99, 2)}, sure

    # AH in "order mode" (an order open for changes): the shopping list gives 412 -> the products of that order
    api_error = importlib.import_module("albert_heijn.api").AlbertHeijnApiError
    real_list = FakeApi.get_shopping_list

    async def order_mode(self):
        raise api_error('API request failed: 412 - {"message":"Server in order mode"}', 412)
    FakeApi.get_shopping_list = order_mode
    c = await h.async_compare_list(None, [{"product_id": 1, "quantity": 2}])
    assert c["source"] == "order" and [r["product_id"] for r in c["items"]] == [1] and c["items"][0]["quantity"] == 2, c
    try:
        await h.async_compare_list()  # no order to fall back on: the error stays
        raise AssertionError("412 without an order must raise")
    except api_error:
        pass
    # the list as the app shows it: no request to AH at all
    c = await h.async_compare_list([{"product_id": 2, "quantity": 3}])
    assert c["source"] == "app" and c["items"][0]["stores"]["jumbo"]["price"] == 0.79, c
    FakeApi.get_shopping_list = real_list

    # search and confirm by hand at Jumbo
    res = await h.async_search_store(1, "calve pindakaas", "jumbo")
    assert res["store"] == "jumbo" and res["items"][0]["article_id"] == "216435STK" and res["items"][0]["score"] >= 90, res
    m = await h.async_confirm_match(1, "216435STK", False, "jumbo")  # reject: the next best (another size)
    assert m["article_id"] == "216438STK" and m["match"] == "size", m

    # Jumbo down: three errors in a row pause Jumbo, Picnic and AH go on
    FakeSession.down = True
    for pid in (21, 22, 23):
        await h._run(db.save_order, 100 + pid, "2026-10-19", "delivered", [{"product_id": pid, "title": f"Merk product {pid}",
                     "brand": "Merk", "unit_size": "1 l", "quantity": 1, "price": 1.0}], now)
    history.STORE_BACKOFF = 0
    err = await h._fetch_store("jumbo", "2026-10-23")
    assert err and "gestopt na" in err, err
    assert "gepauzeerd" in (await h._fetch_store("jumbo", "2026-10-23"))
    assert h._paused("jumbo") and not h._paused("picnic")
    print("JUMBO OK")


asyncio.run(main())
