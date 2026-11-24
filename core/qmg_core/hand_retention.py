"""Rank the benefit of retaining visible cards against the cost of a hand slot."""
from collections import defaultdict

from .content_actions import BASIC_KINDS
from .model import TURN_ORDER, zone


def is_hand_choice(view, seat, *, forced_play_discard=False):
    decision = view["decision"]
    phase = view["turn"]["phase"]
    if decision:
        return (decision["required_country"] == seat and decision.get("operation_type") == "DISCARD" and
                phase in ("Setup", "Discard"))
    return (phase == "Discard" or forced_play_discard) and TURN_ORDER[view["turn"]["country_index"]] == seat


def score_retention(rules, state, seat, settings, short_term, basic_cost=None):
    hand = state.positions[zone(seat, "HAND")]
    limit = rules.settings["hand_limit"]
    crowded = len(hand) >= max(3, limit - 1)
    basic = defaultdict(list)
    values = {}
    for key in hand:
        card = rules.cards[state.tokens[key].definition_id]
        value = short_term(card)
        kind = card.get("legacy_kind")
        values[key] = {"short_term": value, "slot_cost": -settings["slot_cost"],
                       "basic_reserve": 0.0, "redundancy": 0.0}
        if kind in BASIC_KINDS:
            basic[kind].append(key)
            if basic_cost:
                values[key]["used_basic_cost"] = basic_cost(card)
    basic_count = sum(map(len, basic.values()))
    for copies in basic.values():
        copies.sort(key=lambda key: (-values[key]["short_term"], key))
        # A readily useful basic move justifies keeping a second copy for the next turn.
        reserve = 2 if values[copies[0]]["short_term"] >= settings["slot_cost"] else 1
        for index, key in enumerate(copies):
            terms = values[key]
            terms["basic_reserve"] = max(0.0, settings["basic_floor"] - terms["short_term"])
            if crowded and index >= reserve:
                excess = index - reserve + 1
                terms["redundancy"] = -(excess * settings["duplicate_penalty"] +
                                        max(0, basic_count - 3) * settings["basic_congestion"])
    for terms in values.values():
        terms["net"] = sum(terms.values())
    return values
