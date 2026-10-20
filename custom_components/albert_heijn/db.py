"""SQLite storage for the Albert Heijn order history and weekly prices.

All functions are blocking; call them from an executor (hass.async_add_executor_job).
One small file next to the HA config: /config/albert_heijn.db (included in HA backups).
"""

from __future__ import annotations

from contextlib import closing
import json
import sqlite3

try:
    from .suggest import order_effective_prices
except ImportError:  # standalone use
    from suggest import order_effective_prices  # type: ignore[no-redef]
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    order_id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,             -- delivery date, YYYY-MM-DD
    status TEXT NOT NULL,           -- 'open' (snapshot) or 'delivered'
    updated TEXT
);
CREATE TABLE IF NOT EXISTS order_items (
    order_id INTEGER NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL,
    price REAL,                     -- price per item in this order
    price_before_bonus REAL,
    bonus TEXT,                     -- bonus mechanism when the item was in the bonus, e.g. '1+1 gratis'
    unit_price TEXT,                -- e.g. '€ 1,50 per liter'
    effective REAL,                 -- really paid per item (multi-buy offers settled, see suggest.order_effective_prices)
    PRIMARY KEY (order_id, product_id)
);
CREATE TABLE IF NOT EXISTS products (
    product_id INTEGER PRIMARY KEY,
    title TEXT,
    brand TEXT,
    unit_size TEXT,
    category TEXT,
    image_url TEXT,
    in_catalog INTEGER NOT NULL DEFAULT 0,  -- in AH's "Eerder gekocht"
    updated TEXT
);
CREATE TABLE IF NOT EXISTS prices (
    product_id INTEGER NOT NULL,
    date TEXT NOT NULL,             -- day of the weekly price check
    price REAL,
    price_before_bonus REAL,
    bonus TEXT,
    unit_price TEXT,
    effective REAL,                 -- price per item when buying the bonus quantity (1+1 gratis: half)
    PRIMARY KEY (product_id, date)
);
CREATE TABLE IF NOT EXISTS suggestions (
    week TEXT NOT NULL,             -- delivery date (or Monday) the suggestion was made for
    product_id INTEGER NOT NULL,
    title TEXT,
    quantity INTEGER,
    kinds TEXT,                     -- 'ritme', 'bonus' (comma separated)
    reasons TEXT,                   -- JSON list of reasons in plain Dutch
    bonus_check TEXT,               -- 'echt', 'let_op', 'onbekend' or NULL
    added_at TEXT,
    PRIMARY KEY (week, product_id)
);
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id TEXT PRIMARY KEY,   -- in-store purchase with the bonus card
    date TEXT NOT NULL,
    total REAL,
    updated TEXT
);
CREATE TABLE IF NOT EXISTS receipt_items (
    receipt_id TEXT NOT NULL REFERENCES receipts(receipt_id) ON DELETE CASCADE,
    line INTEGER NOT NULL,
    pos_id INTEGER,                 -- till product id
    name TEXT,                      -- till name, e.g. 'AH APPELSAP'
    quantity REAL,
    amount REAL,
    PRIMARY KEY (receipt_id, line)
);
CREATE TABLE IF NOT EXISTS pos_map (
    pos_id INTEGER PRIMARY KEY,     -- till product id
    product_id INTEGER              -- webshop product id, -1 = not sold online
);
CREATE INDEX IF NOT EXISTS idx_order_items_product ON order_items(product_id);
CREATE INDEX IF NOT EXISTS idx_orders_status_date ON orders(status, date);
"""


def _recompute_effective(con: sqlite3.Connection) -> None:
    """Fill order_items.effective for orders stored before the column existed."""
    rows = [dict(r) for r in con.execute("SELECT order_id, product_id, quantity, price, price_before_bonus, bonus FROM order_items")]
    by_order: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        by_order.setdefault(r["order_id"], []).append(r)
    for order_id, items in by_order.items():
        for pid, eff in order_effective_prices(items).items():
            con.execute("UPDATE order_items SET effective = ? WHERE order_id = ? AND product_id = ?", (eff, order_id, pid))


def _connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init(path: str) -> None:
    with closing(_connect(path)) as con, con:
        con.execute("PRAGMA journal_mode = WAL")
        con.executescript(SCHEMA)
        # columns added in later versions
        cols = {r["name"] for r in con.execute("PRAGMA table_info(prices)")}
        if "effective" not in cols:
            con.execute("ALTER TABLE prices ADD COLUMN effective REAL")
        cols = {r["name"] for r in con.execute("PRAGMA table_info(order_items)")}
        if "effective" not in cols:
            con.execute("ALTER TABLE order_items ADD COLUMN effective REAL")
            _recompute_effective(con)


def get_meta(path: str) -> dict[str, str]:
    with closing(_connect(path)) as con:
        return {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM meta")}


def set_meta(path: str, values: dict[str, Any]) -> None:
    with closing(_connect(path)) as con, con:
        con.executemany(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [(k, None if v is None else str(v)) for k, v in values.items()],
        )


def order_status(path: str) -> dict[int, str]:
    with closing(_connect(path)) as con:
        return {r["order_id"]: r["status"] for r in con.execute("SELECT order_id, status FROM orders")}


def _upsert_product(con: sqlite3.Connection, p: dict[str, Any], now: str, catalog: bool = False) -> None:
    con.execute(
        """INSERT INTO products(product_id, title, brand, unit_size, category, image_url, in_catalog, updated)
           VALUES(:product_id, :title, :brand, :unit_size, :category, :image_url, :in_catalog, :now)
           ON CONFLICT(product_id) DO UPDATE SET
             title = COALESCE(NULLIF(excluded.title, ''), products.title),
             brand = COALESCE(NULLIF(excluded.brand, ''), products.brand),
             unit_size = COALESCE(NULLIF(excluded.unit_size, ''), products.unit_size),
             category = COALESCE(NULLIF(excluded.category, ''), products.category),
             image_url = COALESCE(excluded.image_url, products.image_url),
             in_catalog = MAX(products.in_catalog, excluded.in_catalog),
             updated = excluded.updated""",
        {
            "product_id": p["product_id"], "title": p.get("title") or "", "brand": p.get("brand") or "",
            "unit_size": p.get("unit_size") or "", "category": p.get("category") or "",
            "image_url": p.get("image_url"), "in_catalog": 1 if catalog else 0, "now": now,
        },
    )


def save_order(path: str, order_id: int, day: str, status: str, items: list[dict[str, Any]], now: str) -> bool:
    """Store an order with its products. Returns True if anything changed."""
    merged: dict[int, dict[str, Any]] = {}
    for i in items:
        pid = i.get("product_id")
        if not pid:
            continue
        if pid in merged:
            merged[pid]["quantity"] += int(i.get("quantity") or 1)
        else:
            merged[pid] = {**i, "quantity": int(i.get("quantity") or 1)}
    with closing(_connect(path)) as con, con:
        old = con.execute("SELECT date, status FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        old_items = {
            (r["product_id"], r["quantity"])
            for r in con.execute("SELECT product_id, quantity FROM order_items WHERE order_id = ?", (order_id,))
        }
        if old and old["date"] == day and old["status"] == status and old_items == {(p, m["quantity"]) for p, m in merged.items()}:
            return False
        con.execute(
            """INSERT INTO orders(order_id, date, status, updated) VALUES(?, ?, ?, ?)
               ON CONFLICT(order_id) DO UPDATE SET date = excluded.date, status = excluded.status, updated = excluded.updated""",
            (order_id, day, status, now),
        )
        con.execute("DELETE FROM order_items WHERE order_id = ?", (order_id,))
        paid = order_effective_prices([{**i, "product_id": pid} for pid, i in merged.items()])
        for pid, i in merged.items():
            _upsert_product(con, i, now)
            con.execute(
                """INSERT INTO order_items(order_id, product_id, quantity, price, price_before_bonus, bonus, unit_price, effective)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                (order_id, pid, i["quantity"], i.get("price"), i.get("price_before_bonus"), i.get("bonus"), i.get("unit_price"),
                 paid.get(pid)),
            )
        return True


