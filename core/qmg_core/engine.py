"""One serial executor shared by human clients and AI. No callbacks execute data."""
from copy import deepcopy

from .model import CoreError, DecisionRequest, ResolutionFrame, TURN_ORDER, digest, zone
from .rules import LegalActionGenerator, SupplyResolver, board_units, initialize, validate_state
from .operations import OperationExecutor, ScoreResolver, TurnController, outcome, outcome_failure

PREVENTABLE = {"BUILD_ARMY", "BUILD_NAVY", "RECRUIT", "LAND_BATTLE", "SEA_BATTLE", "ELIMINATE"}


class CardInterpreter:
    @staticmethod
    def frame(state, country, source, effects, parent=None, context=None):
        return ResolutionFrame(state.new_id("resolution"), source, country, list(reversed(deepcopy(effects))),
                               parent_resolution_id=parent, context=deepcopy(context or {}))


class AbilityScanner:
    def __init__(self, rules):
        self.rules = rules

    def scan(self, state, op, result, source_frame, executor):
        if not result["success"]:
            return
        matches = []
        for token in state.tokens.values():
            if token.token_type != "ABILITY" or token.position_id != zone(token.owner_country, "ABILITY_READY"):
                continue
            ability = self.rules.abilities[token.definition_id]
            source = state.tokens[token.source_token_id]
            if source.position_id not in {zone(token.owner_country, z) for z in ability.get("listener_scope", ("STATUS", "RESPONSE"))}:
                continue
            trigger = ability["trigger"]
            if trigger["operation_type"] != result["event"]:
                continue
            if trigger.get("announced_type") and trigger["announced_type"] != op.get("announced_type"):
                continue
            actor = op.get("actor_country")
            relation = trigger.get("actor", "any")
            if relation == "self" and actor != token.owner_country:
                continue
            if relation == "enemy" and (not actor or self.rules.team(actor) == self.rules.team(token.owner_country)):
                continue
            victim = state.tokens.get(op.get("target_token"))
            if trigger.get("target_owner") == "self" and (not victim or victim.owner_country != token.owner_country):
                continue
            matches.append((ability["priority"], token.token_id, token, ability))
        # Priorities are content data, not inferred official response precedence.
        priorities = [m[0] for m in matches]
        if len(set(priorities)) != len(priorities):
            raise CoreError("Ambiguous simultaneous ability priorities; content must specify an order")
        frames = []
        for _, _, token, ability in sorted(matches):
            executor.transfer(state, token.token_id, zone(token.owner_country, "ABILITY_PENDING"), result)
            frames.append(CardInterpreter.frame(state, token.owner_country, token.source_token_id,
                          [{"type": "ACTIVATE_ABILITY"}], source_frame.resolution_id if source_frame else None,
                          {"ability_token": token.token_id, "event": deepcopy(op), "event_outcome": deepcopy(result)}))
        state.stack.extend(reversed(frames))


