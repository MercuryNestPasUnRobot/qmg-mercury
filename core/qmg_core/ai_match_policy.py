"""Seat-local rule policy and time-bounded, observation-only candidate search."""
import json
import multiprocessing
from itertools import takewhile
from time import perf_counter

from .api import CoreAPI, observed_position
from .content_effects import available_options, condition_met
from .economic_scoring import self_deck_operations
from .engine import CommandProcessor
from .evaluation import OneStepPlanner, acceptable_play, candidate_key
from .definitions import BATTLES, CONSTRUCTION
from .hand_retention import is_hand_choice
from .model import CoreError, TURN_ORDER, zone
from .recruitment import recruitment_play
from .response_scoring import is_delayed
from .rules import LegalActionGenerator
from .status_scoring import is_airdrop, is_status
from .reallocation_scoring import acquisitions, extra_refill, payment_plans, planned_options, price
from .score_units import ACTION_SUPPORT_EFFECTS, ScoreUnits, VALUED_ACTIONS, equivalent_action


ACTION_PRIORITY = {"GAIN_VP": 0.75, "BUILD_ARMY": 1.6, "RECRUIT": 1.6, "BUILD_NAVY": 1.4,
                   "LAND_BATTLE": 1.3, "SEA_BATTLE": 1.3, "DRAW": 0.45}
EFFECT_PRIORITY = {"choose-space": 1.55, "recruit": 1.55, "build": 1.55, "battle": 1.3,
                   "battle-series": 1.3, "choose-unit": 1.2, "air-action": 1.0,
                   "discard-top": 0.25, "play-from-zone": 0.6, "search-deck": 0.35}
BATTLE_BUILD = {"LAND_BATTLE": "BUILD_ARMY", "SEA_BATTLE": "BUILD_NAVY"}
DEFAULT_BUDGET_MS = 3000


def operation_priority(kind, units):
    if units.enabled and kind == "DRAW":
        return units.card
    if units.enabled and kind == "GAIN_VP":
        return units.vp
    value = ACTION_PRIORITY.get(kind, 0.25)
    return units.action_score(value / 2) if kind in VALUED_ACTIONS else value


def effect_priority(effect, team, economy=None, seat=None, *, closeout=False, frontier=False, units=ScoreUnits(),
                    spent_card=None, tactical=False):
    kind = effect["type"]
    if economy:
        dynamic = economy.effect(seat, effect, ACTION_PRIORITY["GAIN_VP"], closeout=closeout)
        if dynamic is not None:
            return dynamic
        if tactical:
            location = economy.routes.tactical_effect(seat, {"type": "CARD_EFFECT", "effect": effect}, spent_card=spent_card)
            if location is not None:
                return location
        if economy.map_weights and economy.map_weights.get("action_distance_scoring"):
            if kind == "choose-unit":
                state = economy.visible_state()
                if not condition_met(economy.rules, state, seat, effect.get("when")):
                    return 0
                return max((economy.routes.removal_value(token) *
                            (1 if economy.rules.team(state.tokens[token].owner_country) != team else -1)
                            for token in available_options(economy.rules, state, seat, effect)), default=0)
            if kind == "battle":
                return (economy.routes.best_action_bonus(seat, {"type": "CARD_EFFECT", "effect": effect})
                        if frontier else units.action)
            if kind == "battle-series":
                state = economy.visible_state()
                targets = available_options(economy.rules, state, seat, effect)
                values = sorted((economy.routes.removal_value(token) for token in targets
                                 if economy.rules.team(state.tokens[token].owner_country) != team), reverse=True)
                return sum(values[:effect["max"]])
            if kind in ("eliminate-built-unit", "eliminate-air-superiority-attacker"):
                return units.action
    if kind == "points":
        return effect["amount"] * operation_priority("GAIN_VP", units) * (1 if effect["side"] in ("self", team) else -1)
    if kind == "choose-one":
        chooser = effect.get("chooser", seat)
        friendly_choice = frontier and economy.rules.team(chooser) == team
        acting_seat = chooser if friendly_choice else seat
        return max((sum(effect_priority(e, team, economy, acting_seat, closeout=closeout,
                                        frontier=friendly_choice and i == 0, units=units, spent_card=spent_card, tactical=tactical)
                        for i, e in enumerate(option["effects"]))
                    for option in effect["options"]), default=0)
    if units.enabled and kind == "battle" and effect.get("kind") == "either":
        return max(effect_priority({**effect, "kind": unit}, team, economy, seat, closeout=closeout,
                                   frontier=frontier, units=units, spent_card=spent_card, tactical=tactical) for unit in ("army", "navy"))
    action = equivalent_action(effect, seat) if units.enabled else None
    value = EFFECT_PRIORITY.get(kind, 0.2)
    if action:
        value = operation_priority(action, units)
    elif kind in ACTION_SUPPORT_EFFECTS or kind in ("air-action", "battle-series", "play-from-zone"):
        value = units.action_score(value / 2)
    elif kind == "search-deck":
        value = units.card_score(value)
    if frontier and kind in ("choose-space", "recruit", "build", "battle"):
        if action in CONSTRUCTION and economy.routes.conservative_placement:
            return economy.routes.best_action_bonus(seat, {"type": "CARD_EFFECT", "effect": effect},
                                                     placement_base=value, spent_card=spent_card)
        attack_premium = 0
        if kind == "battle" and economy.map_weights.get("mirrored_attacks"):
            matched = operation_priority(BATTLE_BUILD[action], units) if action in BATTLES else EFFECT_PRIORITY["choose-space"]
            attack_premium = matched - value
        value += economy.routes.best_action_bonus(seat, {"type": "CARD_EFFECT", "effect": effect},
                                                  attack_premium=attack_premium, spent_card=spent_card)
    return value


