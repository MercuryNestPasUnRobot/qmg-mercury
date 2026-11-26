"""Price one-shot responses and compare waits supported by the acting seat's hand."""
from dataclasses import replace
from functools import lru_cache
import json

from .air import air_options
from .content_effects import (action_for, available_options, condition_met, event_effect, filtered_units,
                              force_id, point_award, target_regions)
from .content_triggers import matches_trigger
from .definitions import BATTLES, CONSTRUCTION
from .model import CoreError, ResolutionFrame, TURN_ORDER, Token, ZoneRegistry, zone
from .operations import OperationExecutor, outcome
from .positional_scoring import STANDARD_SITE_VP
from .rules import LegalActionGenerator, SupplyResolver, board_units, unit_rule_matches
from .status_scoring import trigger_of


PHASE_WINDOWS = {
    "turn-start": "Play", "play-phase-start": "Play", "air-phase-start": "Air",
    "victory-start": "Victory", "during-victory": "Victory", "discard-start": "Discard",
}
ACTION_WINDOWS = {"after-build", "battle-space", "unit-activity", "after-unit-removal"}
AIR_WINDOWS = {"after-air-action", "after-air-superiority", "after-air-superiority-removal"}
MILITARY_EFFECTS = {"build", "recruit", "choose-space", "battle"}


def is_delayed(card):
    return card.get("type") == "RESPONSE" or card.get("legacy_kind") == "bolster"


