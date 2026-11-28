"""Reusable work within one search over a seat-visible projection."""
from .model import TURN_ORDER, canonical, zone


class SearchCache:
    def __init__(self, *, incremental=True):
        self.incremental = incremental
        self.adjacencies = {}
        self.distances = {}
        self.enemy_distances = {}
        self.construction_distances = {}
        self.side_ports = {}
        self.supply_reach = {}
        self.map_scores = {}
        self.supply_scores = {}
        self.card_scores = {}
        self.resource_scores = {}


def board_dependencies(rules, state, pieces):
    """Identify the board inputs used by map rules and supply."""
    statuses = tuple(state.tokens[key] for seat in TURN_ORDER for key in state.positions[zone(seat, "STATUS")])
    sources_and_rules = tuple(t for t in state.tokens.values()
                             if (t.token_type == "SCORE_SOURCE" and t.position_id in rules.regions)
                             or (t.token_type == "RULE" and t.position_id in (zone(t.owner_country, "RESPONSE"), "RESOLUTION")))
    return tuple(pieces), state.turn.country_index, statuses, sources_and_rules


def army_reserve_heads(rules, state):
    # A side-port probe places the first reserve army, which can have its own supply rule.
    return tuple(next(iter(state.positions[zone(force, "RESERVE_ARMY")]), None) for force in rules.force_controllers)


def card_dependencies(state):
    """Card forecasts can use the board, card order, phase, scores and visible triggers."""
    return (tuple(state.tokens.values()), tuple((area, tuple(keys)) for area, keys in state.positions.items()),
            tuple(vars(state.turn).values()), tuple(state.scores.items()),
            canonical(state.turn_start), canonical(state.announcements))


def cached_score(cache, key, score):
    if cache is None:
        return score()
    if key not in cache:
        cache[key] = score()
    return cache[key]
