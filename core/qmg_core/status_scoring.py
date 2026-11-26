"""Value remaining public status opportunities from a single seat's projected position."""
from collections import Counter, defaultdict
from dataclasses import replace
from functools import cached_property, lru_cache
import json
from math import inf

from .air import air_options
from .content_effects import condition_met, filtered_units, point_award, point_recipient, unit_matches
from .economic_scoring import CARD_RETURN_EFFECTS, POINT_EFFECTS
from .model import CoreError, PHASES, TURN_ORDER, Token, zone
from .regional_sites import regional_award, site_layers
from .rules import LegalActionGenerator, MapGraph, SupplyResolver, board_units, status_enabled

STAGES = ("Setup", *PHASES)
TRIGGER_PHASE = {
    "during-victory": "Victory", "turn-start": "Play", "play-phase-start": "Play",
    "discard-start": "Discard", "discard-stage-card": "Discard", "USE_STATUS": "Play",
    "after-build": "Play", "battle-space": "Play", "before-battle-removal": "Play",
    "economic-play": "Play", "play-army-card": "Play", "unit-activity": "Play",
    "before-rule-use": "Play", "air-phase-start": "Air", "after-air-action": "Air",
}
MAP_MODS = {"always_supplied", "supply_source", "deny_supply_source", "supply_sharing",
            "connection", "recruit_region", "home", "score_bonus"}
OTHER_MODS = {"reallocate_cost", "reallocate_discard", "economic_adjustment", "ignore_enemy_statuses"}
EFFECTS = POINT_EFFECTS | CARD_RETURN_EFFECTS | {
    "discard-top", "prevent-removal", "protect-unit-this-turn", "protect-units-this-turn", "supply-this-turn",
    "supply-units-this-turn", "battle", "build-at-battle-space", "choose-space", "recruit",
    "discard-hand", "play-from-zone", "request-hand-discard", "economic-discard-bonus",
    "order-deck-top", "put-hand-on-deck-bottom", "return-declared-army-card",
}
BASIC = {"army": ("build-army", "land-battle"), "navy": ("build-navy", "sea-battle")}
BASIC_KINDS = frozenset(kind for pair in BASIC.values() for kind in pair)
EXTRA_ACTIONS = {"build", "choose-space", "recruit", "battle", "build-at-battle-space"}
EXTRA_TRIGGERS = {"after-build", "battle-space", "turn-start"}
CONDITIONAL_TRIGGERS = {"after-build", "battle-space", "before-battle-removal", "economic-play",
                        "play-army-card", "unit-activity", "discard-stage-card", "after-air-action"}

STATUS_FORECAST_DEFAULTS = {
    "extra_trigger_opportunities": 1, "projected_distance_offset": 1,
    "condition_safe": 0.95, "condition_threatened": 0.8, "condition_horizon": 4,
    "arrival_accessible": 0.55, "arrival_nearby": 0.18,
    "known_action_likelihood": 0.76, "keyword_draw_likelihood": 0.12, "action_draw_likelihood": 0.22,
    "unknown_hand_likelihood_cap": 0.60, "unknown_hand_scale": 3,
    "own_activity_likelihood": 0.38, "enemy_activity_likelihood": 0.32, "discard_activity_likelihood": 0.3,
    "request_discard_card_value": 0.55, "filter_card_value": 0.28,
    "rule_option_multiplier": 0.32, "rule_option_decay": 0.5, "rule_option_limit": 4,
    "exchange_saved_payment_value": 0.35, "exchange_discard_base": 0.55, "exchange_discard_deck_bonus": 0.35,
    "economic_target_share": 1 / 3, "alternative_play_action_value": 0.375, "forced_discard_card_value": 0.45,
}


def is_status(card):
    return card.get("legacy_kind") == "status" and card["type"] == "STATUS"


def trigger_of(ability):
    trigger = ability["trigger"]
    return trigger.get("card_trigger") or {"type": trigger["operation_type"]}


def is_airdrop(ability):
    return any(step.get("effect", {}).get("nearAirAction") and step["effect"]["type"] in EXTRA_ACTIONS
               for step in ability["effects"])


def verify_status(rules, card):
    """Fail visibly if a transcribed status would silently fall back to a flat value."""
    for key in card.get("abilities", ()):
        ability = rules.abilities[key]
        if trigger_of(ability)["type"] not in TRIGGER_PHASE:
            raise CoreError("Unpriced status trigger: " + key)
        for step in ability["effects"]:
            if step["type"] != "CARD_EFFECT" or step["effect"]["type"] not in EFFECTS:
                raise CoreError("Unpriced status effect: " + key)
    for mod in card.get("modifiers", ()):
        if mod["kind"] not in MAP_MODS | OTHER_MODS:
            raise CoreError("Unpriced status modifier: " + mod["kind"])
    if not card.get("abilities") and not card.get("modifiers"):
        raise CoreError("Status has no forecastable mechanism: " + card["id"])


