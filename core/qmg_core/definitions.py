from copy import deepcopy
from functools import cached_property
import json
from pathlib import Path

from .model import CoreError, TURN_ORDER, PHASES, digest

CONSTRUCTION = {"BUILD_ARMY", "BUILD_NAVY", "RECRUIT"}
BATTLES = {"LAND_BATTLE", "SEA_BATTLE"}
FORCE_OPERATIONS = CONSTRUCTION | BATTLES
TARGETED_OPERATIONS = FORCE_OPERATIONS | {"ELIMINATE", "DISCARD", "TRANSFER"}
OPERATIONS = TARGETED_OPERATIONS | {"CREATE", "DRAW", "GAIN_VP", "CANCEL", "CHOOSE", "REDEPLOY", "CARD_EFFECT"}
CONNECTION_USES = frozenset({"supply", "build", "battle"})
PER_UNIT_SITE = "per-unit"


class RuleSet:
    """Data-only instruction vocabulary. Unknown instructions fail on loading."""
    def __init__(self, data, *, projected=False):
        self.data = deepcopy(data)
        self.projected = projected
        self.version = data["ruleset_version"]
        if not isinstance(self.version, str) or not self.version:
            raise CoreError("Ruleset version required")
        self.countries = self._index(data["countries"], "id")
        self.auxiliary_forces = self._index(data.get("auxiliary_forces", []), "id")
        self.force_controllers = {country: country for country in self.countries}
        for key, force in self.auxiliary_forces.items():
            controller = force.get("controller")
            if not isinstance(controller, str) or controller not in self.countries or key in self.countries:
                raise CoreError(f"Invalid controller or duplicate ID for auxiliary force {key}")
            if "name" in force and (not isinstance(force["name"], str) or not force["name"].strip()):
                raise CoreError(f"Invalid name for auxiliary force {key}")
            self.force_controllers[key] = controller
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
                    "system_response": True,
                    "priority": ability["priority"], "effects": [
                        {"type": "CREATE", "token_type": "ABILITY", "definition": ability["id"], "zone": "ABILITY_READY", "same_copy": True}]}
                ability.setdefault("on_settle", []).append(
                    {"type": "CREATE", "token_type": "ABILITY", "definition": listener, "zone": "RESPONSE", "same_copy": True})
        self.settings = deepcopy(data["settings"])
        self.phases = PHASES if self.settings.get("air_phase") else tuple(p for p in PHASES if p != "Air")
        if any(key not in self.abilities for key in self.settings.get("unit_responses", [])):
            raise CoreError("Unknown temporary unit response")
        opportunities = self.settings.get("turn_opportunities", {})
        if not isinstance(opportunities, dict) or any(key not in self.abilities or force not in self.force_controllers
                                                     for key, force in opportunities.items()):
            raise CoreError("Invalid turn opportunity inventory")
        area = self.settings.get("area_responses", {})
        if not isinstance(area, dict) or any(key not in self.cards or not isinstance(values, list)
                or any(value not in self.abilities for value in values) for key, values in area.items()):
            raise CoreError("Invalid area response inventory")
        for strait in self.settings.get("straits", []):
            if (strait.get("land") not in self.regions or len(strait.get("seas", [])) != 2
                    or any(p not in self.regions or self.regions[p]["type"] != "SEA" for p in strait["seas"])):
                raise CoreError("Invalid strait definition")
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
            if r["type"] not in ("LAND", "SEA") or any(
                r.get(field) is not None and r[field] not in self.force_controllers
                for field in ("home_country", "supply_source_for")
            ):
                raise CoreError("Invalid region")
            restricted = r.get("construction_requires_adjacent_for", [])
            if not isinstance(restricted, list) or any(
                    not isinstance(force, str) or force not in self.force_controllers for force in restricted):
                raise CoreError("Invalid construction restriction")
        for u in self.units.values():
            if u["kind"] not in ("ARMY", "NAVY", "AIR"):
                raise CoreError("Invalid unit kind")
        for card in self.cards.values():
            if card["country"] not in self.countries or card["type"] not in ("EVENT", "STATUS", "RESPONSE"):
                raise CoreError("Invalid card")
            if "title" in card and (not isinstance(card["title"], str) or not card["title"].strip()):
                raise CoreError("Card title must be non-empty text")
            if card["failure_policy"] not in ("stop", "continue"):
                raise CoreError("Explicit card failure policy required")
            if "play_as" in card and (not isinstance(card["play_as"], list) or not card["play_as"]
                                     or any(mode not in CONSTRUCTION - {"RECRUIT"} | BATTLES for mode in card["play_as"])):
                raise CoreError("Invalid alternate play modes")
            if "after" in card and card["after"] not in ("origin", "delete"):
                raise CoreError("Invalid card settlement")
            self._programs(card)
            if any(a not in self.abilities for a in card.get("abilities", [])):
                raise CoreError("Unknown ability")
            if card.get("active_ability") and card["active_ability"] not in card.get("abilities", []):
                raise CoreError("Active ability must belong to the card")
            if "content_condition" in card:
                from .content_effects import validate_condition
                validate_condition(self, card["content_condition"])
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
                if m.get("kind") not in ("connection", "supply_source", "deny_supply_source", "home",
                                        "always_supplied", "build_region", "score_bonus", "ability_cost",
                                        "economic_adjustment", "supply_sharing", "recruit_region",
                                        "ignore_enemy_statuses", "reallocate_cost", "reallocate_discard") or m.get("scope", "owner") not in ("owner", "all"):
                    raise CoreError("Unknown rule modifier or scope")
                if m["kind"] == "connection" and (m["a"] not in self.regions or m["b"] not in self.regions or type(m["enabled"]) is not bool):
                    raise CoreError("Invalid connection modifier")
                if m["kind"] == "connection":
                    self._connection_uses(m)
                if m["kind"] in ("supply_source", "deny_supply_source", "home", "build_region", "recruit_region") and m["region"] not in self.regions:
                    raise CoreError("Invalid modifier region")
                if "force" in m and m["force"] not in self.force_controllers:
                    raise CoreError("Invalid modifier force")
                if "team" in m and m["team"] not in ("axis", "allies"):
                    raise CoreError("Invalid modifier team")
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
            self._programs(ability)
            trigger = ability["trigger"]
            if "card_trigger" in trigger:
                from .content_triggers import supported_trigger, validate_trigger
                if not supported_trigger(trigger["card_trigger"]) and ability["effects"]:
                    raise CoreError(f"Unimplemented transcribed trigger: {ability['id']}")
                if supported_trigger(trigger["card_trigger"]):
                    validate_trigger(self, trigger["card_trigger"])
                if not set(ability.get("listener_scope", ["STATUS", "RESPONSE"])) <= {"STATUS", "RESPONSE", "HAND"}:
                    raise CoreError("Unsupported listener scope")
                continue
            if not set(trigger) <= {"operation_type", "announced_type", "actor", "target_owner", "source_definition_id", "turn_start"} or trigger.get("actor", "any") not in ("self", "enemy", "any") or trigger.get("target_owner", "self") != "self":
                raise CoreError("Unsupported trigger predicate")
            if "turn_start" in trigger and (trigger["operation_type"] != "BEGIN_PHASE" or type(trigger["turn_start"]) is not bool):
                raise CoreError("turn_start is a phase entry predicate")
            if "source_definition_id" in trigger and trigger["source_definition_id"] not in self.cards and trigger["source_definition_id"] not in self.abilities:
                raise CoreError("Unknown trigger source definition")
            if trigger["operation_type"] not in OPERATIONS | {"PLAY_CARD", "USE_STATUS", "ANNOUNCE", "BEGIN_PHASE", "END_PHASE", "TURN_END", "OBJECT_SETTLED", "CHECK_SUPPLY", "CHECK_SCORE", "CHECK_DISCARD"}:
                raise CoreError("Unsupported listener checkpoint")
            if not set(ability.get("listener_scope", ["STATUS", "RESPONSE"])) <= {"STATUS", "RESPONSE", "HAND"}:
                raise CoreError("Unsupported listener scope")
        for site in self.sites.values():
            if site["region"] not in self.regions or type(site["vp"]) is not int or site["vp"] < 0:
                raise CoreError("Invalid score site")
            mode = site.get("scoring", "regular")
            if mode not in ("regular", PER_UNIT_SITE) or (mode == PER_UNIT_SITE and site["vp"] != 1):
                raise CoreError("Invalid score site mode")
            if not set(site["countries"]) <= set(self.countries):
                raise CoreError("Invalid score eligibility")
        for k in ("hand_limit", "initial_hand", "setup_discard", "plays_per_turn", "max_rounds", "victory_margin", "max_atomic_steps"):
            if type(self.settings[k]) is not int or self.settings[k] < 0:
                raise CoreError(f"Invalid setting: {k}")
        if self.settings["setup_discard"] > self.settings["initial_hand"] or self.settings["max_atomic_steps"] == 0 or self.settings["max_rounds"] == 0:
            raise CoreError("Invalid setup or execution budget")
        for setting in ("mandatory_turn_play", "shared_site_scoring", "regional_status_sites"):
            if type(self.settings.get(setting, False)) is not bool:
                raise CoreError(f"Invalid {setting} setting")
        penalty = self.settings.get("discard_phase_penalty", 0)
        if type(penalty) is not int or penalty < 0:
            raise CoreError("Invalid discard phase penalty")
        supply_revision = self.settings.get("supply_revision", 1)
        if type(supply_revision) is not int or supply_revision not in (1, 2):
            raise CoreError("Invalid supply revision")
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
        path = Path(path)
        if path.suffix.lower() in (".sqlite", ".sqlite3", ".db"):
            from .catalog import ContentCatalog
            return ContentCatalog.load(path)
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def team(self, country):
        return self.countries[self.force_controllers[country]]["team"]

    def controlled_forces(self, country):
        return tuple(force for force, controller in self.force_controllers.items() if controller == country)

    @cached_property
    def hidden_score_rules(self):
        from .regional_sites import compile_regional_sites
        return compile_regional_sites(self)

    def _effects(self, effects):
        for op in effects:
            if op.get("type") not in OPERATIONS:
                raise CoreError(f"Unsupported instruction: {op.get('type')}")
            if op["type"] == "CHOOSE":
                self._effects([op["operation"]])
                if op["operation"]["type"] not in TARGETED_OPERATIONS:
                    raise CoreError("Unsupported target choice operation")
            if op["type"] == "CARD_EFFECT":
                from .content_effects import validate_effect
                if not isinstance(op.get("effect"), dict):
                    raise CoreError("Card effect must be a definition")
                validate_effect(self, op["effect"])
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
            if op["type"] in FORCE_OPERATIONS and op.get("target") != "$choice" and op.get("target") not in self.regions:
                raise CoreError("Unknown target region")
            if op["type"] in FORCE_OPERATIONS | {"REDEPLOY"} and "force" in op:
                force = op["force"]
                if not isinstance(force, str) or force not in self.force_controllers:
                    raise CoreError("Unknown acting force")

    def _programs(self, definition):
        for stage in ("effects", "on_settle"):
            effects = definition.get(stage, [] if stage == "on_settle" else None)
            context = f"{definition['id']}.{stage}"
            if not isinstance(effects, list):
                raise CoreError(f"{context} must be an ordered list")
            for index, effect in enumerate(effects):
                try:
                    self._effects([effect])
                    if stage == "on_settle" and (effect["type"] not in ("TRANSFER", "CREATE")
                                                 or effect.get("destination") == "$choice"):
                        raise CoreError("Settlement requires a concrete transfer or creation")
                except (KeyError, TypeError, AttributeError, CoreError) as error:
                    raise CoreError(f"{context}[{index}]: {error}") from error
