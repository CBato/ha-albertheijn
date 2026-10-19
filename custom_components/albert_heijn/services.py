"""Services for Albert Heijn integration."""

from __future__ import annotations

from dataclasses import asdict
import logging

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv

from .api import AlbertHeijnApiError
from .const import DOMAIN
from .coordinator import AlbertHeijnCoordinator

_LOGGER = logging.getLogger(__name__)

SERVICE_SEARCH_PRODUCTS = "search_products"
SERVICE_ADD_TO_ORDER = "add_to_order"
SERVICE_REMOVE_FROM_ORDER = "remove_from_order"
SERVICE_REOPEN_ORDER = "reopen_order"
SERVICE_REVERT_ORDER = "revert_order"
SERVICE_LOOKUP_BARCODE = "lookup_barcode"
SERVICE_GET_FREQUENT_ITEMS = "get_frequent_items"
SERVICE_SYNC_HISTORY = "sync_history"
SERVICE_GET_PRICE_HISTORY = "get_price_history"

ATTR_QUERY = "query"
ATTR_PRODUCT_ID = "product_id"
ATTR_PRODUCTS = "products"
ATTR_QUANTITY = "quantity"
ATTR_ORDER_ID = "order_id"
ATTR_LIMIT = "limit"
ATTR_BARCODE = "barcode"
ATTR_INCLUDE_CATALOG = "include_catalog"
ATTR_REFRESH_CATALOG = "refresh_catalog"


SEARCH_PRODUCTS_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_QUERY): str,
        vol.Optional(ATTR_LIMIT, default=10): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=30)
        ),
    }
)

_QUANTITY = vol.All(vol.Coerce(int), vol.Range(min=0, max=99))

ADD_TO_ORDER_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Optional(ATTR_PRODUCT_ID): vol.Coerce(int),
            vol.Optional(ATTR_QUANTITY, default=1): _QUANTITY,
            vol.Optional(ATTR_PRODUCTS): [
                vol.Schema(
                    {
                        vol.Required(ATTR_PRODUCT_ID): vol.Coerce(int),
                        vol.Optional(ATTR_QUANTITY, default=1): _QUANTITY,
                    }
                )
            ],
            vol.Optional(ATTR_ORDER_ID): vol.Coerce(int),
        }
    ),
    cv.has_at_least_one_key(ATTR_PRODUCT_ID, ATTR_PRODUCTS),
)

REMOVE_FROM_ORDER_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_PRODUCT_ID): vol.Coerce(int),
        vol.Optional(ATTR_ORDER_ID): vol.Coerce(int),
    }
)

ORDER_ID_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ORDER_ID): vol.Coerce(int),
    }
)

LOOKUP_BARCODE_SCHEMA = vol.Schema({vol.Required(ATTR_BARCODE): cv.string})

GET_FREQUENT_ITEMS_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_LIMIT, default=100): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=1000)
        ),
        vol.Optional(ATTR_INCLUDE_CATALOG, default=True): cv.boolean,
    }
)

PRICE_HISTORY_SCHEMA = vol.Schema({vol.Required(ATTR_PRODUCT_ID): vol.Coerce(int)})

SYNC_HISTORY_SCHEMA = vol.Schema(
    {vol.Optional(ATTR_REFRESH_CATALOG, default=False): cv.boolean}
)


def _get_coordinator(hass: HomeAssistant) -> AlbertHeijnCoordinator:
    """Get the first available coordinator."""
    if DOMAIN not in hass.data:
        raise HomeAssistantError("Albert Heijn integration not configured")
    coordinators = hass.data[DOMAIN]
    if not coordinators:
        raise HomeAssistantError("No Albert Heijn accounts configured")
    # Return the first coordinator
    return next(iter(coordinators.values()))


