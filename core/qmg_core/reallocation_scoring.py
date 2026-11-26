"""Price the exchange using visible payments and eligible basic cards."""

from .content_actions import BASIC_KINDS
from .model import zone


def acquisitions(rules, view, seat, *, include_payment=False):
    info = view["reallocation"]
    representatives = {}
    for key, card in sorted(rules.cards.items()):
        if card["country"] == seat and card.get("legacy_kind") in BASIC_KINDS:
            representatives.setdefault(card["legacy_kind"], key)
    for kind, key in representatives.items():
        if info["deck"].get(kind, 0) > 0:
            yield "deck", key, None
    if info["discard_allowed"]:
        known = view["positions"][zone(seat, "DISCARD")].get("tokens", [])
        if include_payment:
            known = [*known, *view["positions"][zone(seat, "HAND")].get("tokens", [])]
        for target in sorted(known):
            definition = view["tokens"][target]["definition_id"]
            if rules.cards[definition].get("legacy_kind") in BASIC_KINDS:
                yield "discard", definition, target


def payment_plans(rules, view, seat, discard_value, *, expansion_cost=None):
    info = view.get("reallocation")
    if not info:
        return
    hand = view["positions"][zone(seat, "HAND")].get("tokens", [])
    cards = view["tokens"]
    cost = info["cost"]
    paid = set(info.get("payment", ()))
    if len(hand) < cost or len(paid) > cost:
        return
    fees = {key: discard_value(rules.cards[cards[key]["definition_id"]]) for key in hand}
    cheap = sorted(hand, key=lambda key: (-fees[key], key))

    for source, definition, target in acquisitions(rules, view, seat, include_payment=True):
        kind = rules.cards[definition].get("legacy_kind")
        fixed = paid | ({target} if target in fees else set())
        if len(fixed) > cost:
            continue
        if expansion_cost is None:
            fixed.update([key for key in cheap if key not in fixed][:cost - len(fixed)])
        else:
            while len(fixed) < cost:
                next_card = min((key for key in cheap if key not in fixed),
                                key=lambda key: (-fees[key] + expansion_cost(seat, fixed | {key}), key))
                fixed.add(next_card)
        payment = sorted(fixed, key=lambda key: (-fees[key], key))
        loss = expansion_cost(seat, fixed) if expansion_cost is not None else 0
        yield {"source": source, "definition": definition, "target": target, "kind": kind,
               "payment": payment, "discard_score": sum(fees[key] for key in payment) - loss,
               **({"discard_expansion_cost": loss} if expansion_cost is not None else {})}


def extra_refill(economy, seat, plan, *, spend=1):
    hand, deck = (economy.count(seat, area) for area in ("HAND", "DECK"))
    limit = economy.rules.settings["hand_limit"]
    baseline = min(deck, max(0, limit - hand + spend))
    after = min(max(0, deck - int(plan["source"] == "deck")),
                max(0, limit - hand + len(plan["payment"]) - 1 + spend))
    return max(0, after - baseline)


def price(plan, play_score):
    return {**plan, "play_score": play_score,
            "total": play_score + plan["discard_score"] + plan.get("cycle_score", 0)}


def planned_options(rules, info, plan):
    """Match the estimate to choices actually offered by the seat; skip obsolete plans."""
    if "payment" in info:
        paid = set(info["payment"])
        if len(plan["payment"]) == info["cost"] and paid <= set(plan["payment"]):
            return [key for key in plan["payment"] if key not in paid] or ["confirm"]
    if "sources" in info:
        return [key for key, source in info["sources"].items() if source == plan["source"]]
    if "targets" in info and info["source"] == plan["source"]:
        if plan["target"] in info["targets"]:
            return [plan["target"]]
        return [key for key, definition in info["targets"].items()
                if rules.cards[definition].get("legacy_kind") == plan["kind"]]
    return []
