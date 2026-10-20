from datetime import date, timedelta

import _harness  # noqa: F401 (sets up the Home Assistant stubs)

import importlib
history = importlib.import_module("albert_heijn.history")
today = date.today()
# like the real account: 2 years of weekly store visits (small), deliveries only the last 12 weeks
receipts = [today - timedelta(days=90 + 7 * i) for i in range(90)]
deliveries = [today - timedelta(days=5 + 7 * i) for i in range(12)]
items = []
# appelsap: in every delivery (2), and in 1 of 6 store visits (the deliveries before the window are unknown)
items += [{"date": d.isoformat(), "source": "bezorging", "product_id": 1, "quantity": 2, "title": "Appelsap"} for d in deliveries]
items += [{"date": d.isoformat(), "source": "winkel", "product_id": 1, "quantity": 1, "title": "Appelsap"} for i, d in enumerate(receipts) if i % 6 == 0]
# zeep: only in the store, every 4 weeks, long ago
items += [{"date": d.isoformat(), "source": "winkel", "product_id": 2, "quantity": 1, "title": "Zeep"} for i, d in enumerate(receipts) if i % 4 == 0 and i > 40]
snap = {"order_days": [d.isoformat() for d in deliveries], "receipt_days": [d.isoformat() for d in receipts], "items": items, "catalog": [], "latest_prices": {}}
rows = {p["title"]: p for p in history.compute_frequent(snap, today, 7)}
a, z = rows["Appelsap"], rows["Zeep"]
print("Appelsap", {k: a[k] for k in ("count", "every_weeks", "share", "per_week", "usual_qty", "rhythm_since")})
print("Zeep    ", {k: z[k] for k in ("count", "every_weeks", "share", "per_week", "rhythm_since", "stopped", "due")})
assert a["every_weeks"] == 1 and a["share"] == 1.0 and a["per_week"] == 2.0 and a["usual_qty"] == 2 and a["count"] == 12 + 15, a
assert z["every_weeks"] == 4 and z["stopped"] is True and z["due"] is False, z
assert a["stopped"] is False
print("WINDOW OK")
