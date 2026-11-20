"""One serial executor shared by human clients and AI. No callbacks execute data."""
from copy import deepcopy

from .model import CoreError, DecisionRequest, ResolutionFrame, TURN_ORDER, digest, zone
from .rules import LegalActionGenerator, SupplyResolver, board_units, initialize, validate_state
from .operations import BATTLES, PREVENTABLE, OperationExecutor, ScoreResolver, TurnController, outcome, outcome_failure


class ResolutionBudgetExceeded(CoreError):
    pass


class CardInterpreter:
    @staticmethod
    def frame(state, country, source, effects, parent=None, context=None):
        return ResolutionFrame(state.new_id("resolution"), source, country, list(reversed(deepcopy(effects))),
                               parent_resolution_id=parent, context=deepcopy(context or {}))


class AbilityScanner:
    def __init__(self, rules):
        self.rules = rules

    def scan(self, state, op, result, source_frame, executor):
        if not result["success"] or state.winner:
            return
        matches = []
        reserved = {f.context.get("ability_token") for f in state.stack}
        selected = set()
        for token in sorted(state.tokens.values(), key=lambda item: item.token_id):
            if token.token_type != "ABILITY" or token.token_id in reserved or token.position_id not in (
                    zone(token.owner_country, "ABILITY_READY"), zone(token.owner_country, "RESPONSE")):
                continue
            ability = self.rules.abilities[token.definition_id]
            source = state.tokens[token.source_token_id]
            if source.position_id not in {zone(token.owner_country, z) for z in ability.get("listener_scope", ("STATUS", "RESPONSE"))}:
                continue
            trigger = ability["trigger"]
            if trigger["operation_type"] != result["event"]:
                continue
            event_source = state.tokens.get(op.get("source_token_id"))
            if result["event"] == "OBJECT_SETTLED" and token.source_token_id == op.get("source_token_id") and (
                    op.get("settled_definition") in (token.definition_id, source.definition_id)):
                continue
            event_definition = op.get("settled_definition", event_source.definition_id if event_source else None)
            if trigger.get("source_definition_id") and event_definition != trigger["source_definition_id"]:
                continue
            if "turn_start" in trigger and trigger["turn_start"] != bool(op.get("turn_start")):
                continue
            if trigger.get("announced_type") and trigger["announced_type"] != op.get("announced_type"):
                continue
            actor = op.get("actor_country")
            relation = trigger.get("actor", "any")
            if relation == "self" and actor != token.owner_country:
                continue
            if relation == "enemy" and (not actor or self.rules.team(actor) == self.rules.team(token.owner_country)):
                continue
            if trigger.get("target_owner") == "self":
                targets = result["context"].get("victims", [])
                if op.get("target_token"):
                    targets = [op["target_token"]]
                elif op.get("announced_type") in BATTLES:
                    targets = [key for key in state.positions[op["target"]] if state.tokens[key].token_type == "UNIT"]
                if not any(target in state.tokens and state.tokens[target].owner_country == token.owner_country
                           for target in targets):
                    continue
            group = (source.token_id, token.definition_id)
            if token.position_id == zone(token.owner_country, "ABILITY_READY"):
                if group in selected:
                    continue
                selected.add(group)
            matches.append((ability["priority"], token.token_id, token, ability))
        # Priorities are content data, not inferred official response precedence.
        priorities = {(priority, ability["id"]) for priority, _, _, ability in matches}
        if len({priority for priority, _ in priorities}) != len(priorities):
            raise CoreError("Ambiguous simultaneous ability priorities; content must specify an order")
        frames = []
        for _, _, token, ability in sorted(matches):
            if token.position_id == zone(token.owner_country, "ABILITY_READY"):
                executor.transfer(state, token.token_id, zone(token.owner_country, "ABILITY_PENDING"), result)
            frames.append(CardInterpreter.frame(state, token.owner_country, token.source_token_id,
                          [{"type": "ACTIVATE_ABILITY"}], source_frame.resolution_id if source_frame else None,
                          {"ability_token": token.token_id, "origin": token.position_id,
                           "event": deepcopy(op), "event_outcome": deepcopy(result)}))
        state.stack.extend(reversed(frames))


