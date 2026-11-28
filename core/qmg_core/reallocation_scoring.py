"""Price the exchange using visible payments and eligible basic cards."""

from .content_actions import BASIC_KINDS
from .content_effects import condition_met
from .model import zone
from .rules import LegalActionGenerator, status_enabled


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


def typed_discard_kinds(rules, state, seat, effects):
    for effect in effects:
        if effect.get("chooser", seat) != seat or not condition_met(rules, state, seat, effect.get("when")):
            continue
        if effect["type"] == "discard-hand" and effect.get("strict") and effect["count"]:
            yield from (kind for kind in effect.get("kinds", ()) if kind in BASIC_KINDS)
        if effect["type"] == "choose-one":
            for branch in effect["options"]:
                yield from typed_discard_kinds(rules, state, seat, branch["effects"])


def discard_sources(rules, state, seat):
    """Known own cards and ready listeners that require a specific basic as payment."""
    sources = {kind: set() for kind in BASIC_KINDS}

    def collect(source, steps, trigger=None):
        card = rules.cards[source.definition_id]
        if card.get("implementation", {}).get("status") == "blocked" or card.get("simulation_unknown"):
            return
        if not condition_met(rules, state, seat, card.get("content_condition")):
            return
        effects = (step["effect"] for step in steps if step["type"] == "CARD_EFFECT")
        for kind in typed_discard_kinds(rules, state, seat, effects):
            sources[kind].add(source.token_id)
        if (trigger and trigger["type"] == "discard-stage-card" and trigger["country"] == seat
                and trigger["kind"] in sources):
            sources[trigger["kind"]].add(source.token_id)

    for key in state.positions[zone(seat, "HAND")]:
        token = state.tokens[key]
        card = rules.cards[token.definition_id]
        if card.get("legacy_kind") not in ("bolster", "air-power"):
            collect(token, card["effects"])
    for area in ("ABILITY_READY", "RESPONSE"):
        for key in state.positions.get(zone(seat, area), ()):
            token = state.tokens[key]
            if token.token_type != "ABILITY" or token.source_token_id not in state.tokens:
                continue
            source = state.tokens[token.source_token_id]
            definition = rules.cards.get(source.definition_id)
            if definition is None:
                continue
            ability = rules.abilities[token.definition_id]
            if source.position_id not in {zone(seat, scope) for scope in ability.get("listener_scope", ("STATUS", "RESPONSE"))}:
                continue
            if definition["type"] == "STATUS" and not status_enabled(rules, state, source):
                continue
            collect(source, ability["effects"], ability["trigger"].get("card_trigger"))
    return {kind: keys for kind, keys in sources.items() if keys}


def shortlist_acquisitions(rules, state, seat, choices, play_value):
    legal = LegalActionGenerator(rules)
    definitions = dict.fromkeys(definition for _, definition, _ in choices)
    scores = {definition: play_value(rules.cards[definition]) for definition in definitions
              if legal.can_start_card(state, seat, rules.cards[definition])}
    best = max(scores, key=scores.get, default=None)
    direct = rules.cards[best]["legacy_kind"] if best is not None else None
    costs = discard_sources(rules, state, seat)
    return [choice for choice in choices if rules.cards[choice[1]]["legacy_kind"] == direct
            or rules.cards[choice[1]]["legacy_kind"] in costs], direct, costs


def payment_plans(rules, view, seat, discard_value, *, choices, direct, cost_sources, expansion_cost=None):
    info = view.get("reallocation")
    if not info or not choices:
        return
    hand = view["positions"][zone(seat, "HAND")].get("tokens", [])
    cards = view["tokens"]
    cost = info["cost"]
    paid = set(info.get("payment", ()))
    if len(hand) < cost or len(paid) > cost:
        return
    fees = {key: discard_value(rules.cards[cards[key]["definition_id"]]) for key in hand}
    cheap = sorted(hand, key=lambda key: (-fees[key], key))

    for source, definition, target in choices:
        kind = rules.cards[definition].get("legacy_kind")
        fixed = paid | ({target} if target in fees else set())
        if len(fixed) > cost:
            continue
        usable = cost_sources.get(kind, set()) - fixed
        deployed = usable - fees.keys()
        if usable and not deployed and len(hand) > cost:
            protected = min(usable, key=lambda key: (fees[key], key))
        else:
            protected = None
        if kind != direct and protected is None and not deployed:
            continue
        available = [key for key in cheap if key != protected]
        if expansion_cost is None:
            fixed.update([key for key in available if key not in fixed][:cost - len(fixed)])
        else:
            while len(fixed) < cost:
                next_card = min((key for key in available if key not in fixed),
                                key=lambda key: (-fees[key] + expansion_cost(seat, fixed | {key}), key))
                fixed.add(next_card)
        payment = sorted(fixed, key=lambda key: (-fees[key], key))
        loss = expansion_cost(seat, fixed) if expansion_cost is not None else 0
        payment_for = sorted(usable - fixed)
        yield {"source": source, "definition": definition, "target": target, "kind": kind,
               "payment": payment, "discard_score": sum(fees[key] for key in payment) - loss,
               **({"payment_for": payment_for} if payment_for else {}),
               **({"payment_only": True} if kind != direct else {}),
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