def mark_delivered(path: str, open_ids: set[int], today: str) -> int:
    """Open snapshots that are no longer open after their delivery date were delivered."""
    with closing(_connect(path)) as con, con:
        rows = [r["order_id"] for r in con.execute("SELECT order_id FROM orders WHERE status = 'open' AND date < ?", (today,))]
        gone = [oid for oid in rows if oid not in open_ids]
        con.executemany("UPDATE orders SET status = 'delivered' WHERE order_id = ?", [(o,) for o in gone])
        return len(gone)


def save_catalog(path: str, products: list[dict[str, Any]], now: str) -> None:
    with closing(_connect(path)) as con, con:
        con.execute("UPDATE products SET in_catalog = 0")
        for p in products:
            if p.get("product_id"):
                _upsert_product(con, p, now, catalog=True)


def save_prices(path: str, day: str, rows: list[dict[str, Any]]) -> int:
    with closing(_connect(path)) as con, con:
        data = [
            (r["product_id"], day, r.get("price"), r.get("price_before_bonus"), r.get("bonus"), r.get("unit_price"),
             r.get("effective", r.get("price")))
            for r in rows
            if r.get("product_id") and r.get("price") is not None
        ]
        con.executemany(
            """INSERT INTO prices(product_id, date, price, price_before_bonus, bonus, unit_price, effective) VALUES(?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(product_id, date) DO UPDATE SET price = excluded.price, price_before_bonus = excluded.price_before_bonus,
                 bonus = excluded.bonus, unit_price = excluded.unit_price, effective = excluded.effective""",
            data,
        )
        return len(data)


