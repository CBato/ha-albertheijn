"""Suggestions for the shopping list: order rhythm + bonus, with a check whether a bonus is a real discount.

Pure functions (no Home Assistant, no I/O), so they are easy to test.
"""

from __future__ import annotations

from datetime import date, timedelta
import math
import re
from statistics import median
from typing import Any

REGULAR_DAYS = 90  # compare a bonus with the normal price of the last 90 days
MIN_REGULAR_POINTS = 3  # at least 3 weekly normal prices before we judge a bonus
RAISE_MARGIN = 1.03  # "normal price raised just before the bonus" from +3%
STOCK_UP_SAVING = 0.20  # from 20% real saving: stock up
STOCK_WEEKS = 2  # stock up for about 2 weeks...
FRESH_STOCK_WEEKS = 1  # ...or 1 week for fresh products
FRESH = re.compile(r"groente|fruit|zuivel|melk|eieren|vlees|kip|vis|brood|bakkerij|kaas|vleeswaren|salade|maaltijd|vers|aardappel", re.I)


def eur(x: float) -> str:
    return f"€{x:.2f}".replace(".", ",")


def week_start(day: date) -> date:
    """Monday of the bonus week (AH bonus weeks run Monday to Sunday)."""
    return day - timedelta(days=day.weekday())


def _num(text: str) -> float:
    return float(text.replace(",", "."))


def parse_promo(mechanism: str | None) -> dict[str, Any] | None:
    """AH bonus text → rule. Returns {"group": n} plus one of:
    "pay": items paid per group (1+1 gratis → group 2, pay 1), "bundle": price for the group (2 voor 5,00),
    "percent": discount in %, "off": discount in euro per item. None if not understood.
    """
    if not mechanism:
        return None
    t = " ".join(mechanism.lower().replace("€", " ").split())
    if m := re.search(r"(\d+)\s*\+\s*(\d+)\s*gratis", t):
        buy, free = int(m[1]), int(m[2])
        return {"group": buy + free, "pay": buy}
    if m := re.search(r"(\d+)e\s*(?:halve prijs|50%)", t):
        n = int(m[1])
        return {"group": n, "pay": n - 0.5}
    if m := re.search(r"(\d+)e\s*gratis", t):
        n = int(m[1])
        return {"group": n, "pay": n - 1}
    if m := re.search(r"(\d+)\s*voor\s*(\d+[.,]\d{2})", t):
        return {"group": int(m[1]), "bundle": _num(m[2])}
    if m := re.search(r"(\d+)\s*voor\s*(\d+)\b", t):  # "2 voor 1" = 1+1
        n, pay = int(m[1]), int(m[2])
        if pay < n:
            return {"group": n, "pay": pay}
    if m := re.search(r"(\d+)\s*%\s*korting", t):
        return {"group": 1, "percent": int(m[1])}
    if m := re.search(r"(\d+[.,]\d{2})\s*korting", t):
        return {"group": 1, "off": _num(m[1])}
    return None


def effective_price(mechanism: str | None, price: float | None, before: float | None) -> float | None:
    """Price per item when you buy the bonus quantity (1+1 gratis at €2,00 → €1,00)."""
    base = before or price
    promo = parse_promo(mechanism)
    if not base or not promo:
        return price
    if "pay" in promo:
        return round(base * promo["pay"] / promo["group"], 3)
    if "bundle" in promo:
        return round(promo["bundle"] / promo["group"], 3)
    if "percent" in promo:
        return round(min(price or base, base * (1 - promo["percent"] / 100)), 3)
    if "off" in promo:
        return round(max(0.0, base - promo["off"]), 3)
    return price


def bonus_check(series: list[dict[str, Any]], today: date) -> dict[str, Any]:
    """Judge the current bonus of one product from its weekly prices (oldest first).

    status: 'echt' (real discount), 'let_op' (normal price raised just before the bonus, or hardly any saving),
    'onbekend' (not enough price history yet) or 'geen' (not in the bonus this week).
    Also: mechanism, group (buy per), effective (price per item), normal (normal price), saving (0..1), text.
    Points: {"d": date, "p": price, "b": price before bonus, "bonus": mechanism or None}.
    """
    if not series:
        return {"status": "geen"}
    cur = series[-1]
    mech = cur.get("bonus")
    if not mech or cur.get("d", "") < week_start(today).isoformat():
        return {"status": "geen"}
    promo = parse_promo(mech) or {"group": 1}
    eff = effective_price(mech, cur.get("p"), cur.get("b"))
    res: dict[str, Any] = {"status": "onbekend", "mechanism": mech, "group": promo["group"], "effective": eff}
    since = (today - timedelta(days=REGULAR_DAYS)).isoformat()
    regular = [x["p"] for x in series[:-1] if not x.get("bonus") and x.get("p") and x.get("d", "") >= since]
    before = cur.get("b") or cur.get("p")
    if len(regular) < MIN_REGULAR_POINTS:
        # no history yet: the saving AH itself shows (against the price before the bonus)
        if eff and before:
            res["saving"] = round(1 - eff / before, 2)
            res["text"] = f"{eur(eff)} p/st i.p.v. {eur(before)} (volgens AH {round(res['saving'] * 100)}%)"
        return res
    normal = median(regular)
    saving = round(1 - eff / normal, 2) if eff else 0
    res.update({"normal": normal, "saving": saving})
    price_txt = f"{eur(eff)} p/st i.p.v. normaal {eur(normal)}"
    if before and before > normal * RAISE_MARGIN:
        res.update({"status": "let_op", "text": f"normale prijs vlak voor de bonus verhoogd ({eur(normal)} → {eur(before)}); "
                                                  f"echt voordeel {round(saving * 100)}%"})
    elif saving < 0.05:
        res.update({"status": "let_op", "text": f"{price_txt}: nauwelijks voordeel"})
    else:
        res.update({"status": "echt", "text": f"{price_txt}, {round(saving * 100)}% voordeel"})
    return res


