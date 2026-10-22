"""Data update coordinator for Albert Heijn."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    AlbertHeijnApi,
    AlbertHeijnApiError,
    AlbertHeijnAuthError,
    AlbertHeijnData,
)
from .suggest import week_start
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

if TYPE_CHECKING:
    from .history import OrderHistory

_LOGGER = logging.getLogger(__name__)


class AlbertHeijnCoordinator(DataUpdateCoordinator[AlbertHeijnData]):
    """Coordinator to manage fetching Albert Heijn data."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.config_entry = entry
        session = async_get_clientsession(hass)
        self.api = AlbertHeijnApi(
            session=session,
            access_token=entry.data[CONF_ACCESS_TOKEN],
            refresh_token=entry.data[CONF_REFRESH_TOKEN],
        )
        self.history: OrderHistory | None = None
        self._open_ids: set[int] | None = None

    async def _async_update_data(self) -> AlbertHeijnData:
        """Fetch data from the API."""
        try:
            return await self._fetch_data()
        except AlbertHeijnAuthError as err:
            # Token refresh failed, trigger reauth flow
            raise ConfigEntryAuthFailed from err
        except AlbertHeijnApiError as err:
            raise UpdateFailed(
                f"Error communicating with Albert Heijn API: {err}"
            ) from err

    async def _fetch_data(self) -> AlbertHeijnData:
        """Fetch all data from the API."""
        fulfillments = await self.api.get_fulfillments()

        data = AlbertHeijnData(fulfillments=fulfillments)

        # Find the next upcoming order
        if fulfillments:
            sorted_fulfillments = sorted(
                fulfillments, key=lambda f: f.delivery_slot.date
            )
            data.next_order = sorted_fulfillments[0]

            # Details for all orders (card badges/deadlines); for the next order also its products, from the same request
            for f in sorted_fulfillments:
                try:
                    if f is data.next_order:
                        details, items = await self.api.get_order_with_items(f.order_id)
                        data.next_order_items = items if details.total_items else []
                    else:
                        details = await self.api.get_order_details(f.order_id)
                    data.all_order_details[f.order_id] = details
                except AlbertHeijnApiError as err:
                    _LOGGER.debug(
                        "Could not fetch details for order %d: %s",
                        f.order_id, err,
                    )

            # Set the next order's details for backward compat
            next_order = data.next_order
            data.order_details = data.all_order_details.get(next_order.order_id)

            # Get minimum value limits if order is modifiable
            if next_order.modifiable:
                try:
                    data.order_value_limits = (
                        await self.api.get_order_value_limits(
                            next_order.order_id
                        )
                    )
                except AlbertHeijnApiError as err:
                    _LOGGER.debug(
                        "Could not fetch order value limits: %s", err
                    )


        if self.history:
            self.history.next_delivery = data.next_order.delivery_slot.date if data.next_order else None

        # An open order disappeared (delivered): pick it up in the order history
        open_ids = {f.order_id for f in fulfillments}
        if self._open_ids is not None and self._open_ids - open_ids and self.history:
            self.config_entry.async_create_background_task(
                self.hass, self.async_sync_history(heavy=False), "albert_heijn_history_sync"
            )
        self._open_ids = open_ids

        # Persist refreshed tokens
        self._update_tokens()

        return data

    async def async_put_order_items(
        self, items: list[dict], order_id: int | None = None
    ) -> dict:
        """Put products in an order (quantity 0 removes them).

        Without order_id: the active order, else the next order that can still be changed.
        A submitted order is reopened first; it then has to be confirmed again in the AH app.
        """
        reopened = False
        if not order_id:
            summary = await self.api.get_active_order_summary()
            order_id = summary.get("id") if summary else None
        if not order_id:
            next_order = self.data.next_order if self.data else None
            if not next_order or not next_order.modifiable:
                raise AlbertHeijnApiError("There is no order that can still be changed")
            order_id = next_order.order_id
        try:
            await self.api.add_to_order(items, order_id=order_id)
        except AlbertHeijnAuthError:
            raise
        except AlbertHeijnApiError as err:
            if not err.status or not 400 <= err.status < 500:
                raise
            # submitted order: reopen it and try once more
            await self.api.reopen_order(order_id)
            reopened = True
            await self.api.add_to_order(items, order_id=order_id)
        self._update_tokens()
        await self.async_request_refresh()
        return {"order_id": order_id, "reopened": reopened}

    async def async_fill_list(self, **options) -> dict:
        """Put this week's suggestions on the AH shopping list (see history.async_fill_list)."""
        if not self.history:
            raise AlbertHeijnApiError("Order history not available")
        data = self.data
        in_order = {i.product_id for i in data.next_order_items} if data else set()
        # suggestions are per delivery; without an order: per bonus week
        week = (data.next_order.delivery_slot.date if data and data.next_order else "") or week_start(date.today()).isoformat()
        result = await self.history.async_fill_list(data.fulfillments if data else [], in_order, week, **options)
        self._update_tokens()
        self.async_update_listeners()
        return result

    async def async_add_to_list(self, product_id: int, title: str, quantity: int = 1) -> int | None:
        """Put a product on the AH shopping list. Returns the quantity already on the list (then nothing changes)."""
        on_list = {i["product_id"]: i for i in await self.api.get_shopping_list() if i.get("product_id")}
        if (item := on_list.get(product_id)) and not item["checked"]:
            return item["quantity"]
        await self.api.set_list_items([{"product_id": product_id, "title": title, "quantity": quantity}])
        self._update_tokens()
        return None

    async def async_set_list_quantity(self, product_id: int, quantity: int, title: str = "") -> None:
        """Set the quantity of a product on the AH shopping list (0 removes it)."""
        await self.api.set_list_items([{"product_id": product_id, "title": title, "quantity": quantity}])
        self._update_tokens()

    async def async_cleanup_suggestions(self, days: int = 14, dry_run: bool = False, snooze_days: int = 7) -> dict:
        """Remove suggestions that were left on the list (see history.async_cleanup_suggestions)."""
        if not self.history:
            raise AlbertHeijnApiError("Order history not available")
        in_order = {i.product_id for i in self.data.next_order_items} if self.data else set()
        result = await self.history.async_cleanup_suggestions(in_order, days, dry_run, snooze_days)
        self._update_tokens()
        return result

    async def async_sync_history(
        self, refresh_catalog: bool = False, heavy: bool = True, max_products: int | None = None, retry_not_found: bool = False
    ) -> bool:
        """Update the stored order history and notify the sensors."""
        if not self.history:
            return False
        open_orders = self.data.fulfillments if self.data else []
        try:
            changed = await self.history.async_sync(open_orders, refresh_catalog, heavy=heavy, max_products=max_products, retry_not_found=retry_not_found)
        except AlbertHeijnAuthError:
            _LOGGER.warning("Albert Heijn: order history sync needs a new login")
            return False
        self._update_tokens()
        self.async_update_listeners()
        return changed

    def _update_tokens(self) -> None:
        """Update stored tokens if they changed."""
        new_data = {
            **self.config_entry.data,
            CONF_ACCESS_TOKEN: self.api.access_token,
            CONF_REFRESH_TOKEN: self.api.refresh_token,
        }
        if new_data != self.config_entry.data:
            self.hass.config_entries.async_update_entry(
                self.config_entry, data=new_data
            )