def load_for_frequent(path: str) -> dict[str, Any]:
    """Everything needed to compute frequent products and rhythm, in one read.

    Purchases = delivered orders + till receipts (receipt lines with a webshop product), each with a date and source.
    """
    with closing(_connect(path)) as con:
        items = [
            dict(r)
            for r in con.execute(
                """SELECT o.date, 'bezorging' AS source, i.product_id, i.quantity, p.title, p.brand, p.unit_size, p.category, p.image_url
                   FROM orders o JOIN order_items i ON i.order_id = o.order_id
                   LEFT JOIN products p ON p.product_id = i.product_id
                   WHERE o.status = 'delivered'
                   UNION ALL
                   SELECT r.date, 'winkel' AS source, m.product_id, ri.quantity, COALESCE(p.title, ri.name), p.brand, p.unit_size,
                          p.category, p.image_url
                   FROM receipts r JOIN receipt_items ri ON ri.receipt_id = r.receipt_id
                   JOIN pos_map m ON m.pos_id = ri.pos_id AND m.product_id > 0
                   LEFT JOIN products p ON p.product_id = m.product_id
                   ORDER BY 1"""
            )
        ]
        order_days = [r["date"] for r in con.execute("SELECT date FROM orders WHERE status = 'delivered' ORDER BY date")]
        receipt_days = [r["date"] for r in con.execute("SELECT date FROM receipts ORDER BY date")]
        catalog = [
            dict(r)
            for r in con.execute(
                "SELECT product_id, title, brand, unit_size, category, image_url FROM products WHERE in_catalog = 1"
            )
        ]
        # latest weekly price per product (SQLite returns the row that belongs to MAX(date))
        latest = {
            r["product_id"]: {"price": r["price"], "bonus": r["bonus"]}
            for r in con.execute("SELECT product_id, price, bonus, MAX(date) AS d FROM prices GROUP BY product_id")
        }
        counts = con.execute(
            """SELECT (SELECT COUNT(*) FROM products) AS products,
                      (SELECT COUNT(*) FROM prices) AS price_points"""
        ).fetchone()
    return {"order_days": order_days, "receipt_days": receipt_days, "items": items, "catalog": catalog,
            "latest_prices": latest, "products": counts["products"], "price_points": counts["price_points"]}


