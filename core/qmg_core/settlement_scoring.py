"""Score completed B branches from settled effects and durable board assets."""
from collections import defaultdict
from dataclasses import dataclass, replace
from time import perf_counter

from .api import CoreAPI
from .content_effects import condition_met, point_award
from .economic_scoring import POINT_EFFECTS
from .engine import AbilityScanner, CommandProcessor, ResolutionBudgetExceeded
from .expansion_scoring import ConstructionNetwork
from .model import TURN_ORDER, ZoneRegistry, zone
from .operations import OperationExecutor, ScoreResolver, outcome
from .rules import SupplyResolver, board_units


@dataclass(frozen=True)
class PositionValue:
    actual_vp: float
    map_value: float
    card_value: float
    resource_adjustment: float
    total: float
    terminal_rank: int
    score_points: tuple
    token_influence: tuple
    unit_removal_score: float
    card_event_value: float
    surviving_units: tuple = ()
    expansion_sites: tuple = ()
    expansion_score: float = 0


class SettlementScorer:
    def __init__(self, units, expansion_vp=0):
        self.units = units
        self.expansion_vp = expansion_vp

    @staticmethod
    def closing_positions(rules, settled, deadline):
        """Verify survival through the active turn using only forced cleanup."""
        probe = CommandProcessor(rules, settled, search_projection=True)
        current = settled.turn.turn_id
        client = CoreAPI(probe, (TURN_ORDER[settled.turn.country_index],))
        state = settled
        try:
            for _ in range(len(rules.phases) + 4):
                if state.winner or state.turn.turn_id != current:
                    return {t.token_id: t.position_id for t in board_units(rules, state)}
                if deadline is not None and perf_counter() >= deadline:
                    return None
                if state.decision:
                    decision = state.decision
                    if decision.optional_flag or len(decision.legal_options) != 1:
                        return None
                    responder = CoreAPI(probe, (decision.required_country,))
                    command = next((c for c in responder.legal_commands() if c["type"] == "CHOOSE"), None)
                else:
                    responder = client
                    command = next((c for c in client.legal_commands() if c["type"] == "END_PHASE"), None)
                if command is None:
                    return None
                responder.apply(command)
                state = probe.state
        except ResolutionBudgetExceeded:
            return None
        return None

    def surviving_board(self, rules, state, deadline):
        resolver = SupplyResolver(rules)
        active = TURN_ORDER[state.turn.country_index]
        pieces = board_units(rules, state)
        checked = False
        proven = None
        while pieces:
            supplies = {seat: resolver.supplied_for_seat(state, seat, pieces) for seat in TURN_ORDER}
            unsupplied = [t for t in pieces if t.token_id not in supplies[rules.force_controllers[t.owner_country]]]
            if not unsupplied and proven is None:
                return pieces, supplies
            if not checked:
                can_verify = any(rules.force_controllers[t.owner_country] == active for t in unsupplied)
                proven = self.closing_positions(rules, state, deadline) if can_verify and not state.winner else None
                checked = True
            pending = {t.token_id for t in unsupplied}
            unproven = []
            for token in pieces:
                seat = rules.force_controllers[token.owner_country]
                if seat == active and proven is not None:
                    if proven.get(token.token_id) != token.position_id:
                        unproven.append(token)
                elif token.token_id in pending:
                    unproven.append(token)
            if not unproven:
                return pieces, supplies
            for token in unproven:
                kind = rules.units[token.definition_id]["kind"]
                ZoneRegistry.transfer(state, token.token_id, zone(token.owner_country, "RESERVE_" + kind))
            pieces = board_units(rules, state)
        return (), {}

    @staticmethod
    def scoring_listeners(rules, state):
        listeners = defaultdict(list)
        for seat in TURN_ORDER:
            deployed = state.positions[zone(seat, "STATUS")]
            for key in state.positions[zone(seat, "ABILITY_READY")]:
                token = state.tokens[key]
                if token.source_token_id not in deployed:
                    continue
                ability = rules.abilities[token.definition_id]
                if ability["trigger"].get("card_trigger", {}).get("type") == "during-victory" and (
                        ability["effects"] and all(step["type"] == "CARD_EFFECT" and
                            step["effect"]["type"] in POINT_EFFECTS for step in ability["effects"])):
                    listeners[seat].append(token)
        return listeners

    @staticmethod
    def status_points(rules, state, seat, tokens):
        if not tokens:
            return {}
        op = {"type": "CHECK_SCORE", "actor_country": seat}
        scan = AbilityScanner(rules)
        executor = OperationExecutor(rules)
        team = rules.team(seat)
        fixed = defaultdict(int)
        best = (0, None)
        for _, _, token, ability in sorted(scan.candidates(state, op, outcome("CHECK_SCORE"), tokens)):
            # Unsettled hand payments and programs that change the board are outside this snapshot.
            if executor.activation_cost(state, token):
                continue
            value = 0
            for step in ability["effects"]:
                effect = step["effect"]
                if condition_met(rules, state, seat, effect.get("when")):
                    recipient, points = point_award(rules, state, seat, effect)
                    value += points if rules.team(recipient) == team else -points
            card = state.tokens[token.source_token_id].definition_id
            key = "status:" + card
            if ability["activation_mode"] == "AUTO":
                fixed[key] += value
            elif value > best[0]:
                best = (value, key)
        if best[1] is not None:
            fixed[best[1]] += best[0]
        return fixed

    def value(self, processor, country, *, initial_units=None, direct_removals=(), constructed=(), played_card=None,
              card_event_value=0, deadline=None):
        state, rules = processor.state, processor.rules
        own = rules.team(country)
        enemy = "allies" if own == "axis" else "axis"
        actual = state.scores[own] - state.scores[enemy]

        def sign(seat):
            return 1 if rules.team(seat) == own else -1

        cards = sum(sign(seat) * len(state.positions.get(zone(seat, area), ()))
                    for seat in TURN_ORDER for area in ("HAND", "STATUS", "DISABLED_STATUS", "RESPONSE"))
        if played_card and state.tokens[played_card].position_id != zone(country, "HAND"):
            cards += 1
        cards = self.units.card_score(cards) + card_event_value
        terminal = 2 if state.winner == own else -2 if state.winner == enemy else 0

        still_on_board = {t.token_id for t in board_units(rules, state)}
        pieces, supplies = self.surviving_board(rules, state, deadline)
        survivors = tuple((t.token_id, t.owner_country) for t in pieces)
        stock = self.units.action_score(sum(sign(owner) for _, owner in survivors))
        removal = 0.0
        if initial_units is not None:
            surviving_ids = {key for key, _ in survivors}
            removal = self.units.action_score(-sum(sign(owner) for key, owner in initial_units.items()
                                                   if key not in surviving_ids))
            # A resolved attack still removed a unit even if that unit had no durable value.
            tactical = self.units.action_score(-sum(sign(state.tokens[key].owner_country) for key in direct_removals
                                                     if key not in initial_units and key not in still_on_board))
            removal += tactical
            stock += tactical
        site_values = {}
        scorer = ScoreResolver(rules)
        listeners = self.scoring_listeners(rules, state)
        for index, seat in enumerate(TURN_ORDER):
            scored = index < state.turn.country_index or (index == state.turn.country_index and
                      state.turn.phase in ("Victory", "Discard", "Draw"))
            if state.winner or (scored and state.turn.round_id == rules.settings["max_rounds"]):
                continue
            # A seat's next scoring window uses its own public rule modifiers.
            snapshot = replace(state, turn=replace(state.turn, country_index=index, phase="Victory"))
            rates = scorer.sites(snapshot, seat, pieces=pieces, supply_cache=supplies)
            rates.update(self.status_points(rules, snapshot, seat, listeners.get(seat)))
            for site, amount in rates.items():
                site_values[site] = site_values.get(site, 0) + sign(seat) * self.units.vp_score(amount)
        points = tuple({"site": site, "value": value} for site, value in site_values.items())
        expansion = ()
        if self.expansion_vp and constructed and not state.winner:
            origins = {t.position_id for t in pieces if t.token_id in constructed and t.owner_country == country}
            if origins:
                expansion = ConstructionNetwork(rules, state, country).score_sites(origins)
        expansion_score = self.units.vp_score(len(expansion) * self.expansion_vp)
        map_value = stock + sum(site_values.values()) + expansion_score
        return PositionValue(float(actual), map_value, cards, 0.0, self.units.vp_score(actual) + map_value + cards,
                             terminal, points, (), removal, card_event_value, survivors, expansion, expansion_score)
