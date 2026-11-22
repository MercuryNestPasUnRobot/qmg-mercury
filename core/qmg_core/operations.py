"""Atomic rule semantics. All physical changes use ZoneRegistry.transfer."""
from .definitions import BATTLES, CONSTRUCTION, FORCE_OPERATIONS
from .model import CoreError, TURN_ORDER, ZoneRegistry, zone
from .rules import board_units, capture_turn_start, deployed_zones, initial_abilities, LegalActionGenerator, SupplyResolver, RuleModifierResolver, unit_rule_matches


PREVENTABLE = FORCE_OPERATIONS | {"ELIMINATE"}


def outcome(kind, **context):
    return {"success": True, "event": kind, "changes": [], "score_changes": [], "context": context}


class ScoreResolver:
    def __init__(self, rules):
        self.rules = rules

    def sites(self, state, country, *, pieces=None, supplied=None):
        if pieces is None:
            pieces = board_units(self.rules, state)
        if supplied is None:
            supplied = SupplyResolver(self.rules).supplied_for_seat(state, country, pieces)
        units = [piece for piece in pieces if self.rules.force_controllers[piece.owner_country] == country
                 and self.rules.units[piece.definition_id]["kind"] != "AIR"]
        result = {}
        for site_id, site in self.rules.sites.items():
            if site.get("dynamic", False):
                sources = [t for t in state.tokens.values() if t.token_type == "SCORE_SOURCE" and
                           t.definition_id == site_id and t.owner_country in (None, country) and t.position_id in self.rules.regions]
                locations = {t.position_id for t in sources}
            else:
                locations = {site["region"]}
            if country in site["countries"] and any(t.position_id in locations and
                    (not site["requires_supply"] or t.token_id in supplied) for t in units):
                result[site_id] = site["vp"]
        for modifier in RuleModifierResolver(self.rules).resolve(state, country):
            if modifier["kind"] == "score_bonus" and modifier["site"] in result:
                result[modifier["site"]] += modifier["amount"]
        return result

    def terminal(self, state, round_end=False):
        diff = state.scores["axis"] - state.scores["allies"]
        margin = self.rules.settings["victory_margin"]
        if margin > 0 and abs(diff) >= margin:
            state.winner = "axis" if diff > 0 else "allies"
        elif round_end and state.turn.round_id > self.rules.settings["max_rounds"]:
            state.winner = "axis" if diff > 0 else "allies" if diff < 0 else "draw"