def known_receipts(path: str) -> set[str]:
    with closing(_connect(path)) as con:
        return {r["receipt_id"] for r in con.execute("SELECT receipt_id FROM receipts")}


def save_receipt(path: str, receipt_id: str, day: str, total: float | None, lines: list[dict[str, Any]], now: str) -> None:
    with closing(_connect(path)) as con, con:
        con.execute(
            """INSERT INTO receipts(receipt_id, date, total, updated) VALUES(?, ?, ?, ?)
               ON CONFLICT(receipt_id) DO UPDATE SET date = excluded.date, total = excluded.total, updated = excluded.updated""",
            (receipt_id, day, total, now),
        )
        con.execute("DELETE FROM receipt_items WHERE receipt_id = ?", (receipt_id,))
        con.executemany(
            "INSERT INTO receipt_items(receipt_id, line, pos_id, name, quantity, amount) VALUES(?, ?, ?, ?, ?, ?)",
            [(receipt_id, n, ln.get("pos_id"), ln.get("name"), ln.get("quantity"), ln.get("amount")) for n, ln in enumerate(lines)],
        )


def unmapped_pos_ids(path: str) -> list[int]:
    with closing(_connect(path)) as con:
        return [
            r["pos_id"]
            for r in con.execute(
                """SELECT DISTINCT ri.pos_id FROM receipt_items ri LEFT JOIN pos_map m ON m.pos_id = ri.pos_id
                   WHERE ri.pos_id IS NOT NULL AND m.pos_id IS NULL"""
            )
        ]


def save_pos_map(path: str, mapping: dict[int, int]) -> None:
    with closing(_connect(path)) as con, con:
        con.executemany(
            "INSERT INTO pos_map(pos_id, product_id) VALUES(?, ?) ON CONFLICT(pos_id) DO UPDATE SET product_id = excluded.product_id",
            list(mapping.items()),
        )


def price_history(path: str, product_id: int) -> list[dict[str, Any]]:
    """Weekly prices and the prices paid in delivered orders, oldest first."""
    with closing(_connect(path)) as con:
        weekly = [
            {"d": r["date"], "p": r["price"], "b": r["price_before_bonus"], "bonus": r["bonus"], "u": r["unit_price"], "e": r["effective"]}
            for r in con.execute(
                "SELECT date, price, price_before_bonus, bonus, unit_price, effective FROM prices WHERE product_id = ? ORDER BY date",
                (product_id,),
            )
        ]
        paid = [
            {"d": r["date"], "p": r["price"], "b": r["price_before_bonus"], "bonus": r["bonus"], "qty": r["quantity"], "order": True}
            for r in con.execute(
                """SELECT o.date, i.price, i.price_before_bonus, i.bonus, i.quantity FROM order_items i
                   JOIN orders o ON o.order_id = i.order_id WHERE i.product_id = ? AND o.status = 'delivered' ORDER BY o.date""",
                (product_id,),
            )
        ]
    return sorted(weekly + paid, key=lambda x: x.get("d") or "")


def price_series(path: str, product_ids: list[int], since: str) -> dict[int, list[dict[str, Any]]]:
    """Weekly prices since a date for several products (oldest first)."""
    out: dict[int, list[dict[str, Any]]] = {}
    if not product_ids:
        return out
    with closing(_connect(path)) as con:
        for i in range(0, len(product_ids), 500):
            chunk = product_ids[i : i + 500]
            marks = ",".join("?" * len(chunk))
            for r in con.execute(
                f"""SELECT product_id, date, price, price_before_bonus, bonus FROM prices
                    WHERE date >= ? AND product_id IN ({marks}) ORDER BY product_id, date""",
                (since, *chunk),
            ):
                out.setdefault(r["product_id"], []).append(
                    {"d": r["date"], "p": r["price"], "b": r["price_before_bonus"], "bonus": r["bonus"]}
                )
    return out