def paper_play_choices(ranked, mandatory):
    if not mandatory:
        return ranked
    normal = ("PLAY_CARD", "USE_STATUS")
    best = next((r for r in ranked if r["command"]["type"] in normal), None)
    if best is None:
        return ranked
    return [r for r in ranked if r["command"]["type"] == "REALLOCATE" or
            (r["command"]["type"] in normal and r.get("terminal_priority", 0) == best.get("terminal_priority", 0)
             and r["rule_score"] == best["rule_score"])]


class RulePolicy:
    """One policy serves as both the comparator and the live fall-back."""
    def __init__(self, rules, evaluator=None):
        self.rules = rules
        self.evaluator = evaluator
        self.units = evaluator.units if evaluator else ScoreUnits()
        self.recruitment_planner = None
        self.sites = {}
        for site in rules.sites.values():
            region = site["region"]
            self.sites[region] = self.sites.get(region, 0) + site["vp"]

    def instructions(self, steps, seat, economy, *, closeout=False, spent_card=None, economic=False):
        team = self.rules.team(seat)
        value = 0
        frontier = bool(closeout and economy and economy.map_weights)
        tactical = bool(economy and self.evaluator and self.evaluator.action_opportunity)
        drawn = 0
        mills = economy.economic_mill_values(seat, steps) if economic else {}
        for index, step in enumerate(steps):
            op = step.get("operation", step)
            if index in mills:
                value += mills[index]
            elif op["type"] == "CARD_EFFECT":
                value += effect_priority(op["effect"], team, economy, seat, closeout=closeout, frontier=frontier,
                                         units=self.units, spent_card=spent_card, tactical=tactical)
            elif op["type"] == "GAIN_VP" and (economy or self.units.enabled):
                recipient = op.get("score_country") or seat
                delta = op["amount"] if self.rules.team(recipient) == team else -op["amount"]
                value += (economy.points(seat, delta, ACTION_PRIORITY["GAIN_VP"], closeout=closeout)
                          if economy else self.units.vp_score(delta))
                frontier = frontier and not economy.closes_game(seat, delta)
                continue
            else:
                if tactical and op["type"] == "DRAW":
                    count = op.get("count", 1)
                    value += economy.draw_value(seat, count, drawn)
                    drawn += count
                    continue
                if tactical and (location := economy.routes.tactical_effect(seat, op, spent_card=spent_card)) is not None:
                    value += location
                    frontier = False
                    continue
                count = op.get("count", 1) if op["type"] == "DRAW" and self.units.enabled else op.get("amount", 1)
                priority = operation_priority(op["type"], self.units)
                if op["type"] == "TRANSFER" and self.evaluator and self.evaluator.action_distance and economy:
                    if (transfer := economy.routes.transfer_value(seat, op)) is not None:
                        value += transfer
                        frontier = False
                        continue
                if self.evaluator and self.evaluator.action_distance and op["type"] in BATTLES:
                    value += economy.routes.best_action_bonus(seat, op) if frontier else self.units.action
                    frontier = False
                    continue
                if frontier and self.evaluator.settlement and op["type"] in CONSTRUCTION:
                    value += economy.routes.best_action_bonus(seat, op, placement_base=priority * count,
                                                              spent_card=spent_card)
                    frontier = False
                    continue
                value += priority * count
                if frontier and op["type"] in CONSTRUCTION | BATTLES:
                    attack_premium = (operation_priority(BATTLE_BUILD[op["type"]], self.units) - priority
                                      if op["type"] in BATTLES and economy.map_weights.get("mirrored_attacks") else 0)
                    value += economy.routes.best_action_bonus(seat, op, attack_premium=attack_premium,
                                                              spent_card=spent_card)
            frontier = False  # Later military targets may depend on the outcome of this instruction.
        return value

    def card_value(self, card, seat, economy, *, active=False, closeout=False, spent_card=None):
        if economy and is_delayed(card) and economy.responses:
            value = economy.responses.staging(seat, card["id"])
            return value["net"] if value["window_remaining"] else min(0, value["net"])
        if economy and economy.status_weights and is_status(card):
            if active:
                return economy.statuses.program(seat, seat, card["active_ability"], None, committed=True)
            return (self.instructions(card["effects"], seat, economy, closeout=closeout, spent_card=spent_card) +
                    economy.statuses.report(seat, card["id"], held=not closeout)["future"])
        if active:
            return self.instructions(self.rules.abilities[card["active_ability"]]["effects"], seat, economy, closeout=closeout)
        if economy and economy.continuous_rules and card.get("legacy_kind") == "economic":
            return self.instructions(card["effects"], seat, economy, closeout=closeout, spent_card=spent_card, economic=True)
        abilities = [self.rules.abilities[a] for a in card.get("abilities", ())]
        airdrop = [ability for ability in abilities if economy and economy.status_weights and
                   self.evaluator.action_distance and is_airdrop(ability)]
        return (self.instructions(card["effects"], seat, economy, closeout=closeout, spent_card=spent_card) +
                sum(max(0, economy.statuses.airdrop_value(seat, ability["id"])) for ability in airdrop) +
                0.35 * sum(self.instructions(a["effects"], seat, economy) for a in abilities if a not in airdrop))

    def discard_value(self, card, seat, economy, view, *, key=None):
        cost = self.evaluator.basic_discard_cost(view, seat, card) if self.evaluator else 0
        if key and economy and self.evaluator.marginal_opportunity:
            cost += economy.routes.expansion_discard_cost(seat, (key,))
        if self.units.enabled:
            cost += self.units.card
        return -max(0, self.card_value(card, seat, economy)) - cost

    def discard_phase_options(self, view, seat, economy, retention, *, draw_slot, replacement):
        if not retention:
            return {}
        penalty = self.rules.settings.get("discard_phase_penalty", 0)
        phase = view["turn"]["phase"]
        saved = self.units.vp_score(penalty) if phase == "Discard" and not view["turn"].get("discarded_in_phase") else 0
        # Each choice is compared with stopping now, so only new draw slots count.
        marginal = self.evaluator.marginal_opportunity
        cycling = self.evaluator.cycle_value(economy, seat, int(bool(replacement and (marginal or penalty))), retention)
        cycle = cycling["cycle_score"]
        exhaustion = (self.units.vp_score(self.rules.settings["deck_exhaustion_vp"])
                      if (marginal or penalty) and draw_slot and not replacement else 0)
        result = {}
        for key, terms in retention.items():
            card = self.rules.cards[view["tokens"][key]["definition_id"]]
            gain = (economy.statuses.discard_gain(seat, card) if phase == "Discard" and economy and
                    self.evaluator.action_distance and economy.status_weights else 0)
            cost = terms["net"] if replacement else (
                max(0, terms["short_term"]) + terms["basic_reserve"] + terms.get("used_basic_cost", 0) +
                terms.get("expansion_cost", 0) + self.units.card)
            score = saved + cycle + gain + exhaustion - cost
            result[key] = (score, {"hand_average": cycling["hand_average"], "extra_draws": int(replacement),
                                   "draw_cycle": cycle, "penalty_saved": saved, "discard_trigger": gain,
                                   "exhaustion": exhaustion, "discard_cost": cost})
        return result

    def play_card_value(self, card, seat, economy, *, spent_card=None, charge_action=True):
        cost = self.evaluator.card_play_cost if self.evaluator else 0
        if self.evaluator and charge_action:
            cost += self.evaluator.action_cost("PLAY_CARD", "Play")
        return self.card_value(card, seat, economy, closeout=True, spent_card=spent_card) - cost

    def terminal_priority(self, view, command, seat, economy):
        if not self.units.enabled or not economy or command["type"] not in ("PLAY_CARD", "USE_STATUS") or command.get("play_as"):
            return 0
        card = self.rules.cards[view["tokens"][command["card"]]["definition_id"]]
        steps = (self.rules.abilities[card["active_ability"]]["effects"] if command["type"] == "USE_STATUS"
                 else card["effects"])
        if not steps:
            return 0
        delta = economy.direct_points(seat, steps[0].get("operation", steps[0]))
        return (1 if delta > 0 else -1) if delta and economy.closes_game(seat, delta) else 0

    def reallocation_plan(self, view, seat, economy):
        if economy and self.evaluator.marginal_opportunity:
            plans = payment_plans(self.rules, view, seat, lambda card: self.discard_value(card, seat, economy, view),
                                  expansion_cost=economy.routes.expansion_discard_cost)
            return max((price({**plan, **economy.cycle_value(seat, extra_refill(economy, seat, plan))},
                              self.play_card_value(self.rules.cards[plan["definition"]], seat, economy)) for plan in plans),
                       key=lambda plan: plan["total"], default=None)
        cycling = (self.evaluator.cycle_value(economy, seat, view["reallocation"]["cost"], self.evaluator.retention(view, seat))
                   if self.evaluator and self.evaluator.hand_slot_cost else {})
        plans = payment_plans(self.rules, view, seat, lambda card: self.discard_value(card, seat, economy, view))
        return max((price({**plan, **cycling}, self.play_card_value(self.rules.cards[plan["definition"]], seat, economy))
                    for plan in plans),
                   key=lambda plan: plan["total"], default=None)

    def response_options(self, view, seat, economy):
        decision = view.get("decision") or {}
        sources = decision.get("response_sources", {})
        if not sources or not economy or not economy.responses:
            return {}
        rows = {}
        for option, source in sources.items():
            token = view["tokens"].get(source)
            if token and is_delayed(self.rules.cards[token["definition_id"]]):
                rows[option] = economy.responses.activation(seat, source, decision["response_abilities"][option],
                    decision.get("response_event"), decision.get("response_unit_moves", ()))
        return rows

    def projected_deck_operations(self, view, seat, key, kind):
        rules, state = observed_position(self.evaluator.rules, view)
        processor = CommandProcessor(rules, state, search_projection=True)
        client = CoreAPI(processor, (seat,))
        command = next((c for c in client.legal_commands() if c["type"] == kind and c.get("card") == key), None)
        if command is None:
            return None
        deadline = perf_counter() + 0.25
        try:
            for _ in range(min(8, self.evaluator.weights["max_simulation_nodes"])):
                client.apply(command)
                projected = client.observe(include_history=False)
                decision = projected["decision"]
                if not decision:
                    counts = []
                    for record in processor.records:
                        for move in record["outcome"]["changes"]:
                            if move["from"] == zone(seat, "DECK") and move["to"] in (zone(seat, "HAND"), zone(seat, "DISCARD")):
                                counts.append(("draw" if move["to"] == zone(seat, "HAND") else "discard", 1))
                    return counts
                if decision["required_country"] != seat or decision["decision_type"] == "ORDER_DECK" or perf_counter() >= deadline:
                    return None
                legal = [choice for choice in client.legal_commands() if choice["type"] == "CHOOSE"]
                command = self.rank(projected, legal, verify_recruitment=False)[0]["command"] if legal else None
                if command is None:
                    return None
        except CoreError:
            return None
        return None

    def deck_order_scores(self, order, seat, economy, view=None):
        state = economy.visible_state()
        operations = list(order["known_operations"])
        if TURN_ORDER[state.turn.country_index] == seat and state.turn.phase == "Play" and state.turn.plays_remaining:
            legal = LegalActionGenerator(self.rules)
            plays = []
            for key in state.positions[zone(seat, "HAND")]:
                token = state.tokens[key]
                card = self.rules.cards.get(token.definition_id)
                if card and legal.can_start_card(state, seat, card):
                    plays.append((self.play_card_value(card, seat, economy, spent_card=key), key, "PLAY_CARD", card["effects"]))
            for key in state.positions[zone(seat, "STATUS")]:
                ability = self.rules.cards[state.tokens[key].definition_id].get("active_ability")
                if ability and any(token.source_token_id == key and token.definition_id == ability
                                   for token in (state.tokens[t] for t in state.positions[zone(seat, "ABILITY_READY")])):
                    effects = self.rules.abilities[ability]["effects"]
                    if legal.can_start_card(state, seat, {**self.rules.cards[state.tokens[key].definition_id],
                                                        "effects": effects}, activating=True):
                        plays.append((self.instructions(effects, seat, economy, closeout=True) - self.units.action, key, "USE_STATUS", effects))
            if plays:
                def choose_branch(options):
                    return max(options, key=lambda option: self.instructions(option["effects"], seat, economy, closeout=True))
                _, key, kind, effects = max(plays, key=lambda play: (play[0], play[1]))
                projected = self.projected_deck_operations(view, seat, key, kind) if view else None
                operations.extend(projected if projected is not None else self_deck_operations(
                    self.rules, state, seat, effects, choose_branch=choose_branch))
        slot = order["selected"]
        discard = False
        for kind, count in operations:
            if slot < count:
                discard = kind == "discard"
                break
            slot -= count
        sign = -1 if discard else 1
        return {key: sign * self.card_value(self.rules.cards[definition], seat, economy, closeout=True)
                if definition in self.rules.cards and not self.rules.cards[definition].get("simulation_unknown") else 0
                for key, definition in order["options"].items()}

    def choice_value(self, view, command, region_scores, economy):
        option = command["option"]
        seat = command["actor"]
        decision = view["decision"]
        kind, op = decision["decision_type"], decision["operation_type"]
        sources = decision.get("response_sources", {})
        if economy and self.evaluator.action_distance and "return_discard_cost" in decision:
            cost = decision["return_discard_cost"]
            return self.units.card_score(1 - cost) if option == "return" else 0
        if sources and economy and economy.status_weights:
            key = decision.get("response_abilities", {}).get(option)
            source = view["tokens"].get(sources.get(option))
            if key and source and is_status(self.rules.cards.get(source["definition_id"], {})):
                return economy.statuses.program(seat, decision.get("event_actor") or seat,
                                                key, decision.get("event_region"), committed=True)
            if key and source and self.evaluator.action_distance:
                if is_airdrop(self.rules.abilities[key]):
                    return economy.statuses.program(seat, decision.get("event_actor") or seat,
                                                    key, decision.get("event_region"), committed=True)
            if option in ("decline", "pass"):
                air_response = self.evaluator.action_distance and any(is_airdrop(self.rules.abilities[key])
                    for key in decision.get("response_abilities", {}).values())
                if air_response or all(is_status(self.rules.cards.get(
                        view["tokens"].get(src, {}).get("definition_id"), {})) for src in sources.values()):
                    return 0
        if option in ("skip", "decline", "pass", "no", "done", "cancel"):
            return -100
        if kind in ("ABILITY", "RESPONSE_ORDER"):
            source = view["tokens"].get(decision.get("response_sources", {}).get(option))
            return 1 + self.card_value(self.rules.cards[source["definition_id"]], seat, economy) if source else 1
        if economy and "deck_discard_count" in decision and option in self.rules.countries:
            count = decision["deck_discard_count"]
            if (economy.continuous_rules and "economic_targets_already_used" in decision and
                    option not in decision["economic_targets_already_used"] and self.rules.team(option) != self.rules.team(seat)):
                from .rules import RuleModifierResolver

                modifiers = RuleModifierResolver(self.rules).resolve(economy.visible_state(), seat)
                count = economy.economic_amount(seat, option, count, modifiers)
            return economy.mill(seat, option, count, ACTION_PRIORITY["GAIN_VP"], closeout=True)
        if option in ("confirm", "air", "discard-top"):
            return 100
        if economy and self.evaluator.action_opportunity:
            if op == "REDEPLOY_SELECTED":
                return max(economy.routes.redeploy_values(seat, option).values(), default=0)
            if op == "REBUILD_UNIT":
                token = decision["redeploy_token"]
                return economy.routes.move_value(view["tokens"][token]["owner_country"], option, token)
            if op == "AIR_REBASE_SELECTED":
                token = decision["air_rebase_token"]
                return economy.routes.move_value(view["tokens"][token]["owner_country"], option, token)
        if op == "AIR_ACTION_SELECTED" and economy and self.evaluator.action_distance and economy.status_weights:
            action, _, region, victim = json.loads(option)
            if victim:
                return economy.routes.removal_value(victim)
            if action in ("deploy", "marshal"):
                position = (economy.routes.air_value(decision.get("air_force", seat), option)
                            if self.evaluator.action_opportunity else 0)
                return position + max((economy.statuses.program(seat, seat, t.definition_id, region, committed=True)
                            for t in economy.visible_state().tokens.values() if t.token_type == "ABILITY" and
                            t.owner_country == seat and t.position_id == zone(seat, "ABILITY_READY") and
                            is_airdrop(self.rules.abilities[t.definition_id]) and
                            economy.visible_state().tokens[t.source_token_id].position_id == zone(seat, "RESPONSE")),
                           default=0)
        if economy and self.evaluator.action_distance and (source := decision.get("score_source")):
            token = economy.visible_state().tokens[source]
            return economy.routes.source_movement_value(seat, token, option)
        if token := view["tokens"].get(option):
            if token["token_type"] == "CARD":
                value = self.card_value(self.rules.cards[token["definition_id"]], seat, economy)
                cost = decision.get("hand_discard") or op in ("DISCARD", "COST_SELECTED") or any(
                    word in kind for word in ("COST", "DISCARD", "PAY", "REALLOCATE"))
                return (self.discard_value(self.rules.cards[token["definition_id"]], seat, economy, view, key=option)
                        if cost or view["turn"]["phase"] in ("Setup", "Discard") else value)
            if token["token_type"] == "UNIT":
                if op == "ELIMINATE" and economy and self.evaluator.action_distance:
                    value = economy.routes.removal_value(option)
                    return value if self.rules.team(token["owner_country"]) != self.rules.team(seat) else -value
                if (kind == "BATTLE_VICTIM" and economy and economy.map_weights and
                        economy.map_weights.get("mirrored_attacks")):
                    return economy.routes.removal_value(option)
                return self.units.vp_score(self.sites.get(token["position_id"], 0)) * (1 if kind == "BATTLE_VICTIM" else -1)
        if option in region_scores:
            return region_scores[option]
        if economy and economy.map_weights and kind == "EXTRA_CARD_MODE" and option in BATTLES | CONSTRUCTION:
            return self.instructions([{"type": option}], seat, economy, closeout=True)
        if economy and economy.map_weights and option in decision.get("effect_branches", {}):
            return self.instructions([{"type": "CARD_EFFECT", "effect": effect}
                                      for effect in decision["effect_branches"][option]], seat, economy, closeout=True)
        if option in self.rules.regions:
            site_value = self.units.vp_score(self.sites.get(option, 0))
            occupants = view["positions"][option].get("tokens", [])
            return site_value + 0.25 * bool(occupants)
        return operation_priority(option, self.units) if option in ACTION_PRIORITY else 0

    def recruitment_forecasts(self, view, commands, seat, deadline):
        if not self.evaluator or not self.evaluator.settlement or view["resolution_depth"] or view["decision"]:
            return {}
        candidates = []
        for command in commands:
            if command["type"] in ("PLAY_CARD", "USE_STATUS"):
                card = self.rules.cards[view["tokens"][command["card"]]["definition_id"]]
                if recruitment_play(self.rules, card, command):
                    candidates.append(command)
        if not candidates:
            return {}
        if self.recruitment_planner is None:
            self.recruitment_planner = OneStepPlanner(self.rules, self.evaluator.weights)
        if deadline is None:
            deadline = perf_counter() + 2.1
        results = {tuple(sorted(c.items())): {"complete": False, "reason": "deadline"} for c in candidates}
        try:
            for row in self.recruitment_planner.iter_rank(CandidateView(view, candidates), seat, deadline=deadline):
                results[tuple(sorted(row["command"].items()))] = row
        except TimeoutError:
            pass
        except CoreError as error:
            for key, row in results.items():
                if not row["complete"] and row["reason"] == "deadline":
                    results[key] = {"complete": False, "reason": str(error)}
        return results

    def rank(self, view, commands, *, recruitment_deadline=None, verify_recruitment=True):
        if not commands:
            return []
        seat = commands[0]["actor"]
        phase = view["turn"]["phase"]
        economy = self.evaluator.economic_from_view(view) if self.evaluator else None
        retain = self.evaluator and self.evaluator.uses_hand_retention
        card_cost = self.evaluator.card_play_cost if self.evaluator else 0
        forced_play_discard = phase == "Play" and any(c["type"] == "DISCARD_CARD" for c in commands)
        retention = (self.evaluator.retention(view, seat)
                     if retain and is_hand_choice(view, seat, forced_play_discard=forced_play_discard) else {})
        decision = view.get("decision") or {}
        responses = self.response_options(view, seat, economy)
        deck_order = (self.deck_order_scores(decision["deck_order"], seat, economy, view)
                      if economy and self.evaluator.marginal_opportunity and "deck_order" in decision else {})
        forecasts = {}
        options = [c["option"] for c in commands if c["type"] == "CHOOSE"]
        if economy and economy.map_weights and decision.get("map_force"):
            regions = {option: option.split("|", 1)[-1] for option in options}
            region_scores = {option: economy.routes.target_value(decision["map_force"], region,
                             battle=decision["map_battle"], construction=not decision["map_battle"])
                             for option, region in regions.items() if region in self.rules.regions}
            if economy.known_abilities and decision.get("map_airdrop_options"):
                from .status_scoring import placement_value

                for option in decision["map_airdrop_options"]:
                    if option in regions:
                        region_scores[option] = placement_value(economy, decision["map_force"], regions[option], airdrop=True)
        else:
            regions = {option for option in options if option in self.rules.regions}
            region_scores = self.evaluator.target_proximity(view, seat, regions) if self.evaluator and regions else {}
        hand = view["positions"][zone(seat, "HAND")]["count"]
        slots = max(0, self.rules.settings["hand_limit"] - hand)
        draw_slot = hand <= self.rules.settings["hand_limit"]
        replacement_available = draw_slot and view["positions"][zone(seat, "DECK")]["count"] > slots
        discards = (self.discard_phase_options(view, seat, economy, retention, draw_slot=draw_slot,
                                               replacement=replacement_available)
                    if phase == "Discard" or (forced_play_discard and self.evaluator and self.evaluator.marginal_opportunity)
                    else {})
        info = view.get("reallocation", {})
        plan = (self.reallocation_plan(view, seat, economy)
                if "payment" in info or any(c["type"] == "REALLOCATE" for c in commands) else None)
        source_values = {}
        if "sources" in info:
            for source, definition, _ in acquisitions(self.rules, view, seat):
                value = self.play_card_value(self.rules.cards[definition], seat, economy)
                source_values[source] = max(value, source_values.get(source, float("-inf")))

        def score(command):
            kind = command["type"]
            if kind == "CHOOSE":
                option = command["option"]
                if option in deck_order:
                    return deck_order[option]
                if option in responses:
                    estimate = responses[option]
                    forecasts[option] = estimate
                    if estimate["immediate"] <= 0:
                        return min(-0.001, estimate["immediate"])
                    return estimate["immediate"] - (estimate["wait"]["net"] if estimate["wait"] else 0)
                if responses and option in ("decline", "pass"):
                    return 0
                if option in retention:
                    return -retention[option]["net"]
                if option in info.get("targets", {}):
                    return self.play_card_value(self.rules.cards[info["targets"][option]], seat, economy)
                if option in info.get("sources", {}):
                    return source_values.get(info["sources"][option], -10)
                return self.choice_value(view, command, region_scores, economy)
            if kind == "END_PHASE":
                if retain and phase in ("Play", "Air", "Discard"):
                    return 0
                return -1
            if kind == "DISCARD_CARD":
                if command["card"] in discards:
                    return discards[command["card"]][0]
                net = retention.get(command["card"], {}).get("net", 0)
                if forced_play_discard:
                    return -net if retention else self.discard_value(
                        self.rules.cards[view["tokens"][command["card"]]["definition_id"]], seat, economy, view)
                return discards.get(command["card"], (-1, None))[0]
            if kind == "FORFEIT_PLAY":
                return self.units.vp_score(-1)
            if kind == "REALLOCATE":
                return plan["total"] if plan else -2
            if kind == "ACTIVATE_CARD":
                source = command["card"]
                card = self.rules.cards[view["tokens"][source]["definition_id"]]
                if economy and economy.responses and is_delayed(card):
                    keys = [key for key in card["abilities"] if
                            self.rules.abilities[key]["trigger"].get("card_trigger", {}).get("type") == "any-time"]
                    if keys:
                        estimate = economy.responses.activation(seat, source, keys[0])
                        forecasts[source] = estimate
                        net = estimate["immediate"] - (estimate["wait"]["net"] if estimate["wait"] else 0)
                        return net if net > 0 else -101
                return -101
            if kind == "USE_AIR":
                if economy and self.evaluator.action_opportunity:
                    return economy.routes.tactical_effect(seat, {"type": "CARD_EFFECT", "effect": {
                        "type": "air-action", "force": command["force"], "actions": ("deploy", "marshal", "superiority")}}) - card_cost
                return self.units.action_score(0.5) - card_cost
            token = view["tokens"][command["card"]]
            card = self.rules.cards[token["definition_id"]]
            if mode := command.get("play_as"):
                return self.instructions([{"type": mode}], seat, economy, closeout=True, spent_card=command["card"]) - card_cost
            return (self.play_card_value(card, seat, economy, spent_card=command["card"], charge_action=False) if kind == "PLAY_CARD" else
                    self.card_value(card, seat, economy, active=kind == "USE_STATUS", closeout=True))
        rows = []
        for command in commands:
            cost = self.evaluator.action_cost(command["type"], phase) if self.evaluator else 0
            rows.append({"command": command, "rule_score": score(command) - cost,
                         **({"action_cost": -cost} if cost else {})})
        recruitment = (self.recruitment_forecasts(
            view, [row["command"] for row in sorted(rows, key=lambda row: -row["rule_score"])], seat, recruitment_deadline)
            if verify_recruitment else {})
        for row in rows:
            command = row["command"]
            if terminal := self.terminal_priority(view, command, seat, economy):
                row["terminal_priority"] = terminal
            if forecast := recruitment.get(tuple(sorted(command.items()))):
                row["recruitment"] = {key: forecast[key] for key in
                                      ("complete", "reason", "map_delta", "actual_vp_delta", "temporary_vp") if key in forecast}
                row["rule_score"] = forecast["q"] if forecast["complete"] else row.get("action_cost", 0)
                if forecast["complete"]:
                    row["recruitment_result"] = forecast
                    rank = forecast["terminal_rank"]
                    if abs(rank) == 2:
                        row["terminal_priority"] = rank // 2
                    steps = zip(forecast["continuations"], forecast["choice_signatures"])
                    row["recruitment_plan"] = [(c, signature, forecast) for c, signature in takewhile(
                        lambda step: step[0]["type"] == "CHOOSE" and step[0]["actor"] == seat, steps)]
            if plan and command["type"] == "REALLOCATE":
                row["reallocation"] = plan
                if self.evaluator and (cost := self.evaluator.action_cost("PLAY_CARD", "Play")):
                    row["action_cost"] = -cost
            key = command.get("option", command.get("card"))
            if key in retention and command["type"] in ("DISCARD_CARD", "CHOOSE"):
                row["retention"] = {**retention[key], "replacement_available": replacement_available,
                                    **({"forced": True} if forced_play_discard else {})}
                if command["type"] == "DISCARD_CARD" and key in discards:
                    row["retention"].update(discards[key][1])
            if card_cost and command["type"] in ("PLAY_CARD", "USE_AIR"):
                row["initial_card_score"] = -card_cost
            if economy and economy.status_weights and command["type"] == "PLAY_CARD":
                card = self.rules.cards[view["tokens"][command["card"]]["definition_id"]]
                if is_status(card) and not command.get("play_as"):
                    row["status_forecast"] = economy.statuses.report(seat, card["id"])
                elif is_delayed(card) and economy.responses and not command.get("play_as"):
                    row["response_forecast"] = economy.responses.staging(seat, card["id"])
            if key in forecasts:
                row["response_forecast"] = forecasts[key]
                if command["type"] == "ACTIVATE_CARD" and row["rule_score"] > 0:
                    row["immediate_response"] = True
            if (command["type"] == "CHOOSE" and key in region_scores and decision.get("map_force") and
                    not decision["map_battle"] and economy and economy.routes.expansion_vp):
                region = key.split("|", 1)[-1]
                row["expansion"] = economy.routes.expansion(decision["map_force"], region)
        # Canonical tie-breaks also apply to own permitted choices out of the deck.
        def order(row):
            command = row["command"]
            planned = plan and command.get("option") in plan["payment"]
            return (not planned, -row.get("terminal_priority", 0), -row["rule_score"], sorted(command.items()))
        return sorted(rows, key=order)


