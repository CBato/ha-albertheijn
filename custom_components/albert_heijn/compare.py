"""Prices at other supermarkets, matched on EAN (barcode).

Picnic: through the official Picnic integration of Home Assistant. Its login (access token + country) is reused,
so there is nothing to configure here; without that integration the comparison is simply skipped.
The Picnic client (python-picnic-api2, installed by Home Assistant for its Picnic integration) is blocking:
call these functions in the executor.
"""

from __future__ import annotations

from typing import Any

PICNIC = "picnic"


def picnic_credentials(hass) -> dict[str, str] | None:
    """Access token and country of the Picnic integration, if it is set up (call in the event loop)."""
    for entry in hass.config_entries.async_entries("picnic"):
        if entry.data.get("access_token"):
            return {"auth_token": entry.data["access_token"], "country_code": entry.data.get("country_code") or "NL"}
    return None


def picnic_client(credentials: dict[str, str]):
    """A Picnic API client, or None when the library is not installed (blocking)."""
    try:
        from python_picnic_api2 import PicnicAPI
    except ImportError:
        return None
    return PicnicAPI(**credentials)


def _euro(cents) -> float | None:
    return round(cents / 100, 2) if isinstance(cents, (int, float)) else None


def picnic_search(client, query: str, limit: int = 8) -> list[dict[str, Any]]:
    """Picnic search results: article_id, title, price (euro), unit (blocking)."""
    result = client.search(query)
    return [
        {"article_id": i.id, "title": getattr(i, "name", None), "price": _euro(getattr(i, "display_price", None)),
         "unit": getattr(i, "unit_quantity", None)}
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
    }


def picnic_lookup(client, gtin: str) -> dict[str, Any] | None:
    """The Picnic article for a barcode: article_id, title, price and original_price (euro), unit. None if Picnic
    doesn't sell it (blocking)."""
    return _article(client.get_article_by_gtin(gtin))