def save_suggestions(path: str, week: str, rows: list[dict[str, Any]], now: str) -> None:
    with closing(_connect(path)) as con, con:
        con.executemany(
            """INSERT INTO suggestions(week, product_id, title, quantity, kinds, reasons, bonus_check, added_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(week, product_id) DO NOTHING""",
            [
                (week, r["product_id"], r.get("title"), r.get("quantity"), ",".join(r.get("kinds") or []),
                 json.dumps(r.get("reasons") or [], ensure_ascii=False), r.get("bonus_check"), now)
                for r in rows
            ],
        )


def get_suggestions(path: str, week: str) -> list[dict[str, Any]]:
    with closing(_connect(path)) as con:
        return [
            {
                "product_id": r["product_id"], "title": r["title"], "quantity": r["quantity"],
                "kinds": [k for k in (r["kinds"] or "").split(",") if k], "reasons": json.loads(r["reasons"] or "[]"),
                "bonus_check": r["bonus_check"], "added_at": r["added_at"],
            }
            for r in con.execute("SELECT * FROM suggestions WHERE week = ? ORDER BY added_at, rowid", (week,))
        ]


def delete_order(path: str, order_id: int) -> bool:
    with closing(_connect(path)) as con, con:
        return con.execute("DELETE FROM orders WHERE order_id = ?", (order_id,)).rowcount > 0


def product_history(path: str, product_id: int) -> dict[str, Any]:
    """Everything about one product: details, every purchase (delivery + till receipt) and the weekly prices."""
    with closing(_connect(path)) as con:
        prod = con.execute(
            "SELECT product_id, title, brand, unit_size, category, image_url, in_catalog FROM products WHERE product_id = ?",
            (product_id,),
        ).fetchone()
        purchases = [
            {"date": r["date"], "source": "bezorging", "quantity": r["quantity"],
             "price": r["effective"] if r["effective"] is not None else r["price"], "price_list": r["price"],
             "price_before_bonus": r["price_before_bonus"], "bonus": r["bonus"],
             "amount": round((r["effective"] if r["effective"] is not None else r["price"]) * (r["quantity"] or 0), 2)
             if r["price"] is not None else None}
            for r in con.execute(
                """SELECT o.date, i.quantity, i.price, i.price_before_bonus, i.bonus, i.effective FROM order_items i
                   JOIN orders o ON o.order_id = i.order_id WHERE i.product_id = ? AND o.status = 'delivered'""",
                (product_id,),
            )
        ]
        purchases += [
            {"date": r["date"], "source": "winkel", "quantity": r["quantity"],
             "price": round(r["amount"] / r["quantity"], 2) if r["amount"] is not None and r["quantity"] else None,
             "price_before_bonus": None, "bonus": None, "amount": r["amount"], "name": r["name"]}
            for r in con.execute(
                """SELECT rc.date, ri.quantity, ri.amount, ri.name FROM receipt_items ri
                   JOIN receipts rc ON rc.receipt_id = ri.receipt_id
                   JOIN pos_map m ON m.pos_id = ri.pos_id WHERE m.product_id = ?""",
                (product_id,),
            )
        ]
        prices = [
            {"d": r["date"], "p": r["price"], "b": r["price_before_bonus"], "bonus": r["bonus"], "e": r["effective"]}
            for r in con.execute(
                "SELECT date, price, price_before_bonus, bonus, effective FROM prices WHERE product_id = ? ORDER BY date",
                (product_id,),
            )
        ]
    purchases.sort(key=lambda x: x["date"])
    return {"product": dict(prod) if prod else {"product_id": product_id}, "purchases": purchases, "prices": prices}