class CandidateView:
    def __init__(self, view, commands):
        self.view = view
        self.commands = commands

    def observe(self):
        return self.view

    def legal_commands(self):
        return self.commands


def _search_worker(connection, data, weights):
    from .definitions import RuleSet
    with connection:
        planner = OneStepPlanner(RuleSet(data, projected=True), weights)
        connection.send(("ready", None))
        while True:
            try:
                view, commands, country, deadline = connection.recv()
            except EOFError:
                return
            try:
                for row in planner.iter_rank(CandidateView(view, commands), country, deadline=deadline):
                    connection.send(("candidate", row))
            except TimeoutError:
                connection.send(("deadline", None))
            except CoreError as exc:
                connection.send(("error", type(exc).__name__))
            else:
                connection.send(("done", None))


class DeadlineSearch:
    def __init__(self, rules, weights):
        # The worker cannot obtain the original card instances, journal, RNG or any live state.
        self.data = {**rules.data, "initial_tokens": []}
        self.weights = weights
        self.process = self.connection = None
        self.retired = []
        self.context = multiprocessing.get_context("spawn")
        self.ready = False

    def close(self):
        self._retire()
        for process in self.retired:
            process.join()
            process.close()
        self.retired.clear()

    def _retire(self):
        if self.connection is not None:
            self.connection.close()
        if self.process is not None:
            self.process.terminate()
            self.retired.append(self.process)
        self.process = self.connection = None
        self.ready = False

    def _receive(self, deadline):
        remaining = deadline - perf_counter()
        if remaining <= 0 or not self.connection.poll(remaining):
            raise TimeoutError
        message = self.connection.recv()
        if perf_counter() > deadline:
            raise TimeoutError
        return message

    def run(self, view, commands, country, deadline):
        rows = []
        try:
            if perf_counter() >= deadline:
                return rows, "deadline"
            if self.process is None:
                for process in self.retired[:]:
                    if not process.is_alive():
                        process.close()
                        self.retired.remove(process)
                self.connection, worker = self.context.Pipe()
                with worker:
                    process = self.context.Process(target=_search_worker, args=(worker, self.data, self.weights),
                                                   daemon=True)
                    process.start()
                    self.process = process
            if not self.ready:
                if self._receive(deadline)[0] != "ready":
                    raise RuntimeError("Unexpected search startup message")
                self.ready = True
            # Run labels can encode the experiment's seed. The projection needs neither them nor a journal.
            remaining = max(0, deadline - perf_counter())
            soft_deadline = deadline - min(0.075, remaining * 0.1)
            self.connection.send(({**view, "game_id": "ai-projection", "history": []}, commands, country, soft_deadline))
            while True:
                status, payload = self._receive(deadline)
                if status == "candidate":
                    rows.append(payload)
                elif status == "done":
                    return rows, "finished"
                elif status == "deadline":
                    return rows, "deadline"
                else:
                    return rows, "simulation-" + payload
        except TimeoutError:
            self._retire()
            return rows, "deadline"
        except (OSError, EOFError):
            self._retire()
            return rows, "worker-exited"


