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


def picnic_lookup(client, gtin: str) -> dict[str, Any] | None:
    """The Picnic article for a barcode: article_id, title, price and original_price (euro), unit. None if Picnic
    doesn't sell it (blocking)."""
    article = client.get_article_by_gtin(gtin)
    if not article:
        return None

    def euro(cents):
        return round(cents / 100, 2) if isinstance(cents, (int, float)) else None

    return {
        "article_id": getattr(article, "id", None),
        "title": getattr(article, "name", None),
        "price": euro(getattr(article, "price", None)),
        "original_price": euro(getattr(article, "original_price", None)),
        "unit": getattr(article, "unit_quantity", None),
    }
