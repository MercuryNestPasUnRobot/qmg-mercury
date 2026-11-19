"""Atomic rule semantics. All physical changes use ZoneRegistry.transfer."""
from .model import CoreError, TURN_ORDER, PHASES, ZoneRegistry, zone
from .rules import board_units, LegalActionGenerator, SupplyResolver, RuleModifierResolver


def outcome(kind, **context):
    return {"success": True, "event": kind, "changes": [], "score_changes": [], "context": context}


class ScoreResolver:
    def __init__(self, rules):
        self.rules = rules

    def sites(self, state, country):
        supplied = SupplyResolver(self.rules).supplied(state, country)
        units = board_units(self.rules, state, country)
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

    def transfer(self, state, token_id, destination, result, index=None):
        result["changes"].append(ZoneRegistry.transfer(state, token_id, destination, index))
        source = state.tokens[token_id]
        if source.token_type == "CARD":
            active = destination in (zone(source.owner_country, "STATUS"), zone(source.owner_country, "RESPONSE"))
            for t in list(state.tokens.values()):
                if t.token_type == "ABILITY" and t.source_token_id == token_id:
                    if not active:
                        p = zone(t.owner_country, "ABILITY_INACTIVE")
                    elif t.position_id == zone(t.owner_country, "ABILITY_INACTIVE"):
                        p = zone(t.owner_country, "ABILITY_READY")
                    else:
                        continue
                    if p != t.position_id:
                        result["changes"].append(ZoneRegistry.transfer(state, t.token_id, p))

    def execute(self, state, op, frame):
        kind, country = op["type"], frame.actor_country
        result = outcome(kind)
        if kind in ("BUILD_ARMY", "BUILD_NAVY", "RECRUIT"):
            target = op.get("target")
            if target not in self.legal.targets(state, country, op):
                return outcome_failure(kind, "Target no longer legal")
            unit_kind = op.get("unit_kind", "NAVY" if kind == "BUILD_NAVY" else "ARMY")
            token_id = state.positions[zone(country, f"RESERVE_{unit_kind}")][0]
            self.transfer(state, token_id, target, result)
        elif kind in ("LAND_BATTLE", "SEA_BATTLE"):
            if op.get("target") not in self.legal.targets(state, country, op):
                return outcome_failure(kind, "Battle no longer legal")
            victims = [t for t in board_units(self.rules, state) if t.position_id == op["target"]]
            for t in victims:
                self.transfer(state, t.token_id, zone(t.owner_country, "RESERVE_" + self.rules.units[t.definition_id]["kind"]), result)
            result["context"]["victims"] = [t.token_id for t in victims]
        elif kind == "ELIMINATE":
            token = state.tokens.get(op.get("target_token"))
            if not token or token.token_type != "UNIT" or token.position_id not in self.rules.regions:
                return outcome_failure(kind, "Victim no longer on board")
            if op.get("reason") != "unsupplied" and token.token_id not in self.legal.targets(state, country, op):
                return outcome_failure(kind, "Elimination victim not eligible")
            if op.get("reason") == "unsupplied" and token.token_id in SupplyResolver(self.rules).supplied(state, token.owner_country):
                return outcome_failure(kind, "Supply restored before removal")
            self.transfer(state, token.token_id, zone(token.owner_country, "RESERVE_" + self.rules.units[token.definition_id]["kind"]), result)
        elif kind == "TRANSFER":
            self.transfer(state, op["token"], op["destination"], result, op.get("index"))
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
            self.gain(state, country, amount, result)
            ScoreResolver(self.rules).terminal(state)
        elif kind == "CANCEL":
            key = op["announcement_id"]
            if key not in state.announcements:
                return outcome_failure(kind, "No pending announced operation")
            state.announcements[key]["cancelled"] = True
        elif kind == "BEGIN_PHASE":
            state.turn.phase = op["phase"]
            for t in list(state.tokens.values()):
                if t.token_type == "ABILITY" and t.position_id == zone(t.owner_country, "ABILITY_COOLDOWN"):
                    a = self.rules.abilities[t.definition_id]
                    if (op.get("turn_end") and a["reset_policy"] == "TURN_END") or (
                            op.get("turn_start") and t.owner_country == country and a["reset_policy"] == "OWNER_TURN_START"):
                        self.transfer(state, t.token_id, zone(t.owner_country, "ABILITY_READY"), result)
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
            state.turn.country_index = (state.turn.country_index + 1) % 6
            state.turn.turn_id += 1
            if state.turn.country_index == 0:
                state.turn.round_id += 1
                ScoreResolver(self.rules).terminal(state, round_end=True)
            state.turn.plays_remaining = self.rules.settings["plays_per_turn"]
            country = TURN_ORDER[state.turn.country_index]
            ops = [{"type": "BEGIN_PHASE", "phase": "Play", "turn_start": True, "turn_end": True}]
        else:
            next_phase = PHASES[PHASES.index(phase) + 1]
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