class CommandProcessor:
    def __init__(self, rules, state, logger=None, record_head="0" * 64, checkpoint_path=None):
        self.rules = rules
        self._state = deepcopy(state)
        validate_state(rules, self._state)
        self.logger, self.record_head = logger, record_head
        self.checkpoint_path = checkpoint_path
        self.records = []
        self.executor, self.legal = OperationExecutor(rules), LegalActionGenerator(rules)

    @property
    def state(self):
        return deepcopy(self._state)

    @classmethod
    def create(cls, rules, seed=1, game_id="fixture-game", logger=None, checkpoint_path=None):
        obj = cls(rules, initialize(rules, seed, game_id), logger, checkpoint_path=checkpoint_path)
        if checkpoint_path is not None:
            from .persistence import SaveRepository
            SaveRepository.save(checkpoint_path, obj)
        count = rules.settings["setup_discard"]
        if count:
            if any(len(obj._state.positions[zone(c, "HAND")]) < count for c in TURN_ORDER):
                raise CoreError("Not enough cards for setup")
            def setup(s):
                s.turn.phase = "Setup"
                s.stack.append(CardInterpreter.frame(s, TURN_ORDER[0], None, [{"type": "SETUP_DONE"}]))
                for c in reversed(TURN_ORDER):
                    s.stack.append(CardInterpreter.frame(s, c, None, [
                        {"type": "CHOOSE", "operation": {"type": "DISCARD", "target_token": "$choice"}}
                        for _ in range(count)]))
                return outcome("SETUP_BEGIN")
            obj._commit({"type": "SETUP_BEGIN"}, setup)
            obj.advance()
        return obj

    def _commit(self, op, apply):
        before = self._state.to_dict()
        draft = deepcopy(self._state)
        operation = deepcopy(op)
        operation.setdefault("operation_id", draft.new_id("operation"))
        result = apply(draft)
        draft.version += 1
        validate_state(self.rules, draft)
        after = draft.to_dict()
        record = {"sequence_id": draft.version, "game_id": draft.game_id,
                  "round_id": draft.turn.round_id, "turn_id": draft.turn.turn_id,
                  "resolution_id": operation.get("resolution_id"), "operation_type": result["event"],
                  "parent_resolution_id": operation.get("parent_resolution_id"),
                  "source": operation.get("source_token_id"), "actor": operation.get("actor_country"),
                  "targets": {k: operation[k] for k in ("target", "target_token") if k in operation},
                  "operation": operation, "outcome": result, "ruleset_version": self.rules.version,
                  "rules_digest": self.rules.fingerprint, "before_version": self._state.version,
                  "after_version": draft.version, "before_hash": digest(before), "after_hash": digest(after),
                  "previous_record": self.record_head,
                  "state_patch": {k: deepcopy(v) for k, v in after.items() if before[k] != v}}
        record["record_digest"] = digest(record)
        if self.logger is not None:
            self.logger.append(deepcopy(record))
        # Publish only after the durable append succeeds; a failed step changes nothing.
        self._state, self.record_head = draft, record["record_digest"]
        self.records.append(record)
        if self.checkpoint_path is not None and result["event"] == "END_PHASE" and result["context"].get("phase") == "Draw":
            from .persistence import SaveRepository
            SaveRepository.save(self.checkpoint_path, self)
        return deepcopy(result)

    def legal_commands(self, controlled_countries):
        s = self._state
        if s.winner:
            return []
        if s.decision:
            d = s.decision
            if d.required_country not in controlled_countries:
                return []
            return [{"type": "CHOOSE", "actor": d.required_country, "expected_version": s.version,
                     "decision_id": d.decision_id, "option": option} for option in d.legal_options]
        country = TURN_ORDER[s.turn.country_index]
        if s.stack or country not in controlled_countries or s.turn.phase == "Setup":
            return []
        commands = [{"type": "END_PHASE", "actor": country, "expected_version": s.version}]
        if s.turn.phase == "Play" and s.turn.plays_remaining > 0:
            for token_id in s.positions[zone(country, "HAND")]:
                card = self.rules.cards[s.tokens[token_id].definition_id]
                first = card["effects"][0] if card["effects"] else None
                effective = first.get("operation") if first and first["type"] == "CHOOSE" else first
                if effective and effective["type"] in PREVENTABLE | {"DISCARD"}:
                    targets = self.legal.targets(s, country, effective)
                    selected = effective.get("target", effective.get("target_token", "$choice"))
                    if not targets or (selected != "$choice" and selected not in targets):
                        continue
                if card.get("simulation_unknown") or not self.legal.requirements_met(s, country, card.get("requirements", [])):
                    continue
                commands.append({"type": "PLAY_CARD", "actor": country, "expected_version": s.version, "card": token_id})
        return commands

    def submit(self, command, controlled_countries, auto_resolve=True):
        if type(command.get("expected_version")) is not int or command not in self.legal_commands(controlled_countries):
            raise CoreError("Unauthorized, stale or illegal command")
        op = {"type": command["type"], "actor_country": command["actor"], "source_token_id": command.get("card")}
        def apply(s):
            if command["type"] == "CHOOSE":
                d = s.decision
                frame = next(f for f in s.stack if f.resolution_id == d.resolution_id)
                option = command["option"]
                frame.selected_targets[d.decision_id] = option
                continuation = deepcopy(d.continuation)
                if continuation["type"] == "ACTIVATE_SELECTED":
                    continuation["use"] = option == "use"
                elif continuation["type"] == "REDEPLOY_SELECTED":
                    continuation["token"] = option
                elif continuation["type"] == "REBUILD_UNIT":
                    continuation["target"] = option
                else:
                    key = "target_token" if continuation["type"] in ("DISCARD", "ELIMINATE", "COST_SELECTED") else "target"
                    continuation[key] = option
                frame.remaining_operations.append(continuation)
                s.decision = None
                return outcome("DECISION_RESOLVED", choice=option)
            if command["type"] == "PLAY_CARD":
                token_id = command["card"]
                definition = self.rules.cards[s.tokens[token_id].definition_id]
                result = outcome("PLAY_CARD")
                self.executor.transfer(s, token_id, "RESOLUTION", result)
                frame = CardInterpreter.frame(s, command["actor"], token_id, definition["effects"], context={"failure_policy": definition["failure_policy"]})
                s.stack.append(frame)
                s.turn.plays_remaining -= 1
                AbilityScanner(self.rules).scan(s, op, result, frame, self.executor)
                return result
            country, operations, result = TurnController(self.rules).next_phase(s)
            frame = CardInterpreter.frame(s, country, None, operations)
            s.stack.append(frame)
            AbilityScanner(self.rules).scan(s, op, result, frame, self.executor)
            return result
        self._commit(op, apply)
        if auto_resolve:
            self.advance()
        return self.state

    def _request(self, s, frame, kind, options, continuation, operation_id, optional=False):
        if s.decision:
            raise CoreError("Only one blocking decision is permitted")
        if not options:
            return outcome_failure(kind, "No legal options")
        s.decision = DecisionRequest(s.new_id("decision"), frame.actor_country, kind, list(options),
                                     operation_id, frame.resolution_id, optional, deepcopy(continuation))
        return outcome("DECISION_REQUESTED", decision_type=kind)

    @staticmethod
    def _resolve(value, frame):
        if isinstance(value, dict):
            return {k: CommandProcessor._resolve(v, frame) for k, v in value.items()}
        if isinstance(value, list):
            return [CommandProcessor._resolve(v, frame) for v in value]
        if isinstance(value, str) and value.startswith("$event."):
            return frame.context["event"][value[7:]]
        return value

    def advance(self, max_steps=None):
        budget = self.rules.settings["max_atomic_steps"] if max_steps is None else max_steps
        for _ in range(budget):
            s = self._state
            if s.decision or not s.stack or s.winner:
                return self.state
            self.step()
        if self._state.stack and not self._state.decision and not self._state.winner:
            raise CoreError("Atomic-step budget exceeded; committed prefix remains resumable")
        return self.state

    def step(self):
        if self._state.decision:
            raise CoreError("Waiting for the required player choice")
        if not self._state.stack or self._state.winner:
            return None
        frame = self._state.stack[-1]
        raw = deepcopy(frame.remaining_operations[-1]) if frame.remaining_operations else {"type": "FINISH_FRAME"}
        op = self._resolve(raw, frame)
        op.update(actor_country=frame.actor_country, source_token_id=frame.source_card_id,
                  resolution_id=frame.resolution_id, parent_resolution_id=frame.parent_resolution_id,
                  operation_id=f"operation:{self._state.serial + 1}")
        def apply(s):
            f = s.stack[-1]
            # A source removed during nested resolution cannot keep producing effects.
            ability_id = f.context.get("ability_token")
            if ability_id and s.tokens[ability_id].position_id == zone(f.actor_country, "ABILITY_INACTIVE"):
                s.stack.pop()
                return outcome("ABILITY_INVALIDATED")
            if op["type"] == "FINISH_FRAME":
                s.stack.pop()
                result = outcome("FINISH_FRAME")
                if ability_id:
                    t = s.tokens[ability_id]
                    if t.position_id == zone(t.owner_country, "ABILITY_PENDING"):
                        self.executor.transfer(s, ability_id, zone(t.owner_country, "ABILITY_READY"), result)
                elif f.source_card_id and s.tokens[f.source_card_id].position_id == "RESOLUTION":
                    card = self.rules.cards[s.tokens[f.source_card_id].definition_id]
                    destination = card["type"] if card["type"] in ("STATUS", "RESPONSE") else "DISCARD"
                    self.executor.transfer(s, f.source_card_id, zone(f.actor_country, destination), result)
                return result
            f.remaining_operations.pop()
            f.current_step += 1
            if op["type"] in PREVENTABLE and "announcement_id" not in op:
                announcement = s.new_id("announcement")
                s.announcements[announcement] = {"cancelled": False}
                f.remaining_operations.append({**raw, "announcement_id": announcement})
                announced = {**op, "type": "ANNOUNCE", "announced_type": op["type"], "announcement_id": announcement}
                result = outcome("ANNOUNCE", announced_type=op["type"], announcement_id=announcement)
                AbilityScanner(self.rules).scan(s, announced, result, f, self.executor)
                return result
            if op["type"] in PREVENTABLE and "announcement_id" in op:
                if s.announcements.pop(op["announcement_id"])["cancelled"]:
                    result = outcome_failure(op["type"], "Cancelled after announcement")
                    if f.context.get("failure_policy") == "stop":
                        f.remaining_operations.clear()
                    return result
            result, scan = self._execute(s, f, op)
            if not result["success"] and f.context.get("failure_policy") == "stop":
                f.remaining_operations.clear()
            if scan:
                AbilityScanner(self.rules).scan(s, op, result, f, self.executor)
            return result
        return self._commit(op, apply)

    def _execute(self, s, f, op):
        kind, country = op["type"], f.actor_country
        if kind == "CHOOSE":
            continuation = self._resolve(op["operation"], f)
            options = self.legal.targets(s, country, continuation)
            return self._request(s, f, "TARGET", options, continuation, op["operation_id"]), False
        if kind in ("ACTIVATE_ABILITY", "ACTIVATE_SELECTED"):
            token = s.tokens[f.context["ability_token"]]
            ability = self.rules.abilities[token.definition_id]
            cost = self.executor.activation_cost(s, token)
            hand = s.positions[zone(country, "HAND")]
            if kind == "ACTIVATE_ABILITY" and ability["activation_mode"] == "OPTIONAL":
                options = ["decline"] + (["use"] if len(hand) >= cost else [])
                return self._request(s, f, "ABILITY", options, {"type": "ACTIVATE_SELECTED"}, op["operation_id"], True), False
            use = op.get("use", True)
            result = outcome("ABILITY_ACTIVATED" if use and len(hand) >= cost else "ABILITY_DECLINED")
            if not use or len(hand) < cost:
                self.executor.transfer(s, token.token_id, zone(country, "ABILITY_READY"), result)
                return result, False
            # Costs are selected, not automatically taken from hidden cards.
            f.context["cost_tokens"] = []
            sequence = [{"type": "SELECT_COST"} for _ in range(cost)]
            sequence += [{"type": "ABILITY_PAID"}] + deepcopy(ability["effects"])
            f.remaining_operations.extend(reversed(sequence))
            return result, False
        if kind == "SELECT_COST":
            selected = f.context["cost_tokens"]
            options = [t for t in s.positions[zone(country, "HAND")] if t not in selected]
            return self._request(s, f, "COST", options, {"type": "COST_SELECTED"}, op["operation_id"]), False
        if kind == "COST_SELECTED":
            f.context["cost_tokens"].append(op["target_token"])
            return outcome("COST_SELECTED"), False
        if kind == "ABILITY_PAID":
            token = s.tokens[f.context["ability_token"]]
            ability = self.rules.abilities[token.definition_id]
            cost_tokens = f.context["cost_tokens"]
            if len(cost_tokens) != self.executor.activation_cost(s, token) or len(set(cost_tokens)) != len(cost_tokens) or any(t not in s.positions[zone(country, "HAND")] for t in cost_tokens):
                f.remaining_operations.clear()
                return outcome_failure("PAY_COST", "Payment unavailable; no cards consumed"), False
            result = outcome("PAY_COST")
            for token_id in cost_tokens:
                self.executor.transfer(s, token_id, zone(country, "DISCARD"), result)
            if ability["reset_policy"] != "CHECKPOINT":
                self.executor.transfer(s, token.token_id, zone(country, "ABILITY_COOLDOWN"), result)
            return result, False
        if kind == "CHECK_SUPPLY":
            supplied = SupplyResolver(self.rules).supplied(s, country)
            ops = [{"type": "ELIMINATE", "target_token": t.token_id, "reason": "unsupplied"} for t in board_units(self.rules, s, country) if t.token_id not in supplied]
            f.remaining_operations.extend(reversed(ops))
            return outcome(kind), True
        if kind == "CHECK_SCORE":
            ops = [{"type": "GAIN_VP", "amount": vp, "score_site": site} for site, vp in ScoreResolver(self.rules).sites(s, country).items()]
            f.remaining_operations.extend(reversed(ops))
            return outcome(kind), True
        if kind == "CHECK_DISCARD":
            count = max(0, len(s.positions[zone(country, "HAND")]) - self.rules.settings["hand_limit"])
            f.remaining_operations.extend(reversed([{"type": "CHOOSE", "operation": {"type": "DISCARD", "target_token": "$choice"}} for _ in range(count)]))
            return outcome(kind), True
        if kind == "DRAW_TO_LIMIT":
            f.remaining_operations.append({"type": "DRAW", "count": max(0, self.rules.settings["hand_limit"] - len(s.positions[zone(country, "HAND")]))})
            return outcome(kind), False
        if kind == "SETUP_DONE":
            s.turn.phase = "Play"
            return outcome(kind), False
        if kind == "REDEPLOY":
            if not f.eligible_token_ids:
                f.eligible_token_ids = sorted(t.token_id for t in board_units(self.rules, s, country) if self.rules.units[t.definition_id]["kind"] == "ARMY")
            choices = [t for t in f.eligible_token_ids if t not in f.processed_token_ids and s.tokens[t].position_id in self.rules.regions]
            if not choices:
                return outcome("REDEPLOY_COMPLETE"), False
            return self._request(s, f, "REDEPLOY_UNIT", choices, {"type": "REDEPLOY_SELECTED"}, op["operation_id"]), False
        if kind == "REDEPLOY_SELECTED":
            token_id = op["token"]
            virtual = deepcopy(s)
            self.executor.transfer(virtual, token_id, zone(country, "RESERVE_ARMY"), outcome("INTERNAL"))
            targets = self.legal.targets(virtual, country, {"type": "BUILD_ARMY"})
            if not targets:
                f.processed_token_ids.append(token_id)
                f.remaining_operations.append({"type": "REDEPLOY"})
                return outcome_failure("REDEPLOY", "No rebuilding site; original unit retained"), False
            return self._request(s, f, "REDEPLOY_TARGET", targets, {"type": "REBUILD_UNIT", "token": token_id}, op["operation_id"]), False
        if kind == "REBUILD_UNIT":
            virtual = deepcopy(s)
            self.executor.transfer(virtual, op["token"], zone(country, "RESERVE_ARMY"), outcome("INTERNAL"))
            if op["target"] not in self.legal.targets(virtual, country, {"type": "BUILD_ARMY"}):
                return outcome_failure("REDEPLOY", "Rebuild invalid; original unit retained"), False
            result = outcome("BUILD_ARMY", redeployed=True)
            self.executor.transfer(s, op["token"], op["target"], result)
            f.processed_token_ids.append(op["token"])
            f.remaining_operations.append({"type": "REDEPLOY"})
            return result, True
        return self.executor.execute(s, op, f), True


ResolutionEngine = CommandProcessor
