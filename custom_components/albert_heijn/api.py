"""Albert Heijn API client."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote

import aiohttp

try:
    from .const import (
        API_BASE_URL,
        AUTH_REFRESH_URL,
        CLIENT_ID,
        CLIENT_VERSION,
        GRAPHQL_URL,
        USER_AGENT,
    )
except ImportError:
    # Allow standalone import for testing without homeassistant
    from const import (  # type: ignore[no-redef]
        API_BASE_URL,
        AUTH_REFRESH_URL,
        CLIENT_ID,
        CLIENT_VERSION,
        GRAPHQL_URL,
        USER_AGENT,
    )

_LOGGER = logging.getLogger(__name__)


@dataclass
class DeliverySlot:
    """Delivery time window."""

    date: str
    date_display: str
    time_display: str
    start_time: str
    end_time: str


@dataclass
class Fulfillment:
    """A scheduled order with delivery information."""

    order_id: int
    status: str
    status_description: str
    shopping_type: str
    total_price: float
    transaction_completed: bool
    modifiable: bool
    delivery_slot: DeliverySlot
    delivery_address: dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderDetails:
    """Detailed order information including closing time."""

    order_id: int
    state: str
    delivery_date: str
    closing_time: str | None
    products_closing_time: str | None
    delivery_start: str | None
    delivery_end: str | None
    total_items: int
    reopenable: bool
    order_method: str | None


@dataclass
class OrderValueLimits:
    """Minimum order value information."""

    minimum_order_value: float
    submittable: bool
    deadline: str | None = None


@dataclass
class Product:
    """A product from the AH webshop."""

    id: int
    title: str
    brand: str
    price: float
    unit_size: str
    is_orderable: bool
    image_url: str | None = None


@dataclass
class OrderItem:
    """An item in an order."""

    product_id: int
    title: str
    brand: str
    quantity: int
    price: float
    unit_size: str
    category: str = ""
    image_url: str | None = None
    price_before_bonus: float | None = None
    bonus: str | None = None
    unit_price: str | None = None


@dataclass
class AlbertHeijnData:
    """Container for all data fetched from the API."""

    fulfillments: list[Fulfillment] = field(default_factory=list)
    next_order: Fulfillment | None = None
    order_details: OrderDetails | None = None
    order_value_limits: OrderValueLimits | None = None
    all_order_details: dict[int, OrderDetails] = field(default_factory=dict)
    next_order_items: list[OrderItem] = field(default_factory=list)


def _image_url(p: dict[str, Any]) -> str | None:
    """Pick a small product image (around 200px) from an AH product dict."""
    images = [i for i in p.get("images") or [] if isinstance(i, dict) and i.get("url")]
    if not images:
        return None
    sized = [i for i in images if i.get("width")]
    if sized:
        return min(sized, key=lambda i: abs(int(i["width"]) - 200))["url"]
    return images[0]["url"]


def _product_from_raw(p: dict[str, Any]) -> Product:
    """Product from the product dict that the AH REST endpoints return."""
    return Product(
        id=p.get("webshopId") or p.get("id") or 0,
        title=p.get("title", ""),
        brand=p.get("brand", ""),
        price=p.get("currentPrice") or p.get("priceBeforeBonus") or 0,
        unit_size=p.get("salesUnitSize", ""),
        is_orderable=p.get("isOrderable", False),
        image_url=_image_url(p),
    )


class AlbertHeijnApiError(Exception):
    """Base exception for API errors."""


class AlbertHeijnAuthError(AlbertHeijnApiError):
    """Authentication error."""


class AlbertHeijnApi:
    """Client for the Albert Heijn API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        access_token: str,
        refresh_token: str,
    ) -> None:
        """Initialize the API client."""
        self._session = session
        self._access_token = access_token
        self._refresh_token = refresh_token

    @property
    def access_token(self) -> str:
        """Return the current access token."""
        return self._access_token

    @property
    def refresh_token(self) -> str:
        """Return the current refresh token."""
        return self._refresh_token

    def _headers(self) -> dict[str, str]:
        """Return default request headers."""
        return {
            "User-Agent": USER_AGENT,
            "x-clientname": "ipad",
            "x-clientversion": CLIENT_VERSION,
            "x-application": "AHWEBSHOP",
            "x-accept-language": "nl-NL",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self._access_token}",
        }

    async def _request(
        self,
        method: str,
        path: str,
        json_data: Any = None,
        retry_auth: bool = True,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        """Make an authenticated API request."""
        url = f"{API_BASE_URL}{path}"
        headers = self._headers()
        if extra_headers:
            headers.update(extra_headers)

        async with self._session.request(
            method, url, headers=headers, json=json_data
        ) as resp:
            if resp.status == 401 and retry_auth:
                await self.refresh_access_token()
                return await self._request(method, path, json_data, retry_auth=False)
            if resp.status == 401:
                raise AlbertHeijnAuthError("Authentication failed after token refresh")
            if resp.status >= 400:
                text = await resp.text()
                raise AlbertHeijnApiError(
                    f"API request failed: {resp.status} - {text}"
                )
            if resp.content_type == "application/json":
                return await resp.json()
            return None

    async def _graphql(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        retry_auth: bool = True,
    ) -> Any:
        """Make a GraphQL request."""
        headers = self._headers()
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        async with self._session.post(
            GRAPHQL_URL, headers=headers, json=payload
        ) as resp:
            if resp.status == 401 and retry_auth:
                await self.refresh_access_token()
                return await self._graphql(query, variables, retry_auth=False)
            if resp.status == 401:
                raise AlbertHeijnAuthError("Authentication failed after token refresh")
            if resp.status >= 400:
                text = await resp.text()
                raise AlbertHeijnApiError(
                    f"GraphQL request failed: {resp.status} - {text}"
                )

            data = await resp.json()
            if errors := data.get("errors"):
                raise AlbertHeijnApiError(f"GraphQL errors: {errors}")
            return data.get("data")

    async def refresh_access_token(self) -> None:
        """Refresh the access token."""
        payload = {
            "clientId": CLIENT_ID,
            "refreshToken": self._refresh_token,
        }
        headers = {
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        }

        async with self._session.post(
            AUTH_REFRESH_URL, headers=headers, json=payload
        ) as resp:
            if resp.status >= 400:
                raise AlbertHeijnAuthError("Failed to refresh access token")
            data = await resp.json()

        self._access_token = data["access_token"]
        self._refresh_token = data["refresh_token"]

    async def get_order_summaries(self, page: int = 0, size: int = 25) -> list[dict[str, Any]]:
        """Order summaries as the AH app's "Mijn bestellingen" list (newest first, 25 per page by default):
        order_id, date (delivery date), state, cancelled. Goes further back than orderFulfillments (max 10)."""
        data = await self._request(
            "GET", f"/mobile-services/order/v1/summaries?sortBy=DEFAULT&page={int(page)}&size={int(size)}"
        )
        rows = data if isinstance(data, list) else (data or {}).get("content") or (data or {}).get("orders") or []
        out = []
        for r in rows:
            if not isinstance(r, dict) or not r.get("orderId"):
                continue
            state = str(r.get("state") or r.get("orderState") or "")
            out.append({
                "order_id": r["orderId"],
                "date": str(r.get("deliveryDate") or "")[:10],
                "state": state,
                "cancelled": "cancel" in state.lower() or "annul" in state.lower(),
            })
        return out

    async def get_closed_fulfillments(self) -> list[dict[str, Any]]:
        """Get closed orders: order_id, date (delivery date), status, cancelled (True for cancelled orders).

        Not documented by AH; raises AlbertHeijnApiError if the status does not exist.
        """
        query = """query OrderFulfillmentsClosed {
  orderFulfillments(status: CLOSED) {
    result {
      orderId
      statusCode
      statusDescription
      delivery { slot { date } }
    }
  }
}"""
        data = await self._graphql(query)
        out = []
        for r in (data.get("orderFulfillments") or {}).get("result") or []:
            if not r.get("orderId"):
                continue
            code, desc = str(r.get("statusCode") or ""), str(r.get("statusDescription") or "")
            out.append({
                "order_id": r["orderId"],
                "date": ((r.get("delivery") or {}).get("slot") or {}).get("date", ""),
                "status": desc or code,
                "cancelled": "cancel" in code.lower() or "annul" in desc.lower(),
            })
        return out

    async def get_fulfillments(self) -> list[Fulfillment]:
        """Get all open order fulfillments."""
        query = """query OrderFulfillments {
  orderFulfillments(status: OPEN) {
    result {
      orderId
      statusCode
      statusDescription
      shoppingType
      transactionCompleted
      modifiable
      totalPrice {
        totalPrice { amount }
      }
      delivery {
        status
        method
        slot {
          date
          dateDisplay
          timeDisplay
          startTime
          endTime
        }
        address {
          street
          houseNumber
          houseNumberExtra
          city
          postalCode
        }
      }
    }
  }
}"""

        data = await self._graphql(query)
        results = data.get("orderFulfillments", {}).get("result", [])

        fulfillments = []
        for r in results:
            slot = r.get("delivery", {}).get("slot", {})
            address = r.get("delivery", {}).get("address", {})
            fulfillments.append(
                Fulfillment(
                    order_id=r["orderId"],
                    status=r.get("delivery", {}).get("status", ""),
                    status_description=r.get("statusDescription", ""),
                    shopping_type=r.get("shoppingType", ""),
                    total_price=r.get("totalPrice", {})
                    .get("totalPrice", {})
                    .get("amount", 0),
                    transaction_completed=r.get("transactionCompleted", False),
                    modifiable=r.get("modifiable", False),
                    delivery_slot=DeliverySlot(
                        date=slot.get("date", ""),
                        date_display=slot.get("dateDisplay", ""),
                        time_display=slot.get("timeDisplay", ""),
                        start_time=slot.get("startTime", ""),
                        end_time=slot.get("endTime", ""),
                    ),
                    delivery_address=address,
                )
            )

        return fulfillments

    async def get_order_details(self, order_id: int) -> OrderDetails:
        """Get detailed order information including closing time."""
        data = await self._request(
            "GET",
            f"/mobile-services/order/v1/{order_id}/details-grouped-by-taxonomy",
        )

        total_items = 0
        for group in data.get("groupedProductsInTaxonomy", []):
            total_items += len(group.get("orderedProducts", []))

        delivery_period = data.get("deliveryTimePeriod", {})

        return OrderDetails(
            order_id=data.get("orderId", order_id),
            state=data.get("orderState", ""),
            delivery_date=data.get("deliveryDate", ""),
            closing_time=data.get("closingTime"),
            products_closing_time=data.get("productsClosingTime"),
            delivery_start=delivery_period.get("startDateTime"),
            delivery_end=delivery_period.get("endDateTime"),
            total_items=total_items,
            reopenable=data.get("reopenable", False),
            order_method=data.get("orderMethod"),
        )

    async def get_order_items(self, order_id: int) -> list[OrderItem]:
        """Get the list of items in an order."""
        data = await self._request(
            "GET",
            f"/mobile-services/order/v1/{order_id}/details-grouped-by-taxonomy",
        )

        items: list[OrderItem] = []
        for group in data.get("groupedProductsInTaxonomy", []):
            category = group.get("taxonomyName") or group.get("name") or group.get("title") or ""
            for op in group.get("orderedProducts", []):
                product = op.get("product", {})
                items.append(
                    OrderItem(
                        product_id=product.get("webshopId", 0),
                        title=product.get("title", ""),
                        brand=product.get("brand", ""),
                        quantity=op.get("quantity", 1),
                        price=product.get("currentPrice") or product.get("priceBeforeBonus", 0),
                        unit_size=product.get("salesUnitSize", ""),
                        category=category,
                        image_url=_image_url(product),
                        price_before_bonus=product.get("priceBeforeBonus"),
                        bonus=(product.get("bonusMechanism") or None) if product.get("isBonus") else None,
                        unit_price=product.get("unitPriceDescription"),
                    )
                )
        return items

    async def get_order_value_limits(self, order_id: int) -> OrderValueLimits:
        """Get minimum order value limits for an order."""
        query = """query FetchOrderMinimumValueLimits($orderId: Int!) {
  orderValueLimits(orderId: $orderId) {
    minimumOrderValue { amount }
    submittable
  }
}"""
        data = await self._graphql(query, {"orderId": order_id})
        limits = data.get("orderValueLimits", {})
        min_value = limits.get("minimumOrderValue", {})

        return OrderValueLimits(
            minimum_order_value=min_value.get("amount", 0),
            submittable=limits.get("submittable", True),
        )

    async def search_products(self, query: str, limit: int = 10) -> list[Product]:
        """Search for products."""
        data = await self._request(
            "GET",
            f"/mobile-services/product/search/v2?query={quote(query)}&page=0&size={int(limit)}&sortOn=RELEVANCE",
        )

        return [_product_from_raw(p) for p in data.get("products", []) if p.get("webshopId")]

    async def get_bonus_periods(self) -> list[dict[str, str]]:
        """Bonus weeks (Monday to Sunday): this week, and next week once AH has published it (a few days ahead)."""
        data = await self._request("GET", "/mobile-services/bonuspage/v3/metadata")
        return [
            {"start": p.get("bonusStartDate"), "end": p.get("bonusEndDate")}
            for p in (data or {}).get("periods") or []
            if p.get("bonusStartDate")
        ]

    async def get_bonus_section(self, kind: str, start: str) -> list[dict[str, Any]]:
        """Bonus offers of one week: kind "previously_bought" (bonus on what you bought before) or "personal".

        Group offers ("Alle Hak ...") are expanded to their products.
        """
        if kind == "personal":
            path = f"/mobile-services/bonuspage/v1/personal?bonusStartDate={start}"
        else:
            path = f"/mobile-services/bonuspage/v2/section/previously-bought?application=AHWEBSHOP&date={start}"
        data = await self._request("GET", path)
        out: list[dict[str, Any]] = []
        for entry in (data or {}).get("bonusGroupOrProducts") or []:
            if product := entry.get("product"):
                if product.get("webshopId"):
                    out.append({
                        "product_id": product["webshopId"], "title": product.get("title") or "",
                        "mechanism": product.get("bonusMechanism") or "Bonus",
                        "price": product.get("currentPrice"), "price_was": product.get("priceBeforeBonus"),
                        "image_url": _image_url(product), "group": None, "source": kind,
                    })
            elif group := entry.get("bonusGroup"):
                for gp in group.get("products") or []:
                    if gp.get("webshopId"):
                        out.append({
                            "product_id": gp["webshopId"], "title": gp.get("title") or "",
                            "mechanism": group.get("discountDescription") or "Bonus",
                            "price": gp.get("currentPrice"), "price_was": gp.get("priceBeforeBonus"),
                            "image_url": _image_url(gp), "group": group.get("segmentDescription"), "source": kind,
                        })
        return out

    async def get_receipts(self, offset: int = 0, limit: int = 50) -> list[dict[str, Any]]:
        """Till receipts (in-store purchases with the bonus card), newest first: id, datetime, total."""
        query = """query FetchPosReceipts($offset: Int!, $limit: Int!) {
  posReceiptsPage(pagination: {offset: $offset, limit: $limit}) {
    posReceipts { id dateTime totalAmount { amount } }
  }
}"""
        data = await self._graphql(query, {"offset": int(offset), "limit": int(limit)})
        rows = ((data or {}).get("posReceiptsPage") or {}).get("posReceipts") or []
        return [
            {"id": r["id"], "datetime": r["dateTime"], "total": (r.get("totalAmount") or {}).get("amount")}
            for r in rows
            if r.get("id") and r.get("dateTime")
        ]

    async def get_receipt_lines(self, receipt_id: str) -> list[dict[str, Any]]:
        """Lines of one till receipt: pos_id (till product id), name, quantity, amount."""
        query = """query FetchReceipt($id: String!) {
  posReceiptDetails(id: $id) {
    id
    products { id quantity name amount { amount } }
  }
}"""
        data = await self._graphql(query, {"id": receipt_id})
        det = (data or {}).get("posReceiptDetails") or {}
        return [
            {"pos_id": p.get("id"), "name": p.get("name") or "", "quantity": p.get("quantity") or 1,
             "amount": (p.get("amount") or {}).get("amount")}
            for p in det.get("products") or []
            if p.get("id")
        ]

    async def convert_pos_ids(self, pos_ids: list[int]) -> dict[int, int]:
        """Till product ids → webshop product ids (-1 when AH has no online product)."""
        out: dict[int, int] = {}
        ids = [int(i) for i in pos_ids]
        for start in range(0, len(ids), 50):
            chunk = ids[start : start + 50]
            aliases = " ".join(f"p{i}: productConvertId(sourceId: {pid})" for i, pid in enumerate(chunk))
            data = await self._graphql(f"query Convert {{ {aliases} }}") or {}
            out.update({pid: data.get(f"p{i}") or -1 for i, pid in enumerate(chunk)})
        return out

    async def graphql_query(self, query: str, variables: dict[str, Any] | None = None) -> Any:
        """Read-only GraphQL query, for exploring what the AH API offers."""
        if "mutation" in query.lower():
            raise AlbertHeijnApiError("Only queries are allowed, no mutations")
        return await self._graphql(query, variables)

    async def get_shopping_list(self) -> list[dict[str, Any]]:
        """The AH shopping list (in the AH app also the basket): product_id (None for free text), title, quantity, checked."""
        data = await self._request("GET", "/mobile-services/shoppinglist/v2/items")
        out = []
        for row in (data or {}).get("items") or []:
            product = (row.get("productDetails") or {}).get("product") or {}
            pid = product.get("webshopId") or row.get("productId")
            out.append({
                "product_id": pid if row.get("originCode") == "PRD" and pid else None,
                "title": product.get("title") or row.get("description") or "",
                "quantity": int(row.get("quantity") or 1),
                "checked": bool(row.get("strikedthrough")),
            })
        return out

    async def set_list_items(self, items: list[dict[str, Any]]) -> None:
        """Put products on the AH shopping list with a quantity. AH upserts on productId and sets (not adds) the quantity;
        quantity 0 removes the product."""
        body = [
            {
                "description": i.get("title") or str(i["product_id"]),
                "searchTerm": i.get("title") or str(i["product_id"]),
                "type": "SHOPPABLE",
                "originCode": "PRD",
                "quantity": int(i["quantity"]) if i.get("quantity") is not None else 1,
                "strikeThrough": False,
                "productId": int(i["product_id"]),
            }
            for i in items
        ]
        if body:
            await self._request("PATCH", "/mobile-services/shoppinglist/v2/items", json_data={"items": body})

    async def get_prices(self, product_ids: list[int]) -> list[dict[str, Any]]:
        """Current prices for up to ~30 products in one request (price, price without bonus, bonus, unit price)."""
        if not product_ids:
            return []
        query = "&".join(f"ids={int(i)}" for i in product_ids)
        data = await self._request(
            "GET", f"/mobile-services/product/search/v2/products?{query}&sortOn=INPUT_PRODUCT_IDS"
        )
        rows = data if isinstance(data, list) else (data or {}).get("products") or []
        return [
            {
                "product_id": p.get("webshopId"),
                "price": p.get("currentPrice") or p.get("priceBeforeBonus"),
                "price_before_bonus": p.get("priceBeforeBonus"),
                "bonus": (p.get("bonusMechanism") or "Bonus") if p.get("isBonus") else None,
                "unit_price": p.get("unitPriceDescription"),
                # product details (to fill in products we only know from a till receipt)
                "title": p.get("title") or "",
                "brand": p.get("brand") or "",
                "unit_size": p.get("salesUnitSize") or "",
                "category": "/".join(c for c in (p.get("mainCategory"), p.get("subCategory")) if c),
                "image_url": _image_url(p),
            }
            for p in rows
            if isinstance(p, dict) and p.get("webshopId")
        ]

    async def lookup_gtin(self, code: str) -> Product | None:
        """Find the AH product for a barcode (EAN-8/13, UPC-A, GTIN-14). None if AH doesn't sell it."""
        digits = "".join(ch for ch in str(code) if ch.isdigit())
        if len(digits) not in (8, 12, 13, 14):
            raise AlbertHeijnApiError(f"Invalid barcode: {code}")
        try:
            data = await self._request("GET", f"/mobile-services/product/search/v1/gtin/{digits}")
        except AlbertHeijnApiError as err:
            if "failed: 404" in str(err):
                return None
            raise
        if not isinstance(data, dict):
            return None
        product = _product_from_raw(data)
        return product if product.id else None

    async def get_previously_bought(
        self, size: int = 100, page: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        """AH's own "Eerder gekocht" list (undocumented GraphQL). Returns (products, total pages)."""
        rich = "id title brand category salesUnitSize images { url width }"
        basic = "id title brand category"
        last_err: AlbertHeijnApiError | None = None
        for fields in (rich, basic):
            query = (
                '{ productSearch(input: { query: "" previouslyBought: true '
                f"size: {int(size)} page: {int(page)} }}) "
                f"{{ products {{ {fields} }} page {{ totalElements totalPages }} }} }}"
            )
            try:
                data = await self._graphql(query)
            except AlbertHeijnApiError as err:
                last_err = err
                continue
            search = (data or {}).get("productSearch") or {}
            products = [
                {
                    "product_id": p.get("id"),
                    "title": p.get("title", ""),
                    "brand": p.get("brand", ""),
                    "category": p.get("category", ""),
                    "unit_size": p.get("salesUnitSize", ""),
                    "image_url": _image_url(p),
                }
                for p in search.get("products") or []
                if p.get("id")
            ]
            return products, int((search.get("page") or {}).get("totalPages") or 0)
        raise last_err or AlbertHeijnApiError("previously bought failed")

    async def add_to_order(self, items: list[dict[str, Any]], order_id: int | None = None) -> None:
        """Add items to an order.

        Items should be a list of dicts with 'productId' and 'quantity'.
        If order_id is provided, adds to that specific order.
        Otherwise uses the current active order.
        """
        request_items = [
            {
                "productId": item["productId"],
                "quantity": item["quantity"],
                "originCode": "PRD",
                "description": "",
                "strikethrough": False,
            }
            for item in items
        ]

        extra_headers = {}
        if order_id:
            extra_headers["Appie-Current-Order-Id"] = str(order_id)

        await self._request(
            "PUT",
            "/mobile-services/order/v1/items?sortBy=DEFAULT",
            json_data={"items": request_items},
            extra_headers=extra_headers or None,
        )

    async def reopen_order(self, order_id: int) -> None:
        """Reopen a submitted order to allow modifications."""
        query = """mutation OrderReopen($id: Int!) {
  orderReopen(id: $id) {
    status
    errorMessage
  }
}"""
        data = await self._graphql(query, {"id": order_id})
        result = data.get("orderReopen", {})
        if result.get("status") != "SUCCESS":
            raise AlbertHeijnApiError(
                f"Failed to reopen order: {result.get('errorMessage', 'unknown error')}"
            )

    async def revert_order(self, order_id: int) -> None:
        """Revert a reopened order back to submitted state."""
        query = """mutation OrderRevert($id: Int!) {
  orderRevert(id: $id) {
    status
    errorMessage
  }
}"""
        data = await self._graphql(query, {"id": order_id})
        result = data.get("orderRevert", {})
        if result.get("status") != "SUCCESS":
            raise AlbertHeijnApiError(
                f"Failed to revert order: {result.get('errorMessage', 'unknown error')}"
            )

    async def get_active_order_summary(self) -> dict[str, Any] | None:
        """Get the active order summary."""
        try:
            data = await self._request(
                "GET",
                "/mobile-services/order/v1/summaries/active?sortBy=DEFAULT",
            )
            return data
        except AlbertHeijnApiError:
            return None
