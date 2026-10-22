"""Prices at other supermarkets, matched on EAN (barcode), else on name and size.

Every store is used through the same small interface (PicnicStore, JumboStore): search(query), article(id) and
lookup(gtin), each returning article_id, title, price and original_price (euro), unit and image_url.

Picnic: through the official Picnic integration of Home Assistant. Its own client (connection) is used, so there is
one login token: Picnic can hand out a new token with any answer, and the integration stores it. Nothing to configure
here; without that integration (or while it is not loaded) the comparison is simply skipped.
The Picnic client (python-picnic-api2, installed by Home Assistant for its Picnic integration) is blocking:
call these functions in the executor (PicnicStore does that).

Jumbo: no account needed. The GraphQL API that jumbo.com itself uses (not documented, no official API); it wants the
client name and version of the website, read from its home page. If Jumbo changes it, the checks fail and Jumbo is
paused like any store after errors; AH and Picnic go on.
"""

from __future__ import annotations

import re
from typing import Any

PICNIC = "picnic"
JUMBO = "jumbo"
STORES = (PICNIC, JUMBO)
STORE_NAMES = {PICNIC: "Picnic", JUMBO: "Jumbo"}
# Picnic's image CDN, as in python-picnic-api2 (helper.get_image): <base>/<image_id>/<size>.png
PICNIC_IMAGE = "https://storefront-prod.nl.picnicinternational.com/static/images/{}/small.png"


def picnic_entry(hass):
    """The config entry of the Picnic integration, if it is set up (call in the event loop)."""
    return next((e for e in hass.config_entries.async_entries("picnic") if e.data.get("access_token")), None)


def picnic_credentials(hass) -> dict[str, str] | None:
    """Access token and country of the Picnic integration, if it is set up (call in the event loop)."""
    entry = picnic_entry(hass)
    if not entry:
        return None
    return {"auth_token": entry.data["access_token"], "country_code": entry.data.get("country_code") or "NL"}


def shared_client(entry):
    """The client of the loaded Picnic integration (one session and token for both), or None."""
    return getattr(getattr(entry, "runtime_data", None), "picnic_api_client", None)


def picnic_loaded(entry) -> bool:
    """False while the Picnic integration is not loaded (e.g. setup failed): then leave Picnic alone."""
    state = getattr(entry, "state", None)
    return state is None or getattr(state, "value", state) == "loaded"


def picnic_client(credentials: dict[str, str]):
    """A Picnic API client, or None when the library is not installed (blocking)."""
    try:
        from python_picnic_api2 import PicnicAPI
    except ImportError:
        return None
    return PicnicAPI(**credentials)


def _euro(cents) -> float | None:
    return round(cents / 100, 2) if isinstance(cents, (int, float)) else None


def _image(obj) -> str | None:
    image_id = getattr(obj, "image_id", None)
    return PICNIC_IMAGE.format(image_id) if image_id else None


def picnic_search(client, query: str, limit: int = 8) -> list[dict[str, Any]]:
    """Picnic search results: article_id, title, price (euro), unit (blocking)."""
    result = client.search(query)
    return [
        {"article_id": i.id, "title": getattr(i, "name", None), "price": _euro(getattr(i, "display_price", None)),
         "unit": getattr(i, "unit_quantity", None), "image_url": _image(i)}
        for i in (getattr(result, "items", None) or [])[:limit]
        if getattr(i, "id", None)
    ]


def picnic_article(client, article_id: str) -> dict[str, Any] | None:
    """One Picnic article by id, like picnic_lookup (blocking)."""
    return _article(client.get_article(article_id))


def _article(article) -> dict[str, Any] | None:
    if not article:
        return None
    return {
        "article_id": getattr(article, "id", None),
        "title": getattr(article, "name", None),
        "price": _euro(getattr(article, "price", None)),
        "original_price": _euro(getattr(article, "original_price", None)),
        "unit": getattr(article, "unit_quantity", None),
        "image_url": _image(article),
    }


def picnic_lookup(client, gtin: str) -> dict[str, Any] | None:
    """The Picnic article for a barcode: article_id, title, price and original_price (euro), unit. None if Picnic
    doesn't sell it (blocking)."""
    return _article(client.get_article_by_gtin(gtin))