def rhythm_text(p: dict[str, Any]) -> str:
    every = p.get("every_weeks")
    parts = ["elke week" if every == 1 else f"elke {every} weken" if every else f"{p.get('count')}× besteld"]
    if (p.get("usual_qty") or 1) > 1:
        parts.append(f"meestal {p['usual_qty']}")
    if p.get("days_since") is not None:
        parts.append(f"laatst {p['days_since']} dagen geleden")
    return " · ".join(parts)


def _round_up(n: float, group: int) -> int:
    return max(group, int(math.ceil(n / group)) * group) if group > 1 else max(1, int(math.ceil(n)))


def compute_suggestions(
    frequent: list[dict[str, Any]],
    series: dict[int, list[dict[str, Any]]],
    exclude: set[int],
    today: date,
    min_count: int = 3,
    min_share: float = 0.3,
    max_items: int = 25,
) -> list[dict[str, Any]]:
    """What to put on the list this week, how many, and why.

    1. due according to the order rhythm and stock (ordered at least min_count times, in at least min_share of the orders)
    2. regularly bought and in a real bonus: stock up (about 2 weeks, fresh products 1 week)
    The quantity is rounded up to the bonus (1+1 gratis: even numbers). A bonus that looks fake is never a reason.
    Products in `exclude` (already on the list, in the order, or suggested before this week) are skipped.
    """
    out = []
    for p in frequent:
        pid = p.get("product_id")
        if not p.get("count") or not pid or pid in exclude:
            continue
        regular = p["count"] >= min_count and (p.get("share") or 0) >= min_share
        due = bool(p.get("due")) and regular
        bonus = bonus_check(series.get(pid, []), today)
        status = bonus["status"]
        usual = max(1, int(p.get("usual_qty") or 1))
        per_week = p.get("per_week") or usual
        fresh = bool(FRESH.search(p.get("category") or ""))
        weeks = FRESH_STOCK_WEEKS if fresh else STOCK_WEEKS
        group = int(bonus.get("group") or 1)
        good = status in ("echt", "onbekend") and (bonus.get("saving") or 0) >= STOCK_UP_SAVING
        kinds, reasons = [], []
        qty = usual if due else 0

        if due:
            kinds.append("ritme")
            reasons.append(rhythm_text(p))
        if status in ("echt", "onbekend") and bonus.get("mechanism") and (due or (regular and good)):
            kinds.append("bonus")
            want = max(qty, per_week * weeks) if good else qty
            new_qty = _round_up(want, group)
            cap = max(group, _round_up(usual * 2, group), _round_up(per_week * weeks, group))
            new_qty = min(new_qty, cap)
            why = bonus["mechanism"]
            if new_qty != usual:
                why += f" → {new_qty} genomen (normaal {usual})"
                if good and new_qty > _round_up(usual, group):
                    why += f", voorraad voor ~{weeks} {'week' if weeks == 1 else 'weken'}"
            if bonus.get("text"):
                why += f" · {'✓ ' if status == 'echt' else ''}{bonus['text']}"
            reasons.append(why)
            qty = new_qty
        elif status == "let_op" and due:
            reasons.append(f"{bonus['mechanism']} · ⚠ {bonus['text']}")
        if not kinds:
            continue
        out.append({
            "product_id": pid,
            "title": p.get("title", ""),
            "image_url": p.get("image_url"),
            "quantity": max(1, qty),
            "kinds": kinds,
            "reasons": reasons,
            "bonus_check": status if bonus.get("mechanism") else None,
            "saving": bonus.get("saving"),
            "_prio": (0 if due and "bonus" in kinds else 1 if due else 2, -(p.get("score") or 0)),
        })
    out.sort(key=lambda s: s["_prio"])
    for s in out:
        s.pop("_prio")
    return out[:max_items]


def order_effective_prices(items: list[dict[str, Any]]) -> dict[int, float | None]:
    """What was really paid per item in one order, by product_id.

    AH lists multi-buy offers ("3 voor 5,00", "1+1 gratis", "2e halve prijs") at the normal price per item and settles
    the discount on the order total, for full groups only, and usually across all products with the same offer
    (mix & match). Discounts that AH already puts in the price (% korting, "VOOR 2.49") are left as they are.
    """
    by_offer: dict[str, list[dict[str, Any]]] = {}
    for i in items:
        if i.get("bonus") and (parse_promo(i["bonus"]) or {}).get("group", 1) > 1:
            by_offer.setdefault(" ".join(i["bonus"].lower().split()), []).append(i)
    out: dict[int, float | None] = {i["product_id"]: i.get("price") for i in items if i.get("product_id")}
    for group_items in by_offer.values():
        promo = parse_promo(group_items[0]["bonus"]) or {}
        size = int(promo.get("group") or 1)
        total = sum(int(i.get("quantity") or 1) for i in group_items)
        full = (total // size) * size  # items in complete groups
        if not full:
            continue  # group not complete: no discount
        share = full / total
        for i in group_items:
            base = i.get("price_before_bonus") or i.get("price")
            unit = effective_price(i["bonus"], i.get("price"), base)
            if base and unit is not None:
                out[i["product_id"]] = round(base - share * (base - unit), 3)
    return out
