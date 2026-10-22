# Albert Heijn for Home Assistant (CBato fork)

[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/)

A Home Assistant integration for Albert Heijn grocery delivery. It shows your upcoming deliveries and can change
them, and it keeps a local history of everything you buy. That history drives the suggestions for your shopping
list: what is due again according to how often you buy it, and what is in a bonus that is really cheaper than
what you normally pay.

This is a fork of [dpvdberg/ha-albertheijn](https://github.com/dpvdberg/ha-albertheijn). The order sensors, the
order services, the login and Assist come from there. The order history, the suggestions, the bonus check and the
analysis services were added in this fork.

> Albert Heijn has no public API. This integration uses the API of the AH app, which can change without notice.

## Contents

- [What you get](#what-you-get)
- [Installation](#installation)
- [How it works](#how-it-works)
- [Suggestions](#suggestions)
- [Sensors](#sensors)
- [Services](#services)
- [Example automations](#example-automations)
- [Database](#database)
- [Development](#development)
- [Limits and known issues](#limits-and-known-issues)

## What you get

| Area | What |
|---|---|
| Upcoming delivery | Date, time window, total, status, edit deadline, products in the order, minimum order value |
| Change orders | Add or remove products, reopen and revert a submitted order |
| Purchase history | Delivered orders and till receipts (in-store purchases with the bonus card) in a local SQLite database that keeps growing |
| Rhythm per product | How often, how many, use per week, and whether it is due again |
| Prices | Weekly price of every product you buy, and what you really paid (multi-buy offers settled) |
| Bonus | This week's and next week's bonus on what you buy, judged against what you paid on average |
| Suggestions | Due products and good bonus deals put on the AH shopping list automatically, with the reason why |
| Analysis | Spending per month and week, biggest items, categories, bonus savings, price movers, a product overview |
| Barcode | Barcode (EAN) to AH product, for scanning what has run out |
| Other stores | The same products at Picnic, matched on barcode (EAN), and the shopping list compared |
| Assist | "Zet melk op de lijst" (asks which one when in doubt), order status and changing the order by voice |

The AH shopping list itself (add, tick off, the basket in the AH app) is not part of this integration. Use
[Appie-Dashboard](https://github.com/eejeha/Appie-Dashboard) for that. It exposes the list as a todo entity, and
`fill_list` writes to the same AH list.

## Installation

### HACS

1. HACS → Integrations → menu → **Custom repositories** → add `https://github.com/CBato/ha-albertheijn`
   (type: Integration).
2. Install **Albert Heijn** and restart Home Assistant.
3. To update later: HACS → Albert Heijn → **Update information**, then **Redownload**, then restart.

If you used dpvdberg's version before, remove it in HACS first. The domain is the same (`albert_heijn`), so your
config entry and entities are kept.

### Log in

1. **Settings → Devices & services → Add integration → Albert Heijn**.
2. Click submit. A login page opens in your browser through a small proxy that Home Assistant starts. Log in with
   your AH account there.
3. The integration receives the tokens and refreshes them by itself. When that is no longer possible, Home Assistant
   asks you to log in again (reauth).

The proxy runs on a random port of your Home Assistant host, and the browser has to be able to reach it. On your
home network it works with the local IP address.

### Other stores: Picnic (optional)

Set up the official **Picnic** integration of Home Assistant. This integration then reuses its login: every week it
looks up the products you buy at Picnic by barcode (EAN). There is nothing to configure here, and without the Picnic
integration this step is skipped.

Each product you buy is matched in this order, and every match gets a kind and a score:

| Kind | How | Score |
|---|---|---|
| `confirmed` | You confirmed this Picnic product (`confirm_match`) | 100 |
| `ean` | Same barcode | 100 |
| `same` | Found by name (without size and store brand), same size | 60–95 |
| `size` | Same product, other size (Calvé 450 g vs 650 g): compared per kg / litre and recalculated to your size | 60–95 |
| `similar` | Comparable product, size not comparable (pieces vs kg, or unknown) | 60–95 |

A branded product only matches the same brand; a store brand (AH) can match another store brand (Picnic).
Other flavours or variants (paprika vs naturel, regular vs zero) score below 60 and are not shown. A barcode often
differs per pack size and sometimes per country label (a German Nutella jar), which is why the name match exists.

`compare_list` totals: **sure** (barcode or confirmed) and **likely** (all matches, other sizes recalculated).
Confirm a likely match to make it sure; reject it and the next best is looked up right away (a rejected product is
never matched again).

### Assist sentences (optional)

HACS does not install the sentences. Copy `custom_sentences/nl/albert_heijn.yaml` (and/or `en`) to
`/config/custom_sentences/<language>/` and restart.

Dutch, shopping list (fork):

| You say | What happens |
|---|---|
| "zet melk op de lijst", "zet 2 melk op de boodschappenlijst", "we hebben geen eieren meer" | Looks in what you bought before. One clear match (or one you buy much more often than the others): it goes on the AH shopping list. Otherwise Assist names up to four products with a number |
| "nummer 2", "de tweede" | Picks from that list (within 3 minutes) |

Products you never bought are searched at AH, and the top three are offered as a choice. A product that is already
on the list is not added twice.

Orders (from the original integration): "wat zit er in mijn bestelling", "wanneer wordt mijn bestelling bezorgd",
"voeg product code 553353 toe aan mijn bestelling".

## How it works

### What is fetched when

| What | When | Incremental |
|---|---|---|
| Upcoming orders, details, products in the next order | Every 5 minutes | Always current |
| Delivered orders | Every night at 04:15, after a restart, and right after a delivery | Only orders not yet stored |
| Till receipts | Same | Stops at the first receipt it already has |
| AH's "Eerder gekocht" list | Once a week | Whole list (it changes) |
| Prices of everything you buy | Once a week (and on Monday before `fill_list`) | Whole list (that is the measurement) |
| Bonus this week and next week | Once a day (the first sync of the day) | Per week |
| EAN (barcode) of the products you buy | Every sync, for products without one | Each product at most once a week while AH doesn't give one |
| Prices at Picnic (with the Picnic integration) | Weekly per product | Matched: only the price of the matched article (1 request). New: barcode, else name. Not found: again after 4 weeks |
| Missing product details (title, category, image) | Every sync, for products that lack them | Each product at most once a week; products AH no longer sells online show the name from the till receipt |

After a restart and after a delivery only a light sync runs (orders, receipts, bonus), so Home Assistant stays
responsive while it is busy. Catalog, prices, product details, EAN codes and other stores run at night (or with
`sync_history`), at most 150 products per step per run: the first big round is spread over a few nights. There is
one sync at a time. Each step that fails is skipped and reported in `last_error` on the "Vaak besteld"
sensor; the rest of the sync continues.

### Sources and how far back

- **Deliveries:** AH returns at most the last 25 orders ("Mijn bestellingen", about 4 months), and paging is
  ignored. Older deliveries cannot be fetched. Everything fetched stays in the database, so the history grows
  from the first install. Only state `DELIVERED` counts, and cancelled orders are removed.
- **Till receipts:** go back much further (two years in practice). The first sync imports at most 400. Till product
  numbers are converted to webshop products. A receipt on the day of a delivery is not counted twice.
- **What you really paid:** AH lists multi-buy offers (1+1 gratis, 2e halve prijs, 3 voor 5,00) at the normal price
  per item and settles them on the order total, only for complete groups and across products with the same offer.
  The integration does the same per order (`order_items.effective`).

## Suggestions

### When a product is due

Per product the integration computes how many you usually buy (`usual_qty`), every how many weeks (`every_weeks`),
and how much you use per week (`per_week`). Only weeks with data count (a delivery or a receipt), so gaps don't make
a product look rarer than it is. The rhythm comes from the period with known deliveries and the active period
(first to last purchase).

A product is **due** when the last purchase (quantity ÷ use per week) runs out before the delivery after the next
one. Stocking up in a bonus therefore pushes "due" back by itself.

- **Pause:** if nothing was bought for more than twice the usual interval (an outage, a holiday), "due" is judged
  as if one usual interval has passed.
- **Stopped:** not bought for 3 usual intervals (at least 6 weeks) means `stopped`, and the product is never
  suggested.

### Is the bonus a real deal

The offer is converted to a price per item (1+1 gratis at €2,00 is €1,00). That price is compared with **what you
really paid per item on average**:

| Saving | Status |
|---|---|
| 5% or more cheaper | `echt` (real) |
| Less | `let_op` (hardly any saving or more expensive for you) |
| No purchase history yet | Compared with the normal price of the last 90 days (from 3 weekly prices); a normal price raised just before the bonus is `let_op` |

### What `fill_list` puts on the list

1. **Rhythm:** due, bought at least `min_count` times, and in at least `min_share` of the weeks. Quantity: the
   usual quantity.
2. **Bonus stock-up:** bought regularly and at least 20% real saving. Enough for about 2 weeks (fresh products:
   1 week).

Quantities are rounded up to the offer (1+1 gratis: even numbers). Products already on the list, in the order,
suggested earlier for the same delivery, or snoozed are skipped. Each product gets reasons in plain Dutch
("elke week · meestal 2", "1+1 gratis → 4 genomen …"). They are stored and shown on the "Voorstellen" sensor.

### Snoozing and cleanup

- `snooze_product`: don't suggest a product for a number of days, never again (`-1`), or again (`0`).
- `cleanup_suggestions`: removes an automatically added product that was left untouched for 14 days. Untouched
  means: same quantity, not ticked, not in the order, and not bought since. It is then snoozed for a week. If it
  is still due after that, it is suggested again. Products you added or changed yourself are never removed.

## Sensors

Entity IDs can get your area as prefix, e.g. `sensor.keuken_albert_heijn_voorstellen`.

| Sensor | State | Attributes |
|---|---|---|
| Next delivery date | Date | `date_display`, `shopping_type` |
| Next delivery time | Time window | `start_time`, `end_time` |
| Next order total | € | |
| Next order status | Status text | `order_id`, `status`, `transaction_completed` |
| Next order edit deadline | Timestamp | `reopenable`, `order_method`, `products_closing_time` |
| Next order modifiable | Yes / No | |
| Next order items | Number of products | `order_id`, `products` (id, title, quantity) |
| Minimum order value | € | `current_total`, `remaining_to_minimum` |
| Order submittable | Yes / No | |
| Open orders | Number | `orders` (per order: date, time, total, state, closing time) |
| Order spending | € of the next non-empty order | `total_upcoming`, `order_count` |
| Vaak besteld | Products in the history | Sync summary, `due`, `top` (25), `last_error` |
| Voorstellen | Suggestions for the current delivery | `week`, `items` (with `reasons`) |
| Bonus volgende week | Good deals next week | `available`, `week_start`, `week_end`, `items` |

Large attributes (`products`, `top`, `due`, `items`) are not recorded in the history database.

## Services

All services are in the `albert_heijn` domain. Services marked with *response* return data (use
`response_variable` in a script, or `return_response` over the websocket).

### Orders

| Service | Fields | Does |
|---|---|---|
| `search_products` *response* | `query`, `limit` (10) | Search the AH assortment |
| `add_to_order` | `product_id` + `quantity`, or `products: [{product_id, quantity}]`; `order_id` | Put products in the active order, or else the next order that can be changed. A submitted order is reopened first; confirm it again in the AH app |
| `remove_from_order` | `product_id`, `order_id` | Remove a product |
| `reopen_order` / `revert_order` | `order_id` | Reopen a submitted order / undo the reopen |
| `lookup_barcode` *response* | `barcode` | EAN-8/13, UPC-A or GTIN-14 to AH product (empty if AH doesn't sell it) |

### History and suggestions

| Service | Fields | Does |
|---|---|---|
| `sync_history` *response* | `refresh_catalog` (false), `max_products` (150, up to 2000) | Sync now, incl. the heavy part; `max_products: 2000` does everything at once instead of 150 per night |
| `get_frequent_items` *response* | `limit` (100, max 1000), `include_catalog` (true) | Products by how often and recently bought, with rhythm and `due`; then the rest of "Eerder gekocht" |
| `fill_list` *response* | `dry_run`, `min_count` (3), `min_share` (0.3), `max_items` (25) | Put the suggestions on the AH shopping list; `dry_run` only returns them |
| `cleanup_suggestions` *response* | `days` (14), `snooze_days` (7), `dry_run` | Remove suggestions that were left untouched (see above) |
| `snooze_product` *response* | `product_id`, `days` (7; -1 never, 0 again) | Don't suggest a product for a while |
| `set_list_quantity` | `product_id`, `quantity` (0 removes), `title` | Set how many of a product are on the AH shopping list |
| `compare_list` *response* | | The shopping list with the AH price and the Picnic price (match kind, score, recalculated size); totals sure and likely |
| `search_store_products` *response* | `product_id`, `query` (optional) | Search Picnic for one of your products; results with match score, best first |
| `confirm_match` *response* | `product_id`, `article_id`, `confirmed` | Confirm or reject the Picnic product matched to one of yours; returns what is matched now |
| `get_bonus` *response* | `week` (`current` / `next`) | Bonus on what you buy, with the price per item, your average and the saving |

### Analysis

| Service | Fields | Does |
|---|---|---|
| `get_products` *response* | | Every product ever bought: last bought, times, average paid, current price and bonus, difference with your average |
| `get_product_history` *response* | `product_id` | One product: every purchase with price, weekly prices, rhythm, totals, snooze |
| `get_insights` *response* | `weeks` (12) | This and last month, per week (delivery / store), biggest items and categories (90 days), bonus savings, price movers |
| `graphql_query` *response* | `query`, `variables` | Read-only GraphQL query on the AH API (no mutations); for exploring |

## Example automations

A complete setup (fill on Monday, check before the deadline with a notification button, tick off what is
already in the order, clean up after the deadline, an on/off switch) is in the
[Starnheim config](https://github.com/CBato/home-assistant-config/blob/main/packages/boodschappen.yaml).
The core:

```yaml
automation:
  - alias: "Boodschappen: lijst vullen"
    triggers:
      - trigger: time
        at: "08:00:00"
    conditions:
      - condition: time
        weekday: mon            # the bonus week starts on Monday
    actions:
      - action: albert_heijn.cleanup_suggestions
        response_variable: opruim
      - action: albert_heijn.fill_list
        data:
          min_count: 3
          min_share: 0.3
          max_items: 25
        response_variable: vulling
      - action: notify.mobile_app_phone
        data:
          title: "Boodschappen"
          message: >
            {{ vulling.added | count }} producten op de lijst gezet:
            {{ vulling.added | map(attribute='title') | join(', ') }}
```

A reminder before the edit deadline:

```yaml
triggers:
  - trigger: template
    value_template: >
      {% set d = as_timestamp(states('sensor.albert_heijn_next_order_edit_deadline'), 0) %}
      {{ d > 0 and d - 3 * 3600 <= as_timestamp(now()) < d }}
conditions:
  - condition: state
    entity_id: sensor.albert_heijn_next_order_modifiable
    state: "Yes"
```

## Database

`/config/albert_heijn.db` (SQLite, part of Home Assistant backups). You can browse it with the SQLite Web add-on.
New columns are added automatically on start. A JSON history from fork 0.6/0.7 is imported once.

| Table | Contents |
|---|---|
| `orders` | Order id, delivery date, status (`open` snapshot / `delivered`) |
| `order_items` | Per order and product: quantity, price, price before bonus, bonus, unit price, `effective` (really paid) |
| `products` | Title, brand, size, category, image, `in_catalog` (in "Eerder gekocht"), `gtin` (EAN), lookup timestamps |
| `store_prices` | Weekly price at another store per product (`picnic`): article, title, price, price without offer, `match`, `score`; no article = not found |
| `store_matches` | Matches you confirmed or rejected, per product and store article |
| `prices` | Weekly price check: price, price before bonus, bonus, unit price, `effective` (per item at the offer) |
| `receipts`, `receipt_items`, `pos_map` | Till receipts, their lines, till product number to webshop product (-1 = not online) |
| `bonus_weeks`, `bonus_offers` | Bonus weeks and their offers (previously bought + personal) |
| `suggestions` | What `fill_list` added, per delivery, with reasons, `added_at` and `done_at` (handled by the cleanup) |
| `snoozes` | Product, not suggested until (`9999-12-31` = never) |
| `meta` | Sync timestamps |

## Development

```
custom_components/albert_heijn/
  __init__.py      setup, nightly sync schedule, services and intents
  api.py           AH API client (REST + GraphQL), data classes
  auth.py          browser login proxy, token exchange and refresh
  config_flow.py   login and reauth
  coordinator.py   upcoming orders every 5 minutes; entry point for history, fill and cleanup
  history.py       sync of history, receipts, catalog, prices and bonus; rhythm (compute_frequent); fill and cleanup
  compare.py       other stores (Picnic through the HA Picnic integration's login)
  suggest.py       pure logic: offers, effective price, bonus check, suggestions (no I/O)
  db.py            SQLite schema and queries (blocking, always run in the executor)
  sensor.py        sensors
  services.py      services (schemas in services.yaml)
  intents.py       Assist intents, incl. "zet ... op de lijst" (sentences in /custom_sentences)
  www/             Lovelace card albert-heijn-orders-card, copied to /config/www/community/albert_heijn
tests/             offline tests (no Home Assistant needed)
scripts/           live scripts against the real AH API (login, fetch orders)
```

**Offline tests.** They stub the few Home Assistant names the code uses, and use a fake AH API and a temporary
database:

```
python tests/run_all.py
```

**Live scripts.** Run them from the repository root. `python scripts/live_login.py` logs in through the browser and
stores tokens in `.tokens.json` (git-ignored). `python scripts/live_api.py` then fetches your orders.

**Releases.** Bump `version` in `manifest.json`. HACS shows the version from the default branch.

### AH endpoints used

| Purpose | Endpoint |
|---|---|
| Open / closed orders | GraphQL `orderFulfillments(status: OPEN / CLOSED)` (max 10) |
| Order list | `GET /mobile-services/order/v1/summaries?sortBy=DEFAULT` (last 25) |
| Order contents | `GET /mobile-services/order/v1/{id}/details-grouped-by-taxonomy` |
| Change order | `PUT /mobile-services/order/v1/items` (header `Appie-Current-Order-Id`); GraphQL `orderReopen` / `orderRevert` |
| Shopping list | `GET` / `PATCH /mobile-services/shoppinglist/v2/items` (upsert on productId, quantity 0 removes) |
| Prices | `GET /mobile-services/product/search/v2/products?ids=…&sortOn=INPUT_PRODUCT_IDS` |
| Search / barcode | `GET /mobile-services/product/search/v2?query=…` / `…/search/v1/gtin/{code}` |
| Eerder gekocht | GraphQL `productSearch(previouslyBought: true)` |
| Till receipts | GraphQL `posReceiptsPage`, `posReceiptDetails`, `productConvertId` |
| Bonus | `/mobile-services/bonuspage/v3/metadata`, `…/v2/section/previously-bought`, `…/v1/personal` |
| EAN of a product | `GET /mobile-services/product/detail/v4/fir/{id}` (every field named gtin/ean is taken) |
| Picnic | `python-picnic-api2` `get_article_by_gtin` (the library of the HA Picnic integration) |

GraphQL introspection is disabled at AH. `graphql_query` is meant for trying things out.

## Limits and known issues

- Delivery history only goes back 25 orders. The database grows from the first install.
- Next week's bonus appears a few days before the week starts. Until then `Bonus volgende week` is 0 and
  `available` is false.
- Till receipt lines are amounts before discount. Store discounts are separate lines and are not attributed to
  products yet.
- After a submitted order is reopened by `add_to_order`, it has to be confirmed again in the AH app.
- One AH account per Home Assistant: services use the first configured account.

## Credits and license

Original integration by [@dpvdberg](https://github.com/dpvdberg). History, suggestions and analysis by
[@CBato](https://github.com/CBato). MIT license.