def insights(path: str, today: str, weeks: int = 12) -> dict[str, Any]:
    """Spending overview: this month, per week, biggest items, categories, bonus savings and price movers.

    Spending: deliveries at what was really paid (order_items.effective), store visits at the receipt total.
    """
    from datetime import date, timedelta

    t = date.fromisoformat(today)
    month_start = t.replace(day=1)
    prev_start = (month_start - timedelta(days=1)).replace(day=1)
    week0 = t - timedelta(days=t.weekday()) - timedelta(weeks=weeks - 1)
    since90 = (t - timedelta(days=90)).isoformat()
    with closing(_connect(path)) as con:
        order_rows = [dict(r) for r in con.execute(
            """SELECT o.order_id, o.date, i.product_id, i.quantity, COALESCE(i.effective, i.price) AS paid, i.price,
                      i.price_before_bonus AS before, i.bonus, p.title, p.category, p.image_url
               FROM order_items i JOIN orders o ON o.order_id = i.order_id
               LEFT JOIN products p ON p.product_id = i.product_id WHERE o.status = 'delivered'""")]
        receipts = [dict(r) for r in con.execute("SELECT receipt_id, date, total FROM receipts")]
        receipt_rows = [dict(r) for r in con.execute(
            """SELECT r.date, m.product_id, ri.quantity, ri.amount AS paid, COALESCE(p.title, ri.name) AS title,
                      p.category, p.image_url
               FROM receipt_items ri JOIN receipts r ON r.receipt_id = ri.receipt_id
               JOIN pos_map m ON m.pos_id = ri.pos_id AND m.product_id > 0
               LEFT JOIN products p ON p.product_id = m.product_id""")]
        weekly = [dict(r) for r in con.execute(
            "SELECT product_id, date, price, bonus FROM prices WHERE date >= ? ORDER BY date",
            ((t - timedelta(days=180)).isoformat(),))]
        titles = {r["product_id"]: (r["title"], r["image_url"]) for r in con.execute("SELECT product_id, title, image_url FROM products")}

    def line_cost(r: dict[str, Any]) -> float:
        return (r.get("paid") or 0) * float(r.get("quantity") or 0)

    def month(lo: date, hi: date) -> dict[str, Any]:
        lo_s, hi_s = lo.isoformat(), hi.isoformat()
        rows = [r for r in order_rows if lo_s <= r["date"] < hi_s]
        orders = {r["order_id"] for r in rows}
        delivery = sum(line_cost(r) for r in rows)
        store = sum(r["total"] or 0 for r in receipts if lo_s <= r["date"] < hi_s)
        saved = sum(max(0.0, (r["before"] or r["price"] or 0) - (r["paid"] or 0)) * float(r["quantity"] or 0)
                    for r in rows if r.get("bonus"))
        n_receipts = sum(1 for r in receipts if lo_s <= r["date"] < hi_s)
        return {"from": lo_s, "spent": round(delivery + store, 2), "delivery": round(delivery, 2),
                "store": round(store, 2), "orders": len(orders), "receipts": n_receipts,
                "avg_order": round(delivery / len(orders), 2) if orders else None, "saved": round(saved, 2)}

    next_month = (month_start + timedelta(days=32)).replace(day=1)
    cur, prev = month(month_start, next_month), month(prev_start, month_start)

    week_list = []
    for w in range(weeks):
        lo = week0 + timedelta(weeks=w)
        hi = lo + timedelta(days=7)
        lo_s, hi_s = lo.isoformat(), hi.isoformat()
        d = sum(line_cost(r) for r in order_rows if lo_s <= r["date"] < hi_s)
        st = sum(r["total"] or 0 for r in receipts if lo_s <= r["date"] < hi_s)
        week_list.append({"week": lo_s, "delivery": round(d, 2), "store": round(st, 2)})

    # biggest items and categories, last 90 days (deliveries at paid price, store lines at their amount)
    spend: dict[int, dict[str, Any]] = {}
    cats: dict[str, float] = {}
    for src, rows in (("bezorging", order_rows), ("winkel", receipt_rows)):
        for r in rows:
            if r["date"] < since90 or not r.get("product_id"):
                continue
            cost = line_cost(r) if src == "bezorging" else (r.get("paid") or 0)
            e = spend.setdefault(r["product_id"], {"product_id": r["product_id"], "title": r.get("title") or "",
                                                   "image_url": r.get("image_url"), "spent": 0.0, "quantity": 0.0})
            e["spent"] += cost
            e["quantity"] += float(r.get("quantity") or 0)
            cat = (r.get("category") or "Overig").split("/")[0].strip() or "Overig"
            cats[cat] = cats.get(cat, 0.0) + cost
    top = sorted(spend.values(), key=lambda e: -e["spent"])[:12]
    for e in top:
        e["spent"], e["quantity"] = round(e["spent"], 2), round(e["quantity"], 1)
    total_cat = sum(cats.values()) or 1
    categories = [{"name": k, "spent": round(v, 2), "share": round(v / total_cat, 3)}
                  for k, v in sorted(cats.items(), key=lambda kv: -kv[1])[:8]]

    # best bonus deals this month
    deals: dict[int, dict[str, Any]] = {}
    for r in order_rows:
        if r["date"] < month_start.isoformat() or not r.get("bonus"):
            continue
        saved = max(0.0, (r["before"] or r["price"] or 0) - (r["paid"] or 0)) * float(r["quantity"] or 0)
        if saved <= 0:
            continue
        e = deals.setdefault(r["product_id"], {"product_id": r["product_id"], "title": r.get("title") or "",
                                               "bonus": r["bonus"], "saved": 0.0})
        e["saved"] += saved
    best = sorted(deals.values(), key=lambda e: -e["saved"])[:5]
    for e in best:
        e["saved"] = round(e["saved"], 2)

    # price movers: normal (non-bonus) price now vs at least 4 weeks earlier; weekly checks, else paid list prices
    series: dict[int, list[tuple[str, float]]] = {}
    for r in weekly:
        if r["price"] is not None and not r.get("bonus"):
            series.setdefault(r["product_id"], []).append((r["date"], r["price"]))
    source = "weekprijzen"
    usable = sum(1 for v in series.values() if len(v) >= 2 and (date.fromisoformat(v[-1][0]) - date.fromisoformat(v[0][0])).days >= 28)
    if usable < 5:
        source = "aankopen"
        series = {}
        for r in sorted(order_rows, key=lambda r: r["date"]):
            listp = r["before"] if r.get("bonus") else r["price"]
            if listp and r["date"] >= (t - timedelta(days=180)).isoformat():
                series.setdefault(r["product_id"], []).append((r["date"], listp))
    movers = []
    for pid, pts in series.items():
        last_d, last_p = pts[-1]
        ref = [pt for pt in pts if (date.fromisoformat(last_d) - date.fromisoformat(pt[0])).days >= 28]
        if not ref:
            continue
        ref_d, ref_p = ref[0]
        if ref_p and abs(last_p - ref_p) >= 0.05:
            title, img = titles.get(pid, ("", None))
            movers.append({"product_id": pid, "title": title, "image_url": img, "from": ref_p, "to": last_p,
                           "pct": round((last_p - ref_p) / ref_p * 100, 1), "since": ref_d})
    up = sorted([m for m in movers if m["pct"] > 0], key=lambda m: -m["pct"])[:6]
    down = sorted([m for m in movers if m["pct"] < 0], key=lambda m: m["pct"])[:6]
    return {"month": cur, "prev_month": prev, "weeks": week_list, "top": top, "categories": categories,
            "best_deals": best, "movers": {"source": source, "up": up, "down": down}}