class StatusForecast:
    def __init__(self, economy, discount, held_factor, coefficients=None):
        self.economy, self.rules = economy, economy.rules
        self.state = economy.visible_state()
        self.discount, self.held_factor = discount, held_factor
        self.config = STATUS_FORECAST_DEFAULTS | (coefficients or {})
        self.marginal = economy.marginal
        self.turn = self.state.turn
        self.units = board_units(self.rules, self.state)
        self.legal = LegalActionGenerator(self.rules)
        self.action_distance = bool(economy.map_weights and economy.map_weights.get("action_distance_scoring"))
        self.sites = Counter()
        for site in self.rules.sites.values():
            self.sites[site["region"]] += site["vp"]
        self.sources = defaultdict(list)
        self.disabled = set()
        for owner in TURN_ORDER:
            for area in ("STATUS", "DISABLED_STATUS"):
                for key in self.state.positions.get(zone(owner, area), ()):
                    card = self.rules.cards.get(self.state.tokens[key].definition_id, {})
                    if is_status(card):
                        self.sources[owner].append((key, card["id"]))
                        if area == "DISABLED_STATUS":
                            self.disabled.add(key)
        self.ready = {(t.source_token_id, t.definition_id) for t in self.state.tokens.values()
                      if t.token_type == "ABILITY" and t.position_id == zone(t.owner_country, "ABILITY_READY")}
        # Caches belong to this one observed position and expire with it.
        for name in ("hand", "targets", "adjacent", "supplied", "program", "signals", "board_changes",
                     "entries", "portfolio", "report", "baseline_play", "possibility", "after_event_targets",
                     "strategic_modifiers", "extra_reaction", "airdrop_value", "regional_awards", "regional_placement"):
            setattr(self, name, lru_cache(maxsize=None)(getattr(self, name)))

    def hand(self, owner):
        return tuple(self.rules.cards.get(self.state.tokens[k].definition_id, {})
                     for k in self.state.positions[zone(owner, "HAND")])

    def chance(self, seat, kinds, later, keyword=None):
        # Known cards can fuel a near-term play once; later turns revert towards a draw prior.
        cards = self.hand(seat)
        matches = sum((card.get("legacy_kind") in kinds or any(k in self.related_actions(card) for k in kinds)) and
                      (not keyword or keyword in card.get("text", "") + card.get("title", "")) for card in cards)
        unknown = sum(not card or card.get("simulation_unknown", False) for card in cards)
        prior = self.config["keyword_draw_likelihood" if keyword else "action_draw_likelihood"]
        if matches:
            return self.config["known_action_likelihood"] if later < matches else prior if self.economy.count(seat, "DECK") else 0
        if unknown:
            scale = self.config["unknown_hand_scale"]
            return min(self.config["unknown_hand_likelihood_cap"], prior * (1 + min(scale, unknown) / scale))
        return prior if later and self.economy.count(seat, "DECK") else 0

    @staticmethod
    def related_actions(card):
        by_native = {"BUILD_ARMY": "build-army", "BUILD_NAVY": "build-navy",
                     "LAND_BATTLE": "land-battle", "SEA_BATTLE": "sea-battle"}
        actions = set(by_native[mode] for mode in card.get("play_as", ()) if mode in by_native)

        def visit(effect):
            kind = effect.get("kind", "army")
            action = effect.get("action", effect["type"])
            if kind in BASIC and action in ("build", "battle"):
                actions.add(BASIC[kind][action == "battle"])
            if effect["type"] == "choose-one":
                for branch in effect["options"]:
                    for child in branch["effects"]:
                        visit(child)
            if effect["type"] == "battle" and kind == "either":
                actions.update(("land-battle", "sea-battle"))

        for step in card.get("effects", ()):
            op = step.get("operation", step)
            if op["type"] in by_native:
                actions.add(by_native[op["type"]])
            elif "effect" in op:
                visit(op["effect"])
        return actions

    def extra_reaction(self, owner, key):
        ability = self.rules.abilities[key]
        trigger = trigger_of(ability)
        trigger_type = trigger["type"]
        effects = [step["effect"] for step in ability["effects"]]
        if trigger_type not in EXTRA_TRIGGERS or not any(e["type"] in EXTRA_ACTIONS for e in effects):
            return None
        if trigger_type == "turn-start":
            trigger_cards = ()
        else:
            trigger_cards = (BASIC[trigger["kind"]][trigger_type == "battle-space"],)
        matching_cards = sum(any(kind == card.get("legacy_kind") or kind in self.related_actions(card)
                                 for kind in trigger_cards) for card in self.hand(owner))
        net = sum(self.economy.units.action for effect in effects if effect["type"] in EXTRA_ACTIONS)
        for effect in effects:
            if effect["type"] == "discard-hand":
                net -= self.economy.units.card_score(effect["count"])
            elif effect["type"] == "discard-top":
                target = owner if effect["target"] == "self" else effect["target"]
                if target in TURN_ORDER:
                    net += self.economy.mill(owner, target, effect["count"], 1)
        return matching_cards + self.config["extra_trigger_opportunities"], net

    def deck_top(self, owner, count):
        seen = min(count, self.economy.count(owner, "DECK"))
        if self.action_distance:
            remaining = self.economy.routes.position.scoring_rounds(owner)
            return self.economy.top_value(seen, remaining, seat=owner)
        return 0.22 * max(0, seen - 1)

    def airdrop_value(self, owner, key):
        terrain = trigger_of(self.rules.abilities[key]).get("spaceKind")
        options = air_options(self.rules, self.state, owner, {"actions": ["deploy", "marshal"]})
        regions = {json.loads(option)[2] for option in options}
        return max((self.program(owner, owner, key, region) for region in regions
                    if terrain is None or self.rules.regions[region]["type"].lower() == terrain), default=0)

    def discard_gain(self, owner, card):
        values = []
        for source, definition in self.sources[owner]:
            for key in self.rules.cards[definition]["abilities"]:
                trigger = trigger_of(self.rules.abilities[key])
                if ((source, key) in self.ready and trigger["type"] == "discard-stage-card" and
                        trigger.get("country") == owner and trigger["kind"] == card.get("legacy_kind")):
                    values.append(self.program(owner, owner, key, None, committed=True))
        return max(values, default=0)

    def regional_awards(self, card_id, *, include_global=False):
        card = self.rules.cards[card_id]
        awards = {}
        for key in card.get("abilities", ()):
            ability = self.rules.abilities[key]
            if trigger_of(ability)["type"] != "during-victory":
                continue
            awards[key] = {index: award for index, step in enumerate(ability["effects"])
                           if (award := regional_award(step["effect"], card.get("content_condition"),
                                                       include_global=include_global))}
        return {key: rows for key, rows in awards.items() if rows}

    def regional_award_value(self, owner, key, rows, *, pieces=None):
        position = self.economy.routes.position
        ability = self.rules.abilities[key]
        team = self.rules.team(owner)
        pieces = self.units if pieces is None else pieces
        value = 0.0
        for index, award in rows.items():
            if not all(condition_met(self.rules, self.state, owner, gate, pieces=pieces) for gate in award.gates):
                continue
            effect = ability["effects"][index]["effect"]
            recipient = point_recipient(self.rules, owner, effect["side"])
            side = 1 if self.rules.team(recipient) == team else -1
            occupier = award.filters.get("occupiedBy")
            if occupier:
                foe = "allies" if team == "axis" else "axis"
                occupied_team = {"self": team, "enemy": foe}.get(occupier, occupier)
                filters = {"force": "team" if occupied_team == team else "enemy", "kind": "army"}
            else:
                filters = award.filters
            eligible = [t for t in pieces if unit_matches(self.rules, self.state, owner, t, filters)]
            if not award.spaces:
                value += side * sum(position.proportional_base(recipient, t.position_id, award.amount)
                                    for t in eligible)
                continue
            estimates = []
            for region in award.spaces:
                base = position.proportional_base(recipient, region, abs(award.amount))
                distances = [position.distances(t.owner_country, t.position_id).get(region, inf) for t in eligible]
                present = distances.count(0)
                if award.mode == "each-unit" and present:
                    estimates.append(base * present)
                else:
                    estimates.append(position.distance_value(base, min(distances, default=inf)))
            area = max(estimates, default=0) if award.mode == "single" else sum(estimates)
            value += side * area * (-1 if award.amount < 0 else 1)
        return max(0, value) if ability["activation_mode"] == "OPTIONAL" else value

    @cached_property
    def active_regional_awards(self):
        active = defaultdict(list)
        for owner, cards in self.sources.items():
            for source, card in cards:
                if status_enabled(self.rules, self.state, self.state.tokens[source]):
                    active[owner].extend((key, rows) for key, rows in self.regional_awards(card, include_global=True).items()
                                         if key not in self.rules.hidden_score_rules.get(card, {}))
        return {owner: awards for owner, awards in active.items() if awards}

    def regional_placement(self, force, region):
        if not self.active_regional_awards:
            return 0
        kind = "ARMY" if self.rules.regions[region]["type"] == "LAND" else "NAVY"
        definition = next((key for key, unit in self.rules.units.items() if unit["kind"] == kind), None)
        if definition is None:
            return 0
        added = Token("forecast:regional-placement", definition, "UNIT", force, region)
        return self.regional_change(force, (*self.units, added))

    def regional_removal(self, token):
        return -self.regional_change(token.owner_country, [t for t in self.units if t.token_id != token.token_id])

    def regional_change(self, force, after):
        def value(owner, awards, pieces=None):
            optional, mandatory = 0, 0
            for key, rows in awards:
                estimate = self.regional_award_value(owner, key, rows, pieces=pieces)
                if self.rules.abilities[key]["activation_mode"] == "OPTIONAL":
                    optional = max(optional, estimate)
                else:
                    mandatory += estimate
            return optional + mandatory

        return sum((1 if self.rules.team(owner) == self.rules.team(force) else -1) *
                   (value(owner, awards, after) - value(owner, awards))
                   for owner, awards in self.active_regional_awards.items())

    def targets(self, owner, force, kind, action):
        return self.action_targets(self.state, owner, force, kind, action)

    def action_targets(self, state, owner, force, kind, action):
        op = {"type": ("LAND_BATTLE" if kind == "army" else "SEA_BATTLE") if action == "battle" else
              "RECRUIT" if action == "recruit" else "BUILD_" + kind.upper(), "force": force,
              "allied_effect": True, "unit_kind": kind.upper()}
        return tuple(self.legal.targets(state, owner, op))

    def after_event_targets(self, owner, force, kind, action, event, event_kind, region):
        """One hypothetical public move can open a beachhead or spend the last reserve."""
        state = self.state
        if event == "after-build":
            reserve = state.positions[zone(owner, "RESERVE_" + event_kind.upper())]
            if not reserve:
                return ()
            piece, destination = state.tokens[reserve[0]], region
        else:
            victims = self.enemies(owner, region, event_kind)
            if not victims:
                return self.targets(owner, force, kind, action)
            piece = victims[0]
            destination = zone(piece.owner_country, "RESERVE_" + event_kind.upper())
        origin = piece.position_id
        positions = {**state.positions, origin: [k for k in state.positions[origin] if k != piece.token_id],
                     destination: [*state.positions[destination], piece.token_id]}
        projected = replace(state, tokens={**state.tokens, piece.token_id: replace(piece, position_id=destination)},
                            positions=positions)
        return self.action_targets(projected, owner, force, kind, action)

    def adjacent(self, seat):
        return MapGraph(self.rules).adjacency(self.state, seat)

    def supplied(self, force):
        return SupplyResolver(self.rules).supplied(self.state, force, units=self.units)

    def location_value(self, region, battle=False, *, force=None):
        units = self.economy.units
        if self.action_distance and force:
            return self.economy.routes.target_value(force, region, battle=battle, construction=not battle)
        value = units.action_score(0.425 if battle else 0.55) + units.vp_score(0.65 * self.sites[region])
        if force and self.economy.map_weights:
            value += self.economy.routes.paper_bonus(force, region, battle=battle)
        return value

    def support_value(self, force, region, battle=False):
        if not self.action_distance:
            return self.location_value(region, battle)
        action = self.economy.units.action_score(0.425 if battle else 0.55)
        return action + (0.65 * self.economy.routes.capture_value(force, region) if region else 0)

    def possibility(self, owner, force, kind, spaces, later):
        """Chance that one additional unit can enter a named scoring area by a later window."""
        if not later:
            return 0
        countries = [f for f in self.rules.force_controllers if
                     (self.rules.team(f) == self.rules.team(owner) if force == "team" else
                      self.rules.team(f) != self.rules.team(owner) if force == "enemy" else
                      f == (owner if force == "self" else force))]
        best = 0
        for country in countries:
            seat = self.rules.force_controllers[country]
            options = self.targets(seat, country, kind, "build")
            if not options:
                continue
            accessible = bool(not spaces or set(options).intersection(spaces))
            nearby = not accessible and any(self.adjacent(seat)[p].intersection(spaces) for p in options)
            chance = self.chance(seat, (BASIC[kind][0],), later - 1)
            if accessible or (nearby and later >= 2):
                best = max(best, chance * self.config["arrival_accessible" if accessible else "arrival_nearby"])
        return best

    def board_condition(self, owner, condition, later):
        if not condition:
            return 1.0
        if condition["type"] == "all":
            return min(self.board_condition(owner, c, later) for c in condition["conditions"])
        f = condition["filter"]
        units = filtered_units(self.rules, self.state, owner, f)
        if condition_met(self.rules, self.state, owner, condition):
            threatened = any(any(self.rules.team(u.owner_country) != self.rules.team(t.owner_country) and
                             u.position_id in self.adjacent(owner)[t.position_id] for u in self.units) for t in units)
            if not units and condition.get("max") == 0:
                return 1 - self.possibility(owner, f["force"], f["kind"], tuple(f.get("spaces", ())), later)
            return self.config["condition_threatened" if threatened else "condition_safe"] ** min(later, self.config["condition_horizon"])
        if len(units) < condition["min"] <= len(units) + later:
            return self.possibility(owner, f["force"], f["kind"], tuple(f.get("spaces", ())), later) ** (condition["min"] - len(units))
        return 0

    def enemies(self, actor, region, kind):
        return [u for u in self.units if u.position_id == region and
                self.rules.team(u.owner_country) != self.rules.team(actor) and
                self.rules.units[u.definition_id]["kind"] == kind.upper()]

    def baseline_play(self, owner):
        # Opportunity cost of spending the regular play; the fallback is one forced discard.
        best = -self.economy.units.card_score(self.config["forced_discard_card_value"])
        for card in self.hand(owner):
            kind = card.get("legacy_kind")
            for unit, (build, battle) in BASIC.items():
                if kind == build:
                    best = max([best] + [self.location_value(p, force=owner) for p in self.targets(owner, owner, unit, "build")])
                if kind == battle:
                    best = max([best] + [self.location_value(p, True, force=owner) for p in self.targets(owner, owner, unit, "battle")
                                          if self.enemies(owner, p, unit)])
            if kind and kind not in ("status", "response", "bolster", "air-power") and kind not in BASIC_KINDS:
                best = max(best, self.economy.units.action_score(self.config["alternative_play_action_value"]))
        return best

    def trigger_card_kinds(self, owner, actor, trigger):
        kind = trigger["type"]
        relation = trigger.get("actor", trigger.get("builder", "any"))
        if ((relation == "self" or trigger.get("ownTurnOnly")) and actor != owner or
                relation == "enemy" and self.rules.team(actor) == self.rules.team(owner) or
                relation == "team" and self.rules.team(actor) != self.rules.team(owner) or
                relation in self.rules.countries and actor != relation):
            return None
        if kind == "economic-play":
            return ("economic",)
        if kind == "discard-stage-card":
            return (trigger["kind"],) if actor == trigger["country"] else None
        if kind == "unit-activity":
            force, terrain = trigger["force"], trigger["kind"]
            if actor == self.rules.force_controllers[force]:
                return (BASIC[terrain][0],) if set(trigger["actions"]) & {"build", "recruit"} else None
            if "attacked" in trigger["actions"] and self.rules.team(actor) != self.rules.team(force):
                return (BASIC[terrain][1],)
            return None
        if kind == "before-battle-removal":
            return (BASIC[trigger["kind"]][1],) if self.rules.team(actor) != self.rules.team(owner) else None
        if actor != owner and (kind in ("play-army-card", "after-air-action") or relation == "any"):
            return None
        if kind == "after-air-action":
            return ("air-power",)
        if kind == "play-army-card":
            return ("build-army",)
        return (BASIC[trigger["kind"]][int(kind == "battle-space")],)

    def known_triggers(self, actor, trigger, kinds):
        keyword = trigger.get("keyword")
        return sum((card.get("legacy_kind") in kinds or bool(self.related_actions(card).intersection(kinds))) and
                   (not keyword or keyword in card.get("text", "") + card.get("title", "")) for card in self.hand(actor))

    def signals(self, owner, actor, key, later, *, deterministic=False):
        """Mutually exclusive representative event locations with their probability."""
        trigger = trigger_of(self.rules.abilities[key])
        kind = trigger["type"]
        if kind in ("before-rule-use",):
            return ()
        if kind == "unit-activity":
            force, unit = trigger["force"], trigger["kind"]
            controller = self.rules.force_controllers[force]
            if actor == controller:
                return ((None, self.config["own_activity_likelihood"]),) if self.targets(actor, force, unit, "recruit") else ()
            if self.rules.team(actor) == self.rules.team(controller):
                return ()
            reachable = self.targets(actor, actor, unit, "battle")
            return tuple((p, self.config["enemy_activity_likelihood"] / len(reachable)) for p in reachable if any(
                u.owner_country == force for u in self.enemies(actor, p, unit)))
        if kind == "economic-play":
            return ((None, 1 if deterministic else self.chance(actor, ("economic",), later, trigger.get("keyword"))),)
        if not deterministic:
            if kind == "before-battle-removal":
                if self.rules.team(actor) == self.rules.team(owner):
                    return ()
            elif actor != owner:
                return ()
        if kind == "discard-stage-card":
            return ((None, 1 if deterministic else self.config["discard_activity_likelihood"] *
                     self.chance(owner, (trigger["kind"],), later)),)
        if kind == "play-army-card":
            return ((None, 1 if deterministic else self.chance(owner, ("build-army",), later)),) if self.targets(owner, owner, "army", "build") else ()
        if kind == "after-air-action" and deterministic:
            options = air_options(self.rules, self.state, owner, {"actions": ("deploy", "marshal")})
            terrain = trigger.get("spaceKind")
            return tuple((region, 1) for region in sorted({json.loads(option)[2] for option in options})
                         if not terrain or self.rules.regions[region]["type"].lower() == terrain)
        if kind not in ("after-build", "battle-space", "before-battle-removal"):
            return ((None, 1.0),)
        unit = trigger["kind"]
        build = kind == "after-build"
        targets = self.targets(actor, actor, unit, "build" if build else "battle")
        if not build:
            targets = [p for p in targets if self.enemies(actor, p, unit)]
        chance = self.chance(actor, (BASIC[unit][0 if build else 1],), later)
        weights = {p: 1 if deterministic else self.location_value(p, not build, force=actor) for p in targets}
        total = sum(weights.values())
        if not total:
            return ()
        signals = []
        for p, weight in weights.items():
            if trigger.get("spaces") and p not in trigger["spaces"]:
                continue
            if adjacent := trigger.get("adjacentSupplied"):
                if not any(u.position_id in self.adjacent(actor)[p] and
                           unit_matches(self.rules, self.state, owner, u, {**adjacent, "suppliedOnly": True})
                           for u in self.units):
                    continue
            victims = self.enemies(actor, p, unit)
            if kind == "before-battle-removal" and not any(
                    u.owner_country in trigger.get("victims", [owner]) and
                    (not trigger.get("suppliedOnly") or u.token_id in self.supplied(u.owner_country)) for u in victims):
                continue
            signals.append((p, 1 if deterministic else chance * weight / total))
        return tuple(signals)

    def military_effect(self, owner, effect, region, trigger, committed):
        kind, unit = effect["type"], effect.get("kind", "army")
        force = effect.get("force", owner)
        if force == "self":
            force = owner
        action = "build" if kind == "build-at-battle-space" else effect.get("action", kind)
        if self.marginal and not committed and region is None and trigger["type"] in ("after-build", "battle-space"):
            return self.economy.units.action
        projected = region and not committed and trigger["type"] in ("after-build", "battle-space")
        if projected:
            targets = set(self.after_event_targets(owner, force, unit, action, trigger["type"], trigger["kind"], region))
        else:
            targets = set(self.targets(owner, force, unit, action))
        if (action in ("build", "recruit") and self.action_distance
                and self.economy.routes.conservative_placement):
            targets = {p for p in targets if self.economy.routes.placement_supplied(force, p)}
        if self.action_distance and effect.get("nearAirAction"):
            if not region:
                return 0
            nearby = self.adjacent(force)[region]
            if self.marginal:
                position = self.economy.routes.position
                return max((position.value(force, place, construction=True,
                                           distance_offset=self.config["projected_distance_offset"])
                            for place in targets & nearby), default=0)
            return self.economy.routes.position.best_adjacent_site(force, targets & nearby)
        if kind == "build-at-battle-space":
            if self.action_distance and trigger["type"] in EXTRA_TRIGGERS:
                return self.economy.units.action if region in targets else 0
            return self.location_value(region, force=force) if region in targets else 0
        if "space" in effect:
            targets &= {effect["space"]}
        if "spaces" in effect:
            targets &= set(effect["spaces"])
        if region:
            if effect.get("nearBuilt") or effect.get("battleRange") == 1:
                targets &= self.adjacent(owner)[region] | ({region} if "battleRange" in effect else set())
            if effect.get("battleRange") == 0:
                targets &= {region}
        if action == "battle":
            targets = {p for p in targets if len(self.enemies(owner, p, unit)) >
                       int(bool(projected) and p == region and trigger["type"] == "battle-space")}
        if self.action_distance and trigger["type"] in EXTRA_TRIGGERS:
            return self.economy.units.action if targets else 0
        return max((self.location_value(p, action == "battle", force=force) for p in targets), default=0)

    def program(self, owner, actor, key, region, committed=False, later=0, excluded_steps=()):
        ability = self.rules.abilities[key]
        trigger = trigger_of(ability)
        net = 0
        for index, step in enumerate(ability["effects"]):
            if index in excluded_steps:
                continue
            effect = step["effect"]
            kind = effect["type"]
            if not condition_met(self.rules, self.state, owner, effect.get("when")):
                continue
            if self.action_distance and kind in CARD_RETURN_EFFECTS:
                net += self.economy.return_value(owner, effect)
            elif kind in POINT_EFFECTS:
                recipient, value = point_award(self.rules, self.state, owner, effect)
                if later and kind != "points":
                    f = effect.get("filter", {"force": "team", "kind": "army", "spaces": effect.get("spaces", ())})
                    potential = self.possibility(owner, f["force"], f["kind"], tuple(f.get("spaces", ())), later)
                    value = value * self.config["condition_safe"] ** min(self.config["condition_horizon"], later) + effect["amountPer"] * potential
                net += self.economy.units.vp_score(value if self.rules.team(recipient) == self.rules.team(owner) else -value)
            elif kind == "discard-top":
                target = actor if effect["target"] == "battle-attacker" else effect["target"]
                target = owner if target == "self" else target
                value = self.economy.mill(owner, target, effect["count"], 0.75)
                if target == owner and not value and not self.action_distance:
                    value = -0.06 * effect["count"]
                net += value
                if effect["target"] == "battle-attacker" and region and not committed:
                    alternatives = []
                    for p in self.targets(actor, actor, trigger["kind"], "battle"):
                        victims = self.enemies(actor, p, trigger["kind"])
                        protected = any(u.owner_country in trigger.get("victims", [owner]) for u in victims)
                        if p != region and victims and not (protected and p in trigger.get("spaces", (p,))):
                            alternatives.append(self.location_value(p, True))
                    net = min(net, max(0, self.location_value(region, True) - max(alternatives, default=0)))
            elif kind in EXTRA_ACTIONS:
                net += self.military_effect(owner, effect, region, trigger, committed)
            elif kind == "prevent-removal":
                if self.marginal:
                    victims = [t for t in self.units if t.position_id == region and
                               self.rules.team(t.owner_country) == self.rules.team(owner)]
                    routes = self.economy.routes
                    net += max((routes.protection_value(t.owner_country, region, token=t.token_id) for t in victims),
                               default=routes.protection_value(owner, region))
                else:
                    net += self.support_value(owner, region) if region else self.economy.units.action_score(0.55)
            elif self.marginal and kind in ("protect-unit-this-turn", "protect-units-this-turn", "supply-this-turn",
                                           "supply-units-this-turn"):
                if kind == "protect-unit-this-turn":
                    net += self.economy.routes.protection_value(owner, region)
                else:
                    net += self.economy.routes.tactical_effect(owner, step)
            elif kind == "discard-hand":
                n = effect["count"]
                hand = self.hand(owner)
                if effect.get("kinds"):
                    eligible = sum(c.get("legacy_kind") in effect["kinds"] or not c or c.get("simulation_unknown", False) for c in hand)
                else:
                    eligible = len(hand)
                if eligible < n:
                    return 0
                units = self.economy.units
                net -= units.card_score(n if units.enabled else n * (0.8 if effect.get("kinds") else 0.55))
            elif kind == "request-hand-discard":
                target = effect["target"]
                if target == "battle-attacker":
                    target = actor
                if target in TURN_ORDER:
                    sign = -1 if self.marginal and self.rules.team(target) == self.rules.team(owner) else 1
                    net += self.economy.units.card_score(sign * self.config["request_discard_card_value"] *
                                                        min(self.economy.count(target, "HAND"), effect["min"]))
            elif kind == "play-from-zone":
                if not any(self.rules.cards.get(self.state.tokens[k].definition_id, {}).get("legacy_kind") in effect["kinds"]
                           for k in self.state.positions[zone(owner, effect["zone"].upper())]) and not any(
                               c.get("legacy_kind") in effect["kinds"] for c in self.hand(owner)):
                    return 0
                net += max((self.location_value(p, force=owner) for p in self.targets(owner, owner, "army", "build")), default=0)
            elif kind == "return-declared-army-card":
                net += self.economy.units.card_score(0.8 if self.targets(owner, owner, "army", "build") else 0.3)
            elif kind == "put-hand-on-deck-bottom":
                net += self.config["filter_card_value"] * min(effect["max"], len(self.hand(owner)), self.economy.count(owner, "DECK"))
            elif kind == "order-deck-top":
                net += self.deck_top(owner, effect["count"])
            elif kind == "economic-discard-bonus":
                bonus = effect.get("bonusIf", {})
                extra = effect["amount"] + (bonus.get("amount", 0) if any(u.position_id == bonus.get("space") and
                        u.owner_country == bonus.get("force") and self.rules.units[u.definition_id]["kind"].lower() == bonus.get("kind")
                        for u in self.units) else 0)
                targets = [s for s in TURN_ORDER if self.rules.team(s) != self.rules.team(actor)]
                net += max(self.economy.mill(owner, s, extra, 0.75) for s in targets)
        return net

    def modifier_states(self, owner, card_id, source):
        state = replace(self.state, turn=replace(self.turn, country_index=TURN_ORDER.index(owner)))
        position = zone(owner, "STATUS")
        tokens = dict(state.tokens)
        if source and source not in self.disabled:
            tokens.pop(source)
            keys = [k for k in state.positions[position] if k != source]
        else:
            key = "forecast:" + card_id
            tokens[key] = Token(key, card_id, "CARD", owner, position)
            keys = [*state.positions[position], key]
        changed = replace(state, tokens=tokens, positions={**state.positions, position: keys})
        return (changed, state) if source and source not in self.disabled else (state, changed)

    def strategic_modifiers(self, owner, card_id, source):
        from .map_influence import ScoringRoutes

        mods = self.rules.cards[card_id].get("modifiers", ())
        endpoints = {region for m in mods if m["kind"] == "connection" for region in (m["a"], m["b"])}
        bonuses = [m for m in mods if m["kind"] == "score_bonus"]
        if not (endpoints or bonuses):
            return 0
        before, after = self.modifier_states(owner, card_id, source)
        old = ScoringRoutes(self.rules, before, self.units, self.economy.map_weights).position
        new = ScoringRoutes(self.rules, after, self.units, self.economy.map_weights).position
        offset = self.config["projected_distance_offset"] if self.marginal else 0
        value = old.connection_change(new, owner, endpoints, distance_offset=offset) if endpoints else 0
        for modifier in bonuses:
            site = modifier["site"]
            sign = -1 if modifier["amount"] < 0 else 1
            countries = tuple(seat for seat in self.rules.sites[site]["countries"]
                              if (modifier.get("scope", "owner") == "all" or seat == owner)
                              and modifier.get("force", seat) == seat
                              and modifier.get("team", self.rules.team(seat)) == self.rules.team(seat))
            values = (new.closest_site_interest(owner, region, abs(modifier["amount"]), countries)
                      for region in sorted(new.locations.get(site, ())))
            value += sign * max(values, key=abs, default=0)
        return value

    def board_changes(self, owner, card_id, source):
        """Counterfactual on projected pieces only. Separate supply already priced by B's map."""
        card = self.rules.cards[card_id]
        excluded = {"connection", "score_bonus"} if self.action_distance else set()
        mods = [m for m in card.get("modifiers", ()) if m["kind"] not in excluded and
                (m["kind"] in MAP_MODS or m["kind"] == "ignore_enemy_statuses")]
        if not mods:
            return 0, 0
        before, after = self.modifier_states(owner, card_id, source)
        supply = SupplyResolver(self.rules)
        option_delta = supplied_delta = 0
        for force, seat in self.rules.force_controllers.items():
            if not any(m.get("force", force) == force and force in m.get("forces", [force]) and
                       m.get("team", self.rules.team(force)) == self.rules.team(force) for m in mods):
                continue
            sign = 1 if self.rules.team(owner) == self.rules.team(force) else -1
            first = supply.supplied(before, force, units=self.units)
            second = supply.supplied(after, force, units=self.units)
            for piece in self.units:
                change = int(piece.token_id in second) - int(piece.token_id in first)
                if change:
                    value = (self.economy.routes.protection_value(force, piece.position_id, token=piece.token_id)
                             if self.marginal else self.support_value(force, piece.position_id))
                    supplied_delta += sign * change * value
            for action, unit in (("BUILD_ARMY", "ARMY"), ("BUILD_NAVY", "NAVY"), ("LAND_BATTLE", "ARMY"), ("SEA_BATTLE", "NAVY")):
                op = {"type": action, "unit_kind": unit, "force": force, "allied_effect": True}
                old = set(self.legal.targets(before, seat, op))
                new = set(self.legal.targets(after, seat, op))
                battle = action.endswith("BATTLE")
                if battle:
                    old = {p for p in old if self.enemies(seat, p, unit.lower())}
                    new = {p for p in new if self.enemies(seat, p, unit.lower())}
                if new != old:
                    def options_value(regions):
                        values = sorted((self.support_value(force, p, battle) for p in regions), reverse=True)
                        return sum(v * self.config["rule_option_decay"] ** i
                                   for i, v in enumerate(values[:self.config["rule_option_limit"]]))
                    option_delta += sign * self.config["rule_option_multiplier"] * (options_value(new) - options_value(old))
        return supplied_delta, option_delta

    def modifier(self, owner, actor, card_id, source, later, board):
        mods = self.rules.cards[card_id].get("modifiers", ())
        value = 0
        if actor == owner and any(m["kind"] in MAP_MODS or m["kind"] == "ignore_enemy_statuses" for m in mods):
            supply, options = self.board_changes(owner, card_id, source)
            value += options + (0 if board else supply)
        for m in mods:
            kind = m["kind"]
            if kind == "reallocate_cost" and actor == owner:
                if len(self.hand(owner)) >= m["count"] and self.economy.count(owner, "DECK"):
                    units = self.economy.units
                    value += units.card_score(self.config["exchange_saved_payment_value"] * (1 if units.enabled else 0.55) *
                                             max(0, self.rules.settings.get("reallocation_cost", 3) - m["count"]))
            elif kind == "reallocate_discard" and actor == owner:
                if any(self.rules.cards.get(self.state.tokens[k].definition_id, {}).get("legacy_kind")
                       in BASIC_KINDS for k in self.state.positions[zone(owner, "DISCARD")]):
                    value += self.economy.units.card_score(self.config["exchange_saved_payment_value"] * (
                        self.config["exchange_discard_base"] + self.config["exchange_discard_deck_bonus"] /
                        (1 + self.economy.count(owner, "DECK"))))
            elif kind == "economic_adjustment":
                if m["role"] == "actor" and actor == owner:
                    loss = max(self.economy.mill(owner, s, m["amount"], 0.75) for s in TURN_ORDER
                               if self.rules.team(s) != self.rules.team(owner))
                    value += self.chance(owner, ("economic",), later) * loss
                elif m["role"] == "target" and self.rules.team(owner) != self.rules.team(actor):
                    value += (self.chance(actor, ("economic",), later) * self.config["economic_target_share"] *
                              self.economy.mill(actor, owner, -m["amount"], 0.75))
            elif kind == "ignore_enemy_statuses" and actor == owner and not board:
                for foe in TURN_ORDER:
                    if self.rules.team(foe) == self.rules.team(owner):
                        continue
                    for _, other in self.sources[foe]:
                        for key in self.rules.cards[other].get("abilities", ()):
                            t = trigger_of(self.rules.abilities[key])["type"]
                            if t in ("before-battle-removal", "economic-play", "unit-activity"):
                                value += sum(p * max(0, self.program(foe, actor, key, region))
                                             for region, p in self.signals(foe, actor, key, later))
        return value

    def entries(self, owner, card_id, source, board, held=False):
        card = self.rules.cards[card_id]
        active = TURN_ORDER[self.turn.country_index]
        seat_count = len(TURN_ORDER)
        first_play = (TURN_ORDER.index(owner) - self.turn.country_index) % seat_count
        phase = STAGES.index(self.turn.phase)
        if first_play == 0 and self.turn.phase != "Setup" and (phase > 1 or self.turn.plays_remaining < 1):
            first_play += seat_count
        if held and first_play == 0 and self.turn.phase == "Play":
            first_play += seat_count
        rows = []
        counted = Counter()
        regional = self.regional_awards(card_id) if self.action_distance else {}
        hidden = self.rules.hidden_score_rules.get(card_id, {})
        income = {key: sum(site.income(self.rules, self.state, self.units)
                          for site in site_layers(owner, source or card_id, key, awards))
                  for key, awards in hidden.items()}
        repeating_look = set()
        if self.action_distance and self.economy.count(owner, "DECK"):
            for key in card.get("abilities", ()):
                ability = self.rules.abilities[key]
                steps = ability["effects"]
                if (len(steps) == 1 and steps[0]["effect"]["type"] == "order-deck-top"
                        and (self.marginal or steps[0]["effect"]["count"] == 1) and not ability.get("discard_cost")
                        and trigger_of(ability)["type"] in ("turn-start", "play-phase-start", "air-phase-start",
                                                            "during-victory", "discard-start")):
                    repeating_look.add(key)
        extras = ({key: result for key in card.get("abilities", ()) if (result := self.extra_reaction(owner, key))}
                  if self.action_distance and not self.marginal else {})
        conditional = {}
        if self.marginal:
            for key in card.get("abilities", ()):
                trigger = trigger_of(self.rules.abilities[key])
                if trigger["type"] in CONDITIONAL_TRIGGERS:
                    conditional[key] = {actor: self.known_triggers(actor, trigger, kinds) for actor in TURN_ORDER
                                        if (kinds := self.trigger_card_kinds(owner, actor, trigger)) is not None}
        limit = self.economy.horizon
        remaining = (self.rules.settings["max_rounds"] - self.turn.round_id + 1) * seat_count - self.turn.country_index
        if repeating_look and limit:
            limit = max(limit, min(remaining, first_play + 2 * seat_count)) if self.marginal else max(limit, remaining)
        if extras and limit:
            needed = max(count for count, _ in extras.values())
            limit = max(limit, min(remaining, first_play + (needed + 1) * seat_count))
        if conditional and limit:
            known = max((max(matches.values(), default=0) for matches in conditional.values()), default=0)
            limit = max(limit, min(remaining, first_play + (known + self.config["extra_trigger_opportunities"] + 1) * seat_count))
        enabled = not self.action_distance or condition_met(self.rules, self.state, owner, card.get("content_condition"))
        for delay in range(limit):
            actor = TURN_ORDER[(self.turn.country_index + delay) % seat_count]
            if (source in self.disabled and delay == 0) or (not source and delay < first_play) or (self.rules.team(owner) != self.rules.team(actor) and any(
                    any(m["kind"] == "ignore_enemy_statuses" for m in self.rules.cards[c].get("modifiers", ()))
                    for token, c in self.sources[actor] if token not in self.disabled or delay)):
                continue
            later = (delay + (self.turn.phase != "Setup" and actor == active and phase > 1)) // seat_count
            weight = self.discount ** (delay / seat_count)
            development = max(0, (delay - (0 if source else first_play)) // seat_count)
            weight *= self.board_condition(owner, card.get("content_condition"), development)
            for key in card.get("abilities", ()):
                ability = self.rules.abilities[key]
                trigger = trigger_of(ability)
                kind = trigger["type"]
                stage = STAGES.index(TRIGGER_PHASE[kind])
                if kind == "before-rule-use" and any(m["kind"] in MAP_MODS for m in card.get("modifiers", ())):
                    stage = STAGES.index("Supply")
                if delay == 0 and (stage < phase or (stage == phase and
                        (kind in ("turn-start", "play-phase-start", "air-phase-start", "during-victory", "discard-start") or
                         (stage == 1 and self.turn.plays_remaining < 1)))):
                    continue
                if not source and delay == first_play and stage <= 1 and actor == owner:
                    # Deployment happens after start-of-play and uses the regular play.
                    if kind in ("turn-start", "play-phase-start") or self.rules.settings["plays_per_turn"] <= 1:
                        continue
                if key in hidden:
                    if actor == owner and delay < self.economy.horizon and not (source and board):
                        if income[key]:
                            window = (delay, kind, "hidden-site", source or card_id, key)
                            rows.append((window, self.discount ** (delay / seat_count),
                                         self.economy.units.vp_score(income[key])))
                    continue
                if source and delay == 0 and (source, key) not in self.ready:
                    continue
                if key in extras:
                    count, net = extras[key]
                    if (enabled and actor == owner and counted[key] < count and
                            (net > 0 or ability["activation_mode"] != "OPTIONAL")):
                        rows.append(((delay, kind, trigger.get("kind"), None), 1, net))
                        counted[key] += 1
                    continue
                if key in repeating_look:
                    if enabled and actor == owner and (not self.marginal or not counted[key]):
                        value = self.deck_top(owner, ability["effects"][0]["effect"]["count"])
                        if not self.marginal or value > 0 or ability["activation_mode"] != "OPTIONAL":
                            rows.append(((delay, kind, "deck-look", None), 1, value))
                        counted[key] += 1
                    continue
                if key in conditional:
                    matches = conditional[key]
                    if actor not in matches or (not enabled and not self.board_condition(owner, card.get("content_condition"), development)):
                        continue
                    if counted[key, actor] >= matches[actor] and counted[key, "extra"] >= self.config["extra_trigger_opportunities"]:
                        continue
                    omitted = set(regional.get(key, ()))
                    if counted[key, "look"]:
                        omitted.update(i for i, step in enumerate(ability["effects"]) if step["effect"]["type"] == "order-deck-top")
                    options = self.signals(owner, actor, key, later, deterministic=True)
                    if not options:
                        if kind not in ("after-build", "battle-space", "before-battle-removal"):
                            continue
                        options = ((None, 1),)
                    net = max(self.program(owner, actor, key, region, excluded_steps=tuple(sorted(omitted))) for region, _ in options)
                    if net <= 0 and ability["activation_mode"] == "OPTIONAL":
                        continue
                    window = (delay, kind, trigger.get("kind"), None)
                    if actor != owner or ability["activation_mode"] != "OPTIONAL":
                        window += (source or card_id, key)
                    rows.append((window, 1, net))
                    if counted[key, actor] < matches[actor]:
                        counted[key, actor] += 1
                    else:
                        counted[key, "extra"] += 1
                    counted[key, "look"] += 1
                    continue
                if delay >= self.economy.horizon:
                    continue
                if key in regional and actor == owner and not counted[("regional", key)]:
                    value = self.regional_award_value(owner, key, regional[key])
                    if value:
                        window = (delay, kind, "regional", None)
                        if ability["activation_mode"] != "OPTIONAL":
                            window += (source or card_id, key)
                        rows.append((window, 1, value))
                    counted[("regional", key)] += 1
                if self.action_distance and actor == owner and enabled:
                    if kind == "before-rule-use" and counted["strategic"] == 0:
                        value = self.strategic_modifiers(owner, card_id, source)
                        if value:
                            rows.append(((delay, kind, "map-change", card_id), 1, value))
                        counted["strategic"] += 1
                    if any(step["effect"]["type"] == "order-deck-top" for step in ability["effects"]):
                        if not counted[key]:
                            rows.append(((delay, kind, "deck-top", None), 1, self.program(owner, actor, key, None)))
                            counted[key] += 1
                        continue
                    if kind == "after-air-action":
                        if not counted[key]:
                            best = self.airdrop_value(owner, key)
                            if best > 0:
                                rows.append(((delay, kind, None, None), 1, best))
                            counted[key] += 1
                        continue
                if not weight:
                    continue
                if kind == "before-rule-use":
                    permanent_supply = self.marginal and any(m["kind"] == "always_supplied" for m in card.get("modifiers", ()))
                    if permanent_supply and counted[key, "permanent-supply"]:
                        continue
                    value = self.modifier(owner, actor, card_id, source, later, board)
                    if permanent_supply and actor == owner:
                        value += self.economy.units.action
                        weight = 1
                        counted[key, "permanent-supply"] += 1
                    if value:
                        rows.append(((delay, "modifier", card_id), weight, value))
                    continue
                for region, probability in self.signals(owner, actor, key, later):
                    net = self.program(owner, actor, key, region, later=development,
                                       excluded_steps=tuple(regional.get(key, ())))
                    if kind == "USE_STATUS":
                        net = max(0, net - self.baseline_play(owner))
                    if ability["activation_mode"] == "OPTIONAL":
                        net = max(0, net)
                    if probability and net:
                        window = (delay, kind, trigger.get("kind"), region)
                        if kind != "USE_STATUS" and (actor != owner or ability["activation_mode"] != "OPTIONAL"):
                            window += (source or card_id, key)
                        rows.append((window, weight * probability, net))
        return rows

    def portfolio(self, owner, board):
        return self._portfolio(owner, board, residual=False)

    def residual(self, owner):
        return self._portfolio(owner, True, residual=True)

    def _portfolio(self, owner, board, *, residual):
        values = {}
        for source, card in self.sources[owner]:
            if residual and not any(trigger_of(self.rules.abilities[key])["type"] in CONDITIONAL_TRIGGERS or
                                    any(step["effect"]["type"] == "order-deck-top" for step in self.rules.abilities[key]["effects"])
                                    for key in self.rules.cards[card].get("abilities", ())):
                continue
            for window, probability, gain in self.entries(owner, card, source, board):
                if residual and window[1] not in CONDITIONAL_TRIGGERS and window[2] not in ("deck-top", "deck-look"):
                    continue
                previous = values.get(window)
                value = probability * gain
                values[window] = value if previous is None else max(previous, value)
        return values

    def report(self, owner, card, held=False):
        existing = self.portfolio(owner, False)
        added = {}
        for window, weight, net in self.entries(owner, card, None, False, held):
            value = weight * net
            if window not in added or added[window][0] < value:
                added[window] = value, weight, net
        turns, gross, effective = set(), 0.0, 0.0
        incremental = 0.0
        for window, (value, weight, net) in added.items():
            gross += value
            change = max(0, value - existing[window]) if window in existing else value
            incremental += change
            if change:
                turns.add(window[0])
                effective += weight
        breakdown = defaultdict(float)
        for window, (value, _, _) in added.items():
            breakdown[window[1]] += max(0, value - existing[window]) if window in existing else value
        return {"windows": len(turns), "effective": round(effective, 4),
                "by_trigger": {k: round(v, 4) for k, v in breakdown.items()},
                "gross": round(gross, 4), "competition": round(gross - incremental, 4),
                "future": round(incremental * (self.held_factor if held else 1), 4)}
