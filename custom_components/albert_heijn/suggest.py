"""Suggestions for the shopping list: order rhythm + bonus, with a check whether a bonus is a real discount.

Pure functions (no Home Assistant, no I/O), so they are easy to test.
"""

from __future__ import annotations

from datetime import date, timedelta
import math
import re
from statistics import median
from typing import Any
import unicodedata

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


def bonus_check(series: list[dict[str, Any]], today: date, avg_paid: float | None = None) -> dict[str, Any]:
    """Judge the current bonus of one product.

    With avg_paid (what you really paid per item on average): the offer price per item against that, the same
    comparison as the Producten table in the app. Without it: against the normal price of the last 90 days.

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
    if avg_paid and eff:
        saving = round(1 - eff / avg_paid, 2)
        res.update({"normal": avg_paid, "saving": saving})
        txt = f"{eur(eff)} p/st, je betaalt gemiddeld {eur(avg_paid)}"
        if saving >= 0.05:
            res.update({"status": "echt", "text": f"{txt}: {round(saving * 100)}% voordeel"})
        else:
            res.update({"status": "let_op", "text": f"{txt}: {'nauwelijks voordeel' if saving >= 0 else 'duurder dan normaal voor jou'}"})
        return res
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
    avg_paid: dict[int, float] | None = None,
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
        bonus = bonus_check(series.get(pid, []), today, (avg_paid or {}).get(pid))
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


# words that say nothing about which product you mean ("zet een pak melk op de lijst")
_FILLER = {"de", "het", "een", "wat", "nog", "paar", "pak", "pakje", "pakken", "fles", "flessen", "zak", "zakje",
           "ah", "albert", "heijn", "van", "en", "met"}


def _plain(text: str) -> str:
    """Lowercase, no accents, only letters, digits and spaces ("Crème fraîche" -> "creme fraiche")."""
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


def _stem(word: str) -> str:
    """Rough Dutch plural to singular, so "eieren" finds "ei" and "bananen" finds "banaan" (via "banan")."""
    for suffix in ("eren", "en", "s"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def match_products(products: list[dict[str, Any]], query: str, limit: int = 4) -> tuple[list[dict[str, Any]], bool]:
    """Products whose title (and brand) contain every word you said, best first, and whether the choice is clear.

    `products` are rows from compute_frequent (bought products with a score, then "Eerder gekocht" with score 0).
    Words match at the start of a word in the title; only when that finds nothing, anywhere in a word.
    Clear: only one bought product matches, or the best one is bought regularly (3+ times) and scores at least twice
    as high as the next one, or nothing was bought and exactly one "Eerder gekocht" product matches.
    """
    words = [_stem(w) for w in _plain(query).split() if w not in _FILLER]
    if not words:
        return [], False
    texts = [(p, " " + _plain(f"{p.get('title') or ''} {p.get('brand') or ''}")) for p in products if p.get("product_id")]
    # each word at the start of a word ("volle" is not "halfvolle"); only if that finds nothing, anywhere ("karnemelk")
    hits = [p for p, text in texts if all(" " + w in text for w in words)] or [p for p, text in texts if all(w in text for w in words)]
    hits.sort(key=lambda p: (-(p.get("score") or 0), -(p.get("count") or 0), len(p.get("title") or "")))
    bought = [p for p in hits if p.get("count")]
    if len(bought) == 1:
        clear = True
    elif bought:
        top, second = bought[0], bought[1]
        clear = (top.get("count") or 0) >= 3 and (top.get("score") or 0) >= 2 * (second.get("score") or 0)
    else:
        clear = len(hits) == 1
    return hits[:limit], clear


# ---------- matching a product at another store by name and size ----------

# size units -> (factor, dimension): grams, millilitres, pieces
_UNITS = {"kg": (1000, "g"), "kilo": (1000, "g"), "g": (1, "g"), "gr": (1, "g"), "gram": (1, "g"),
          "l": (1000, "ml"), "lt": (1000, "ml"), "ltr": (1000, "ml"), "liter": (1000, "ml"), "cl": (10, "ml"),
          "ml": (1, "ml"), "st": (1, "st"), "stuk": (1, "st"), "stuks": (1, "st"), "stück": (1, "st")}
_UNIT_RE = "|".join(sorted(_UNITS, key=len, reverse=True))
_SIZE_WORDS = set(_UNITS) | {"x", "per", "ca", "circa", "stuk", "pak", "fles", "blik", "pot", "doos", "zak", "los", "kilo",
                              # packaging: the size says it already ("grootverpakking" 500 g vs 500 g)
                              "grootverpakking", "voordeelverpakking", "voordeelpak", "familieverpakking", "multipack",
                              "verpakking", "navulverpakking", "navulling", "voordeel", "bulk", "xl", "xxl", "maxi", "mega"}
# words that say little about what the product is ("Bolletje Echte beschuit" = "Bolletje beschuit")
_WEAK = {"echte", "echt", "original", "originele", "origineel", "classic", "klassiek", "klassieke", "lekker", "heerlijke",
         "verse", "vers", "huismerk", "nieuw", "new"}
# parts of a compound word that change the product ("half" + "volle" is not "volle")
_QUALIFIERS = {"half", "mager", "extra", "super", "light", "licht", "dubbel", "volkoren", "wit", "witte", "bruin", "bruine",
               "zoet", "zuur", "zout", "ongezouten", "pittig", "mild", "jong", "oud", "belegen", "zero", "suikervrij", "vol"}
# the same word written differently
_SYNONYMS = {"biologisch": "bio", "biologische": "bio", "organic": "bio", "eko": "bio"}
# store brands: different at every store, so the brand doesn't have to match (the product does)
_HOUSE = {"ah", "albert", "heijn", "picnic", "excellent", "terra", "basic", "huismerk", "g&g"}
MATCH_MIN_SCORE = 60  # below this a name match is not shown


def parse_size(text: str | None) -> tuple[float, str] | None:
    """Pack size as (amount, dimension) in grams, millilitres or pieces: "6 x 33 cl" -> (1980, "ml"),
    "1,5 liter" -> (1500, "ml"), "450 gram" -> (450, "g"), "10 stuks" -> (10, "st"), "per stuk" -> (1, "st")."""
    t = (text or "").lower().replace(",", ".")
    if m := re.search(rf"(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*({_UNIT_RE})\b", t):
        factor, dim = _UNITS[m[3]]
        return round(int(m[1]) * float(m[2]) * factor, 3), dim
    found = [(round(float(m[1]) * _UNITS[m[2]][0], 3), _UNITS[m[2]][1]) for m in re.finditer(rf"(\d+(?:\.\d+)?)\s*({_UNIT_RE})\b", t)]
    # "1 stuk (ca. 800 gram)": the weight says more than the piece
    if found:
        return next((f for f in found if f[1] != "st"), found[0])
    if re.search(r"\bper (kilo|kg)\b", t):
        return 1000.0, "g"  # sold by weight: the price is per kilo
    if re.search(r"\bper (liter|l)\b", t):
        return 1000.0, "ml"
    if re.search(r"\bper stuk\b", t):
        return 1.0, "st"
    return None


def unit_price(price: float | None, size: tuple[float, str] | None) -> float | None:
    """Price per kg, per litre or per piece."""
    if price is None or not size or not size[0]:
        return None
    amount, dim = size
    return round(price / amount * (1 if dim == "st" else 1000), 3)


def _keep(word: str) -> bool:
    return word not in _SIZE_WORDS and word not in _FILLER and word not in _HOUSE and word not in _WEAK and not any(c.isdigit() for c in word)


def _words(text: str) -> set[str]:
    return {_stem(_SYNONYMS.get(w, w)) for w in _plain(text).split() if _keep(w)}


def is_house_brand(brand: str | None) -> bool:
    words = _plain(brand or "").split()
    return not words or words[0] in _HOUSE


def search_query(title: str, brand: str | None = None) -> str:
    """What to search for at another store: the product name without size, packaging and store brand."""
    return " ".join(_SYNONYMS.get(w, w) for w in _plain(title).split() if _keep(w))


def search_queries(title: str, brand: str | None = None) -> list[str]:
    """Searches to try, from precise to broad: the whole name; for a brand: brand + the longest word; the first word
    ("andijvie fijngesneden" -> "andijvie"). Store search engines often find nothing for a long exact name."""
    full = search_query(title, brand)
    words = full.split()
    brand_words = [w for w in _plain(brand or "").split() if _keep(w)] if not is_house_brand(brand) else []
    rest = [w for w in words if w not in brand_words and w not in _SYNONYMS.values()]
    out = [full]
    if rest:
        longest = max(rest, key=len)
        out.append(" ".join(brand_words + [longest]) if brand_words else longest)
        out.append(rest[0])
    return [q for i, q in enumerate(out) if q and q not in out[:i]]


def match_score(product: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """How well a product at another store matches one of yours, by name and size.

    product: title, brand, unit_size. candidate: title, unit. Returns score (0-99; 100 is kept for the same barcode
    or a match you confirmed) and kind: "same" (same name and size), "size" (same product, other size: compare per
    kg/litre), "similar" (other size unit or unknown size). Words count when part of a compound word.
    """
    branded = not is_house_brand(product.get("brand"))
    brand_words = _words(product.get("brand") or "") if branded else set()
    # the brand is checked on its own; the product words (flavour, kind) must match without it
    mine = _words(product.get("title") or "") - brand_words
    theirs = _words(candidate.get("title") or "") - brand_words
    if not mine or not theirs:
        return {"score": 0, "kind": "similar"}

    def found(word: str, others: set[str]) -> bool:
        """A word also counts when it is part of a longer word, either way ("scharrel" / "scharreleieren",
        "slamelange" / "melange")."""
        def inside(short: str, long: str) -> bool:
            if len(short) < 3 or short not in long:
                return False
            rest = long.replace(short, "", 1)
            return len(rest) < 2 or rest not in _QUALIFIERS

        return word in others or any(inside(word, o) or (len(o) >= 4 and inside(o, word)) for o in others)

    def compact(text: str) -> str:
        return "".join(_SYNONYMS.get(w, w) for w in _plain(text).split() if _keep(w))

    if compact(product.get("title") or "").replace(compact(product.get("brand") or ""), "") ==             compact(candidate.get("title") or "").replace(compact(product.get("brand") or ""), ""):
        hits, extra = len(mine), 0  # same words, only written apart or together ("scharrel eieren" / "scharreleieren")
    else:
        hits = sum(found(w, theirs) for w in mine)
        extra = sum(not found(w, mine) for w in theirs)
    score = 100 * (0.65 * hits / len(mine) + 0.35 * hits / (len(mine) + extra))
    if branded:
        # a branded product must be the same brand there
        if brand_words and brand_words <= _words(candidate.get("title") or ""):
            score += 10
        else:
            score = min(score, 50)
    size_mine = parse_size(product.get("unit_size")) or parse_size(product.get("title"))
    size_theirs = parse_size(candidate.get("unit")) or parse_size(candidate.get("title"))
    if size_mine and size_theirs and size_mine[1] == size_theirs[1]:
        kind = "same" if abs(size_mine[0] - size_theirs[0]) <= 0.03 * size_mine[0] else "size"
        score -= 0 if kind == "same" else 5
    else:
        kind = "similar"
        score -= 15
    return {"score": max(0, min(95, round(score))), "kind": kind}  # 100 = same barcode or confirmed


def best_match(product: dict[str, Any], candidates: list[dict[str, Any]], skip: set[str] | frozenset = frozenset()) -> dict[str, Any] | None:
    """The best scoring candidate (not in skip) with score and kind, or None below MATCH_MIN_SCORE."""
    best = None
    for c in candidates:
        if not c.get("article_id") or c["article_id"] in skip:
            continue
        m = {**c, **match_score(product, c)}
        if best is None or m["score"] > best["score"]:
            best = m
    return best if best and best["score"] >= MATCH_MIN_SCORE else None