class OperationExecutor:
    def __init__(self, rules):
        self.rules = rules
        self.legal = LegalActionGenerator(rules)

    def activation_cost(self, state, token):
        cost = self.rules.abilities[token.definition_id].get("discard_cost", 0)
        for modifier in RuleModifierResolver(self.rules).resolve(state, token.owner_country):
            if modifier["kind"] == "ability_cost" and modifier.get("ability", token.definition_id) == token.definition_id:
                cost += modifier["amount"]
        return max(0, cost)

    def transfer(self, state, token_id, destination, result, index=None, resolving_ability=None):
        change = ZoneRegistry.transfer(state, token_id, destination, index)
        result["changes"].append(change)
        source = state.tokens[token_id]
        if source.token_type == "CARD":
            if change["from"] == zone(source.owner_country, "HAND"):
                for frame in state.stack:
                    acquired = frame.context.get("search_tokens", [])
                    if token_id in acquired:
                        acquired.remove(token_id)
            deployed = deployed_zones(self.rules, source)
            if destination in deployed and change["from"] not in deployed:
                for replica in initial_abilities(self.rules, source):
                    if replica.token_id in state.tokens and state.tokens[replica.token_id].position_id != replica.position_id:
                        result["changes"].append(ZoneRegistry.transfer(state, replica.token_id, replica.position_id))
            elif destination not in deployed and (change["from"] in deployed or change["from"] == "RESOLUTION"):
                pool = zone(source.owner_country, "OBJECT_POOL")
                for t in sorted(state.tokens.values(), key=lambda item: item.token_id):
                    if t.source_token_id == token_id and t.token_type == "ABILITY" and t.token_id != resolving_ability and t.position_id != pool:
                        result["changes"].append(ZoneRegistry.transfer(state, t.token_id, pool))
                        for frame in state.stack:
                            if frame.context.get("ability_token") == t.token_id:
                                frame.context["invalidated"] = True

    def transfer_from_pool(self, state, kind, definition, country, destination, result, source_id=None, exact=None):
        for key in state.positions[zone(country, "OBJECT_POOL")]:
            token = state.tokens[key]
            if (exact is None or key == exact) and token.token_type == kind and token.definition_id == definition and token.source_token_id == source_id:
                self.transfer(state, key, destination, result)
                return True
        return False

    def settle_card(self, state, token_id, origin, default_zone, result):
        card = state.tokens[token_id]
        after = self.rules.cards[card.definition_id].get("after")
        destination = origin if after == "origin" else zone(card.owner_country, "OBJECT_POOL" if after == "delete" else default_zone)
        self.transfer(state, token_id, destination, result)

    def target_failure(self, state, op):
        kind, country = op["type"], op["actor_country"]
        if kind == "ELIMINATE":
            token = state.tokens.get(op.get("target_token"))
            if not token or token.token_type != "UNIT" or token.position_id not in self.rules.regions:
                return "Victim no longer on board"
            if token.position_id != op.get("expected_from", token.position_id):
                return "Victim has left the recorded region"
            if op.get("reason") == "unsupplied":
                if token.token_id in SupplyResolver(self.rules).supplied(state, token.owner_country):
                    return "Supply restored before removal"
            elif token.token_id not in self.legal.targets(state, country, op):
                return "Elimination victim not eligible"
        elif op.get("target") not in self.legal.targets(state, country, op):
            return "Battle no longer legal" if kind in BATTLES else "Target no longer legal"
        if kind in BATTLES and op.get("target_token"):
            from .air import victim_options
            if op["target_token"] not in victim_options(self.rules, state, op):
                return "Selected battle victim no longer eligible"
        return None

    def execute(self, state, op, frame):
        kind, country = op["type"], op["actor_country"]
        if kind in PREVENTABLE:
            if reason := self.target_failure(state, op):
                return outcome_failure(kind, reason)
        result = outcome(kind)
        if kind in CONSTRUCTION:
            target = op.get("target")
            unit_kind = op.get("unit_kind", "NAVY" if kind == "BUILD_NAVY" else "ARMY")
            force = op.get("force", country)
            if kind == "BUILD_ARMY" and any(m["kind"] == "recruit_region" and m["region"] == target
                                           for m in RuleModifierResolver(self.rules).resolve(state, force)):
                result["event"] = "RECRUIT"
            token_id = state.positions[zone(force, f"RESERVE_{unit_kind}")][0]
            self.transfer(state, token_id, target, result)
        elif kind in BATTLES:
            unit_kind = "ARMY" if kind == "LAND_BATTLE" else "NAVY"
            exact = op.get("target_token", op.get("expected_victim"))
            victims = [t for t in board_units(self.rules, state) if t.position_id == op["target"]
                       and self.rules.units[t.definition_id]["kind"] == unit_kind and
                       (not exact or t.token_id == exact) and self.rules.team(t.owner_country) != self.rules.team(country)]
            result["context"]["attacked_units"] = [t.token_id for t in victims]
            victims = [t for t in victims if not unit_rule_matches(self.rules, state, t, "protection")]
            result["context"]["removed_supplied"] = {
                t.token_id: t.token_id in SupplyResolver(self.rules).supplied(state, t.owner_country)
                for t in victims}
            for t in victims:
                self.transfer(state, t.token_id, zone(t.owner_country, "RESERVE_" + self.rules.units[t.definition_id]["kind"]), result)
            result["context"]["victims"] = [t.token_id for t in victims]
        elif kind == "ELIMINATE":
            token = state.tokens[op["target_token"]]
            if unit_rule_matches(self.rules, state, token, "protection"):
                return outcome(kind, prevented=True)
            result["context"]["removed_supplied"] = {
                token.token_id: token.token_id in SupplyResolver(self.rules).supplied(state, token.owner_country)}
            self.transfer(state, token.token_id, zone(token.owner_country, "RESERVE_" + self.rules.units[token.definition_id]["kind"]), result)
        elif kind == "CREATE":
            source_id = None
            exact = op.get("token")
            if op["token_type"] == "ABILITY":
                source = state.tokens.get(frame.source_card_id)
                if (not source or source.position_id not in (zone(country, "STATUS"), zone(country, "RESPONSE"))
                        or op["definition"] not in self.rules.bound_abilities[source.definition_id]):
                    return outcome_failure(kind, "Ability source is no longer deployed")
                source_id = source.token_id
                if op.get("same_copy"):
                    origin = state.tokens.get(frame.context.get("ability_token"))
                    group = self.rules.copy_groups.get((source_id, op["definition"]), [])
                    if origin is None:
                        return outcome_failure(kind, "Copy selection needs a resolving ability")
                    slot = self.rules.copy_groups[(source_id, origin.definition_id)].index(origin.token_id)
                    if slot >= len(group):
                        return outcome_failure(kind, "No corresponding copy in inventory")
                    exact = group[slot]
            elif self.rules.cards[op["definition"]]["country"] != country:
                return outcome_failure(kind, "Card definition belongs to another country")
            if not self.transfer_from_pool(state, op["token_type"], op["definition"], country, zone(country, op["zone"]), result, source_id, exact):
                return outcome_failure(kind, "No matching object available in pool")
        elif kind == "TRANSFER":
            destination = op["destination"]
            if destination not in self.legal.targets(state, country, op):
                return outcome_failure(kind, "Token or destination is no longer eligible")
            self.transfer(state, op["token"], destination, result, op.get("index"))
        elif kind == "DISCARD":
            source = zone(country, op.get("from_zone", "HAND"))
            token_id = op.get("target_token")
            if token_id not in state.positions[source]:
                return outcome_failure(kind, "Discard card unavailable")
            self.transfer(state, token_id, zone(country, "DISCARD"), result)
        elif kind == "DRAW":
            count = op.get("count", 1)
            if type(count) is not int or count < 0:
                raise CoreError("Invalid draw count")
            for _ in range(count):
                deck = state.positions[zone(country, "DECK")]
                if deck:
                    self.transfer(state, deck[0], zone(country, "HAND"), result)
                else:
                    amount = self.rules.settings["deck_exhaustion_vp"]
                    self.gain(state, country, amount, result)
        elif kind == "GAIN_VP":
            amount = op["amount"]
            if type(amount) is not int:
                raise CoreError("VP must be an integer")
            self.gain(state, op.get("score_country", country), amount, result)
            if not any(active.context.get("closing_turn") for active in state.stack):
                ScoreResolver(self.rules).terminal(state)
        elif kind == "CANCEL":
            key = op["announcement_id"]
            if key not in state.announcements:
                return outcome_failure(kind, "No pending announced operation")
            state.announcements[key]["cancelled"] = True
        elif kind == "TURN_END":
            result["context"]["phase"] = state.turn.phase
        elif kind == "BEGIN_PHASE":
            if op.get("advance_country"):
                scoring = ScoreResolver(self.rules)
                scoring.terminal(state)
                if state.winner:
                    return outcome("GAME_END", reason="victory_margin")
                state.turn.country_index = (state.turn.country_index + 1) % len(TURN_ORDER)
                state.turn.turn_id += 1
                state.turn.plays_remaining = self.rules.settings["plays_per_turn"]
                if state.turn.country_index == 0:
                    state.turn.round_id += 1
                    scoring.terminal(state, round_end=True)
            state.turn.phase = op["phase"]
            if op.get("advance_country"):
                capture_turn_start(self.rules, state)
        else:
            raise CoreError(f"Unsupported executor operation: {kind}")
        return result

    def gain(self, state, country, amount, result):
        team = self.rules.team(country)
        state.scores[team] += amount
        result["score_changes"].append({"team": team, "amount": amount})


def outcome_failure(kind, reason):
    result = outcome(kind, reason=reason)
    result["success"] = False
    return result


class TurnController:
    def __init__(self, rules):
        self.rules = rules

    def next_phase(self, state):
        country = TURN_ORDER[state.turn.country_index]
        result = outcome("END_PHASE", phase=state.turn.phase)
        phase = state.turn.phase
        ops = []
        if phase == "Draw":
            ops = [{"type": "TURN_END"},
                   {"type": "BEGIN_PHASE", "phase": "Play", "turn_start": True,
                    "turn_end": True, "advance_country": True}]
        else:
            next_phase = self.rules.phases[self.rules.phases.index(phase) + 1]
            ops.append({"type": "BEGIN_PHASE", "phase": next_phase})
            if next_phase == "Supply":
                ops.append({"type": "CHECK_SUPPLY"})
            elif next_phase == "Victory":
                ops.append({"type": "CHECK_SCORE"})
            elif next_phase == "Discard":
                ops.append({"type": "CHECK_DISCARD"})
            elif next_phase == "Draw":
                ops.append({"type": "DRAW_TO_LIMIT"})
        return country, ops, result
