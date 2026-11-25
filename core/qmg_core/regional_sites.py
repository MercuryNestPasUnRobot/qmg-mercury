"""Score locations granted by deployed status cards, separate from supply sources."""
from dataclasses import dataclass

from .content_effects import condition_met, unit_matches
from .model import TURN_ORDER, Token, zone
from .rules import status_enabled


REGION_FILTER_FIELDS = {"force", "forces", "kind", "kinds", "spaces", "excludeSpaces"}


@dataclass(frozen=True)
class RegionalAward:
    spaces: tuple
    filters: dict
    amount: int
    mode: str
    gates: tuple


def regional_award(effect, condition, *, include_global=False, include_absence=False):
    kind = effect["type"]
    when = effect.get("when")
    gates = tuple(gate for gate in (condition, when) if gate)
    if kind == "points-per-occupied-space":
        return RegionalAward(tuple(dict.fromkeys(effect["spaces"])), {"kind": "army", "occupiedBy": effect["occupiedBy"]},
                             effect["amountPer"], "each-space", gates)
    if kind == "points-per-unit":
        filters, amount, mode = effect["filter"], effect["amountPer"], "each-unit"
    elif kind == "points":
        occupation = next((gate for gate in gates if gate["type"] == "unit-count" and gate["filter"].get("spaces") and
                           ((gate["min"] == 1 and "max" not in gate) or
                            (include_absence and gate["min"] == gate.get("max") == 0))), None)
        if occupation is None:
            return None
        filters, amount = occupation["filter"], effect["amount"]
        mode = "single" if occupation["min"] else "absence"
        gates = tuple(gate for gate in gates if gate is not occupation)
    else:
        return None
    if filters.keys() - REGION_FILTER_FIELDS or (not filters.get("spaces") and not include_global):
        return None
    excluded = set(filters.get("excludeSpaces", ()))
    spaces = tuple(space for space in dict.fromkeys(filters.get("spaces", ())) if space not in excluded)
    if not spaces and "spaces" in filters:
        return None
    return RegionalAward(spaces, {key: value for key, value in filters.items() if key not in ("spaces", "excludeSpaces")},
                         amount, mode, gates)


def compile_regional_sites(rules):
    compiled = {}
    if not rules.settings.get("regional_status_sites"):
        return compiled
    for card_id, card in rules.cards.items():
        if card["type"] != "STATUS" or card.get("implementation", {}).get("status") == "blocked":
            continue
        for key in card.get("abilities", ()):
            ability = rules.abilities[key]
            if (ability["trigger"].get("card_trigger", {}).get("type") != "during-victory"
                    or ability.get("after") != "origin" or ability.get("on_settle") or ability.get("discard_cost")):
                continue
            steps = [step["effect"] for step in ability["effects"] if step["type"] == "CARD_EFFECT"]
            if not steps or len(steps) != len(ability["effects"]) or any(step.get("side") != "self" for step in steps):
                continue
            awards = tuple(regional_award(step, card.get("content_condition"), include_absence=True) for step in steps)
            if all(award and award.amount > 0 for award in awards):
                compiled.setdefault(card_id, {})[key] = awards
    return compiled


@dataclass(frozen=True)
class HiddenSite:
    key: str
    owner: str
    award: RegionalAward
    spaces: tuple

    def eligible(self, rules, state, token):
        filters = self.award.filters
        occupier = filters.get("occupiedBy")
        if occupier:
            own = rules.team(self.owner)
            enemy = "axis" if own == "allies" else "allies"
            expected = {"self": own, "enemy": enemy}.get(occupier, occupier)
            return rules.team(token.owner_country) == expected and rules.units[token.definition_id]["kind"] == "ARMY"
        return unit_matches(rules, state, self.owner, token, filters)

    def active(self, rules, state, pieces=None):
        return all(condition_met(rules, state, self.owner, gate, pieces=pieces) for gate in self.award.gates)

    def build_eligible(self, rules, state, force, region, definitions):
        if rules.team(force) != rules.team(self.owner) or self.award.mode == "absence":
            return False
        kind = "ARMY" if rules.regions[region]["type"] == "LAND" else "NAVY"
        definition = definitions.get(kind)
        return bool(definition and self.eligible(rules, state, Token("", definition, "UNIT", force, region)))

    def income(self, rules, state, pieces):
        if not self.active(rules, state, pieces):
            return 0
        count = sum(t.position_id in self.spaces and self.eligible(rules, state, t) for t in pieces)
        if self.award.mode == "absence":
            return self.award.amount if count == 0 else 0
        return self.award.amount * (count if self.award.mode == "each-unit" else int(count > 0))


def site_layers(owner, source, key, awards):
    for index, award in enumerate(awards):
        label = f"hidden:{source}:{key}:{index}"
        if award.mode in ("absence", "single"):
            yield HiddenSite(label, owner, award, award.spaces)
        else:
            for region in award.spaces:
                yield HiddenSite(f"{label}:{region}", owner, award, (region,))


def active_hidden_sites(rules, state, *, owner=None):
    if not rules.hidden_score_rules:
        return
    for seat in (owner,) if owner else TURN_ORDER:
        for key in state.positions.get(zone(seat, "STATUS"), ()):
            token = state.tokens[key]
            abilities = rules.hidden_score_rules.get(token.definition_id)
            if abilities and status_enabled(rules, state, token):
                for ability, awards in abilities.items():
                    yield from site_layers(seat, key, ability, awards)
