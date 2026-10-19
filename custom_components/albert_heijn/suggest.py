"""Suggestions for the shopping list: order rhythm + bonus, with a check whether a bonus is a real discount.

Pure functions (no Home Assistant, no I/O), so they are easy to test.
"""

from __future__ import annotations

from datetime import date, timedelta
from statistics import median
from typing import Any

REGULAR_DAYS = 90  # compare a bonus with the normal price of the last 90 days
MIN_REGULAR_POINTS = 3  # at least 3 weekly normal prices before we judge a bonus
RAISE_MARGIN = 1.03  # "normal price raised just before the bonus" from +3%


def eur(x: float) -> str:
    return f"€{x:.2f}".replace(".", ",")


def week_start(day: date) -> date:
    """Monday of the bonus week (AH bonus weeks run Monday to Sunday)."""
    return day - timedelta(days=day.weekday())


def bonus_check(series: list[dict[str, Any]], today: date) -> tuple[str, str | None]:
    """Judge the current bonus of one product from its weekly prices (oldest first).

    Returns (status, explanation): 'echt' (real discount), 'let_op' (normal price raised just before the bonus),
    'onbekend' (not enough price history yet) or 'geen' (not in the bonus this week).
    Points: {"d": date, "p": price, "b": price before bonus, "bonus": mechanism or None}.
    """
    if not series:
        return "geen", None
    current = series[-1]
    if not current.get("bonus") or current.get("d", "") < week_start(today).isoformat():
        return "geen", None
    since = (today - timedelta(days=REGULAR_DAYS)).isoformat()
    regular = [x["p"] for x in series[:-1] if not x.get("bonus") and x.get("p") and not x.get("order") and x.get("d", "") >= since]
    if len(regular) < MIN_REGULAR_POINTS:
        return "onbekend", None
    normal, lowest = median(regular), min(regular)
    before = current.get("b") or current.get("p")
    if before and before > normal * RAISE_MARGIN:
        return "let_op", f"normale prijs vlak voor de bonus verhoogd ({eur(normal)} → {eur(before)})"
    if current.get("p") and current["p"] < lowest - 0.005:
        return "echt", f"{eur(current['p'])}, laagste normale prijs was {eur(lowest)}"
    return "echt", "normale prijs ongewijzigd"


def rhythm_text(p: dict[str, Any]) -> str:
    every = p.get("every_weeks")
    parts = ["elke week" if every == 1 else f"elke {every} weken" if every else f"{p.get('count')}× besteld"]
    if (p.get("usual_qty") or 1) > 1:
        parts.append(f"meestal {p['usual_qty']}")
    if p.get("days_since") is not None:
        parts.append(f"laatst {p['days_since']} dagen geleden")
    return " · ".join(parts)


def compute_suggestions(
    frequent: list[dict[str, Any]],
    series: dict[int, list[dict[str, Any]]],
    exclude: set[int],
    today: date,
    min_count: int = 3,
    min_share: float = 0.3,
    max_items: int = 25,
) -> list[dict[str, Any]]:
    """What to put on the list this week, and why.

    1. due according to the order rhythm (ordered at least min_count times, in at least min_share of the orders)
    2. regularly bought and in the bonus now (stock up), unless the bonus looks fake
    Products in `exclude` (already on the list, in the order, or suggested before this week) are skipped.
    """
    out = []
    for p in frequent:
        pid = p.get("product_id")
        if not p.get("count") or not pid or pid in exclude:
            continue
        regular = p["count"] >= min_count and (p.get("share") or 0) >= min_share
        due = bool(p.get("due")) and regular
        status, why = bonus_check(series.get(pid, []), today)
        mechanism = (series.get(pid) or [{}])[-1].get("bonus") if status != "geen" else None
        kinds, reasons = [], []
        if due:
            kinds.append("ritme")
            reasons.append(rhythm_text(p))
        if mechanism and status != "let_op" and (due or regular):
            kinds.append("bonus")
            reasons.append(mechanism + (f" · ✓ {why}" if status == "echt" and why else ""))
        elif mechanism and status == "let_op" and due:
            reasons.append(f"{mechanism} · ⚠ {why}")
        if not kinds:
            continue
        out.append({
            "product_id": pid,
            "title": p.get("title", ""),
            "image_url": p.get("image_url"),
            "quantity": max(1, int(p.get("usual_qty") or 1)),
            "kinds": kinds,
            "reasons": reasons,
            "bonus_check": status if mechanism else None,
            "_prio": (0 if due and "bonus" in kinds else 1 if due else 2, -(p.get("score") or 0)),
        })
    out.sort(key=lambda s: s["_prio"])
    for s in out:
        s.pop("_prio")
    return out[:max_items]