class CommandProcessor:
    def __init__(self, rules, state, logger=None, record_head="0" * 64, checkpoint_path=None, *, legacy_snapshot=False):
        self.rules = rules
        self._state = deepcopy(state)
        validate_state(rules, self._state, legacy_snapshot=legacy_snapshot)
        self.logger, self.record_head = logger, record_head
        self.checkpoint_path = checkpoint_path
        self.records = []
        self._first_commit = True
        self.executor, self.legal = OperationExecutor(rules), LegalActionGenerator(rules)

    @property
    def state(self):
        return deepcopy(self._state)

    @classmethod
    def create(cls, rules, seed=1, game_id="fixture-game", logger=None, checkpoint_path=None):
        obj = cls(rules, initialize(rules, seed, game_id), logger, checkpoint_path=checkpoint_path)
        count = rules.settings["setup_discard"]
        if count:
            state = obj._state
            if any(len(state.positions[zone(c, "HAND")]) < count for c in TURN_ORDER):
                raise CoreError("Not enough cards for setup")
            state.turn.phase = "Setup"
            state.stack.append(CardInterpreter.frame(state, TURN_ORDER[0], None, [{"type": "SETUP_DONE"}]))
            for country in reversed(TURN_ORDER):
                state.stack.append(CardInterpreter.frame(state, country, None, [
                    {"type": "CHOOSE", "operation": {"type": "DISCARD", "target_token": "$choice"}}
                    for _ in range(count)]))
        if checkpoint_path is not None:
            from .persistence import SaveRepository
            SaveRepository.save(checkpoint_path, obj)
        obj.advance()
        return obj

    def _commit(self, op, apply):
        before = self._state.to_dict()
        draft = deepcopy(self._state)
        operation = deepcopy(op)
        operation.setdefault("operation_id", draft.new_id("operation"))
        legacy_changes = []
        if self._first_commit and zone(TURN_ORDER[0], "OBJECT_POOL") not in draft.positions:
            from .persistence import upgrade_object_pools
            legacy_changes = upgrade_object_pools(draft, self.rules, self.executor)
        result = apply(draft)
        result["changes"][:0] = legacy_changes
        if draft.winner and not self._state.winner:
            self._close_terminal_resolution(draft, result)
        draft.version += 1
        validate_state(self.rules, draft)
        after = draft.to_dict()
        record = {"sequence_id": draft.version, "game_id": draft.game_id,
                  "round_id": draft.turn.round_id, "turn_id": draft.turn.turn_id,
                  "resolution_id": operation.get("resolution_id") or result["context"].get("resolution_id"),
                  "operation_type": result["event"],
                  "parent_resolution_id": operation.get("parent_resolution_id"),
                  "source": operation.get("source_token_id"), "actor": operation.get("actor_country"),
                  "targets": {k: operation[k] for k in ("target", "target_token", "token", "destination") if k in operation},
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
        self._first_commit = False
        self.records.append(record)
        if self.checkpoint_path is not None and operation.get("advance_country"):
            from .persistence import SaveRepository
            SaveRepository.save(self.checkpoint_path, self)
        return deepcopy(result)

    def _close_terminal_resolution(self, state, result):
        # Keep the winning operation and retire unfinished cards without running their effects.
        for token_id in sorted(state.positions["RESOLUTION"]):
            token = state.tokens.get(token_id)
            if token and token.token_type == "CARD":
                self.executor.transfer(state, token_id, zone(token.owner_country, "DISCARD"), result)
            elif token:
                self.executor.transfer(state, token_id, zone(token.owner_country, "OBJECT_POOL"), result)
        for token in sorted(state.tokens.values(), key=lambda item: item.token_id):
            if token.token_type == "ABILITY" and token.position_id == zone(token.owner_country, "ABILITY_PENDING"):
                self.executor.transfer(state, token.token_id, zone(token.owner_country, "ABILITY_READY"), result)
        state.stack.clear()
        state.announcements.clear()
        state.decision = None

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
                if not self.legal.can_start_card(s, country, card):
                    continue
                commands.append({"type": "PLAY_CARD", "actor": country, "expected_version": s.version, "card": token_id})
        return commands

    def submit(self, command, controlled_countries, auto_resolve=True):
        if not isinstance(command, dict) or type(command.get("expected_version")) is not int or command not in self.legal_commands(controlled_countries):
            raise CoreError("Unauthorized, stale or illegal command")
        op = {"type": command["type"], "actor_country": command["actor"], "source_token_id": command.get("card")}
        if command["type"] == "CHOOSE":
            frame = self._state.stack[-1]
            op.update(resolution_id=frame.resolution_id, parent_resolution_id=frame.parent_resolution_id,
                      source_token_id=frame.source_card_id, decision_id=command["decision_id"])
            if "ability_token" in frame.context:
                op.update(object_token_id=frame.context["ability_token"], object_origin=frame.context.get("origin"))
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
                elif continuation["type"] == "TRANSFER":
                    continuation["destination"] = option
                else:
                    key = "target_token" if continuation["type"] in ("DISCARD", "ELIMINATE", "COST_SELECTED") else "target"
                    continuation[key] = option
                frame.remaining_operations.append(continuation)
                s.decision = None
                return outcome("DECISION_RESOLVED", choice=option)
            if command["type"] == "PLAY_CARD":
                token_id = command["card"]
                definition = self.rules.cards[s.tokens[token_id].definition_id]
                if definition["type"] == "RESPONSE":
                    result = outcome("SET_RESPONSE")
                    self.executor.transfer(s, token_id, zone(command["actor"], "RESPONSE"), result)
                    s.turn.plays_remaining -= 1
                    return result
                result = outcome("PLAY_CARD")
                self.executor.transfer(s, token_id, "RESOLUTION", result)
                frame = CardInterpreter.frame(s, command["actor"], token_id, definition["effects"],
                                              context={"failure_policy": definition["failure_policy"], "origin": zone(command["actor"], "HAND")})
                s.stack.append(frame)
                result["context"]["resolution_id"] = frame.resolution_id
                s.turn.plays_remaining -= 1
                AbilityScanner(self.rules).scan(s, op, result, frame, self.executor)
                return result
            country, operations, result = TurnController(self.rules).next_phase(s)
            frame = CardInterpreter.frame(s, country, None, operations, context={"closing_turn": result["context"]["phase"] == "Draw"})
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
            raise ResolutionBudgetExceeded("Atomic-step budget exceeded; committed prefix remains resumable")
        return self.state

    def step(self):
        if self._state.decision:
            raise CoreError("Waiting for the required player choice")
        if not self._state.stack or self._state.winner:
            return None
        frame = self._state.stack[-1]
        raw = deepcopy(frame.remaining_operations[-1]) if frame.remaining_operations else {"type": "FINISH_FRAME"}
        op = self._resolve(raw, frame)
        actor = TURN_ORDER[(self._state.turn.country_index + 1) % len(TURN_ORDER)] if op.get("advance_country") else frame.actor_country
        op.update(actor_country=actor, source_token_id=frame.source_card_id,
                  resolution_id=frame.resolution_id, parent_resolution_id=frame.parent_resolution_id,
                  operation_id=f"operation:{self._state.serial + 1}")
        if "ability_token" in frame.context:
            op["object_token_id"] = frame.context["ability_token"]
            op["object_origin"] = frame.context.get("origin")
        def apply(s):
            f = s.stack[-1]
            # A source removed during nested resolution cannot keep producing effects.
            ability_id = f.context.get("ability_token")
            if ability_id and (f.context.get("invalidated") or s.tokens[ability_id].position_id in
                               (zone(f.actor_country, "ABILITY_INACTIVE"), zone(f.actor_country, "OBJECT_POOL"))):
                abandoned = []
                for pending in f.remaining_operations:
                    announcement_id = pending.get("announcement_id")
                    if announcement_id in s.announcements:
                        del s.announcements[announcement_id]
                        abandoned.append(announcement_id)
                s.stack.pop()
                return outcome("ABILITY_INVALIDATED", abandoned_announcements=abandoned)
            if op["type"] == "FINISH_FRAME":
                s.stack.pop()
                result = outcome("FINISH_FRAME")
                completed_object = False
                on_settle = ()
                settled_definition = None
                if ability_id:
                    t = s.tokens[ability_id]
                    ability = self.rules.abilities[t.definition_id]
                    if s.tokens[t.source_token_id].position_id == "RESOLUTION":
                        settled_definition = s.tokens[t.source_token_id].definition_id
                        self.executor.settle_card(s, t.source_token_id, zone(t.owner_country, "RESPONSE"), "DISCARD", result)
                        completed_object = True
                        on_settle = ability.get("on_settle", ())
                    elif t.position_id == "RESOLUTION":
                        completed_object = True
                        settled_definition = t.definition_id
                        on_settle = ability.get("on_settle", ())
                        if ability["after"] == "origin":
                            self.executor.transfer(s, ability_id, f.context.get("origin", zone(t.owner_country, "ABILITY_READY")), result)
                        else:
                            self.executor.transfer(s, ability_id, zone(t.owner_country, "OBJECT_POOL"), result)
                    elif t.position_id == zone(t.owner_country, "ABILITY_PENDING"):
                        self.executor.transfer(s, ability_id, zone(t.owner_country, "ABILITY_READY"), result)
                elif f.source_card_id and s.tokens[f.source_card_id].position_id == "RESOLUTION":
                    card = self.rules.cards[s.tokens[f.source_card_id].definition_id]
                    settled_definition = card["id"]
                    destination = card["type"] if card["type"] in ("STATUS", "RESPONSE") else "DISCARD"
                    self.executor.settle_card(s, f.source_card_id, f.context.get("origin", zone(f.actor_country, "HAND")), destination, result)
                    completed_object = True
                    on_settle = card.get("on_settle", ())
                if completed_object:
                    result["event"] = "OBJECT_SETTLED"
                    for definition in on_settle:
                        transfer = self._resolve(definition, f)
                        transfer["actor_country"] = f.actor_country
                        output = self.executor.execute(s, transfer, f)
                        result["changes"].extend(output["changes"])
                        if not output["success"]:
                            result["context"].setdefault("output_failures", []).append(output["context"]["reason"])
                    parent = next((frame for frame in s.stack if frame.resolution_id == f.parent_resolution_id), None)
                    result["context"]["settled_definition"] = settled_definition
                    AbilityScanner(self.rules).scan(s, {**op, "type": "OBJECT_SETTLED", "settled_definition": settled_definition}, result, parent, self.executor)
                return result
            f.remaining_operations.pop()
            f.current_step += 1
            if op["type"] in PREVENTABLE and "announcement_id" not in op:
                if reason := self.executor.target_failure(s, op):
                    if f.context.get("failure_policy") == "stop":
                        f.remaining_operations.clear()
                    return outcome_failure(op["type"], reason)
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
                origin = f.context.get("origin", zone(country, "ABILITY_READY"))
                if token.position_id != origin:
                    self.executor.transfer(s, token.token_id, origin, result)
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
            source = s.tokens[token.source_token_id]
            if source.position_id == zone(country, "RESPONSE") and token.position_id != source.position_id:
                self.executor.transfer(s, source.token_id, "RESOLUTION", result, resolving_ability=token.token_id)
                result["context"]["revealed_source"] = source.token_id
            else:
                self.executor.transfer(s, token.token_id, "RESOLUTION", result)
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
