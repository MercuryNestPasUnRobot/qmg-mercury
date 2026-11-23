"""One serial executor shared by human clients and AI. No callbacks execute data."""
from copy import deepcopy
from dataclasses import replace

from .model import CoreError, DecisionRequest, ResolutionFrame, TURN_ORDER, digest, zone
from .rules import LegalActionGenerator, SupplyResolver, board_units, initialize, status_enabled, validate_state
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

    def candidates(self, state, op, result, tokens):
        matches = []
        reserved = {f.context.get("ability_token") for f in state.stack}
        selected = set()
        for token in tokens:
            if token.token_type not in ("ABILITY", "RULE") or token.token_id in reserved or token.position_id not in (
                    zone(token.owner_country, "ABILITY_READY"), zone(token.owner_country, "RESPONSE")):
                continue
            ability = self.rules.abilities[token.definition_id]
            if token.token_type == "RULE" and token.position_id != zone(token.owner_country, "RESPONSE"):
                continue
            source = state.tokens[token.source_token_id]
            source_definition = self.rules.cards.get(source.definition_id, {})
            if source_definition.get("implementation", {}).get("status") == "blocked":
                continue
            if (token.token_type == "ABILITY" and not ability.get("system_response")
                    and source_definition.get("type") == "STATUS" and not status_enabled(self.rules, state, source)):
                continue
            if token.token_type != "RULE" and source.position_id not in {zone(token.owner_country, z) for z in ability.get("listener_scope", ("STATUS", "RESPONSE"))}:
                continue
            trigger = ability["trigger"]
            if "card_trigger" in trigger:
                from .content_triggers import matches_trigger
                if not matches_trigger(self.rules, state, token, trigger["card_trigger"], op, result):
                    continue
            elif trigger["operation_type"] != result["event"]:
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
                if not any(target in state.tokens and
                           self.rules.force_controllers.get(state.tokens[target].owner_country) == token.owner_country
                           for target in targets):
                    continue
            group = (source.token_id, token.definition_id)
            if token.position_id == zone(token.owner_country, "ABILITY_READY"):
                if group in selected:
                    continue
                selected.add(group)
            matches.append((ability["priority"], token.token_id, token, ability))
        return matches

    def scan(self, state, op, result, source_frame):
        if not result["success"] or state.winner:
            return
        matches = self.candidates(state, op, result, sorted(state.tokens.values(), key=lambda token: token.token_id))
        if not matches:
            return
        # A candidate remains in its own zone until selected. It can answer a nested
        # event while this window is suspended, and its live position is checked again.
        active = TURN_ORDER[state.turn.country_index]
        window = CardInterpreter.frame(
            state, active, None, [{"type": "NEXT_RESPONSE"}], source_frame.resolution_id if source_frame else None,
            {"event": op, "event_outcome": result, "response_candidates": [key for _, key, _, _ in matches]},
        )
        state.stack.append(window)

    def groups(self, state, window):
        active = state.turn.country_index
        groups = {}
        for key in window.context["response_candidates"]:
            token = state.tokens[key]
            ability = self.rules.abilities[token.definition_id]
            optional = ability["activation_mode"] == "OPTIONAL"
            seat_order = (TURN_ORDER.index(token.owner_country) - active) % len(TURN_ORDER)
            order = (ability["priority"], seat_order, optional)
            groups.setdefault(order, []).append(token)
        return [groups[key] for key in sorted(groups)]

    def eligible(self, state, window, tokens, executor):
        event, result = window.context["event"], window.context["event_outcome"]
        if event["type"] == "ANNOUNCE":
            announced = state.announcements.get(event["announcement_id"])
            if not announced or announced["cancelled"] or executor.target_failure(
                    state, {**event, "type": event["announced_type"]}):
                return []
        if event["type"] in ("CARD_DECLARED", "ECONOMIC_PLAY", "ECONOMIC_TARGET"):
            parent = next((f for f in state.stack if f.resolution_id == event["resolution_id"]), None)
            if parent is None or parent.context.get("invalidated") or parent.context.get("effect_cancelled") or (
                    parent.context.get("economic", {}).get("cancelled")):
                return []
        active = TURN_ORDER[state.turn.country_index]
        return [token for _, _, token, ability in self.candidates(state, event, result, tokens)
                if not (window.context.get("active_response_used") and token.owner_country == active and
                        ability["activation_mode"] == "OPTIONAL")
                and self.can_activate(state, window, token, executor)]

    def can_activate(self, state, frame, token, executor):
        from .content_effects import can_begin, condition_met, event_effect
        from .definitions import TARGETED_OPERATIONS
        ability = self.rules.abilities[token.definition_id]
        if ability.get("system_response"):
            return True
        source = state.tokens[token.source_token_id]
        country = token.owner_country
        card = self.rules.cards.get(source.definition_id, {})
        if not condition_met(self.rules, state, country, card.get("content_condition")):
            return False
        hand_costs = [key for key in state.positions[zone(country, "HAND")] if key != source.token_id]
        if len(hand_costs) < executor.activation_cost(state, token):
            return False
        if ability["activation_mode"] == "AUTO":
            return True
        context = {**frame.context, "excluded_card": source.token_id, "ability_token": token.token_id,
                   "bound_source": source.token_id}
        candidate = replace(frame, actor_country=country, context=context)
        for step in ability["effects"]:
            if step["type"] == "CARD_EFFECT":
                effect = event_effect(step["effect"], candidate)
                chooser = effect.get("chooser", country)
                if not condition_met(self.rules, state, chooser, effect.get("when")):
                    continue
                if not can_begin(self.rules, state, chooser, effect, context):
                    return False
                # These payments do not move units or change map rules. Check the
                # first real effect before asking the player to pay for it.
                if effect["type"] in ("discard-hand", "discard-top", "discard-top-per-unit"):
                    continue
                return True
            template = step.get("operation", step)
            if template["type"] == "CANCEL":
                operation = CommandProcessor._resolve(template, candidate)
                announcement = state.announcements.get(operation["announcement_id"])
                return bool(announcement and not announcement["cancelled"])
            if template["type"] in TARGETED_OPERATIONS:
                operation = CommandProcessor._resolve(template, candidate)
                if step["type"] == "CHOOSE":
                    return bool(executor.legal.targets(state, country, operation))
                if operation["type"] in PREVENTABLE:
                    return executor.target_failure(state, {**operation, "actor_country": country}) is None
                return operation.get("destination", operation.get("target_token")) in executor.legal.targets(
                    state, country, operation)
            return True
        return True

    def open_frame(self, state, window, token, executor, result):
        origin = token.position_id
        if origin == zone(token.owner_country, "ABILITY_READY"):
            executor.transfer(state, token.token_id, zone(token.owner_country, "ABILITY_PENDING"), result)
        source = state.tokens[token.source_token_id]
        source_id = source.token_id if token.token_type == "ABILITY" else None
        state.stack.append(CardInterpreter.frame(
            state, token.owner_country, source_id, [{"type": "ACTIVATE_SELECTED", "use": True}],
            window.resolution_id,
            {"ability_token": token.token_id, "origin": origin, "bound_source": source.token_id,
             "failure_policy": self.rules.cards[source.definition_id]["failure_policy"] if source_id else "stop",
             "event": window.context["event"], "event_outcome": window.context["event_outcome"]},
        ))


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
        if "response_country" in result["context"]:
            operation["actor_country"] = result["context"]["response_country"]
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
                  "targets": {k: operation[k] for k in ("target", "target_token", "token", "destination", "force") if k in operation},
                  "operation": operation, "outcome": result, "ruleset_version": self.rules.version,
                  "rules_digest": self.rules.fingerprint, "before_version": self._state.version,
                  "after_version": draft.version, "before_hash": digest(before), "after_hash": digest(after),
                  "previous_record": self.record_head,
                  "state_patch": {k: deepcopy(v) for k, v in after.items() if before.get(k) != v}}
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
        active = TURN_ORDER[s.turn.country_index]
        decision_frame = s.stack[-1] if s.decision else None
        consumed_interrupt = decision_frame and (
            decision_frame.context.get("active_response_used")
            if "response_candidates" in decision_frame.context else
            decision_frame.context.get("active_interrupt_at") == s.decision.originating_operation_id
        )
        interrupts = []
        if (s.turn.phase != "Setup" and (s.decision or not s.stack)
                and sum(f.context.get("interrupt_anchor", False) for f in s.stack) < 4):
            reserved = {f.context.get("ability_token") for f in s.stack}
            for token in s.tokens.values():
                if (token.token_type != "ABILITY" or token.owner_country not in controlled_countries
                        or token.token_id in reserved or token.position_id != zone(token.owner_country, "ABILITY_READY")):
                    continue
                ability = self.rules.abilities[token.definition_id]
                trigger = ability["trigger"].get("card_trigger", {})
                source = s.tokens[token.source_token_id]
                card = self.rules.cards[source.definition_id]
                if trigger.get("type") != "any-time" or card.get("implementation", {}).get("status") == "blocked":
                    continue
                if trigger.get("ownTurnOnly") and TURN_ORDER[s.turn.country_index] != token.owner_country:
                    continue
                if token.owner_country == active and consumed_interrupt:
                    continue
                if source.position_id not in {zone(token.owner_country, kind) for kind in ability.get("listener_scope", ("STATUS", "RESPONSE"))}:
                    continue
                if card["type"] == "STATUS" and not status_enabled(self.rules, s, source):
                    continue
                candidate = ResolutionFrame("", source.token_id, token.owner_country, [])
                if AbilityScanner(self.rules).can_activate(s, candidate, token, self.executor):
                    command = {"type": "ACTIVATE_CARD", "actor": token.owner_country,
                               "expected_version": s.version, "card": source.token_id}
                    if command not in interrupts:
                        interrupts.append(command)
        if s.decision:
            d = s.decision
            if d.required_country not in controlled_countries:
                return interrupts
            return [{"type": "CHOOSE", "actor": d.required_country, "expected_version": s.version,
                     "decision_id": d.decision_id, "option": option} for option in d.legal_options] + interrupts
        country = TURN_ORDER[s.turn.country_index]
        if s.stack or country not in controlled_countries or s.turn.phase == "Setup":
            return interrupts
        commands = [{"type": "END_PHASE", "actor": country, "expected_version": s.version}]
        if s.turn.phase == "Discard":
            commands.extend({"type": "DISCARD_CARD", "actor": country, "expected_version": s.version, "card": key}
                            for key in s.positions[zone(country, "HAND")])
        if s.turn.phase == "Air":
            from .air import air_options, opportunity
            for force in self.rules.controlled_forces(country):
                if not opportunity(s, "air-opportunity-" + force):
                    continue
                if not air_options(self.rules, s, country, {"force": force, "actions": ["deploy", "marshal", "superiority"]}):
                    continue
                for token_id in s.positions[zone(country, "HAND")]:
                    if self.rules.cards[s.tokens[token_id].definition_id].get("legacy_kind") == "air-power":
                        commands.append({"type": "USE_AIR", "actor": country, "expected_version": s.version,
                                         "card": token_id, "force": force})
        if s.turn.phase == "Play" and self.rules.settings.get("reallocation"):
            from .content_actions import reallocation_available
            if reallocation_available(self.rules, s, country):
                commands.append({"type": "REALLOCATE", "actor": country, "expected_version": s.version})
        if s.turn.phase == "Play" and s.turn.plays_remaining > 0:
            for token_id in s.positions[zone(country, "HAND")]:
                card = self.rules.cards[s.tokens[token_id].definition_id]
                if not self.legal.can_start_card(s, country, card):
                    continue
                modes = card.get("play_as")
                if modes:
                    for mode in modes:
                        if self.legal.targets(s, country, {"type": mode}):
                            commands.append({"type": "PLAY_CARD", "actor": country, "expected_version": s.version,
                                             "card": token_id, "play_as": mode})
                else:
                    commands.append({"type": "PLAY_CARD", "actor": country, "expected_version": s.version, "card": token_id})
            for token_id in s.positions[zone(country, "STATUS")]:
                card = self.rules.cards[s.tokens[token_id].definition_id]
                active = card.get("active_ability")
                if active is None:
                    continue
                available = any(t.source_token_id == token_id and t.definition_id == active
                                and t.position_id == zone(country, "ABILITY_READY") for t in s.tokens.values())
                if available and self.legal.can_start_card(
                        s, country, {**card, "effects": self.rules.abilities[active]["effects"]}, activating=True):
                    commands.append({"type": "USE_STATUS", "actor": country, "expected_version": s.version, "card": token_id})
        return commands + interrupts

    def submit(self, command, controlled_countries, auto_resolve=True):
        if not isinstance(command, dict) or type(command.get("expected_version")) is not int or command not in self.legal_commands(controlled_countries):
            raise CoreError("Unauthorized, stale or illegal command")
        op = {"type": command["type"], "actor_country": command["actor"], "source_token_id": command.get("card")}
        if "play_as" in command:
            op["play_as"] = command["play_as"]
        if command["type"] == "CHOOSE":
            frame = self._state.stack[-1]
            op.update(resolution_id=frame.resolution_id, parent_resolution_id=frame.parent_resolution_id,
                      source_token_id=frame.source_card_id, decision_id=command["decision_id"])
            if "ability_token" in frame.context:
                op.update(object_token_id=frame.context["ability_token"], object_origin=frame.context.get("origin"))
        def apply(s):
            if command["type"] == "DISCARD_CARD":
                s.stack.append(CardInterpreter.frame(s, command["actor"], None,
                    [{"type": "DISCARD", "target_token": command["card"]}]))
                return outcome("DISCARD_CARD")
            if command["type"] in ("USE_AIR", "REALLOCATE"):
                from .content_actions import begin_action
                return begin_action(self, s, command)
            if command["type"] == "ACTIVATE_CARD":
                country, card_id = command["actor"], command["card"]
                ability = next(t for t in s.tokens.values() if t.source_token_id == card_id and
                    t.position_id == zone(country, "ABILITY_READY") and
                    self.rules.abilities[t.definition_id]["trigger"].get("card_trigger", {}).get("type") == "any-time")
                parent_id = s.stack[-1].resolution_id if s.stack else None
                if s.decision and country == TURN_ORDER[s.turn.country_index]:
                    pending = s.stack[-1]
                    if "response_candidates" in pending.context:
                        pending.context["active_response_used"] = True
                    else:
                        pending.context["active_interrupt_at"] = s.decision.originating_operation_id
                context = {"interrupt_anchor": True, "suspended_decision": deepcopy(s.decision.__dict__) if s.decision else None}
                anchor = CardInterpreter.frame(s, country, None, [{"type": "RESTORE_INTERRUPTED_DECISION"}],
                                               parent_id, context=context)
                s.decision = None
                s.stack.append(anchor)
                result = outcome("ACTIVATE_CARD")
                self.executor.transfer(s, ability.token_id, zone(country, "ABILITY_PENDING"), result)
                s.stack.append(CardInterpreter.frame(s, country, card_id, [{"type": "ACTIVATE_SELECTED", "use": True}],
                    anchor.resolution_id, {"ability_token": ability.token_id, "origin": zone(country, "ABILITY_READY"),
                                          "failure_policy": "stop", "event": op, "event_outcome": result}))
                return result
            if command["type"] == "CHOOSE":
                d = s.decision
                frame = next(f for f in s.stack if f.resolution_id == d.resolution_id)
                option = command["option"]
                frame.selected_targets[d.decision_id] = option
                continuation = deepcopy(d.continuation)
                if continuation["type"] == "ACTIVATE_SELECTED":
                    continuation["use"] = option == "use"
                elif continuation["type"] in ("RESPONSE_SELECTED", "CARD_EFFECT_SELECTED", "PAYMENT_SELECTED", "ORDER_DECK_SELECTED", "RETURN_CARD_SELECTED", "BATTLE_ORDER_SELECTED", "COUNTRY_LOSS_SELECTED",
                                               "BATTLE_VICTIM_SELECTED", "AIR_ACTION_SELECTED", "AIR_DEFENSE_SELECTED", "AIR_COUNTER_SELECTED", "AIR_REBASE_SELECTED", "REALLOCATION_SELECTED", "EXTRA_MODE_SELECTED"):
                    continuation["choice"] = option
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
            if command["type"] == "USE_STATUS":
                country, card_id = command["actor"], command["card"]
                active = self.rules.cards[s.tokens[card_id].definition_id]["active_ability"]
                ability = next(t for t in s.tokens.values() if t.source_token_id == card_id
                               and t.definition_id == active and t.position_id == zone(country, "ABILITY_READY"))
                result = outcome("USE_STATUS")
                self.executor.transfer(s, ability.token_id, zone(country, "ABILITY_PENDING"), result)
                s.stack.append(CardInterpreter.frame(s, country, card_id, [{"type": "ACTIVATE_ABILITY"}],
                    context={"ability_token": ability.token_id, "origin": zone(country, "ABILITY_READY"),
                             "failure_policy": "stop", "event": op, "event_outcome": result}))
                s.turn.plays_remaining -= 1
                return result
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
                effects = ([{"type": "CHOOSE", "operation": {"type": command["play_as"], "target": "$choice"}}]
                           if "play_as" in command else definition["effects"])
                frame = CardInterpreter.frame(s, command["actor"], token_id, effects,
                                              context={"failure_policy": definition["failure_policy"], "origin": zone(command["actor"], "HAND")})
                if definition.get("legacy_kind") == "economic":
                    frame.remaining_operations.append({"type": "ECONOMIC_PLAY"})
                s.stack.append(frame)
                result["context"]["resolution_id"] = frame.resolution_id
                s.turn.plays_remaining -= 1
                AbilityScanner(self.rules).scan(s, op, result, frame)
                return result
            country, operations, result = TurnController(self.rules).next_phase(s)
            frame = CardInterpreter.frame(s, country, None, operations, context={"closing_turn": result["context"]["phase"] == "Draw"})
            s.stack.append(frame)
            AbilityScanner(self.rules).scan(s, op, result, frame)
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

    def _next_response(self, state, window, op):
        scanner = AbilityScanner(self.rules)
        remaining = window.context["response_candidates"]
        for group in scanner.groups(state, window):
            eligible = scanner.eligible(state, window, group, self.executor)
            if not eligible:
                for token in group:
                    remaining.remove(token.token_id)
                continue
            first = eligible[0]
            window.actor_country = first.owner_country
            if self.rules.abilities[first.definition_id]["activation_mode"] == "AUTO":
                remaining.remove(first.token_id)
                window.remaining_operations.append({"type": "NEXT_RESPONSE"})
                result = outcome("RESPONSE_QUEUED", response_country=first.owner_country)
                scanner.open_frame(state, window, first, self.executor, result)
                return result
            keys = [token.token_id for token in eligible]
            options = ["decline", "use"] if len(keys) == 1 else ["decline", *keys]
            result = self._request(state, window, "ABILITY" if len(keys) == 1 else "RESPONSE_ORDER", options,
                                   {"type": "RESPONSE_SELECTED", "candidates": keys,
                                    "group": [token.token_id for token in group]}, op["operation_id"], optional=True)
            result["context"]["response_country"] = first.owner_country
            return result
        return outcome("RESPONSES_FINISHED")

    def _select_response(self, state, window, op):
        remaining = window.context["response_candidates"]
        window.remaining_operations.append({"type": "NEXT_RESPONSE"})
        if op["choice"] == "decline":
            window.context["response_candidates"] = [key for key in remaining if key not in op["group"]]
            return outcome("ABILITY_DECLINED")
        key = op["candidates"][0] if op["choice"] == "use" else op["choice"]
        token = state.tokens[key]
        remaining.remove(key)
        scanner = AbilityScanner(self.rules)
        if not scanner.eligible(state, window, [token], self.executor):
            return outcome("RESPONSE_NO_LONGER_LEGAL")
        if token.owner_country == TURN_ORDER[state.turn.country_index]:
            window.context["active_response_used"] = True
        result = outcome("RESPONSE_QUEUED")
        scanner.open_frame(state, window, token, self.executor, result)
        return result

    @staticmethod
    def _resolve(value, frame):
        if isinstance(value, dict):
            return {k: CommandProcessor._resolve(v, frame) for k, v in value.items()}
        if isinstance(value, list):
            return [CommandProcessor._resolve(v, frame) for v in value]
        if isinstance(value, str) and value.startswith("$event."):
            return frame.context["event"][value[7:]]
        if value == "$object":
            return frame.context["ability_token"]
        if value == "$owner_ready":
            return zone(frame.actor_country, "ABILITY_READY")
        if value == "$owner_status":
            return zone(frame.actor_country, "STATUS")
        if value == "$bound_source":
            return frame.context["bound_source"]
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
        actor = TURN_ORDER[(self._state.turn.country_index + 1) % len(TURN_ORDER)] if op.get("advance_country") else op.get("actor_override", frame.actor_country)
        op.update(actor_country=actor, source_token_id=frame.source_card_id or frame.context.get("effect_source"),
                  resolution_id=frame.resolution_id, parent_resolution_id=frame.parent_resolution_id,
                  operation_id=f"operation:{self._state.serial + 1}")
        if op["type"] == "PLAY_EXTRA_CARD":
            op["source_token_id"] = op["token"]
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
                        source_origin = f.context.get("source_origin", zone(t.owner_country, "RESPONSE"))
                        self.executor.settle_card(s, t.source_token_id, source_origin, "DISCARD", result)
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
                    destination = f.context.get("settlement_destination",
                        card["type"] if card["type"] in ("STATUS", "RESPONSE") else "DISCARD")
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
                    AbilityScanner(self.rules).scan(s, {**op, "type": "OBJECT_SETTLED", "settled_definition": settled_definition}, result, parent)
                return result
            f.remaining_operations.pop()
            f.current_step += 1
            if f.context.get("effect_cancelled"):
                f.remaining_operations.clear()
                return outcome("CARD_EFFECT_CANCELLED")
            if f.context.get("economic", {}).get("cancelled"):
                f.remaining_operations.clear()
                return outcome("ECONOMIC_CANCELLED")
            if self.rules.settings.get("declare_passives") and not op.get("rule_checkpoint"):
                rule_kinds = []
                if op["type"] in ("BUILD_ARMY", "BUILD_NAVY", "RECRUIT", "LAND_BATTLE", "SEA_BATTLE", "CHECK_SUPPLY", "CHECK_SCORE"):
                    rule_kinds = ["connection", "supply_source", "deny_supply_source", "home", "always_supplied", "supply_sharing", "recruit_region", "build_region", "score_bonus", "ignore_enemy_statuses"]
                if op["type"] == "CARD_EFFECT" and op["effect"]["type"] in ("discard-top", "discard-top-per-unit", "country-loss-choice") and "economic" in f.context:
                    rule_kinds = ["economic_adjustment", "ignore_enemy_statuses"]
                if op["type"] == "REALLOCATION_REQUEST" and not op.get("selected"):
                    rule_kinds = ["reallocate_cost", "reallocate_discard", "ignore_enemy_statuses"]
                if rule_kinds:
                    f.remaining_operations.append({**raw, "rule_checkpoint": True})
                    event = {**op, "type": "RULE_USE", "rule_kinds": rule_kinds, "rule_force": op.get("force", actor)}
                    result = outcome("RULE_USE", kinds=rule_kinds)
                    AbilityScanner(self.rules).scan(s, event, result, f)
                    return result
            if op["type"] in BATTLES and not op.get("target_token") and not op.get("expected_victim"):
                from .air import victim_options
                victims = victim_options(self.rules, s, op)
                if len(victims) > 1:
                    return self._request(s, f, "BATTLE_VICTIM", victims,
                        {"type": "BATTLE_VICTIM_SELECTED", "battle": op}, op["operation_id"])
                if victims:
                    op["target_token"] = victims[0]
                    raw["target_token"] = victims[0]
            if op["type"] in PREVENTABLE and "announcement_id" not in op and not op.get("skip_announcement"):
                if reason := self.executor.target_failure(s, op):
                    if f.context.get("failure_policy") == "stop":
                        f.remaining_operations.clear()
                    return outcome_failure(op["type"], reason)
                announcement = s.new_id("announcement")
                s.announcements[announcement] = {"cancelled": False}
                f.remaining_operations.append({**raw, "announcement_id": announcement})
                announced = {**op, "type": "ANNOUNCE", "announced_type": op["type"], "announcement_id": announcement}
                result = outcome("ANNOUNCE", announced_type=op["type"], announcement_id=announcement)
                AbilityScanner(self.rules).scan(s, announced, result, f)
                return result
            if op["type"] in PREVENTABLE and "announcement_id" in op:
                if s.announcements.pop(op["announcement_id"])["cancelled"]:
                    result = outcome_failure(op["type"], "Cancelled after announcement")
                    if f.context.get("failure_policy") == "stop":
                        f.remaining_operations.clear()
                    return result
                if op["type"] in BATTLES and self.rules.settings.get("air_phase"):
                    battle = {k: v for k, v in op.items() if k != "announcement_id"}
                    battle.update(skip_announcement=True, actor_override=actor)
                    f.remaining_operations.append({"type": "BATTLE_AIR_WINDOW", "battle": battle})
                    return outcome("BATTLE_AIR_WINDOW_OPENED")
            result, scan = self._execute(s, f, op)
            if result["success"] and op["type"] in ("BUILD_ARMY", "BUILD_NAVY", "RECRUIT"):
                f.context["last_built_region"] = op["target"]
            if not result["success"] and f.context.get("failure_policy") == "stop":
                f.remaining_operations.clear()
            if scan:
                # Base loss resumes in a child frame after the ordinary removal responses.
                if op["type"] in BATTLES | {"ELIMINATE"}:
                    from .air import air_units
                    removed = {(s.tokens[c["token_id"]].owner_country, c["from"]) for c in result["changes"]
                               if c["from"] in self.rules.regions and s.tokens[c["token_id"]].token_type == "UNIT"
                               and self.rules.units[s.tokens[c["token_id"]].definition_id]["kind"] != "AIR"}
                    for air in air_units(self.rules, s):
                        if (air.owner_country, air.position_id) in removed:
                            s.stack.append(CardInterpreter.frame(s, self.rules.force_controllers[air.owner_country], None,
                                [{"type": "CHECK_AIR_BASE", "air": air.token_id}], f.resolution_id))
                AbilityScanner(self.rules).scan(s, op, result, f)
            return result
        return self._commit(op, apply)

    def _execute(self, s, f, op):
        kind, country = op["type"], f.actor_country
        if kind == "NEXT_RESPONSE":
            return self._next_response(s, f, op), False
        if kind == "RESPONSE_SELECTED":
            return self._select_response(s, f, op), False
        if kind.startswith("AIR_") or kind in ("BATTLE_VICTIM_SELECTED", "BATTLE_AIR_WINDOW", "CHECK_AIR_BASE"):
            from .air import execute_air
            return execute_air(self, s, f, op)
        if kind.startswith("REALLOCATION_"):
            from .content_actions import execute_action
            return execute_action(self, s, f, op)
        if kind == "RESTORE_INTERRUPTED_DECISION":
            saved = f.context.get("suspended_decision")
            if not saved:
                return outcome("INTERRUPT_COMPLETE"), False
            # The anchor is finished before exposing the parent's refreshed decision.
            s.stack.pop()
            parent = next((frame for frame in s.stack if frame.resolution_id == saved["resolution_id"]), None)
            if parent is None or parent.context.get("invalidated"):
                return outcome("INTERRUPTED_CHOICE_INVALIDATED"), False
            continuation = deepcopy(saved["continuation"])
            choice_kind = continuation["type"]
            if choice_kind == "RESPONSE_SELECTED":
                return self._next_response(s, parent, op), False
            if choice_kind == "ACTIVATE_SELECTED":
                token = s.tokens[parent.context["ability_token"]]
                cost = self.executor.activation_cost(s, token)
                options = ["decline"] + (["use"] if len(s.positions[zone(parent.actor_country, "HAND")]) >= cost else [])
            elif choice_kind in ("COST_SELECTED", "PAYMENT_SELECTED"):
                hand = s.positions[zone(parent.actor_country, "HAND")]
                selected = parent.context.get("cost_tokens", []) if choice_kind == "COST_SELECTED" else continuation["selected"]
                remaining = [key for key in selected if key in hand]
                if choice_kind == "COST_SELECTED":
                    parent.context["cost_tokens"] = remaining
                    parent.remaining_operations.extend({"type": "SELECT_COST"} for _ in range(len(selected) - len(remaining)))
                    options = [key for key in hand if key not in remaining]
                else:
                    continuation["selected"] = remaining
                    options = [key for key in hand if key not in remaining] if len(remaining) < continuation["effect"]["max"] else []
                    if len(remaining) >= continuation["effect"]["min"]:
                        options.append("confirm")
                    if continuation["effect"].get("onDecline") or len(hand) < continuation["effect"]["min"]:
                        options.append("decline")
            elif choice_kind == "CARD_EFFECT_SELECTED":
                from .content_effects import available_options
                options = available_options(self.rules, s, parent.actor_country, continuation["effect"], parent.context)
                selected = continuation["selected"]
                live = [key for key in selected if key in options]
                if continuation["effect"]["type"] == "discard-hand" and continuation["effect"].get("strict"):
                    continuation.update(selected=live, remaining=continuation["remaining"] + len(selected) - len(live))
                options = [key for key in options if key not in continuation["selected"]]
                if saved["optional_flag"]:
                    options.append("done")
            elif choice_kind == "REDEPLOY_SELECTED":
                options = [key for key in parent.eligible_token_ids if key not in parent.processed_token_ids
                           and s.tokens[key].position_id in self.rules.regions]
            elif choice_kind == "REBUILD_UNIT":
                token = s.tokens[continuation["token"]]
                virtual = deepcopy(s)
                self.executor.transfer(virtual, token.token_id, zone(token.owner_country, "RESERVE_ARMY"), outcome("INTERNAL"))
                options = self.legal.targets(virtual, parent.actor_country, {"type": "BUILD_ARMY", "force": token.owner_country})
            elif choice_kind == "RETURN_CARD_SELECTED":
                options = ["done"]
                if len(s.positions[zone(parent.actor_country, "HAND")]) >= continuation["effect"]["discardHandCount"]:
                    options.append("return")
            elif choice_kind == "ORDER_DECK_SELECTED":
                options = [key for key in continuation["eligible"] if key not in continuation["selected"]
                           and s.tokens[key].position_id == zone(parent.actor_country, "DECK")]
            elif choice_kind == "BATTLE_ORDER_SELECTED":
                from .content_effects import available_options
                available = available_options(self.rules, s, parent.actor_country, continuation["effect"])
                if any(key not in available for key in continuation["selected"]):
                    parent.remaining_operations.clear()
                    return outcome("INTERRUPTED_CHOICE_INVALIDATED"), False
                options = [key for key in available if key not in continuation["selected"]]
                if len(continuation["selected"]) >= continuation["effect"]["min"]:
                    options.append("confirm")
            elif choice_kind == "COUNTRY_LOSS_SELECTED":
                from .rules import unit_rule_matches
                effect = continuation["effect"]
                options = ["discard"] + [t.token_id for t in board_units(self.rules, s, parent.actor_country)
                    if self.rules.units[t.definition_id]["kind"].lower() in effect["removeKinds"]
                    and ("removeSpaces" not in effect or t.position_id in effect["removeSpaces"])
                    and not unit_rule_matches(self.rules, s, t, "protection")]
            elif choice_kind == "EXTRA_MODE_SELECTED":
                definition = self.rules.cards[s.tokens[parent.source_card_id].definition_id]
                options = [mode for mode in definition["play_as"] if self.legal.targets(s, parent.actor_country, {"type": mode})]
            elif choice_kind in ("AIR_ACTION_SELECTED", "BATTLE_VICTIM_SELECTED", "AIR_REBASE_SELECTED", "AIR_DEFENSE_SELECTED", "AIR_COUNTER_SELECTED"):
                from .air import air_options, air_units, rebase_options, victim_options
                if choice_kind == "AIR_ACTION_SELECTED":
                    options = air_options(self.rules, s, parent.actor_country, continuation["effect"])
                elif choice_kind == "BATTLE_VICTIM_SELECTED":
                    options = victim_options(self.rules, s, continuation["battle"])
                elif choice_kind == "AIR_REBASE_SELECTED":
                    options = rebase_options(self.rules, s, s.tokens[continuation["air"]])
                elif choice_kind == "AIR_DEFENSE_SELECTED":
                    options = ["unit"] + (["air"] if s.tokens[continuation["air"]].position_id in self.rules.regions else [])
                else:
                    battle = continuation["battle"]
                    force = battle.get("force", parent.actor_country)
                    from .rules import MapGraph
                    supplied = SupplyResolver(self.rules).supplied(s, force)
                    neighbors = MapGraph(self.rules).neighbors(battle["target"], s, force)
                    options = ["pass"] + [t.token_id for t in air_units(self.rules, s, force)
                                          if t.token_id in supplied and t.position_id in neighbors]
            elif choice_kind == "REALLOCATION_SELECTED":
                from .content_actions import reallocation_payment_options
                hand = s.positions[zone(parent.actor_country, "HAND")]
                selected = [key for key in continuation["selected"] if key in hand]
                continuation["selected"] = selected
                options = reallocation_payment_options(self.rules, s, parent.actor_country, selected)
            else:
                options = self.legal.targets(s, parent.actor_country, continuation)
            if not options:
                parent.remaining_operations.clear()
                return outcome("INTERRUPTED_CHOICE_INVALIDATED"), False
            return self._request(s, parent, saved["decision_type"], options, continuation,
                                 saved["originating_operation_id"], saved["optional_flag"]), False
        if kind == "PLAY_EXTRA_CARD":
            token = s.tokens[op["token"]]
            definition = self.rules.cards[token.definition_id]
            if token.position_id != op["expected_from"] or not self.legal.can_start_card(s, country, definition):
                return outcome_failure("PLAY_CARD", "Extra card is no longer playable"), False
            if token.owner_country != country:
                return outcome_failure("PLAY_CARD", "Extra card belongs to another seat"), False
            for pending in s.stack:
                if token.token_id in pending.context.get("revealed_discard", {}).values():
                    pending.context.setdefault("revealed_used", []).append(token.token_id)
            result = outcome("SET_RESPONSE" if definition["type"] == "RESPONSE" else "PLAY_CARD")
            if definition["type"] == "RESPONSE":
                self.executor.transfer(s, token.token_id, zone(country, "RESPONSE"), result)
            else:
                self.executor.transfer(s, token.token_id, "RESOLUTION", result)
                effects = [{"type": "EXTRA_MODE_REQUEST"}] if definition.get("play_as") else definition["effects"]
                child = CardInterpreter.frame(s, country, token.token_id, effects, f.resolution_id,
                    context={"failure_policy": definition["failure_policy"], "origin": op["expected_from"], "extra_play": True})
                if definition.get("legacy_kind") == "economic":
                    child.remaining_operations.append({"type": "ECONOMIC_PLAY"})
                s.stack.append(child)
            return result, True
        if kind == "EXTRA_MODE_REQUEST":
            definition = self.rules.cards[s.tokens[f.source_card_id].definition_id]
            options = [mode for mode in definition["play_as"] if self.legal.targets(s, country, {"type": mode})]
            return self._request(s, f, "EXTRA_CARD_MODE", options, {"type": "EXTRA_MODE_SELECTED"}, op["operation_id"]), False
        if kind == "EXTRA_MODE_SELECTED":
            f.remaining_operations.append({"type": "CHOOSE", "operation": {"type": op["choice"], "target": "$choice"}})
            return outcome("EXTRA_CARD_MODE", play_as=op["choice"]), False
        if kind == "COUNTRY_LOSS_STEP":
            from .rules import unit_rule_matches
            effect = op["effect"]
            options = ["discard"] + [t.token_id for t in board_units(self.rules, s, country)
                if self.rules.units[t.definition_id]["kind"].lower() in effect["removeKinds"]
                and ("removeSpaces" not in effect or t.position_id in effect["removeSpaces"])
                and not unit_rule_matches(self.rules, s, t, "protection")]
            return self._request(s, f, "COUNTRY_LOSS", options,
                                 {"type": "COUNTRY_LOSS_SELECTED", "effect": effect}, op["operation_id"]), False
        if kind == "COUNTRY_LOSS_SELECTED":
            if op["choice"] == "discard":
                from .content_effects import instruction
                f.remaining_operations.append(instruction({"type": "discard-top", "target": "self", "count": 1}))
            else:
                f.remaining_operations.extend([
                    {"type": "CHECK_COUNTRY_LOSS", "effect": op["effect"], "token": op["choice"]},
                    {"type": "ELIMINATE", "target_token": op["choice"], "allow_friendly": True,
                     "actor_override": f.context.get("effect_actor", country)},
                ])
            return outcome("LOSS_SELECTED"), False
        if kind == "CHECK_COUNTRY_LOSS":
            if s.tokens[op["token"]].position_id in self.rules.regions:
                f.remaining_operations.append({"type": "COUNTRY_LOSS_STEP", "effect": op["effect"]})
            return outcome("LOSS_CHECKED"), False
        if kind == "ECONOMIC_PLAY":
            f.context["economic"] = {"cancelled": False, "bonus": 0, "target_adjustment": 0, "prevent_discard": False}
            return outcome(kind), True
        if kind == "ECONOMIC_TARGET":
            return outcome(kind, target_country=op["target_country"]), True
        if kind == "CARD_EFFECT":
            from .content_effects import execute_effect
            return execute_effect(self, s, f, op)
        if kind == "CARD_EFFECT_SELECTED":
            from .content_effects import execute_selection
            return execute_selection(self, s, f, op)
        if kind == "REQUEST_PAYMENT":
            from .content_effects import request_payment
            return request_payment(self, s, f, op)
        if kind == "PAYMENT_SELECTED":
            from .content_effects import select_payment
            return select_payment(self, s, f, op)
        if kind == "ORDER_DECK_SELECTED":
            f.remaining_operations.append({"type": "CARD_EFFECT", "effect": op["effect"],
                                           "selected": [*op["selected"], op["choice"]], "eligible": op["eligible"]})
            return outcome("ORDER_SELECTED"), False
        if kind == "BATTLE_ORDER_SELECTED":
            if op["choice"] != "confirm":
                f.remaining_operations.append({"type": "CARD_EFFECT", "effect": op["effect"],
                                               "selected": [*op["selected"], op["choice"]]})
                return outcome("BATTLE_ORDER_SELECTED"), False
            force = op["effect"].get("force", country)
            force = country if force == "self" else force
            operations = [{"type": "LAND_BATTLE" if op["effect"]["kind"] == "army" else "SEA_BATTLE",
                           "target": s.tokens[key].position_id, "expected_victim": key, "force": force}
                          for key in op["selected"]]
            f.remaining_operations.extend(reversed(operations))
            return outcome("BATTLE_ORDER_CONFIRMED"), False
        if kind == "RETURN_CARD_SELECTED":
            if op["choice"] == "done":
                return outcome("RETURN_DECLINED"), False
            from .content_effects import instruction
            f.remaining_operations.extend(reversed([
                instruction({"type": "discard-hand", "count": op["effect"]["discardHandCount"], "strict": True}),
                {"type": "RETURN_CURRENT_CARD"},
            ]))
            return outcome("RETURN_REQUESTED"), False
        if kind == "RETURN_CURRENT_CARD":
            f.context["settlement_destination"] = "HAND"
            return outcome("RETURN_CARD"), False
        if kind == "CHOOSE":
            continuation = self._resolve(op["operation"], f)
            options = self.legal.targets(s, country, continuation)
            return self._request(s, f, "TARGET", options, continuation, op["operation_id"]), False
        if kind in ("ACTIVATE_ABILITY", "ACTIVATE_SELECTED"):
            token = s.tokens[f.context["ability_token"]]
            ability = self.rules.abilities[token.definition_id]
            cost = self.executor.activation_cost(s, token)
            hand = [key for key in s.positions[zone(country, "HAND")] if key != f.source_card_id]
            if kind == "ACTIVATE_ABILITY" and ability["activation_mode"] == "OPTIONAL":
                first = ability["effects"][0] if ability["effects"] else None
                available = True
                if first and first["type"] == "CARD_EFFECT":
                    from .content_effects import can_begin
                    from .content_effects import event_effect
                    available = can_begin(self.rules, s, country, event_effect(first["effect"], f),
                                          {**f.context, "excluded_card": f.source_card_id})
                options = ["decline"] + (["use"] if len(hand) >= cost and available else [])
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
            f.context["source_origin"] = s.tokens[token.source_token_id].position_id
            sequence = [{"type": "SELECT_COST"} for _ in range(cost)]
            sequence += [{"type": "ABILITY_PAID"}] + deepcopy(ability["effects"])
            source = self.rules.cards.get(s.tokens[f.source_card_id].definition_id, {}) if f.source_card_id else {}
            if (source.get("legacy_kind") in ("status", "bolster") and
                    ("card_trigger" in ability["trigger"] or ability["trigger"]["operation_type"] == "USE_STATUS")):
                kind = source["legacy_kind"]
                sequence.insert(0, {"type": "CARD_DECLARED", "declared_kind": kind})
                sequence.append({"type": "CARD_EFFECT_FINISHED", "declared_kind": kind})
            f.remaining_operations.extend(reversed(sequence))
            return result, False
        if kind == "CARD_DECLARED":
            token = s.tokens[f.context["ability_token"]]
            source = s.tokens[token.source_token_id]
            result = outcome(kind)
            if op["declared_kind"] == "bolster" and source.position_id != "RESOLUTION":
                self.executor.transfer(s, source.token_id, "RESOLUTION", result, resolving_ability=token.token_id)
                result["context"]["revealed_source"] = source.token_id
            return result, True
        if kind == "CARD_EFFECT_FINISHED":
            return outcome(kind), True
        if kind == "SELECT_COST":
            selected = f.context["cost_tokens"]
            options = [t for t in s.positions[zone(country, "HAND")] if t not in selected and t != f.source_card_id]
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
            physical_source = (source.position_id == zone(country, "RESPONSE") or
                               (source.position_id == zone(country, "HAND") and
                                self.rules.cards[source.definition_id].get("legacy_kind") == "bolster"))
            if physical_source and token.position_id != source.position_id:
                self.executor.transfer(s, source.token_id, "RESOLUTION", result, resolving_ability=token.token_id)
                result["context"]["revealed_source"] = source.token_id
            else:
                self.executor.transfer(s, token.token_id, "RESOLUTION", result)
            return result, False
        if kind == "CHECK_SUPPLY":
            pieces = board_units(self.rules, s)
            supplied = SupplyResolver(self.rules).supplied_for_seat(s, country, pieces)
            ops = [{"type": "ELIMINATE", "target_token": t.token_id, "reason": "unsupplied"}
                   for t in pieces if self.rules.force_controllers[t.owner_country] == country and t.token_id not in supplied]
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
            return outcome(kind), True
        if kind == "REDEPLOY":
            force = op.get("force", country)
            if self.rules.force_controllers.get(force) != country:
                return outcome_failure(kind, "Acting seat does not control this force"), False
            if not f.eligible_token_ids:
                f.eligible_token_ids = [t.token_id for t in board_units(self.rules, s, force)
                                        if self.rules.units[t.definition_id]["kind"] == "ARMY"]
                f.context["redeploy_remaining"] = op.get("count", len(f.eligible_token_ids))
            choices = [t for t in f.eligible_token_ids if t not in f.processed_token_ids and s.tokens[t].position_id in self.rules.regions]
            if not choices or f.context["redeploy_remaining"] == 0:
                f.eligible_token_ids.clear()
                f.processed_token_ids.clear()
                f.context.pop("redeploy_remaining", None)
                return outcome("REDEPLOY_COMPLETE"), False
            return self._request(s, f, "REDEPLOY_UNIT", choices, {"type": "REDEPLOY_SELECTED"}, op["operation_id"]), False
        if kind in ("REDEPLOY_SELECTED", "REBUILD_UNIT"):
            token_id = op["token"]
            force = s.tokens[token_id].owner_country
            force_args = {"force": force} if force != country else {}
            restart = {"type": "REDEPLOY", **force_args}
            virtual = deepcopy(s)
            self.executor.transfer(virtual, token_id, zone(force, "RESERVE_ARMY"), outcome("INTERNAL"))
            targets = self.legal.targets(virtual, country, {"type": "BUILD_ARMY", **force_args})
            if kind == "REDEPLOY_SELECTED":
                if not targets:
                    f.processed_token_ids.append(token_id)
                    f.remaining_operations.append(restart)
                    return outcome_failure("REDEPLOY", "No rebuilding site; original unit retained"), False
                continuation = {"type": "REBUILD_UNIT", "token": token_id, **force_args}
                return self._request(s, f, "REDEPLOY_TARGET", targets, continuation, op["operation_id"]), False
            if op["target"] not in targets:
                return outcome_failure("REDEPLOY", "Rebuild invalid; original unit retained"), False
            result = outcome("BUILD_ARMY", redeployed=True)
            self.executor.transfer(s, token_id, op["target"], result)
            f.processed_token_ids.append(token_id)
            f.context["redeploy_remaining"] -= 1
            f.remaining_operations.append(restart)
            return result, True
        return self.executor.execute(s, op, f), True


ResolutionEngine = CommandProcessor
