# Albert Heijn Integration for Home Assistant

[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/)

A Home Assistant custom integration for Albert Heijn grocery delivery, providing order tracking sensors and voice control through Assist.

## This fork (CBato, 0.16.0)

Fork of [dpvdberg/ha-albertheijn](https://github.com/dpvdberg/ha-albertheijn) with:

- **Order history in SQLite** — `/config/albert_heijn.db` (part of HA backups) with the tables `orders`,
  `order_items` (quantity, price, price without bonus, bonus, unit price), `products` (incl. AH's "Eerder gekocht"
  as `in_catalog`) and `prices` (weekly). The history keeps growing even when AH stops returning old orders.
  Synced shortly after start, once a day and after each delivery; catalog and prices once a week.
  Cancelled orders are skipped (and removed if they were stored before). Older orders come from the "Mijn bestellingen"
  list (`/order/v1/summaries`: the last 25 orders, as in the AH app, ~4 months; AH ignores paging), which goes back
  further than `orderFulfillments` (max 10). Only state DELIVERED counts. The database keeps everything from then on.
  A JSON history from 0.6/0.7 is imported once. `order_items.effective` is what was really paid per item: AH lists
  multi-buy offers (3 voor 5,00, 1+1 gratis, 2e halve prijs) at the normal price and settles them on the order total,
  for complete groups and across products with the same offer (mix & match).
- **`sensor.albert_heijn_vaak_besteld`** — number of products in the history, with the top 25
  (count, last ordered, average interval) as attribute.
- **`albert_heijn.get_frequent_items`** — up to 1000 products, most often and recently ordered first,
  followed by the rest of "Eerder gekocht".
- **`albert_heijn.fill_list`** — puts this week's suggestions on the AH shopping list (= the basket in the AH app)
  with the usual quantity: products due according to the order rhythm, and regularly bought products in a bonus
  that is a real discount (the price before the bonus is compared with the normal price of the last 90 days).
  `sensor.albert_heijn_voorstellen` lists what was added this week and why. `dry_run: true` only shows them.
- **Bonus mechanisms understood** — 1+1 / 2+1 gratis, 2e halve prijs, 2e gratis, "2 voor 5,00", % and € korting give an
  effective price per item. The bonus check compares that with what you really paid per item on average (the same
  comparison as a product table in a dashboard); without purchase history with the normal price of the last 90 days. `fill_list` rounds quantities up to the bonus and stocks up on a real saving of 20%+
  (about 2 weeks, fresh products 1 week); the reason says how many and why.
- **Stock-based rhythm** — a product is due when the last purchase (quantity / use per week) runs out before the delivery
  after the next one, so a bonus stock-up is taken into account.
- **Till receipts** (in-store purchases with the bonus card) are imported too (tables `receipts`, `receipt_items`,
  `pos_map`): till product ids are converted to webshop products, so store purchases count in the rhythm.
  A receipt on the day of a delivery is not counted twice. The first sync imports up to 400 receipts.
- **Pause-proof rhythm** — after a pause (nothing bought for more than twice the usual interval, e.g. an AH outage)
  "due" is judged as if one usual interval has passed. Rhythm (`share`, `every_weeks`, `per_week`) is counted over the
  weeks we have data for (a delivery or a receipt), so gaps in the data don't make products look rarer than they are.
  The rhythm is taken from the period with known deliveries (receipts alone miss the deliveries) and measured over the
  active period (first to last purchase). Products not bought for 3 usual intervals (6 weeks at least) are `stopped`
  and never suggested.
- **`albert_heijn.graphql_query`** — read-only GraphQL query, to explore the AH API (introspection is disabled at AH).
- **`albert_heijn.probe_order_history`** — tries pagination/status/REST variants to find older delivered orders.
- **`albert_heijn.get_product_history`** — one product: every purchase (delivery and store, with price per item),
  weekly prices, rhythm and totals (for a product card in a dashboard).
- **`albert_heijn.get_insights`** — spending this month and last month, per week (delivery / store), biggest items and
  categories (90 days), bonus savings and price movers (weekly prices, or paid list prices until there are enough).
- **`albert_heijn.get_products`** — every product ever bought: last bought, times, average paid, current price and bonus,
  difference current vs average paid (for a product table with category filter).
- **`albert_heijn.sync_history`** — sync now; reports whether AH supports closed orders.
- **`albert_heijn.lookup_barcode`** — barcode (EAN) → AH product.
- **`albert_heijn.add_to_order`** — also works for a submitted (planned) order: it is reopened first and has to be
  confirmed again in the AH app. Accepts several `products` at once and an optional `order_id`.
- **Next order items** sensor now lists the products in the next order (`products` attribute).
- **Order rhythm** per product in `get_frequent_items`: `usual_qty` (usually per order), `every_weeks`,
  `share` (part of the orders since it was first ordered), `per_week` (average quantity per week) and `due`.
  The sensor lists what is due again (`due` attribute).
- **Weekly price check** of all products you buy or bought before (price, price without bonus, bonus, unit price), and the
  price paid per order; `albert_heijn.get_price_history` returns them per product. Basis for a price watch and
  for checking whether a bonus is a real discount.

## Features

### Sensors
- **Next delivery date** — when your next order arrives
- **Next delivery time** — delivery time window (e.g., "18:00 - 20:00")
- **Next order total** — total price of the upcoming order
- **Next order status** — current status description
- **Next order edit deadline** — the last moment you can modify your order (timestamp sensor, great for automations!)
- **Next order modifiable** — whether the order can still be changed
- **Next order items** — number of products in the order
- **Minimum order value** — subscription minimum (with `remaining_to_minimum` attribute)
- **Order submittable** — whether the order meets the minimum value
- **Open orders** — total number of pending deliveries

### Services
- `albert_heijn.search_products` — search for products
- `albert_heijn.add_to_order` — add a product by ID
- `albert_heijn.add_product_by_name` — search and add the first match
- `albert_heijn.reopen_order` — reopen a submitted order for editing
- `albert_heijn.revert_order` — revert a reopened order

### Voice Control (Assist)
Works with Home Assistant's built-in Assist in both Dutch and English:

**Dutch:**
- "Voeg melk toe aan mijn bestelling"
- "Bestel 2 brood"
- "Wanneer wordt mijn bestelling bezorgd?"

**English:**
- "Add milk to my order"
- "Order 2 bread"
- "When will my groceries be delivered?"

## Installation

### HACS (Recommended)
1. Add this repository as a custom repository in HACS
2. Search for "Albert Heijn" and install
3. Restart Home Assistant

### Manual
1. Copy the `custom_components/albert_heijn` folder to your HA `custom_components/` directory
2. Restart Home Assistant

## Configuration

### Setup in Home Assistant
1. Go to **Settings → Devices & Services → Add Integration**
2. Search for "Albert Heijn"
3. Enter your Albert Heijn email and password
4. The integration will log in, obtain tokens, and set up sensors

Tokens are automatically stored, refreshed, and re-created using your stored credentials if they expire.

### Requirements

The integration uses [`curl_cffi`](https://github.com/yifeikong/curl_cffi) to impersonate a Chrome browser during login (helps bypass TLS fingerprinting and reduces captcha triggers). This is installed automatically as a dependency.

If the automated login is blocked by hCaptcha, you can alternatively obtain tokens via the [appie CLI tool](appie-go/) and store them manually.

## Automation Examples

### Notify before order deadline
```yaml
automation:
  - alias: "AH Order Deadline Reminder"
    trigger:
      - platform: template
        value_template: >
          {{ (as_timestamp(states('sensor.albert_heijn_next_order_edit_deadline')) - as_timestamp(now())) < 7200 }}
    condition:
      - condition: state
        entity_id: sensor.albert_heijn_next_order_modifiable
        state: "Yes"
    action:
      - service: notify.mobile_app
        data:
          title: "Albert Heijn bestelling"
          message: >
            Je bestelling sluit over 2 uur! 
            Totaal: €{{ states('sensor.albert_heijn_next_order_total') }}
            ({{ state_attr('sensor.albert_heijn_minimum_order_value', 'remaining_to_minimum') | round(2) }}€ onder minimum)
```

### Notify when minimum not reached
```yaml
automation:
  - alias: "AH Minimum Order Warning"
    trigger:
      - platform: state
        entity_id: sensor.albert_heijn_order_submittable
        to: "No"
    action:
      - service: notify.mobile_app
        data:
          title: "Albert Heijn"
          message: >
            Je bestelling is onder het minimum van €{{ states('sensor.albert_heijn_minimum_order_value') }}.
            Nog €{{ state_attr('sensor.albert_heijn_minimum_order_value', 'remaining_to_minimum') | round(2) }} nodig.
```

## API Reference

This integration is built from the Albert Heijn mobile API, documented in `appie-go/doc/albertheijn_api.md`. The Go reference implementation in `appie-go/` shows the full API capabilities.

## License

MIT
