from datetime import date, timedelta

import _harness  # noqa: F401 (sets up the Home Assistant stubs)

import importlib
history = importlib.import_module("albert_heijn.history")
today = date.today()
receipts = [today - timedelta(days=60 + 35 * i) for i in range(20)]       # a store visit every 5 weeks, 2 years back
deliveries = [today - timedelta(days=3 + 7 * i) for i in range(6)]        # last 6 weeks weekly deliveries
items = [{"date": d.isoformat(), "source": "winkel", "product_id": 1, "quantity": 1, "title": "Eierkoeken"} for i, d in enumerate(receipts) if i % 5 in (0, 2)]
items += [{"date": d.isoformat(), "source": "bezorging", "product_id": 1, "quantity": 1, "title": "Eierkoeken"} for i, d in enumerate(deliveries) if i in (0, 1, 3, 5)]
snap = {"order_days": [d.isoformat() for d in deliveries], "receipt_days": [d.isoformat() for d in receipts], "items": items, "catalog": [], "latest_prices": {}}
p = history.compute_frequent(snap, today, 7)[0]
print({k: p[k] for k in ("count", "share", "every_weeks", "per_week", "covers_days", "due", "rhythm_since")})
# rhythm from the delivery window (4 of the 6 deliveries), the count from all 2 years
assert p["count"] == 12 and p["every_weeks"] == 2 and 0.6 <= p["share"] <= 0.7 and p["stopped"] is False, p
print("SPARSE OK")
