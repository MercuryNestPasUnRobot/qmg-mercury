"""Price one-shot responses and compare waits supported by the acting seat's hand."""
from dataclasses import replace
from functools import lru_cache
import json

from .air import air_options
from .content_effects import (action_for, available_options, can_begin, condition_met, event_effect, filtered_units,
                              force_id, point_award, target_regions)
from .content_triggers import matches_trigger
from .definitions import BATTLES, CONSTRUCTION
from .model import CoreError, ResolutionFrame, TURN_ORDER, Token, ZoneRegistry, zone
from .operations import OperationExecutor, outcome
from .rules import LegalActionGenerator, SupplyResolver, board_units, status_enabled, unit_rule_matches
from .score_units import STANDARD_SITE_VP
from .status_scoring import BASIC_WINDOWS, FIXED_WINDOWS, air_placement, placement_value, trigger_of


PHASE_WINDOWS = FIXED_WINDOWS
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
        return turns * self.units.site_score(STANDARD_SITE_VP)

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
            if self.economy.known_abilities:
                rows.extend(self.known_staging(owner, card, ability, estimate_draw))
            else:
                base = self.program(owner, ability, baseline=True, estimate_draw=estimate_draw)
                turns = self.delay(owner, trigger_of(ability), card.get("legacy_kind"))
                rows.append(self.opening_value(owner, base, turns))
        return max(rows, key=lambda row: (row["window_remaining"], row["net"]),
                   default={"base": 0, "wait_turns": 0, "time_cost": 0, "net": 0, "window_remaining": False,
                            **({"activations": 0} if self.economy.known_abilities else {})})

    def opening_value(self, owner, base, turns, *, window_remaining=None):
        cost = self.time_cost(turns)
        if window_remaining is None:
            window_remaining = self.within_game(owner, turns)
        row = {"base": base, "wait_turns": turns, "time_cost": cost, "net": base - cost,
               "window_remaining": window_remaining}
        if self.economy.known_abilities:
            row["activations"] = int(row["window_remaining"])
        return row

    def known_staging(self, owner, card, ability, estimate_draw):
        if not condition_met(self.rules, self.state, owner, card.get("content_condition")):
            return
        area = (zone(owner, "HAND"), zone(owner, "RESPONSE"))
        source = next((key for p in area for key in self.state.positions[p]
                       if self.state.tokens[key].definition_id == card["id"]), None)
        trigger = trigger_of(ability)
        name, kind = trigger["type"], card.get("legacy_kind")
        staged = source and self.state.tokens[source].position_id == zone(owner, "RESPONSE")
        if name in PHASE_WINDOWS or name == "any-time":
            turns = self.delay(owner, trigger, kind)
            if name == "any-time" and trigger.get("ownTurnOnly") and self.active != owner:
                turns = 1
            if (kind != "bolster" and name in ("turn-start", "play-phase-start") and self.active == owner
                    and self.turn.phase == "Setup"):
                turns = 1  # It can only be staged after the opening of the Play phase.
            value = self.program(owner, ability, source=source, estimate_draw=estimate_draw, check_conditions=True)
            if value is not None:
                yield self.opening_value(owner, value, turns)
            return
        for actor, event, moves, state in self.known_events(owner, card, source, trigger):
            phase = "Air" if name in AIR_WINDOWS else "Discard" if name == "discard-stage-card" else "Play"
            turns = self.turns_to(actor, phase, remaining_play=True, next_window=phase == "Play")
            if phase == "Play" and kind != "bolster" and not staged:
                turns = max(1, turns)  # Playing the response consumes the present Play opportunity.
            if not self.within_game(actor, turns):
                continue
            value = self.program(owner, ability, source=source, event=event, moves=moves, economy=self.on_board(state),
                                 estimate_draw=estimate_draw, check_conditions=True)
            if value is not None:
                yield self.opening_value(owner, value, turns, window_remaining=True)

    def known_events(self, owner, card, source, trigger):
        """Yield rule-checked stimuli from visible cards and one legal basic-action assumption."""
        name = trigger["type"]
        listener = Token("", "", "ABILITY", owner, "", source or "forecast:source:" + card["id"])
        if source is None:
            token = Token(listener.source_token_id, card["id"], "CARD", owner, zone(owner, "OBJECT_POOL"))
        else:
            token = None
        for actor in TURN_ORDER:
            relation = trigger.get("actor", trigger.get("builder", "any"))
            same_team = self.rules.team(actor) == self.rules.team(owner)
            if (relation == "self" and actor != owner or relation == "enemy" and same_team or
                    relation == "team" and not same_team or relation in TURN_ORDER and actor != relation or
                    trigger.get("ownTurnOnly") and actor != owner):
                continue
            for event, moves, state, context in self.event_candidates(actor, owner, source, trigger):
                if token:
                    state = replace(state, tokens={**state.tokens, token.token_id: token})
                result = {**outcome(event["type"]), "changes": moves, "context": context}
                source_card = state.tokens.get(event.get("source_token_id"))
                if name == "after-card-use" and not source_card:
                    continue
                if matches_trigger(self.rules, state, listener, trigger, event, result):
                    yield actor, event, moves, state

    def event_candidates(self, actor, owner, source, trigger):
        name = trigger["type"]
        if name in BASIC_WINDOWS | AIR_WINDOWS and name != "play-army-card":
            if name in AIR_WINDOWS and name != "after-air-superiority-removal" and actor != owner:
                return
            phase = "Air" if name in AIR_WINDOWS else "Play"
            for card, target, event, key, destination, phase in self.projections(actor, source, trigger, phase,
                                                                                 assume_basic=name in BASIC_WINDOWS):
                original = self.state.tokens[key]
                move = {"token_id": key, "from": original.position_id, "to": destination}
                before = name in ("before-battle-removal", "before-any-removal")
                state = self.projected_move(actor, card, None if before else key, destination, phase)
                context = {"attacked_units": [key]}
                if before:
                    announcement = "forecast:announcement"
                    event = {**event, "announced_type": event["type"], "type": "ANNOUNCE", "target_token": key,
                             "announcement_id": announcement}
                    state = replace(state, announcements={**state.announcements, announcement: {"cancelled": False}})
                elif name == "after-unit-removal" and trigger.get("suppliedOnly"):
                    context["removed_supplied"] = {key: key in SupplyResolver(self.rules).supplied(self.state, original.owner_country)}
                yield event, [] if before else [move], state, context
            return
        phase = "Discard" if name == "discard-stage-card" else "Play"
        state = self.projected_move(actor, None, None, None, phase)
        if name in ("before-card-effect", "after-card-effect") and trigger["kind"] == "status":
            for key in state.positions[zone(actor, "STATUS")]:
                t = state.tokens[key]
                definition = self.rules.cards[t.definition_id]
                if status_enabled(self.rules, state, t) and condition_met(self.rules, state, actor, definition.get("content_condition")) and any(
                        self.rules.abilities[a]["effects"] for a in definition.get("abilities", ())):
                    yield {"type": "CARD_DECLARED" if name == "before-card-effect" else "CARD_EFFECT_FINISHED",
                           "actor_country": actor, "source_token_id": key, "declared_kind": "status"}, [], state, {}
            return
        for key, card in self.hand(actor, source):
            kind = card.get("legacy_kind")
            event = {"actor_country": actor, "source_token_id": key}
            moves = []
            if name == "discard-stage-card" and kind == trigger["kind"]:
                event["type"] = "DISCARD"
                moves = [{"token_id": key, "from": zone(actor, "HAND"), "to": zone(actor, "DISCARD")}]
            elif name in ("before-card-effect", "after-card-effect") and kind == trigger["kind"] == "bolster":
                # A visible, legal scheduled bolster supplies a known declared effect.
                if not any(trigger_of(self.rules.abilities[a])["type"] in PHASE_WINDOWS for a in card.get("abilities", ())):
                    continue
                event.update(type="CARD_DECLARED" if name == "before-card-effect" else "CARD_EFFECT_FINISHED", declared_kind=kind)
            elif not self.legal.can_start_card(state, actor, card):
                continue
            elif name in ("economic-play", "economic-target") and kind == "economic":
                event["type"] = "ECONOMIC_PLAY" if name == "economic-play" else "ECONOMIC_TARGET"
                if name == "economic-target":
                    if not self.economic_targets(actor, card, owner):
                        continue
                    event["target_country"] = owner
            elif name == "after-card-use" and actor == owner and card["type"] != "STATUS":
                event.update(type="OBJECT_SETTLED", settled_definition=card["id"])
            elif name == "play-army-card" and actor == owner and (kind == "build-army" or "BUILD_ARMY" in card.get("play_as", ())):
                event.update(type="PLAY_CARD", play_as="BUILD_ARMY")
            else:
                continue
            yield event, moves, state, {}

    def economic_targets(self, actor, card, owner):
        for step in card["effects"]:
            effect = step.get("effect", {})
            if not condition_met(self.rules, self.state, actor, effect.get("when")):
                continue
            if effect.get("type") == "choose-country" and effect.get("action") == "discard-top":
                if owner in available_options(self.rules, self.state, actor, effect):
                    return True
            if effect.get("type") in ("discard-top", "discard-top-per-unit") and force_id(effect.get("target"), actor) == owner:
                return True
        return False

    def on_board(self, state):
        return self.economy.on_board(state)

    def program(self, owner, ability, *, source=None, event=None, moves=(), baseline=False, economy=None,
                estimate_draw=True, check_conditions=False):
        economy = self.economy if economy is None else economy
        state = economy.visible_state()
        event = event or {}
        context = {"event": event, "event_outcome": {"changes": moves}, "excluded_card": source}
        frame = ResolutionFrame("", source, owner, [], context=context)
        token = next((t for t in state.tokens.values() if t.source_token_id == source and
                      t.definition_id == ability["id"]), None) if source else None
        cost = self.executor.activation_cost(state, token) if token else ability.get("discard_cost", 0)
        if not baseline and cost > len(self.hand(owner, source, state)):
            return None if check_conditions else 0

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
                air_effect = effect if current_economy.known_abilities else None
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
                            return None if check_conditions else net
                        continue
                if not condition_met(self.rules, current_state, effect.get("chooser", owner), effect.get("when")):
                    continue
                kind = effect["type"]
                if kind == "discard-hand":
                    choices = [k for k, card in self.hand(owner, source, current_state) if k not in paid and
                               (not effect.get("kinds") or card.get("legacy_kind") in effect["kinds"])]
                    if not baseline and len(choices) < effect["count"] and effect.get("strict"):
                        return None if check_conditions else 0
                    paid.update(choices[:effect["count"]])
                    net -= self.units.card_score(effect["count"] if baseline else min(len(choices), effect["count"]))
                elif kind == "choose-one":
                    branches = [[{"type": "CARD_EFFECT", "effect": e} for e in branch["effects"]]
                                for branch in effect["options"]]
                    values = [v for branch in branches if (v := effects_value(branch, current_economy)) is not None]
                    if check_conditions and not values and not effect.get("optional"):
                        return None
                    net += max(values, default=0)
                    net += self.units.card_score(cost)
                elif kind in MILITARY_EFFECTS and (check_conditions or index < len(steps) - 1):
                    value, placement, playable = self.first_military_step(owner, ability, effect, current_economy,
                                                                          distance_offset=offset, air_effect=air_effect)
                    net += value
                    if placement:
                        token, target, is_build = placement
                        current_economy = self.on_board(self.projected_move(owner, None, token, target,
                                                                          current_state.turn.phase, state=current_state))
                        if is_build:
                            frame.context["last_built_region"] = target
                    elif not playable and not effect.get("optional"):
                        return None if check_conditions else net
                else:
                    if check_conditions and not can_begin(self.rules, current_state, effect.get("chooser", owner), effect, context):
                        if effect.get("optional"):
                            continue
                        return None
                    net += self.effect_value(owner, ability, effect, current_economy, context, baseline,
                                             distance_offset=offset, air_effect=air_effect)
            return net

        return effects_value(ability["effects"], economy)

    def first_military_step(self, owner, ability, effect, economy, *, distance_offset=0, air_effect=None):
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
                airborne = air_effect is not None and air_placement(self.rules, state, force, region, air_effect)
                if airborne and any(t.position_id == region and self.rules.team(t.owner_country) != self.rules.team(force)
                                    for t in economy.routes.pieces):
                    continue
                playable = True
                if battle:
                    enemy = economy.routes._removable_targets(force, region)
                    victim = max(enemy, key=lambda t: economy.routes.removal_value(t.token_id), default=None)
                    if victim:
                        value = economy.routes.removal_value(victim.token_id)
                        dest = zone(victim.owner_country, "RESERVE_" + variant.upper())
                        if value > best:
                            best, placement = value, (victim.token_id, dest, False)
                elif airborne or economy.routes.placement_supplied(force, region):
                    if air_effect is not None:
                        value = placement_value(economy, force, region, airdrop=airborne)
                    else:
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
                return economy.routes.protection_value(owner)
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

    def effect_value(self, owner, ability, effect, economy, context, baseline, *, distance_offset=0, air_effect=None):
        from .ai_match_policy import effect_priority

        rules, state = self.rules, economy.visible_state()
        kind, event = effect["type"], context["event"]
        if kind in MILITARY_EFFECTS:
            if effect.get("kind") == "same":
                effect = {**effect, "kind": trigger_of(ability).get("kind", "either")}
            if distance_offset or air_effect is not None:
                return self.first_military_step(owner, ability, effect, economy,
                                                distance_offset=distance_offset, air_effect=air_effect)[0]
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

    def projections(self, owner, source, trigger, phase, *, assume_basic=False):
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
                         "air_action": action, "attacker_token": attacker,
                         "removed_force": self.state.tokens[victim].owner_country if victim else None}
                yield card, target, event, token, destination, "Air"
            return
        actions = self.hand_actions(owner)
        if assume_basic:
            assumed = ("BUILD_ARMY", "BUILD_NAVY", "LAND_BATTLE", "SEA_BATTLE")
            actions = actions + [(None, {"type": kind}, None) for kind in assumed
                                 if not any(action["type"] == kind and action.get("force", owner) == owner and effect is None
                                            for _, action, effect in actions)]
        for card, action, effect in actions:
            kind, force = action["type"], action.get("force", owner)
            battle = kind in BATTLES
            if (source is not None and card == source or name == "after-build" and battle or
                    name in ("battle-space", "after-unit-removal", "before-battle-removal", "before-any-removal") and not battle):
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
                    victims = pieces if assume_basic else pieces[:1]
                    event = {"type": kind, "actor_country": owner, "force": force, "target": target, "unit_kind": unit}
                    for victim in victims:
                        yield card, target, event, victim.token_id, zone(victim.owner_country, "RESERVE_" + unit), phase
                    continue
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
            value = self.program(owner, ability, source=source, event=signal, moves=moves, economy=economy,
                                 check_conditions=self.economy.known_abilities)
            if value is None:
                continue
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
