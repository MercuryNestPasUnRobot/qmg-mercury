"""Score completed B branches from settled effects and durable board assets."""
from collections import defaultdict
from dataclasses import dataclass, replace
from time import perf_counter

from .api import CoreAPI
from .content_effects import condition_met, point_award
from .definitions import PER_UNIT_SITE
from .economic_scoring import POINT_EFFECTS
from .engine import AbilityScanner, CommandProcessor, ResolutionBudgetExceeded
from .expansion_scoring import ConstructionNetwork
from .model import TURN_ORDER, ZoneRegistry, zone
from .operations import OperationExecutor, ScoreResolver, outcome
from .rules import SupplyResolver, board_units, status_enabled
from .supply_stability import SupplyStability


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
    temporary_vp: float = 0
    supply_stability: float = 0


class SettlementScorer:
    def __init__(self, units, expansion_vp=0, *, location_weights=None, economy_factory=None, snapshot_value=None):
        self.units = units
        self.expansion_value = units.linked("expansion", units.vp_score(expansion_vp))
        self.location_weights = location_weights
        self.economy_factory = economy_factory
        self.snapshot_value = snapshot_value

    @staticmethod
    def closing_positions(rules, settled, deadline):
        """Verify survival and site income using only the active turn's forced cleanup."""
        probe = CommandProcessor(rules, settled, search_projection=True)
        current = settled.turn.turn_id
        client = CoreAPI(probe, (TURN_ORDER[settled.turn.country_index],))
        state = settled
        try:
            for _ in range(len(rules.phases) + 4):
                if state.winner or state.turn.turn_id != current:
                    points = defaultdict(int)
                    for record in probe.records:
                        if site := record["operation"].get("score_site"):
                            points[site] += sum(change["amount"] for change in record["outcome"]["score_changes"])
                    return {t.token_id: t.position_id for t in board_units(rules, state)}, points
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
                probe.submit(command, responder.controlled_countries, auto_resolve=False)
                state = probe.advance(stop_on_turn_change=True)
        except ResolutionBudgetExceeded:
            return None
        return None

    def surviving_board(self, rules, state, deadline, recruited=()):
        resolver = SupplyResolver(rules)
        active = TURN_ORDER[state.turn.country_index]
        pieces = board_units(rules, state)
        checked = False
        proven = None
        income = {}
        uncertain = set()
        while pieces:
            supplies = {seat: resolver.supplied_for_seat(state, seat, pieces) for seat in TURN_ORDER}
            unsupplied = [t for t in pieces if t.token_id not in supplies[rules.force_controllers[t.owner_country]]]
            if not checked:
                checked = True
                live_recruits = {t.token_id for t in pieces if t.token_id in recruited}
                needs_check = live_recruits or any(rules.force_controllers[t.owner_country] == active for t in unsupplied)
                if needs_check and not state.winner:
                    closure = self.closing_positions(rules, state, deadline)
                    if closure is not None:
                        proven, income = closure
                    else:
                        uncertain = {t.token_id for t in pieces if t.token_id in live_recruits and
                                     rules.force_controllers[t.owner_country] == active}
            pending = {t.token_id for t in unsupplied}
            unproven = []
            for token in pieces:
                seat = rules.force_controllers[token.owner_country]
                if seat == active and proven is not None:
                    if proven.get(token.token_id) != token.position_id:
                        unproven.append(token)
                elif (token.token_id in pending or token.token_id in uncertain or
                      (token.token_id in recruited and proven is not None and
                       proven.get(token.token_id) != token.position_id)):
                    unproven.append(token)
            if not unproven:
                return pieces, supplies, income
            for token in unproven:
                kind = rules.units[token.definition_id]["kind"]
                ZoneRegistry.transfer(state, token.token_id, zone(token.owner_country, "RESERVE_" + kind))
            pieces = board_units(rules, state)
        return (), {}, income

    @staticmethod
    def scoring_listeners(rules, state):
        listeners = defaultdict(list)
        for seat in TURN_ORDER:
            deployed = state.positions[zone(seat, "STATUS")]
            for key in state.positions[zone(seat, "ABILITY_READY")]:
                token = state.tokens[key]
                if token.source_token_id not in deployed:
                    continue
                source = state.tokens[token.source_token_id].definition_id
                if token.definition_id in rules.hidden_score_rules.get(source, {}):
                    continue  # ScoreResolver already evaluates this site's income and homeland gate.
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

    def value(self, processor, country, *, initial_units=None, direct_removals=(), constructed=(), recruited=(), played_card=None,
              card_event_value=0, deadline=None, airdrops=None, project_cleanup=False, played_potential=0):
        state, rules = processor.state, processor.rules
        if self.snapshot_value:
            return self.projected_value(rules, state, country, initial_units, direct_removals, constructed,
                                        recruited, played_card, card_event_value, deadline, airdrops, project_cleanup,
                                        played_potential)
        own = rules.team(country)
        enemy = "allies" if own == "axis" else "axis"
        actual = state.scores[own] - state.scores[enemy]

        def sign(seat):
            return 1 if rules.team(seat) == own else -1

        cards = sum(sign(seat) for seat in TURN_ORDER for area in ("HAND", "STATUS", "DISABLED_STATUS", "RESPONSE")
                    for key in state.positions.get(zone(seat, area), ()) if state.tokens[key].token_type == "CARD")
        if played_card and state.tokens[played_card].position_id != zone(country, "HAND"):
            cards += 1
        cards = self.units.card_score(cards) + card_event_value
        terminal = 2 if state.winner == own else -2 if state.winner == enemy else 0

        board = board_units(rules, state)
        stability_state = replace(state, tokens=dict(state.tokens)) if self.units.supply_stability else state
        still_on_board = {t.token_id for t in board}
        landed = {t.token_id: t for t in board if (airdrops or {}).get(t.token_id) == t.position_id and not any(
            enemy.position_id == t.position_id and rules.team(enemy.owner_country) != rules.team(t.owner_country)
            for enemy in board)}
        pieces, supplies, closing_income = self.surviving_board(rules, state, deadline, recruited)
        survivors = tuple((t.token_id, t.owner_country) for t in pieces)
        stock = 0 if self.location_weights else self.units.action_score(sum(sign(owner) for _, owner in survivors))
        removal = 0.0
        if initial_units is not None:
            surviving_ids = {key for key, _ in survivors}
            removal_unit = self.units.linked("unit_removal", self.units.action)
            removal = -removal_unit * sum(sign(owner) for key, owner in initial_units.items() if key not in surviving_ids)
            # A resolved attack still removed a unit even if that unit had no durable value.
            tactical = -removal_unit * sum(sign(state.tokens[key].owner_country) for key in direct_removals
                                           if key not in initial_units and key not in still_on_board)
            removal += tactical
            stock += removal if self.location_weights else tactical
        site_values = defaultdict(float)
        next_income = defaultdict(float)
        temporary_vp = 0
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
            if recruited and index == state.turn.country_index and not scored:
                temporary_vp = sum(max(0, amount - rates.get(site, 0)) for site, amount in closing_income.items())
                temporary_vp *= sign(seat)
            rates.update(self.status_points(rules, snapshot, seat, listeners.get(seat)))
            for site, amount in rates.items():
                if self.location_weights and site in rules.sites:
                    next_income[site, rules.team(seat)] += self.units.site_score(amount)
                else:
                    value = self.units.site_score(amount) if site.startswith("hidden:") else self.units.vp_score(amount)
                    site_values[site] += sign(seat) * value
        air_support = 0
        landing_attraction = 0
        if self.location_weights and not state.winner:
            from .map_influence import ScoringRoutes
            position = ScoringRoutes(rules, state, pieces, self.location_weights).position
            held_per_unit = defaultdict(float)
            surviving_ids = {key for key, _ in survivors}
            air_distance = self.location_weights.get("status_forecast", {}).get("projected_distance_offset", 1)
            # A regular site contributes the best durable surface position or next income once per side.
            for piece in (*pieces, *(t for key, t in landed.items() if key not in surviving_ids)):
                force = piece.owner_country
                if rules.units[piece.definition_id]["kind"] == "AIR":
                    # Air support uses its position even when the basing unit already holds the site.
                    air_support += sign(force) * (
                        position.value(force, piece.position_id, excluded=piece.token_id, air=True) +
                        (self.units.action if position.regional_defense else 0))
                    continue
                airdrop = piece.token_id in landed
                offset = air_distance if airdrop else 0
                surface = position.site_values(force, piece.position_id, excluded=piece.token_id,
                                               construction=airdrop, distance_offset=offset, enemy_attraction=False)
                if position.regional_defense and (piece.token_id in constructed or piece.token_id in recruited):
                    with_attraction = position.site_values(force, piece.position_id, excluded=piece.token_id,
                                                          construction=airdrop, distance_offset=offset)
                    # A hostile-held point attracts a new approach without granting its income to our side.
                    landing_attraction += sign(force) * (sum(value for _, value in with_attraction.values()) -
                                                        sum(value for _, value in surface.values()))
                for site, (distance, value) in surface.items():
                    if site in rules.sites:
                        key = site, rules.team(force)
                        if (not position.regional_defense and distance == 0 and
                                rules.sites[site].get("scoring") == PER_UNIT_SITE):
                            held_per_unit[key] += value
                        else:
                            next_income[key] = max(next_income[key], value)
            for key, value in held_per_unit.items():
                next_income[key] = max(next_income[key], value)
            site_values.update(position.home_board_values(country))
        for (site, team), value in next_income.items():
            site_values[site] += value if team == own else -value
        points = tuple({"site": site, "value": value} for site, value in site_values.items())
        expansion = ()
        if self.expansion_value and constructed and not state.winner:
            origins = {t.position_id for t in pieces if t.token_id in constructed and t.owner_country == country}
            if origins:
                expansion = ConstructionNetwork(rules, state, country).score_sites(origins)
        expansion_score = len(expansion) * self.expansion_value
        settled_ids = {t.token_id for t in pieces}
        vanished_placements = (set(constructed) | set(recruited)) - settled_ids - landed.keys()
        if self.units.supply_stability:
            for key in vanished_placements:
                stability_state.tokens[key] = state.tokens[key]
        stability_pieces = [t for t in board if t.token_id not in vanished_placements]
        stability = (SupplyStability(rules, stability_state, self.units.supply_stability, pieces=stability_pieces).value(country)
                     if not state.winner else 0)
        map_value = (stock + sum(site_values.values()) + expansion_score + self.units.site_score(temporary_vp) +
                     air_support + landing_attraction + stability)
        resources = 0
        if self.economy_factory and not state.winner:
            economy = self.economy_factory(rules, state)
            resources = -sum(sign(seat) * economy.future_loss(seat) for seat in TURN_ORDER)
            if economy.known_abilities and any(state.positions[zone(seat, "RESPONSE")] for seat in TURN_ORDER):
                responses = economy.responses
                if responses:
                    for seat in TURN_ORDER:
                        for key in state.positions[zone(seat, "RESPONSE")]:
                            card = rules.cards.get(state.tokens[key].definition_id)
                            if card and not card.get("simulation_unknown"):
                                cards += sign(seat) * max(0, responses.staging(seat, card["id"])["net"])
            if economy.status_weights and any(state.positions[zone(seat, "STATUS")] for seat in TURN_ORDER):
                status = economy.statuses
                cards += sum(sign(seat) * sum(status.residual(seat).values()) for seat in TURN_ORDER)
                if economy.continuous_rules:
                    cards += sum(sign(seat) * status.continuous_residual(seat) for seat in TURN_ORDER)
                else:
                    for seat in TURN_ORDER:
                        for key in state.positions[zone(seat, "STATUS")]:
                            source = state.tokens[key]
                            definition = rules.cards[source.definition_id]
                            if (any(mod["kind"] == "always_supplied" for mod in definition.get("modifiers", ()))
                                    and status_enabled(rules, state, source)
                                    and condition_met(rules, state, seat, definition.get("content_condition"))):
                                cards += sign(seat) * self.units.action
        return PositionValue(float(actual), map_value, cards, resources, self.units.vp_score(actual) + map_value + cards + resources,
                             terminal, points, (), removal, card_event_value, survivors, expansion, expansion_score,
                             temporary_vp, stability)

    def projected_value(self, rules, state, country, initial_units, direct_removals, constructed, recruited,
                        played_card, card_event_value, deadline, airdrops, project_cleanup, played_potential):
        if state.winner or not project_cleanup:
            return self.snapshot_value(rules, state, country, deadline=deadline)

        board = {t.token_id: t for t in board_units(rules, state)}
        pieces, supplies, closing_income = self.surviving_board(rules, state, deadline, recruited)
        surviving = {t.token_id: t.owner_country for t in pieces}
        team = rules.team(country)
        sign = lambda owner: 1 if rules.team(owner) == team else -1
        removal = -self.units.linked("unit_removal", self.units.action) * (
            sum(sign(owner) for key, owner in (initial_units or {}).items() if key not in surviving) +
            sum(sign(state.tokens[key].owner_country) for key in direct_removals
                if key not in (initial_units or {}) and key not in board))
        temporary_vp = 0
        active = TURN_ORDER[state.turn.country_index]
        if recruited and closing_income and state.turn.phase not in ("Victory", "Discard", "Draw"):
            rates = ScoreResolver(rules).sites(state, active, pieces=pieces, supply_cache=supplies)
            temporary_vp = sign(active) * sum(max(0, amount - rates.get(site, 0)) for site, amount in closing_income.items())
        expansion = ()
        if constructed and self.expansion_value:
            origins = {t.position_id for t in pieces if t.token_id in constructed and t.owner_country == country}
            if origins:
                expansion = ConstructionNetwork(rules, state, country).score_sites(origins)

        for key, region in (airdrops or {}).items():
            # A response/status airdrop has the same location estimate used by A.
            if key not in surviving and key in board and board[key].position_id == region:
                ZoneRegistry.transfer(state, key, region)
        value = self.snapshot_value(rules, state, country, airdrops=airdrops,
                                    placements=set(constructed) | set(recruited), deadline=deadline)
        map_adjustment = removal + self.units.site_score(temporary_vp) + len(expansion) * self.expansion_value
        card_adjustment = card_event_value
        if played_card and state.tokens[played_card].position_id != zone(country, "HAND"):
            card_adjustment += self.units.card + played_potential
        return replace(value, map_value=value.map_value + map_adjustment,
                       card_value=value.card_value + card_adjustment, total=value.total + map_adjustment + card_adjustment,
                       unit_removal_score=removal, card_event_value=card_event_value,
                       surviving_units=tuple(surviving.items()), expansion_sites=expansion,
                       expansion_score=len(expansion) * self.expansion_value, temporary_vp=temporary_vp)
