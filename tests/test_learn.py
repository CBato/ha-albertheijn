"""Offline test: a product new to us (put on the list, never bought) is looked up right away: details, AH price, EAN
and the match at Picnic; once, and only what is missing."""

import asyncio
import os
from types import SimpleNamespace as NS

from _harness import tmp, ConfigEntries  # sets up the Home Assistant stubs

import importlib

history = importlib.import_module("albert_heijn.history")
compare = importlib.import_module("albert_heijn.compare")
db = importlib.import_module("albert_heijn.db")
history.STORE_PAUSE = 0
compare.STORES = (compare.PICNIC,)  # Jumbo has its own test
history.REQUEST_PAUSE = 0

ARTICLES = {"s9": NS(id="s9", name="Organix bio appel rijstwafeltjes", price=229, original_price=None, unit_quantity="40 gram")}


class Hass:
    config_entries = ConfigEntries([NS(domain="picnic", data={"access_token": "t", "country_code": "NL"})])
    tasks: list = []

    class config:
        @staticmethod
        def path(name):
            return os.path.join(tmp, "learn-" + name)

    async def async_add_executor_job(self, func, *args):
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)

    def async_create_background_task(self, coro, name):
        task = asyncio.ensure_future(coro)
        Hass.tasks.append(task)
        return task


class FakeApi:
    price_calls = 0
    gtin_calls = 0

    async def get_prices(self, ids):
        FakeApi.price_calls += 1
        return [{"product_id": 9, "price": 1.99, "price_before_bonus": 2.19, "bonus": "10% KORTING", "unit_price": None,
                 "title": "Organix Apple rice cake clouds 12m+", "brand": "Organix", "unit_size": "40 g",
                 "category": "Baby", "image_url": "https://ah/img9.png"}] if 9 in ids else []

    async def get_product_gtins(self, pid):
        FakeApi.gtin_calls += 1
        return []  # no EAN: found by name


class FakePicnic:
    calls = 0

    def get_article_by_gtin(self, gtin):
        FakePicnic.calls += 1

    def get_article(self, article_id):
        return ARTICLES.get(article_id)

    def search(self, query):
        FakePicnic.calls += 1
        hits = ["s9"] if "organix" in query else []
        return NS(items=[NS(id=a, name=ARTICLES[a].name, display_price=ARTICLES[a].price, unit_quantity=ARTICLES[a].unit_quantity,
                            image_id=None) for a in hits])


compare.picnic_client = lambda credentials: FakePicnic()


async def main():
    h = history.OrderHistory(Hass(), FakeApi(), "e1")
    await h._run(db.init)

    # the product sheet of a product we never saw: it answers right away and looks it up in the background
    first = await h.async_product_history(9)
    assert first["learning"] is True and first["purchases"] == []
    await asyncio.gather(*Hass.tasks)
    d = await h.async_product_history(9)
    assert d["learning"] is False, "looked up once"
    assert d["product"]["title"] == "Organix Apple rice cake clouds 12m+" and d["product"]["brand"] == "Organix", d["product"]
    assert d["prices"] and d["prices"][-1]["p"] == 1.99 and d["prices"][-1]["bonus"] == "10% KORTING", d["prices"]
    assert d["stores"]["picnic"]["article_id"] == "s9" and d["stores"]["picnic"]["score"] >= 60, d["stores"]
    assert FakeApi.price_calls == 1 and FakeApi.gtin_calls == 1

    # on the bon too: AH price and the Picnic price
    ah = await h._run(db.ah_prices_now, [9])
    assert ah[9]["price"] == history.effective_price("10% KORTING", 1.99, 2.19), ah

    # again (the list every 5 minutes): nothing is asked again
    calls = (FakeApi.price_calls, FakeApi.gtin_calls, FakePicnic.calls)
    assert await h.async_learn([9]) is False
    # after a restart (memory empty) neither: the database knows it was looked up
    h2 = history.OrderHistory(Hass(), FakeApi(), "e1")
    assert await h2.async_learn([9]) is False
    assert (FakeApi.price_calls, FakeApi.gtin_calls, FakePicnic.calls) == calls

    # a product we know but never checked at Picnic: only the Picnic check
    await h._run(db.fill_product_details, [{"product_id": 7, "title": "Organix Melon rice cakes", "brand": "Organix"}], "2026-10-22")
    await h._run(db.mark_details_tried, [7], "2026-10-22")
    await h._run(db.save_gtins, 7, [], "2026-10-22")
    p = FakeApi.price_calls
    assert await h.async_learn([7]) is True
    assert FakeApi.price_calls == p
    assert (await h._run(db.product_history, 7))["store_checked"]["picnic"]
    print("learn: ok")


asyncio.run(main())