class SeatPolicy:
    def __init__(self, rules, *, search=None, evaluator=None, width=3, budget_ms=DEFAULT_BUDGET_MS):
        self.rule = RulePolicy(rules, evaluator)
        self.search = search
        self.width = width
        self.budget_ms = budget_ms
        self.plan = []
        self.exchange = None

    def planned_choice(self, view, commands):
        decision = view["decision"]
        if not self.plan or not decision:
            return None
        planned, signature, score = self.plan.pop(0)
        actual = (decision["decision_type"], decision["operation_type"], sorted(decision["legal_options"]))
        # Simulated decision IDs are from a different state. Bind only to the equivalent live window.
        command = {**planned, "decision_id": decision["decision_id"]}
        if "exchange_followup" in score:
            command["expected_version"] = view["version"]
        if signature == actual and command in commands:
            return command, score
        self.plan.clear()
        return None

    def remember_choices(self, score, country, *, acquired=None):
        followup = score.get("exchange_followup") if acquired is not None else None
        steps = followup or score
        target = followup["target"] if followup else None
        self.plan = []
        for choice, signature in takewhile(
                lambda item: item[0]["type"] == "CHOOSE" and item[0]["actor"] == country,
                zip(steps["continuations"], steps["choice_signatures"])):
            if target is not None:
                kind, operation, options = signature
                signature = (kind, operation, sorted(acquired if option == target else option for option in options))
                if choice["option"] == target:
                    choice = {**choice, "option": acquired}
            self.plan.append((choice, signature, score))

    def resume_exchange(self, view, commands, country):
        if self.exchange is None or view["decision"]:
            return None
        _, score, acquired = self.exchange
        self.exchange = None
        followup = score.get("exchange_followup")
        if not followup or acquired is None:
            return None
        play = {**followup["play"], "expected_version": view["version"]}
        if play.get("card") == followup["target"]:
            play["card"] = acquired
        if play in commands:
            self.remember_choices(score, country, acquired=acquired)
            return play, score
        return None

    def decide(self, client, country):
        started = perf_counter()
        view = client.observe(include_history=False)
        commands = client.legal_commands()
        if not commands or any(c["actor"] != country for c in commands):
            raise CoreError("No legal action for the AI seat")
        phase = view["turn"]["phase"]
        compare_exchange = bool(self.search and self.rule.evaluator and self.rule.evaluator.settlement and
                                phase == "Play" and any(c["type"] == "REALLOCATE" for c in commands))
        ranked = self.rule.rank(view, commands, recruitment_deadline=started + min(2.1, self.budget_ms * 0.00075),
                                verify_recruitment=not compare_exchange)
        mandatory = bool(phase == "Play" and self.rule.rules.settings.get("mandatory_turn_play"))
        preferred = paper_play_choices(ranked, mandatory)
        manual = next((row["command"] for row in ranked if row.get("immediate_response")), None)
        response_choice = any(row.get("response_forecast") and row["command"]["type"] == "CHOOSE" for row in ranked)
        chosen = preferred[0]["command"]
        if compare_exchange:
            chosen = next((row["command"] for row in preferred if row["command"]["type"] != "REALLOCATE"), chosen)
        mode, status, tried, rows, selected = "rules", "rules-only", [], [], None
        if manual and not view["decision"]:
            chosen = manual
            self.plan.clear()
            self.exchange = None
            status = "response-timing"
        if len(commands) == 1:
            mode, status = "forced", "only-legal-command"
        forced_play_discard = mandatory and any(c["type"] == "DISCARD_CARD" for c in commands)
        if (not compare_exchange and self.rule.evaluator and self.rule.evaluator.uses_hand_retention and
                is_hand_choice(view, country, forced_play_discard=forced_play_discard)):
            status = "hand-retention"
        elif response_choice:
            self.plan.clear()
            status = "response-timing"
        elif not self.search and (planned := self.planned_choice(view, commands)):
            chosen, selected = planned
            mode, status = "plan", "matched-continuation"
        elif self.search and status != "response-timing":
            if self.exchange and self.exchange[0] != view["turn"]["turn_id"]:
                self.exchange = None
            info = view.get("reallocation", {})
            options = planned_options(self.rule.rules, info, self.exchange[1]["reallocation"]) if self.exchange else []
            if "payment" in info and not set(options) <= {c.get("option") for c in commands}:
                options = []
            if planned_exchange := next((c for c in commands if c.get("option") in options), None):
                chosen, selected = planned_exchange, self.exchange[1]
                if "targets" in info:
                    self.exchange = (self.exchange[0], selected, chosen["option"])
                mode, status = "plan", "matched-exchange"
            elif resumed := self.resume_exchange(view, commands, country):
                chosen, selected = resumed
                mode, status = "plan", "matched-exchange-play"
            elif planned := self.planned_choice(view, commands):
                chosen, selected = planned
                mode, status = "plan", "matched-continuation"
            elif view["decision"]:
                status = "pending-choice"
            else:
                self.plan = []
                eligible = [r["command"] for r in preferred
                            if r["command"]["type"] != "ACTIVATE_CARD" and
                            (r["rule_score"] >= -1 or r["command"]["type"] == "REALLOCATE" or
                             (r.get("action_cost") and r["command"]["type"] == "USE_AIR") or
                             (mandatory and r["command"]["type"] in ("PLAY_CARD", "USE_STATUS")))]
                if len(eligible) > 1 or (mandatory and any(c["type"] in ("PLAY_CARD", "USE_STATUS") for c in eligible)):
                    tried = eligible[:self.width]
                    exchange = next((c for c in eligible if c["type"] == "REALLOCATE"), None)
                    if compare_exchange and exchange:
                        direct = [c for c in eligible if c != exchange]
                        tried = direct[:1] + [exchange] + direct[1:self.width - 1]
                    elif exchange and exchange not in tried and self.width > 1:
                        tried.insert(1, exchange)
                        del tried[self.width:]
                    end = next((c for c in eligible if c["type"] == "END_PHASE"), None)
                    if end is not None and end not in tried:
                        tried.append(end)
                    # Reserve a small part of the total budget for choosing and recording the result.
                    reserve_ms = min(75, self.budget_ms * 0.05)
                    verified = {tuple(sorted(row["command"].items())): row["recruitment_result"]
                                for row in preferred if "recruitment_result" in row}
                    pending = []
                    for command in tried:
                        existing = verified.get(tuple(sorted(command.items())))
                        if existing:
                            rows.append(existing)
                        else:
                            pending.append(command)
                    status = "finished"
                    if pending:
                        extra, status = self.search.run(
                            view, pending, country, started + (self.budget_ms - reserve_ms) / 1000)
                        rows.extend(extra)
                        if status == "finished" and any(row.get("reason") == "deadline" for row in extra):
                            status = "deadline"
                    completed = [row for row in rows if row["complete"]]
                    if compare_exchange and not any(row["command"]["type"] != "REALLOCATE" for row in completed):
                        completed = [row for row in completed if row["command"]["type"] != "REALLOCATE"]
                    rejected = [row["command"] for row in completed if not acceptable_play(row, mandatory=mandatory)]
                    selected = max((row for row in completed if acceptable_play(row, mandatory=mandatory)),
                                   key=candidate_key, default=None)
                    if selected is None and chosen in rejected:
                        chosen = next(row["command"] for row in preferred if row["command"] not in rejected)
                    if selected:
                        mode, chosen = "search", selected["command"]
                        if selected.get("reallocation"):
                            self.exchange = (view["turn"]["turn_id"], selected, None)
                        self.remember_choices(selected, country)
                elif len(commands) > 1:
                    status = "no-comparable-commands"
        if not view["decision"] and not selected:
            self.plan = next(row.get("recruitment_plan", []) for row in ranked if row["command"] == chosen)
        # Projected opposing choices are internal to the worker result and never become commands.
        logged_rows = [{k: v for k, v in row.items()
                        if k not in ("continuations", "choice_signatures", "exchange_followup")}
                       for row in rows]
        for command in tried:
            if not any(row["command"] == command for row in rows):
                logged_rows.append({"command": command, "complete": False, "reason": status,
                                    "q": None, "nodes": 0})
        return chosen, {"version": view["version"], "round": view["turn"]["round_id"],
                        "phase": view["turn"]["phase"], "country": country,
                        "budget_ms": self.budget_ms, "elapsed_ms": (perf_counter() - started) * 1000,
                        "mode": mode, "search_status": status, "command": chosen,
                        "rule_scores": [{k: v for k, v in row.items() if k not in ("recruitment_plan", "recruitment_result")}
                                        for row in ranked],
                        "candidate_scores": logged_rows,
                        **({"selected_reallocation": {k: selected["reallocation"][k] for k in ("source", "kind", "target")}}
                           if selected and chosen["type"] == "REALLOCATE" else {}),
                        "selected_q": selected["q"] if selected else None}
