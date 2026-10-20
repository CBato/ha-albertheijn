"""Intent handlers for Albert Heijn Assist integration."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent

from .api import AlbertHeijnApiError
from .const import DOMAIN
from .coordinator import AlbertHeijnCoordinator
from .suggest import match_products

_LOGGER = logging.getLogger(__name__)

INTENT_ORDER_STATUS = "AlbertHeijnOrderStatus"
INTENT_SEARCH_PRODUCTS = "AlbertHeijnSearchProducts"
INTENT_ADD_TO_ORDER = "AlbertHeijnAddToOrder"
INTENT_LIST_ORDER_ITEMS = "AlbertHeijnListOrderItems"
INTENT_REMOVE_FROM_ORDER = "AlbertHeijnRemoveFromOrder"
INTENT_ADD_TO_LIST = "AlbertHeijnAddToList"
INTENT_PICK_CHOICE = "AlbertHeijnPickChoice"

CHOICE_SECONDS = 180  # "nummer 2" works for 3 minutes after the question
_NUMBERS = {"een": 1, "twee": 2, "drie": 3, "vier": 4, "vijf": 5, "zes": 6, "zeven": 7, "acht": 8, "negen": 9, "tien": 10,
            "eerste": 1, "tweede": 2, "derde": 3, "vierde": 4}


async def async_setup_intents(hass: HomeAssistant) -> None:
    """Set up intent handlers for Albert Heijn."""
    intent.async_register(hass, OrderStatusIntentHandler())
    intent.async_register(hass, SearchProductsIntentHandler())
    intent.async_register(hass, AddToOrderIntentHandler())
    intent.async_register(hass, ListOrderItemsIntentHandler())
    intent.async_register(hass, RemoveFromOrderIntentHandler())
    intent.async_register(hass, AddToListIntentHandler())
    intent.async_register(hass, PickChoiceIntentHandler())


def _get_coordinator(hass: HomeAssistant) -> AlbertHeijnCoordinator:
    """Get the first available coordinator."""
    if DOMAIN not in hass.data or not hass.data[DOMAIN]:
        raise intent.IntentHandleError("Albert Heijn integration not configured")
    return next(iter(hass.data[DOMAIN].values()))


class SearchProductsIntentHandler(intent.IntentHandler):
    """Search for products and return IDs + names for the AI to pick from."""

    intent_type = INTENT_SEARCH_PRODUCTS
    slot_schema = {
        "query": intent.non_empty_string,
    }

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        """Handle the intent."""
        slots = self.async_validate_slots(intent_obj.slots)
        query = slots["query"]["value"]

        coordinator = _get_coordinator(intent_obj.hass)

        try:
            products = await coordinator.api.search_products(query, 10)
        except AlbertHeijnApiError as err:
            raise intent.IntentHandleError(
                f"Kon niet zoeken naar '{query}': {err}"
            ) from err

        if not products:
            response = intent_obj.create_response()
            response.async_set_speech(f"Geen producten gevonden voor '{query}'.")
            return response

        lines = []
        for p in products:
            orderable = "✓" if p.is_orderable else "✗"
            price = f"€{p.price:.2f}" if p.price else "?"
            lines.append(f"- [{orderable}] {p.title} ({p.brand}, {p.unit_size}) {price} → id:{p.id}")

        response = intent_obj.create_response()
        response.async_set_speech(
            f"Zoekresultaten voor '{query}':\n" + "\n".join(lines)
        )
        return response


class AddToOrderIntentHandler(intent.IntentHandler):
    """Add a product to the order by product ID."""

    intent_type = INTENT_ADD_TO_ORDER
    slot_schema = {
        "product_id": intent.non_empty_string,
    }

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        """Handle the intent."""
        slots = self.async_validate_slots(intent_obj.slots)
        try:
            product_id = int(slots["product_id"]["value"])
        except (ValueError, TypeError):
            raise intent.IntentHandleError("Ongeldig product ID.")

        quantity_str = intent_obj.slots.get("quantity", {}).get("value", "1")
        try:
            quantity = int(quantity_str)
        except (ValueError, TypeError):
            quantity = 1

        coordinator = _get_coordinator(intent_obj.hass)

        try:
            await coordinator.async_put_order_items(
                [{"productId": product_id, "quantity": quantity}],
            )
        except AlbertHeijnApiError as err:
            raise intent.IntentHandleError(
                f"Kon product {product_id} niet toevoegen: {err}"
            ) from err

        response = intent_obj.create_response()
        response.async_set_speech(
            f"{quantity}x product {product_id} toegevoegd aan je bestelling."
        )
        return response


class ListOrderItemsIntentHandler(intent.IntentHandler):
    """List items currently in the order."""

    intent_type = INTENT_LIST_ORDER_ITEMS

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        """Handle the intent."""
        coordinator = _get_coordinator(intent_obj.hass)
        data = coordinator.data

        if not data or not data.next_order:
            response = intent_obj.create_response()
            response.async_set_speech("Geen actieve bestelling gevonden.")
            return response

        # Find the first modifiable order
        order_id = None
        for f in sorted(data.fulfillments, key=lambda f: f.delivery_slot.date):
            if f.modifiable:
                order_id = f.order_id
                break

        if not order_id:
            order_id = data.next_order.order_id

        try:
            items = await coordinator.api.get_order_items(order_id)
        except AlbertHeijnApiError as err:
            raise intent.IntentHandleError(
                f"Kon bestelling niet ophalen: {err}"
            ) from err

        if not items:
            response = intent_obj.create_response()
            response.async_set_speech("Je bestelling is leeg.")
            return response

        lines = []
        for item in items:
            price = f"€{item.price:.2f}" if item.price else "?"
            lines.append(f"- {item.quantity}x {item.title} ({item.unit_size}) {price} → id:{item.product_id}")

        response = intent_obj.create_response()
        response.async_set_speech(
            f"Er zitten {len(items)} producten in je bestelling:\n" + "\n".join(lines)
        )
        return response


class RemoveFromOrderIntentHandler(intent.IntentHandler):
    """Remove a product from the order by product ID."""

    intent_type = INTENT_REMOVE_FROM_ORDER
    slot_schema = {
        "product_id": intent.non_empty_string,
    }

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        """Handle the intent."""
        slots = self.async_validate_slots(intent_obj.slots)
        try:
            product_id = int(slots["product_id"]["value"])
        except (ValueError, TypeError):
            raise intent.IntentHandleError("Ongeldig product ID.")

        coordinator = _get_coordinator(intent_obj.hass)

        try:
            await coordinator.async_put_order_items(
                [{"productId": product_id, "quantity": 0}],
            )
        except AlbertHeijnApiError as err:
            raise intent.IntentHandleError(
                f"Kon product {product_id} niet verwijderen: {err}"
            ) from err

        response = intent_obj.create_response()
        response.async_set_speech(
            f"Product {product_id} is verwijderd uit je bestelling."
        )
        return response


class OrderStatusIntentHandler(intent.IntentHandler):
    """Handle order status queries via Assist."""

    intent_type = INTENT_ORDER_STATUS

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        """Handle the intent."""
        coordinator = _get_coordinator(intent_obj.hass)
        data = coordinator.data

        response = intent_obj.create_response()

        if not data or not data.next_order:
            response.async_set_speech(
                "Je hebt geen openstaande bestellingen bij Albert Heijn."
            )
            return response

        order = data.next_order
        parts = []
        parts.append(
            f"Je volgende bestelling wordt bezorgd op {order.delivery_slot.date_display} "
            f"tussen {order.delivery_slot.time_display}."
        )
        parts.append(f"Het totaalbedrag is €{order.total_price:.2f}.")

        if data.order_details and data.order_details.total_items:
            parts.append(
                f"Er zitten {data.order_details.total_items} producten in je bestelling."
            )

        if order.modifiable:
            parts.append("Je kunt nog producten toevoegen of wijzigen.")
        else:
            parts.append("De bestelling kan niet meer worden gewijzigd.")

        response.async_set_speech(" ".join(parts))
        return response


# ---------- shopping list by voice: "zet melk op de lijst" ----------
# Looks in what you bought before. A clear match goes on the AH shopping list straight away; otherwise Assist names
# up to four products with a number and you answer "nummer 2". Assist has no follow-up question for custom intents,
# so the choices are kept for CHOICE_SECONDS.

_choices: dict[str, Any] = {}


def _split_quantity(text: str) -> tuple[int, str]:
    """ "2 melk" / "twee pakken melk" -> (2, "pakken melk"); without a number (1, text)."""
    first, _, rest = text.strip().partition(" ")
    if rest and (first.isdigit() or first.lower() in _NUMBERS):
        return (int(first) if first.isdigit() else _NUMBERS[first.lower()]), rest
    return 1, text


def _label(p: dict[str, Any]) -> str:
    extra = [p["unit_size"]] if p.get("unit_size") else []
    if p.get("count"):
        extra.append(f"{p['count']} keer gekocht")
    title = p.get("title") or f"product {p['product_id']}"
    return f"{title} ({', '.join(extra)})" if extra else title


async def _put_on_list(coordinator: AlbertHeijnCoordinator, p: dict[str, Any], quantity: int) -> str:
    title = p.get("title") or f"product {p['product_id']}"
    try:
        already = await coordinator.async_add_to_list(int(p["product_id"]), title, quantity)
    except AlbertHeijnApiError as err:
        raise intent.IntentHandleError(f"Kon {title} niet op de lijst zetten: {err}") from err
    if already:
        return f"{title} staat al op de lijst ({already})."
    return f"{quantity} keer {title} op de lijst gezet." if quantity > 1 else f"{title} op de lijst gezet."


class AddToListIntentHandler(intent.IntentHandler):
    """Put a product on the AH shopping list by name; asks which one when it is not clear."""

    intent_type = INTENT_ADD_TO_LIST
    slot_schema = {"product": intent.non_empty_string}

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        """Handle the intent."""
        slots = self.async_validate_slots(intent_obj.slots)
        quantity, query = _split_quantity(slots["product"]["value"])
        coordinator = _get_coordinator(intent_obj.hass)
        response = intent_obj.create_response()

        known = coordinator.history.frequent(5000) if coordinator.history else []
        options, clear = match_products(known, query)
        where = "eerder gekocht"
        if not options:
            # never bought: AH's search results, always as a choice
            try:
                found = await coordinator.api.search_products(query, 3)
            except AlbertHeijnApiError as err:
                raise intent.IntentHandleError(f"Kon niet zoeken naar {query}: {err}") from err
            options = [{"product_id": p.id, "title": p.title, "unit_size": p.unit_size} for p in found if p.id]
            clear, where = False, "bij AH"
        if not options:
            response.async_set_speech(f"Ik kan {query} niet vinden bij Albert Heijn.")
            return response
        if clear:
            response.async_set_speech(await _put_on_list(coordinator, options[0], quantity))
            return response

        _choices.update({"at": time.monotonic(), "options": options, "quantity": quantity})
        listed = "; ".join(f"{n}: {_label(p)}" for n, p in enumerate(options, 1))
        say = " of ".join(f"nummer {n}" for n in range(1, len(options) + 1))
        response.async_set_speech(f"Welke {query} bedoel je ({where})? {listed}. Zeg {say}.")
        return response


class PickChoiceIntentHandler(intent.IntentHandler):
    """The answer to the question of AddToListIntentHandler: "nummer 2"."""

    intent_type = INTENT_PICK_CHOICE
    slot_schema = {"keuze": intent.non_empty_string}

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        """Handle the intent."""
        slots = self.async_validate_slots(intent_obj.slots)
        response = intent_obj.create_response()
        if not _choices or time.monotonic() - _choices["at"] > CHOICE_SECONDS:
            response.async_set_speech("Ik weet niet meer waar je uit kiest. Zeg opnieuw welk product op de lijst moet.")
            return response
        raw = str(slots["keuze"]["value"]).strip().lower()
        digits = re.sub(r"[^0-9]", "", raw)
        n = int(digits) if digits else _NUMBERS.get(raw, 0)
        options = _choices["options"]
        if not 1 <= n <= len(options):
            response.async_set_speech(f"Kies een nummer van 1 tot {len(options)}.")
            return response
        choice, quantity = options[n - 1], _choices["quantity"]
        _choices.clear()
        response.async_set_speech(await _put_on_list(_get_coordinator(intent_obj.hass), choice, quantity))
        return response