class ResponseForecast:
    def __init__(self, economy):
        self.economy, self.rules = economy, economy.rules
        self.units = economy.units
        self.location_scoring = economy.map_weights.get("action_opportunity_scoring", False)
        self.placement_base = 0 if self.location_scoring else self.units.action
        self.state = economy.visible_state()
        self.turn = self.state.turn
        self.active = TURN_ORDER[self.turn.country_index]
        self.legal = LegalActionGenerator(self.rules)
        self.executor = OperationExecutor(self.rules)
        self.staging = lru_cache(maxsize=None)(self.staging)
        self.hand_actions = lru_cache(maxsize=None)(self.hand_actions)

    def time_cost(self, turns):
        return self.units.vp_score(STANDARD_SITE_VP * turns)

    def turns_to(self, owner, phase, *, remaining_play=False, next_window=False):
        if self.active != owner:
            return 1
        phases = self.rules.phases
        current = -1 if self.turn.phase == "Setup" else phases.index(self.turn.phase)
        if phase not in phases:
            return 1
        if current < phases.index(phase):
            return 0
        if remaining_play and phase == self.turn.phase and self.turn.plays_remaining:
            return 0
        return int(current > phases.index(phase) or next_window)

    def hand(self, owner, excluded=None, state=None):
        state = self.state if state is None else state
        return [(key, self.rules.cards[t.definition_id]) for key in state.positions[zone(owner, "HAND")]
                if key != excluded and (t := state.tokens[key]).definition_id in self.rules.cards
                and not self.rules.cards[t.definition_id].get("simulation_unknown")]

    def delay(self, owner, trigger, kind):
        name = trigger["type"]
        if name == "any-time":
            return 0
        if name in PHASE_WINDOWS:
            return self.turns_to(owner, PHASE_WINDOWS[name], next_window=True)
        if name in AIR_WINDOWS and any(c.get("legacy_kind") == "air-power" for _, c in self.hand(owner)):
            return self.turns_to(owner, "Air")
        # A staged response consumes this turn's regular play. An unknown opposing play
        # supplies no known same-turn timing.
        return 1 if kind != "bolster" else self.turns_to(owner, "Play", remaining_play=True, next_window=True)

    def within_game(self, owner, turns):
        wraps = (TURN_ORDER.index(owner) < self.turn.country_index)
        distance = max(0, turns - 1) + (int(wraps) if self.active != owner else turns)
        return self.turn.round_id + distance <= self.rules.settings["max_rounds"]

    def staging(self, owner, card_id, *, estimate_draw=True):
        card = self.rules.cards[card_id]
        rows = []
        for key in card.get("abilities", ()):
            ability = self.rules.abilities[key]
            base = self.program(owner, ability, baseline=True, estimate_draw=estimate_draw)
            turns = self.delay(owner, trigger_of(ability), card.get("legacy_kind"))
            cost = self.time_cost(turns)
            rows.append({"base": base, "wait_turns": turns, "time_cost": cost, "net": base - cost,
                         "window_remaining": self.within_game(owner, turns)})
        return max(rows, key=lambda row: (row["window_remaining"], row["net"]),
                   default={"base": 0, "wait_turns": 0, "time_cost": 0, "net": 0, "window_remaining": False})

    def on_board(self, state):
        from .economic_scoring import EconomicForecast

        return EconomicForecast(self.rules, self.economy.settings, vars(state.turn), state.scores,
                                lambda seat, area: len(state.positions[zone(seat, area)]), lambda: state,
                                state.winner, status_weights=self.economy.status_weights,
                                map_weights=self.economy.map_weights, units=self.units,
                                hand_average=self.economy.hand_average)

    def program(self, owner, ability, *, source=None, event=None, moves=(), baseline=False, economy=None, estimate_draw=True):
        economy = self.economy if economy is None else economy
        state = economy.visible_state()
        event = event or {}
        context = {"event": event, "event_outcome": {"changes": moves}, "excluded_card": source}
        frame = ResolutionFrame("", source, owner, [], context=context)
        token = next((t for t in state.tokens.values() if t.source_token_id == source and
                      t.definition_id == ability["id"]), None) if source else None
        cost = self.executor.activation_cost(state, token) if token else ability.get("discard_cost", 0)
        if not baseline and cost > len(self.hand(owner, source, state)):
            return 0

        def effects_value(steps, current_economy):
            net = -self.units.card_score(cost)
            paid = set()
            drawn = 0
            for index, step in enumerate(steps):
                current_state = current_economy.visible_state()
                op = step.get("operation", step)
                if op["type"] != "CARD_EFFECT":
                    if op["type"] == "GAIN_VP":
                        sign = 1 if self.rules.team(op.get("score_country") or owner) == self.rules.team(owner) else -1
                        net += self.units.vp_score(sign * op["amount"])
                    elif op["type"] in BATTLES | CONSTRUCTION:
                        net += current_economy.routes.best_action_bonus(owner, op,
                            placement_base=self.placement_base if op["type"] in CONSTRUCTION else 0)
                    elif op["type"] == "DRAW" and estimate_draw and current_economy.hand_average:
                        count = op.get("count", 1)
                        net += current_economy.draw_value(owner, count, drawn)
                        drawn += count
                    elif op["type"] == "CANCEL":
                        net += self.defense_value(owner, ability, current_economy, event, baseline)
                    continue
                effect = op["effect"]
                airdrop = current_economy.marginal and (effect.get("nearAirAction") or effect.get("atAirAction"))
                offset = (current_economy.map_weights.get("status_forecast", {}).get("projected_distance_offset", 1)
                          if airdrop else 0)
                if baseline:
                    effect = {k: v for k, v in effect.items() if k not in
                              ("nearBuilt", "nearAirAction", "atAirAction", "battleRange", "battleDistance")}
                    if effect["type"].endswith("-at-battle-space") or effect["type"] == "recruit-at-removed-space":
                        effect["type"] = effect["type"].split("-at-", 1)[0]
                else:
                    try:
                        effect = event_effect(effect, frame)
                    except CoreError:
                        if not effect.get("optional"):
                            break
                        continue
                if not condition_met(self.rules, current_state, effect.get("chooser", owner), effect.get("when")):
                    continue
                kind = effect["type"]
                if kind == "discard-hand":
                    choices = [k for k, card in self.hand(owner, source, current_state) if k not in paid and
                               (not effect.get("kinds") or card.get("legacy_kind") in effect["kinds"])]
                    if not baseline and len(choices) < effect["count"] and effect.get("strict"):
                        return 0
                    paid.update(choices[:effect["count"]])
                    net -= self.units.card_score(effect["count"] if baseline else min(len(choices), effect["count"]))
                elif kind == "choose-one":
                    branches = [[{"type": "CARD_EFFECT", "effect": e} for e in branch["effects"]]
                                for branch in effect["options"]]
                    net += max((effects_value(branch, current_economy) for branch in branches), default=0)
                    net += self.units.card_score(cost)
                elif kind in MILITARY_EFFECTS and index < len(steps) - 1:
                    value, placement, playable = self.first_military_step(owner, ability, effect, current_economy,
                                                                          distance_offset=offset)
                    net += value
                    if placement:
                        token, target, is_build = placement
                        current_economy = self.on_board(self.projected_move(owner, None, token, target,
                                                                          current_state.turn.phase, state=current_state))
                        if is_build:
                            frame.context["last_built_region"] = target
                    elif not playable and not effect.get("optional"):
                        break
                else:
                    net += self.effect_value(owner, ability, effect, current_economy, context, baseline, distance_offset=offset)
            return net

        return effects_value(ability["effects"], economy)

    def first_military_step(self, owner, ability, effect, economy, *, distance_offset=0):
        state = economy.visible_state()
        kind = effect.get("kind", "army")
        if kind == "same":
            kind = trigger_of(ability).get("kind", "either")
        variants = ("army", "navy") if kind == "either" else (kind,)
        best, placement, playable = 0, None, False
        for variant in variants:
            effect = {**effect, "kind": variant}
            action = action_for(effect, owner)
            force = action["force"]
            battle = action["type"] in BATTLES
            for region in target_regions(self.rules, state, owner, effect):
                playable = True
                if battle:
                    enemy = economy.routes._removable_targets(force, region)
                    victim = max(enemy, key=lambda t: economy.routes.removal_value(t.token_id), default=None)
                    if victim:
                        value = economy.routes.removal_value(victim.token_id)
                        dest = zone(victim.owner_country, "RESERVE_" + variant.upper())
                        if value > best:
                            best, placement = value, (victim.token_id, dest, False)
                elif economy.routes.placement_supplied(force, region):
                    value = (economy.routes.position.value(force, region, construction=True, distance_offset=distance_offset)
                             if distance_offset else self.placement_base + economy.routes.target_value(force, region, construction=True))
                    if value > best:
                        key = state.positions[zone(force, "RESERVE_" + variant.upper())][0]
                        best, placement = value, (key, region, True)
        return best, placement, playable

    def defense_value(self, owner, ability, economy, event, baseline):
        state = economy.visible_state()
        trigger = trigger_of(ability)
        terrain = trigger.get("kind")
        friendly = self.rules.team(owner)
        region = event.get("target")
        if economy.marginal:
            if region not in self.rules.regions or baseline:
                return self.units.action
            victims = [t for t in board_units(self.rules, state) if self.rules.team(t.owner_country) == friendly
                       and (not terrain or self.rules.units[t.definition_id]["kind"].lower() == terrain)
                       and t.position_id == region and event.get("target_token", t.token_id) == t.token_id]
            return max((economy.routes.protection_value(t.owner_country, region, token=t.token_id) for t in victims),
                       default=economy.routes.protection_value(owner, region))
        values = [economy.routes.removal_value(t.token_id) for t in board_units(self.rules, state)
                  if (self.rules.team(t.owner_country) == friendly
                      and (not terrain or self.rules.units[t.definition_id]["kind"].lower() == terrain)
                      and (baseline or (t.position_id == event.get("target") and
                                         event.get("target_token", t.token_id) == t.token_id)))]
        return max(values, default=0)

    def effect_value(self, owner, ability, effect, economy, context, baseline, *, distance_offset=0):
        from .ai_match_policy import effect_priority

        rules, state = self.rules, economy.visible_state()
        kind, event = effect["type"], context["event"]
        if kind in MILITARY_EFFECTS:
            if effect.get("kind") == "same":
                effect = {**effect, "kind": trigger_of(ability).get("kind", "either")}
            if distance_offset:
                return self.first_military_step(owner, ability, effect, economy, distance_offset=distance_offset)[0]
            return economy.routes.best_action_bonus(owner, {"type": "CARD_EFFECT", "effect": effect},
                                                    placement_base=0 if kind == "battle" else self.placement_base)
        if kind in ("prevent-removal", "protect-unit-this-turn"):
            return self.defense_value(owner, ability, economy, event, baseline)
        if kind in ("eliminate-built-unit", "eliminate-air-superiority-attacker"):
            targets = {m["token_id"] for m in context["event_outcome"]["changes"] if m["to"] in rules.regions}
            if event.get("attacker_token"):
                targets.add(event["attacker_token"])
            if baseline:
                terrain = "air" if kind == "eliminate-air-superiority-attacker" else trigger_of(ability).get("kind")
                targets = {t.token_id for t in board_units(rules, state)
                           if rules.team(t.owner_country) != rules.team(owner) and
                           (not terrain or rules.units[t.definition_id]["kind"].lower() == terrain)}
            return max((economy.routes.removal_value(key) for key in targets if key in state.tokens and
                        state.tokens[key].position_id in rules.regions and
                        rules.team(state.tokens[key].owner_country) != rules.team(owner)), default=0)
        if kind == "air-action":
            if self.location_scoring:
                return economy.routes.tactical_effect(owner, {"type": "CARD_EFFECT", "effect": effect})
            options = [json.loads(command) for command in air_options(rules, state, effect.get("chooser", owner), effect)]
            return max((economy.routes.removal_value(victim) if victim else
                       (self.units.action if action in ("deploy", "marshal") else 0)
                       for action, _, _, victim in options), default=0)
        if kind in ("supply-this-turn", "supply-units-this-turn", "protect-units-this-turn"):
            pieces = (filtered_units(rules, state, owner, effect["filter"]) if "filter" in effect else
                      board_units(rules, state, force_id(effect["force"], owner)))
            if kind == "protect-units-this-turn":
                return self.defense_value(owner, ability, economy, event, baseline) if pieces else 0
            if not baseline and (TURN_ORDER[state.turn.country_index] != owner or
                                 state.turn.phase in ("Victory", "Discard", "Draw")):
                return 0
            if economy.marginal:
                return economy.routes.temporary_supply_value(owner, effect)
            supply = SupplyResolver(rules)
            return max((economy.routes.target_value(t.owner_country, t.position_id)
                        for t in pieces if t.token_id not in supply.supplied(state, t.owner_country)), default=0)
        if kind == "discard-top" and effect["target"] == "battle-attacker":
            attacker = event.get("actor_country")
            seats = (attacker,) if attacker else (s for s in TURN_ORDER if rules.team(s) != rules.team(owner))
            return max((economy.mill(owner, seat, effect["count"], 1) for seat in seats), default=0)
        if kind in ("discard-status", "play-from-zone", "stage-bolster"):
            if (kind == "play-from-zone" and effect["zone"] == "hand"
                    and force_id(effect.get("target"), owner) != owner):
                return 0
            options = available_options(rules, state, owner, effect, context)
            return self.units.card_score(1) if options else 0
        if kind == "request-hand-discard" and effect.get("target") == "battle-attacker":
            if economy.marginal:
                attacker = event.get("actor_country")
                targets = (attacker,) if attacker in TURN_ORDER else (s for s in TURN_ORDER if rules.team(s) != rules.team(owner))
                return max((economy.routes.tactical_effect(owner, {"type": "CARD_EFFECT", "effect": {**effect, "target": t}})
                            for t in targets), default=0)
            return self.defense_value(owner, ability, economy, event, baseline)
        return effect_priority(effect, rules.team(owner), economy, owner, units=self.units, tactical=economy.marginal)

    def hand_actions(self, owner):
        """Only concrete first actions of present, individually playable cards."""
        actions = []
        for key, card in self.hand(owner):
            modes = card.get("play_as", ())
            step = card["effects"][0] if card["effects"] else {}
            op = step.get("operation", step)
            effect = op.get("effect", {})
            military = op.get("type") in BATTLES | CONSTRUCTION or effect.get("type") in MILITARY_EFFECTS
            if not (modes or military):
                continue
            if not self.legal.can_start_card(self.state, owner, card):
                continue
            actions.extend((key, {"type": mode}, None) for mode in modes)
            if op.get("type") in BATTLES | CONSTRUCTION:
                actions.append((key, op, None))
            elif effect.get("type") in MILITARY_EFFECTS:
                if not condition_met(self.rules, self.state, owner, effect.get("when")):
                    continue
                variants = ("army", "navy") if effect.get("kind") == "either" else (effect.get("kind", "army"),)
                for variant in variants:
                    selected = {**effect, "kind": variant}
                    if selected.get("chooser", owner) == owner:
                        actions.append((key, action_for(selected, owner), selected))
        return actions

    def projected_move(self, owner, card, token, destination, phase, *, state=None):
        state = self.state if state is None else state
        state = replace(state, tokens=dict(state.tokens), positions=dict(state.positions),
                        turn=replace(state.turn, country_index=TURN_ORDER.index(owner), phase=phase))
        for key, target in ((card, zone(owner, "DISCARD")), (token, destination)):
            if key is not None:
                origin = state.tokens[key].position_id
                state.positions[origin] = list(state.positions[origin])
                state.positions[target] = list(state.positions[target])
                ZoneRegistry.transfer(state, key, target)
        return state

    def projections(self, owner, source, trigger, phase):
        name = trigger["type"]
        if name in AIR_WINDOWS:
            card = next((key for key, c in self.hand(owner, source) if c.get("legacy_kind") == "air-power"), None)
            if card is None:
                return
            for option in air_options(self.rules, self.state, owner, {"actions": ["deploy", "marshal", "superiority"]}):
                action, attacker, target, victim = json.loads(option)
                if action == "superiority":
                    if victim is None:
                        continue
                    token, destination = victim, zone(self.state.tokens[victim].owner_country, "RESERVE_AIR")
                else:
                    token, destination = attacker or self.state.positions[zone(owner, "RESERVE_AIR")][0], target
                event = {"type": "AIR_ACTION_FINISHED", "actor_country": owner, "force": owner, "target": target,
                         "air_action": action, "removed_force": self.state.tokens[victim].owner_country if victim else None}
                yield card, target, event, token, destination, "Air"
            return
        for card, action, effect in self.hand_actions(owner):
            kind, force = action["type"], action.get("force", owner)
            battle = kind in BATTLES
            if card == source or (name == "after-build" and battle) or (name in ("battle-space", "after-unit-removal") and not battle):
                continue
            targets = (target_regions(self.rules, self.state, owner, effect) if effect else
                       self.legal.targets(self.state, owner, action))
            if not effect and action.get("target", "$choice") != "$choice":
                targets = [p for p in targets if p == action["target"]]
            unit = action.get("unit_kind", "NAVY" if kind in ("BUILD_NAVY", "SEA_BATTLE") else "ARMY")
            for target in targets:
                if battle:
                    pieces = [t for t in board_units(self.rules, self.state) if t.position_id == target and
                              self.rules.units[t.definition_id]["kind"] == unit and
                              self.rules.team(t.owner_country) != self.rules.team(owner) and
                              not unit_rule_matches(self.rules, self.state, t, "protection")]
                    if not pieces:
                        continue
                    token = pieces[0].token_id
                    destination = zone(pieces[0].owner_country, "RESERVE_" + unit)
                else:
                    token = self.state.positions[zone(force, "RESERVE_" + unit)][0]
                    destination = target
                event = {"type": kind, "actor_country": owner, "force": force, "target": target, "unit_kind": unit}
                yield card, target, event, token, destination, phase

    def future(self, owner, source, ability, now):
        trigger = trigger_of(ability)
        name = trigger["type"]
        phase = PHASE_WINDOWS.get(name, "Air" if name in AIR_WINDOWS else "Play")
        if name not in ACTION_WINDOWS | AIR_WINDOWS and name not in PHASE_WINDOWS and name != "any-time":
            return None
        start = self.turns_to(owner, "Play", remaining_play=True, next_window=True)
        if name in AIR_WINDOWS:
            start = self.turns_to(owner, "Air", next_window=True)
        turns = max(start, self.turns_to(owner, phase, next_window=(name in PHASE_WINDOWS)))
        if name in PHASE_WINDOWS and phase == "Play" and start:
            turns = start + 1  # The next Play entry is before its regular play.
        if not self.within_game(owner, turns):
            return None
        best = now
        selected = None
        for card, target, signal, target_token, destination, projected_phase in self.projections(owner, source, trigger, phase):
            state = self.projected_move(owner, card, target_token, destination, projected_phase)
            if destination in self.rules.regions and target_token not in SupplyResolver(self.rules).supplied(
                    state, state.tokens[target_token].owner_country):
                continue
            moves = [{"token_id": target_token, "from": self.state.tokens[target_token].position_id, "to": destination}]
            if name in ACTION_WINDOWS | AIR_WINDOWS:
                result = {**outcome(signal["type"]), "changes": moves}
                listener = Token("", ability["id"], "ABILITY", owner, "", source)
                if not matches_trigger(self.rules, state, listener, trigger, signal, result):
                    continue
            elif not condition_met(self.rules, state, owner, self.rules.cards[state.tokens[source].definition_id].get("content_condition")):
                continue
            else:
                signal, moves = {}, []
            economy = self.on_board(state)
            value = self.program(owner, ability, source=source, event=signal, moves=moves, economy=economy)
            net = value - self.time_cost(turns)
            if net > max(0, best):
                best = net
                selected = {"card": card, "target": target, "base": value, "wait_turns": turns,
                            "time_cost": self.time_cost(turns), "net": net}
        return selected

    def activation(self, owner, source, key, event=None, moves=()):
        ability = self.rules.abilities[key]
        event = event or {}
        now = self.program(owner, ability, source=source, event=event, moves=moves)
        future = self.future(owner, source, ability, now) if not self.state.winner else None
        return {"immediate": now, "wait": future}