async def async_setup_services(hass: HomeAssistant) -> None:
    """Set up services for the Albert Heijn integration."""

    async def handle_search_products(call: ServiceCall) -> ServiceResponse:
        """Handle search products service call."""
        coordinator = _get_coordinator(hass)
        query = call.data[ATTR_QUERY]
        limit = call.data.get(ATTR_LIMIT, 5)

        try:
            products = await coordinator.api.search_products(query, limit)
        except AlbertHeijnApiError as err:
            raise HomeAssistantError(f"Failed to search products: {err}") from err

        return {"products": [asdict(p) for p in products]}

    async def handle_add_to_order(call: ServiceCall) -> ServiceResponse:
        """Put one or more products in the next order (reopens a submitted order when needed)."""
        coordinator = _get_coordinator(hass)
        products = call.data.get(ATTR_PRODUCTS) or [
            {ATTR_PRODUCT_ID: call.data[ATTR_PRODUCT_ID], ATTR_QUANTITY: call.data[ATTR_QUANTITY]}
        ]
        items = [{"productId": p[ATTR_PRODUCT_ID], "quantity": p[ATTR_QUANTITY]} for p in products]
        try:
            return await coordinator.async_put_order_items(items, call.data.get(ATTR_ORDER_ID))
        except AlbertHeijnApiError as err:
            raise HomeAssistantError(f"Failed to add to order: {err}") from err

    async def handle_remove_from_order(call: ServiceCall) -> None:
        """Handle remove from order service call."""
        coordinator = _get_coordinator(hass)
        try:
            await coordinator.async_put_order_items(
                [{"productId": call.data[ATTR_PRODUCT_ID], "quantity": 0}],
                call.data.get(ATTR_ORDER_ID),
            )
        except AlbertHeijnApiError as err:
            raise HomeAssistantError(f"Failed to remove from order: {err}") from err

    async def handle_reopen_order(call: ServiceCall) -> None:
        """Handle reopen order service call."""
        coordinator = _get_coordinator(hass)
        order_id = call.data[ATTR_ORDER_ID]

        try:
            await coordinator.api.reopen_order(order_id)
        except AlbertHeijnApiError as err:
            raise HomeAssistantError(f"Failed to reopen order: {err}") from err

        await coordinator.async_request_refresh()

    async def handle_revert_order(call: ServiceCall) -> None:
        """Handle revert order service call."""
        coordinator = _get_coordinator(hass)
        order_id = call.data[ATTR_ORDER_ID]

        try:
            await coordinator.api.revert_order(order_id)
        except AlbertHeijnApiError as err:
            raise HomeAssistantError(f"Failed to revert order: {err}") from err

        await coordinator.async_request_refresh()

    async def handle_lookup_barcode(call: ServiceCall) -> ServiceResponse:
        """Barcode (EAN) → AH product. Empty list if AH doesn't sell it."""
        coordinator = _get_coordinator(hass)
        try:
            product = await coordinator.api.lookup_gtin(call.data[ATTR_BARCODE])
        except AlbertHeijnApiError as err:
            raise HomeAssistantError(f"Failed to look up barcode: {err}") from err
        return {"products": [asdict(product)] if product else []}

    async def handle_get_frequent_items(call: ServiceCall) -> ServiceResponse:
        """Products from the order history, most often ordered first; then the rest of "Eerder gekocht"."""
        coordinator = _get_coordinator(hass)
        if not coordinator.history:
            raise HomeAssistantError("Order history not available")
        history = coordinator.history
        return {
            "orders": history.summary.get("orders"),
            "synced_at": history.summary.get("synced_at"),
            "products": history.frequent(call.data[ATTR_LIMIT], call.data[ATTR_INCLUDE_CATALOG]),
        }

    async def handle_get_price_history(call: ServiceCall) -> ServiceResponse:
        """Weekly prices and paid prices of one product."""
        coordinator = _get_coordinator(hass)
        if not coordinator.history:
            raise HomeAssistantError("Order history not available")
        return {"prices": await coordinator.history.async_price_history(call.data[ATTR_PRODUCT_ID])}

    async def handle_sync_history(call: ServiceCall) -> ServiceResponse:
        """Fetch new delivered orders now (normally once a day)."""
        coordinator = _get_coordinator(hass)
        changed = await coordinator.async_sync_history(call.data[ATTR_REFRESH_CATALOG])
        history = coordinator.history
        return {
            "changed": changed,
            **(history.summary if history else {}),
            "closed_orders_supported": history.closed_supported if history else None,
            "last_error": history.last_error if history else None,
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEARCH_PRODUCTS,
        handle_search_products,
        schema=SEARCH_PRODUCTS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_TO_ORDER,
        handle_add_to_order,
        schema=ADD_TO_ORDER_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_REMOVE_FROM_ORDER,
        handle_remove_from_order,
        schema=REMOVE_FROM_ORDER_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_REOPEN_ORDER,
        handle_reopen_order,
        schema=ORDER_ID_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_REVERT_ORDER,
        handle_revert_order,
        schema=ORDER_ID_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_LOOKUP_BARCODE,
        handle_lookup_barcode,
        schema=LOOKUP_BARCODE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_FREQUENT_ITEMS,
        handle_get_frequent_items,
        schema=GET_FREQUENT_ITEMS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_PRICE_HISTORY,
        handle_get_price_history,
        schema=PRICE_HISTORY_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SYNC_HISTORY,
        handle_sync_history,
        schema=SYNC_HISTORY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


async def async_unload_services(hass: HomeAssistant) -> None:
    """Unload services."""
    for service in (
        SERVICE_SEARCH_PRODUCTS,
        SERVICE_ADD_TO_ORDER,
        SERVICE_REMOVE_FROM_ORDER,
        SERVICE_REOPEN_ORDER,
        SERVICE_REVERT_ORDER,
        SERVICE_LOOKUP_BARCODE,
        SERVICE_GET_FREQUENT_ITEMS,
        SERVICE_SYNC_HISTORY,
        SERVICE_GET_PRICE_HISTORY,
    ):
        hass.services.async_remove(DOMAIN, service)
