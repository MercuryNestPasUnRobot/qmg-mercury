from copy import deepcopy
import json
from pathlib import Path

from .model import CoreError, TURN_ORDER, digest

OPERATIONS = {"TRANSFER", "BUILD_ARMY", "BUILD_NAVY", "RECRUIT", "LAND_BATTLE", "SEA_BATTLE",
              "ELIMINATE", "DRAW", "DISCARD", "GAIN_VP", "CANCEL", "CHOOSE", "REDEPLOY"}


class RuleSet:
    """Data-only instruction vocabulary. Unknown instructions fail on loading."""
    def __init__(self, data):
        self.data = deepcopy(data)
        self.version = data["ruleset_version"]
        if not isinstance(self.version, str) or not self.version:
            raise CoreError("Ruleset version required")
        self.countries = self._index(data["countries"], "id")
        self.regions = self._index(data["regions"], "id")
        self.cards = self._index(data["cards"], "id")
        self.units = self._index(data["units"], "id")
        self.abilities = self._index(data["abilities"], "id")
        self.settings = deepcopy(data["settings"])
        self.sites = self._index(data["score_sites"], "id")
        if tuple(data["turn_order"]) != TURN_ORDER or set(self.countries) != set(TURN_ORDER):
            raise CoreError("The six-country architecture turn order is required")
        self.connections = self._index(data["connections"], "id")
        for c in self.connections.values():
            if c["a"] not in self.regions or c["b"] not in self.regions or c["a"] == c["b"]:
                raise CoreError("Invalid connection")
        for country in self.countries.values():
            if country["team"] not in ("axis", "allies"):
                raise CoreError("Invalid team")
        for r in self.regions.values():
            if r["type"] not in ("LAND", "SEA") or (r.get("home_country") and r["home_country"] not in self.countries):
                raise CoreError("Invalid region")
        for u in self.units.values():
            if u["kind"] not in ("ARMY", "NAVY"):
                raise CoreError("Invalid unit kind")
        for card in self.cards.values():
            if card["country"] not in self.countries or card["type"] not in ("EVENT", "STATUS", "RESPONSE"):
                raise CoreError("Invalid card")
            if card["failure_policy"] not in ("stop", "continue"):
                raise CoreError("Explicit card failure policy required")
            self._effects(card["effects"])
            if any(a not in self.abilities for a in card.get("abilities", [])):
                raise CoreError("Unknown ability")
            for req in card.get("requirements", []):
                if req.get("kind") not in ("min_hand", "occupied", "phase"):
                    raise CoreError("Unknown card requirement")
                if req["kind"] == "min_hand" and (type(req["count"]) is not int or req["count"] < 0):
                    raise CoreError("Invalid hand requirement")
                if req["kind"] == "occupied" and req["region"] not in self.regions:
                    raise CoreError("Invalid requirement region")
            for m in card.get("modifiers", []):
                if m.get("kind") not in ("connection", "supply_source", "always_supplied", "build_region", "score_bonus", "ability_cost") or m.get("scope", "owner") not in ("owner", "all"):
                    raise CoreError("Unknown rule modifier or scope")
                if m["kind"] == "connection" and (m["a"] not in self.regions or m["b"] not in self.regions or type(m["enabled"]) is not bool):
                    raise CoreError("Invalid connection modifier")
                if m["kind"] in ("supply_source", "build_region") and m["region"] not in self.regions:
                    raise CoreError("Invalid modifier region")
                if m["kind"] == "build_region" and m["unit_kind"] not in ("ARMY", "NAVY"):
                    raise CoreError("Invalid build modifier")
                if m["kind"] in ("score_bonus", "ability_cost") and type(m["amount"]) is not int:
                    raise CoreError("Invalid numeric modifier")
                if m["kind"] == "score_bonus" and m["site"] not in self.sites:
                    raise CoreError("Unknown score site modifier")
        for ability in self.abilities.values():
            if ability["activation_mode"] not in ("AUTO", "OPTIONAL") or ability["reset_policy"] not in ("CHECKPOINT", "TURN_END", "OWNER_TURN_START", "NEVER"):
                raise CoreError("Invalid ability lifecycle")
            if type(ability["priority"]) is not int or type(ability.get("discard_cost", 0)) is not int or ability.get("discard_cost", 0) < 0:
                raise CoreError("Explicit ability priority and nonnegative cost required")
            self._effects(ability["effects"])
            trigger = ability["trigger"]
            if not set(trigger) <= {"operation_type", "announced_type", "actor", "target_owner"} or trigger.get("actor", "any") not in ("self", "enemy", "any") or trigger.get("target_owner", "self") != "self":
                raise CoreError("Unsupported trigger predicate")
            if trigger["operation_type"] not in OPERATIONS | {"PLAY_CARD", "ANNOUNCE", "BEGIN_PHASE", "END_PHASE", "CHECK_SUPPLY", "CHECK_SCORE", "CHECK_DISCARD"}:
                raise CoreError("Unsupported listener checkpoint")
            if not set(ability.get("listener_scope", ["STATUS", "RESPONSE"])) <= {"STATUS", "RESPONSE"}:
                raise CoreError("Unsupported listener scope")
        for site in self.sites.values():
            if site["region"] not in self.regions or type(site["vp"]) is not int or site["vp"] < 0:
                raise CoreError("Invalid score site")
            if not set(site["countries"]) <= set(self.countries):
                raise CoreError("Invalid score eligibility")
        for k in ("hand_limit", "initial_hand", "setup_discard", "plays_per_turn", "max_rounds", "victory_margin", "max_atomic_steps"):
            if type(self.settings[k]) is not int or self.settings[k] < 0:
                raise CoreError(f"Invalid setting: {k}")
        if self.settings["setup_discard"] > self.settings["initial_hand"] or self.settings["max_atomic_steps"] == 0 or self.settings["max_rounds"] == 0:
            raise CoreError("Invalid setup or execution budget")
        self.fingerprint = digest(self.data)

    @staticmethod
    def _index(rows, key):
        result = {}
        for row in deepcopy(rows):
            name = row[key]
            if not isinstance(name, str) or not name or ":" in name or name in result:
                raise CoreError("Duplicate or invalid definition ID")
            result[name] = row
        return result

    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def team(self, country):
        return self.countries[country]["team"]

    def _effects(self, effects):
        for op in effects:
            if op.get("type") not in OPERATIONS:
                raise CoreError(f"Unsupported instruction: {op.get('type')}")
            if op["type"] == "CHOOSE":
                self._effects([op["operation"]])
                if op["operation"]["type"] not in ("BUILD_ARMY", "BUILD_NAVY", "RECRUIT", "LAND_BATTLE", "SEA_BATTLE", "ELIMINATE", "DISCARD"):
                    raise CoreError("Unsupported target choice operation")
            if op["type"] == "GAIN_VP" and type(op.get("amount")) is not int:
                raise CoreError("Invalid VP amount")
            if op["type"] == "DRAW" and (type(op.get("count", 1)) is not int or op.get("count", 1) < 0):
                raise CoreError("Invalid draw count")
            if op["type"] in ("BUILD_ARMY", "BUILD_NAVY", "RECRUIT", "LAND_BATTLE", "SEA_BATTLE") and op.get("target") != "$choice" and op.get("target") not in self.regions:
                raise CoreError("Unknown target region")