def products_overview(path: str) -> list[dict[str, Any]]:
    """Every product ever bought: last bought, times, average paid per item (what was really paid), last paid,
    current price and bonus (latest weekly check), and the difference current vs average paid."""
    with closing(_connect(path)) as con:
        buys = [dict(r) for r in con.execute(
            """SELECT i.product_id, o.date, i.quantity, COALESCE(i.effective, i.price) AS paid
               FROM order_items i JOIN orders o ON o.order_id = i.order_id WHERE o.status = 'delivered'
               UNION ALL
               SELECT m.product_id, r.date, ri.quantity,
                      CASE WHEN ri.quantity > 0 THEN ri.amount / ri.quantity END AS paid
               FROM receipt_items ri JOIN receipts r ON r.receipt_id = ri.receipt_id
               JOIN pos_map m ON m.pos_id = ri.pos_id AND m.product_id > 0""")]
        products = {r["product_id"]: dict(r) for r in con.execute(
            "SELECT product_id, title, brand, unit_size, category, image_url, in_catalog FROM products")}
        latest = {r["product_id"]: dict(r) for r in con.execute(
            "SELECT product_id, price, price_before_bonus, bonus, effective, MAX(date) AS d FROM prices GROUP BY product_id")}
    agg: dict[int, dict[str, Any]] = {}
    for b in sorted(buys, key=lambda b: b["date"]):
        a = agg.setdefault(b["product_id"], {"days": set(), "qty": 0.0, "paid_sum": 0.0, "paid_qty": 0.0, "last": None, "last_paid": None})
        q = float(b["quantity"] or 0)
        a["days"].add(b["date"])
        a["qty"] += q
        if b["paid"] is not None and q > 0:
            a["paid_sum"] += b["paid"] * q
            a["paid_qty"] += q
            a["last_paid"] = round(b["paid"], 3)
        a["last"] = b["date"]
    out = []
    for pid, a in agg.items():
        prod = products.get(pid, {})
        lp = latest.get(pid) or {}
        avg = round(a["paid_sum"] / a["paid_qty"], 3) if a["paid_qty"] else None
        normal = lp.get("price_before_bonus") or lp.get("price")
        cat = prod.get("category") or ""
        out.append({
            "product_id": pid, "title": prod.get("title") or f"Product {pid}", "brand": prod.get("brand"),
            "unit_size": prod.get("unit_size"), "image_url": prod.get("image_url"),
            "category": cat.split("/")[0].strip() if cat else "Overig", "subcategory": cat.split("/", 1)[1].strip() if "/" in cat else None,
            "times": len(a["days"]), "quantity": round(a["qty"], 1), "last_bought": a["last"],
            "avg_paid": avg, "last_paid": a["last_paid"],
            "price": lp.get("price"), "normal_price": normal, "bonus": lp.get("bonus"), "effective": lp.get("effective"),
            "price_date": lp.get("d"),
            # current normal price vs what you paid on average (positive = more expensive now)
            "diff_pct": round((normal - avg) / avg * 100, 1) if normal and avg else None,
        })
    out.sort(key=lambda r: r["last_bought"] or "", reverse=True)
    return out