class PicnicStore:
    """Picnic through its blocking client, in the executor."""

    def __init__(self, hass, client) -> None:
        self._hass, self._client = hass, client

    async def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        return await self._hass.async_add_executor_job(picnic_search, self._client, query, limit)

    async def article(self, article_id: str) -> dict[str, Any] | None:
        return await self._hass.async_add_executor_job(picnic_article, self._client, article_id)

    async def lookup(self, gtin: str) -> dict[str, Any] | None:
        return await self._hass.async_add_executor_job(picnic_lookup, self._client, gtin)


JUMBO_SITE = "https://www.jumbo.com/"
JUMBO_API = "https://www.jumbo.com/api/graphql"
JUMBO_VERSION = "master-v39.5.2-web"  # used when the home page doesn't say (seen in October 2026)
JUMBO_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
_JUMBO_FIELDS = "id title subtitle ean image price { price promoPrice }"
_JUMBO_SEARCH = ("query SearchProducts($input: ProductSearchInput!) { searchProducts(input: $input) { products { "
                 + _JUMBO_FIELDS + " } } }")
_JUMBO_PRODUCT = "query Product($sku: String!) { product(sku: $sku) { " + _JUMBO_FIELDS + " } }"


class JumboError(Exception):
    """Jumbo answered with an error (or not with data)."""


class JumboStore:
    """Jumbo through the GraphQL API of its website (async, aiohttp)."""

    def __init__(self, session) -> None:
        self._session = session
        self._version: str | None = None

    async def _headers(self) -> dict[str, str]:
        if not self._version:
            # the website's own client version (the API refuses requests without one)
            try:
                async with self._session.get(JUMBO_SITE, headers={"User-Agent": JUMBO_UA}) as resp:
                    page = await resp.text()
                m = re.search(r'name:"JUMBO_WEB",version:"([^"]+)"', page)
                self._version = m[1] if m else JUMBO_VERSION
            except Exception:  # noqa: BLE001 - any network error: try the known version
                self._version = JUMBO_VERSION
        return {"User-Agent": JUMBO_UA, "content-type": "application/json", "apollographql-client-name": "JUMBO_WEB",
                "apollographql-client-version": self._version, "x-source": "JUMBO_WEB-search"}

    async def _query(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        async with self._session.post(JUMBO_API, json={"query": query, "variables": variables},
                                      headers=await self._headers()) as resp:
            if resp.status != 200:
                self._version = None  # maybe a new version of the website: read it again next time
                raise JumboError(f"Jumbo: HTTP {resp.status}")
            body = await resp.json(content_type=None)
        if not isinstance(body, dict) or body.get("errors") and not body.get("data"):
            raise JumboError(f"Jumbo: {(body.get('errors') or [{}])[0].get('message') if isinstance(body, dict) else body}")
        return body.get("data") or {}

    async def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        data = await self._query(_JUMBO_SEARCH, {"input": {"searchTerms": query, "searchType": "keyword", "offSet": 0,
                                                             "limit": limit}})
        return [a for p in ((data.get("searchProducts") or {}).get("products") or []) if (a := jumbo_article(p))]

    async def article(self, article_id: str) -> dict[str, Any] | None:
        return jumbo_article((await self._query(_JUMBO_PRODUCT, {"sku": article_id})).get("product"))

    async def lookup(self, gtin: str) -> dict[str, Any] | None:
        """Search on the barcode; only a product with exactly that EAN counts."""
        ean = gtin.lstrip("0").zfill(13)  # a GTIN-14 with a leading zero is the EAN-13 Jumbo searches on
        data = await self._query(_JUMBO_SEARCH, {"input": {"searchTerms": ean, "searchType": "keyword", "offSet": 0,
                                                             "limit": 3}})
        for p in (data.get("searchProducts") or {}).get("products") or []:
            if (p.get("ean") or "").lstrip("0") == gtin.lstrip("0"):
                return jumbo_article(p)
        return None


def jumbo_article(p: dict[str, Any] | None) -> dict[str, Any] | None:
    """A Jumbo product as an article: the offer price when there is one (then original_price is the normal one)."""
    if not p or not p.get("id"):
        return None
    price = p.get("price") or {}
    promo = price.get("promoPrice")
    return {
        "article_id": p["id"],
        "title": p.get("title"),
        "price": _euro(promo if promo else price.get("price")),
        "original_price": _euro(price.get("price")) if promo else None,
        "unit": p.get("subtitle"),
        "image_url": p.get("image"),
    }
