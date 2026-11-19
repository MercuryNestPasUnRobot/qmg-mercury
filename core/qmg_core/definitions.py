from copy import deepcopy
import json
from pathlib import Path

from .model import CoreError, TURN_ORDER, digest

OPERATIONS = {"TRANSFER", "CREATE", "BUILD_ARMY", "BUILD_NAVY", "RECRUIT", "LAND_BATTLE", "SEA_BATTLE",
              "ELIMINATE", "DRAW", "DISCARD", "GAIN_VP", "CANCEL", "CHOOSE", "REDEPLOY"}
CONNECTION_USES = frozenset({"supply", "build", "battle"})


class RuleSet:
    """Data-only instruction vocabulary. Unknown instructions fail on loading."""
    def __init__(self, data, *, projected=False):
        self.data = deepcopy(data)
        self.version = data["ruleset_version"]
        if not isinstance(self.version, str) or not self.version:
            raise CoreError("Ruleset version required")
        self.countries = self._index(data["countries"], "id")
        self.regions = self._index(data["regions"], "id")
        self.cards = self._index(data["cards"], "id")
        self.units = self._index(data["units"], "id")
        self.abilities = self._index(data["abilities"], "id")
        # Expand the original content vocabulary into ordinary settlement instructions.
        for ability in list(self.abilities.values()):
            policy = ability.get("reset_policy")
            if policy is None:
                continue
            if policy not in ("CHECKPOINT", "NEVER", "TURN_END", "OWNER_TURN_START") or "after" in ability:
                raise CoreError("Invalid ability lifecycle")
            ability["after"] = "origin" if policy == "CHECKPOINT" else "delete"
            if policy in ("TURN_END", "OWNER_TURN_START"):
                listener = "renew-" + ability["id"]
                if listener in self.abilities:
                    raise CoreError("Duplicate generated listener definition")
                trigger = ({"operation_type": "TURN_END"} if policy == "TURN_END" else
                           {"operation_type": "BEGIN_PHASE", "actor": "self", "turn_start": True})
                self.abilities[listener] = {
                    "id": listener, "trigger": trigger, "activation_mode": "AUTO", "after": "delete",
                    "priority": ability["priority"], "effects": [
                        {"type": "CREATE", "token_type": "ABILITY", "definition": ability["id"], "zone": "ABILITY_READY", "same_copy": True}]}
                ability.setdefault("on_settle", []).append(
                    {"type": "CREATE", "token_type": "ABILITY", "definition": listener, "zone": "RESPONSE", "same_copy": True})
        self.settings = deepcopy(data["settings"])
        staging = self.settings.get("staging_countries", [])
        if not isinstance(staging, list) or any(not isinstance(country, str) or country not in TURN_ORDER for country in staging):
            raise CoreError("Invalid staging countries")
        self.sites = self._index(data["score_sites"], "id")
        if tuple(data["turn_order"]) != TURN_ORDER or set(self.countries) != set(TURN_ORDER):
            raise CoreError("The six-country architecture turn order is required")
        self.connections = self._index(data["connections"], "id")
        for c in self.connections.values():
            if c["a"] not in self.regions or c["b"] not in self.regions or c["a"] == c["b"]:
                raise CoreError("Invalid connection")
            self._connection_uses(c)
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
            if "after" in card and card["after"] not in ("origin", "delete"):
                raise CoreError("Invalid card settlement")
            self._effects(card["effects"])
            self._completion_transfers(card.get("on_settle", []))
            if any(a not in self.abilities for a in card.get("abilities", [])):
                raise CoreError("Unknown ability")
            if card["type"] == "RESPONSE" and (card["effects"] or not card.get("abilities")) and not card.get("simulation_unknown"):
                raise CoreError("Face-down responses must define their effects on abilities")
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
                if m["kind"] == "connection":
                    self._connection_uses(m)
                if m["kind"] in ("supply_source", "build_region") and m["region"] not in self.regions:
                    raise CoreError("Invalid modifier region")
                if m["kind"] == "build_region" and m["unit_kind"] not in ("ARMY", "NAVY"):
                    raise CoreError("Invalid build modifier")
                if m["kind"] in ("score_bonus", "ability_cost") and type(m["amount"]) is not int:
                    raise CoreError("Invalid numeric modifier")
                if m["kind"] == "score_bonus" and m["site"] not in self.sites:
                    raise CoreError("Unknown score site modifier")
        for ability in self.abilities.values():
            if ability["activation_mode"] not in ("AUTO", "OPTIONAL") or ability.get("after") not in ("origin", "delete"):
                raise CoreError("Invalid ability lifecycle")
            if type(ability.get("copies", 1)) is not int or ability.get("copies", 1) < 1:
                raise CoreError("Ability copies must be a positive integer")
            if "pool_capacity" in ability and (type(ability["pool_capacity"]) is not int or ability["pool_capacity"] < 1):
                raise CoreError("Pool capacity must be a positive integer")
            if type(ability["priority"]) is not int or type(ability.get("discard_cost", 0)) is not int or ability.get("discard_cost", 0) < 0:
                raise CoreError("Explicit ability priority and nonnegative cost required")
            self._effects(ability["effects"])
            self._completion_transfers(ability.get("on_settle", []))
            trigger = ability["trigger"]
            if not set(trigger) <= {"operation_type", "announced_type", "actor", "target_owner", "source_definition_id", "turn_start"} or trigger.get("actor", "any") not in ("self", "enemy", "any") or trigger.get("target_owner", "self") != "self":
                raise CoreError("Unsupported trigger predicate")
            if "turn_start" in trigger and (trigger["operation_type"] != "BEGIN_PHASE" or type(trigger["turn_start"]) is not bool):
                raise CoreError("turn_start is a phase entry predicate")
            if "source_definition_id" in trigger and trigger["source_definition_id"] not in self.cards and trigger["source_definition_id"] not in self.abilities:
                raise CoreError("Unknown trigger source definition")
            if trigger["operation_type"] not in OPERATIONS | {"PLAY_CARD", "ANNOUNCE", "BEGIN_PHASE", "END_PHASE", "TURN_END", "OBJECT_SETTLED", "CHECK_SUPPLY", "CHECK_SCORE", "CHECK_DISCARD"}:
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
        self.bound_abilities = {}
        for card in self.cards.values():
            reachable = set()
            pending = list(card.get("abilities", []))
            for op in card["effects"] + card.get("on_settle", []):
                if op["type"] == "CREATE" and op["token_type"] == "ABILITY":
                    pending.append(op["definition"])
            while pending:
                ability_id = pending.pop()
                if ability_id not in reachable:
                    reachable.add(ability_id)
                    ability = self.abilities[ability_id]
                    pending.extend(op["definition"] for op in ability["effects"] + ability.get("on_settle", [])
                                   if op["type"] == "CREATE" and op["token_type"] == "ABILITY")
            self.bound_abilities[card["id"]] = reachable
        from .rules import initial_inventory
        self.inventory = initial_inventory(self, projected)
        self.copy_groups = {}
        for key, token in sorted(self.inventory.items()):
            if token.token_type == "ABILITY":
                self.copy_groups.setdefault((token.source_token_id, token.definition_id), []).append(key)

    @staticmethod
    def _connection_uses(definition):
        tags = definition.get("movement_or_supply_tags")
        if tags is not None and (not isinstance(tags, list) or not tags or
                                 any(not isinstance(tag, str) or tag not in CONNECTION_USES for tag in tags) or
                                 len(tags) != len(set(tags))):
            raise CoreError("Invalid connection tags")

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
                if op["operation"]["type"] not in ("BUILD_ARMY", "BUILD_NAVY", "RECRUIT", "LAND_BATTLE", "SEA_BATTLE", "ELIMINATE", "DISCARD", "TRANSFER"):
                    raise CoreError("Unsupported target choice operation")
            if op["type"] == "GAIN_VP" and type(op.get("amount")) is not int:
                raise CoreError("Invalid VP amount")
            if op["type"] == "DRAW" and (type(op.get("count", 1)) is not int or op.get("count", 1) < 0):
                raise CoreError("Invalid draw count")
            if op["type"] == "TRANSFER":
                if any(not isinstance(op.get(key), str) or not op[key] for key in ("token", "destination")):
                    raise CoreError("Transfer requires a token and destination")
                if "expected_from" in op and not isinstance(op["expected_from"], str):
                    raise CoreError("Invalid expected source position")
                destinations = op.get("destinations")
                if destinations is not None and (not isinstance(destinations, list) or not destinations or
                                                 any(not isinstance(position, str) for position in destinations)):
                    raise CoreError("Invalid transfer destinations")
                if op.get("index") is not None and (type(op["index"]) is not int or op["index"] < 0):
                    raise CoreError("Transfer position index must be nonnegative")
            if op["type"] == "CREATE":
                kind = op.get("token_type")
                if "token" in op and (not isinstance(op["token"], str) or not op["token"]):
                    raise CoreError("Pool token must be an instance ID")
                if "same_copy" in op and (kind != "ABILITY" or type(op["same_copy"]) is not bool or "token" in op):
                    raise CoreError("Invalid pool copy selection")
                if kind == "ABILITY":
                    if op.get("definition") not in self.abilities or op.get("zone") not in ("RESPONSE", "ABILITY_READY"):
                        raise CoreError("Ability creation needs a known definition and a listening zone")
                elif kind == "CARD":
                    if op.get("definition") not in self.cards or op.get("zone") not in ("HAND", "DECK", "DISCARD", "RESPONSE", "STATUS"):
                        raise CoreError("Card creation needs a known definition and card zone")
                else:
                    raise CoreError("Unknown created object type")
            if op["type"] in ("BUILD_ARMY", "BUILD_NAVY", "RECRUIT", "LAND_BATTLE", "SEA_BATTLE") and op.get("target") != "$choice" and op.get("target") not in self.regions:
                raise CoreError("Unknown target region")

    def _completion_transfers(self, effects):
        self._effects(effects)
        if any(effect["type"] not in ("TRANSFER", "CREATE") or effect.get("destination") == "$choice" for effect in effects):
            raise CoreError("Settlement requires a concrete transfer or creation")