def fill_product_details(path: str, rows: list[dict[str, Any]], now: str) -> int:
    """Fill in missing title, brand, size, category and image (never overwrite what is already known)."""
    n = 0
    with closing(_connect(path)) as con, con:
        for r in rows:
            if not r.get("product_id"):
                continue
            con.execute("INSERT OR IGNORE INTO products(product_id, in_catalog, updated) VALUES(?, 0, ?)", (r["product_id"], now))
            cur = con.execute(
                """UPDATE products SET
                     title = CASE WHEN title IS NULL OR title = '' OR title LIKE 'Product %' THEN NULLIF(?, '') ELSE title END,
                     brand = COALESCE(NULLIF(brand, ''), NULLIF(?, '')),
                     unit_size = COALESCE(NULLIF(unit_size, ''), NULLIF(?, '')),
                     category = COALESCE(NULLIF(category, ''), NULLIF(?, '')),
                     image_url = COALESCE(image_url, ?),
                     updated = ?
                   WHERE product_id = ? AND (title IS NULL OR title = '' OR title LIKE 'Product %' OR brand IS NULL OR brand = ''
                     OR unit_size IS NULL OR unit_size = '' OR category IS NULL OR category = '' OR image_url IS NULL)""",
                (r.get("title") or "", r.get("brand") or "", r.get("unit_size") or "", r.get("category") or "",
                 r.get("image_url"), now, r["product_id"]),
            )
            n += cur.rowcount
    return n
