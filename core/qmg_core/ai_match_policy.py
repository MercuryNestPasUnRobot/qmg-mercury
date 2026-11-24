"""Seat-local rule policy and time-bounded, observation-only candidate search."""
import multiprocessing
from itertools import takewhile
from time import perf_counter

from .evaluation import OneStepPlanner, acceptable_play, candidate_key
from .definitions import BATTLES, CONSTRUCTION
from .hand_retention import is_hand_choice
from .model import CoreError, zone
from .status_scoring import is_status
from .reallocation_scoring import acquisitions, payment_plans, planned_options, price


ACTION_PRIORITY = {"GAIN_VP": 0.75, "BUILD_ARMY": 1.6, "RECRUIT": 1.6, "BUILD_NAVY": 1.4,
                   "LAND_BATTLE": 1.3, "SEA_BATTLE": 1.3, "DRAW": 0.45}
EFFECT_PRIORITY = {"choose-space": 1.55, "recruit": 1.55, "build": 1.55, "battle": 1.3,
                   "battle-series": 1.3, "choose-unit": 1.2, "air-action": 1.0,
                   "discard-top": 0.25, "play-from-zone": 0.6, "search-deck": 0.35}


def effect_priority(effect, team, economy=None, seat=None, *, closeout=False, frontier=False):
    if economy:
        dynamic = economy.effect(seat, effect, ACTION_PRIORITY["GAIN_VP"], closeout=closeout)
        if dynamic is not None:
            return dynamic
    if effect["type"] == "points":
        return effect["amount"] * ACTION_PRIORITY["GAIN_VP"] * (1 if effect["side"] in ("self", team) else -1)
    if effect["type"] == "choose-one":
        chooser = effect.get("chooser", seat)
        friendly_choice = frontier and economy.rules.team(chooser) == team
        acting_seat = chooser if friendly_choice else seat
        return max((sum(effect_priority(e, team, economy, acting_seat, closeout=closeout, frontier=friendly_choice and i == 0)
                        for i, e in enumerate(option["effects"]))
                    for option in effect["options"]), default=0)
    value = EFFECT_PRIORITY.get(effect["type"], 0.2)
    if frontier and effect["type"] in ("choose-space", "recruit", "build", "battle"):
        value += economy.routes.best_action_bonus(seat, {"type": "CARD_EFFECT", "effect": effect})
    return value


def paper_play_choices(ranked, mandatory):
    if not mandatory:
        return ranked
    normal = ("PLAY_CARD", "USE_STATUS")
    best = next((r for r in ranked if r["command"]["type"] in normal), None)
    if best is None:
        return ranked
    return [r for r in ranked if r["command"]["type"] == "REALLOCATE" or
            (r["command"]["type"] in normal and r["rule_score"] == best["rule_score"])]


class RulePolicy:
    """One policy serves as both the comparator and the live fall-back."""
    def __init__(self, rules, evaluator=None):
        self.rules = rules
        self.evaluator = evaluator
        self.sites = {}
        for site in rules.sites.values():
            region = site["region"]
            self.sites[region] = self.sites.get(region, 0) + site["vp"]

    def instructions(self, steps, seat, economy, *, closeout=False):
        team = self.rules.team(seat)
        value = 0
        frontier = bool(closeout and economy and economy.map_weights)
        for step in steps:
            op = step.get("operation", step)
            if op["type"] == "CARD_EFFECT":
                value += effect_priority(op["effect"], team, economy, seat, closeout=closeout, frontier=frontier)
            elif op["type"] == "GAIN_VP" and economy:
                recipient = op.get("score_country") or seat
                delta = op["amount"] if self.rules.team(recipient) == team else -op["amount"]
                value += economy.points(seat, delta, ACTION_PRIORITY["GAIN_VP"], closeout=closeout)
                frontier = frontier and not economy.closes_game(seat, delta)
                continue
            else:
                value += ACTION_PRIORITY.get(op["type"], 0.25) * op.get("amount", 1)
                if frontier and op["type"] in CONSTRUCTION | BATTLES:
                    value += economy.routes.best_action_bonus(seat, op)
            frontier = False  # Later military targets may depend on the outcome of this instruction.
        return value

    def card_value(self, card, seat, economy, *, active=False, closeout=False):
        abilities = (self.rules.abilities[a]["effects"] for a in card.get("abilities", []))
        if economy and economy.status_weights and is_status(card):
            if active:
                return economy.statuses.program(seat, seat, card["active_ability"], None, committed=True)
            return (self.instructions(card["effects"], seat, economy, closeout=closeout) +
                    economy.statuses.report(seat, card["id"], held=not closeout)["future"])
        if active:
            return self.instructions(self.rules.abilities[card["active_ability"]]["effects"], seat, economy, closeout=closeout)
        return (self.instructions(card["effects"], seat, economy, closeout=closeout) +
                0.35 * sum(self.instructions(e, seat, economy) for e in abilities))

    def discard_value(self, card, seat, economy, view):
        cost = self.evaluator.basic_discard_cost(view, seat, card) if self.evaluator else 0
        return -max(0, self.card_value(card, seat, economy)) - cost

    def play_card_value(self, card, seat, economy):
        slot_cost = self.evaluator.hand_slot_cost if self.evaluator else 0
        return self.card_value(card, seat, economy, closeout=True) - slot_cost

    def reallocation_plan(self, view, seat, economy):
        plans = payment_plans(self.rules, view, seat, lambda card: self.discard_value(card, seat, economy, view))
        return max((price(plan, self.play_card_value(self.rules.cards[plan["definition"]], seat, economy))
                    for plan in plans),
                   key=lambda plan: plan["total"], default=None)

    def choice_value(self, view, command, region_scores, economy):
        option = command["option"]
        seat = command["actor"]
        decision = view["decision"]
        kind, op = decision["decision_type"], decision["operation_type"]
        sources = decision.get("response_sources", {})
        if sources and economy and economy.status_weights:
            key = decision.get("response_abilities", {}).get(option)
            source = view["tokens"].get(sources.get(option))
            if key and source and is_status(self.rules.cards.get(source["definition_id"], {})):
                return economy.statuses.program(seat, decision.get("event_actor") or seat,
                                                key, decision.get("event_region"), committed=True)
            if option in ("decline", "pass") and all(is_status(self.rules.cards.get(
                    view["tokens"].get(src, {}).get("definition_id"), {})) for src in sources.values()):
                return 0
        if option in ("skip", "decline", "pass", "no", "done", "cancel"):
            return -100
        if kind in ("ABILITY", "RESPONSE_ORDER"):
            source = view["tokens"].get(decision.get("response_sources", {}).get(option))
            return 1 + self.card_value(self.rules.cards[source["definition_id"]], seat, economy) if source else 1
        if economy and "deck_discard_count" in decision and option in self.rules.countries:
            return economy.mill(seat, option, decision["deck_discard_count"], ACTION_PRIORITY["GAIN_VP"], closeout=True)
        if option in ("confirm", "air", "discard-top"):
            return 100
        if token := view["tokens"].get(option):
            if token["token_type"] == "CARD":
                value = self.card_value(self.rules.cards[token["definition_id"]], seat, economy)
                cost = decision.get("hand_discard") or op in ("DISCARD", "COST_SELECTED") or any(
                    word in kind for word in ("COST", "DISCARD", "PAY", "REALLOCATE"))
                return (self.discard_value(self.rules.cards[token["definition_id"]], seat, economy, view)
                        if cost or view["turn"]["phase"] in ("Setup", "Discard") else value)
            if token["token_type"] == "UNIT":
                return self.sites.get(token["position_id"], 0) * (1 if kind == "BATTLE_VICTIM" else -1)
        if option in region_scores:
            return region_scores[option]
        if economy and economy.map_weights and kind == "EXTRA_CARD_MODE" and option in BATTLES | CONSTRUCTION:
            return self.instructions([{"type": option}], seat, economy, closeout=True)
        if economy and economy.map_weights and option in decision.get("effect_branches", {}):
            return self.instructions([{"type": "CARD_EFFECT", "effect": effect}
                                      for effect in decision["effect_branches"][option]], seat, economy, closeout=True)
        if option in self.rules.regions:
            site_value = self.sites.get(option, 0)
            occupants = view["positions"][option].get("tokens", [])
            return site_value + 0.25 * bool(occupants)
        return ACTION_PRIORITY.get(option, 0)

    def rank(self, view, commands):
        if not commands:
            return []
        seat = commands[0]["actor"]
        phase = view["turn"]["phase"]
        economy = self.evaluator.economic_from_view(view) if self.evaluator else None
        slot_cost = self.evaluator.hand_slot_cost if self.evaluator else 0
        forced_play_discard = phase == "Play" and any(c["type"] == "DISCARD_CARD" for c in commands)
        retention = (self.evaluator.retention(view, seat)
                     if slot_cost and is_hand_choice(view, seat, forced_play_discard=forced_play_discard) else {})
        decision = view.get("decision") or {}
        options = [c["option"] for c in commands if c["type"] == "CHOOSE"]
        if economy and economy.map_weights and decision.get("map_force"):
            regions = {option: option.split("|", 1)[-1] for option in options}
            region_scores = {option: economy.routes.target_value(decision["map_force"], region,
                             battle=decision["map_battle"], construction=not decision["map_battle"])
                             for option, region in regions.items() if region in self.rules.regions}
        else:
            regions = {option for option in options if option in self.rules.regions}
            region_scores = self.evaluator.target_proximity(view, seat, regions) if self.evaluator and regions else {}
        hand = view["positions"][zone(seat, "HAND")]["count"]
        slots = max(0, self.rules.settings["hand_limit"] - hand)
        replacement_available = view["positions"][zone(seat, "DECK")]["count"] > slots
        info = view.get("reallocation", {})
        plan = (self.reallocation_plan(view, seat, economy)
                if "payment" in info or any(c["type"] == "REALLOCATE" for c in commands) else None)
        source_values = {}
        if "sources" in info:
            for source, definition, _ in acquisitions(self.rules, view, seat):
                value = self.play_card_value(self.rules.cards[definition], seat, economy)
                source_values[source] = max(value, source_values.get(source, float("-inf")))

        def score(command):
            kind = command["type"]
            if kind == "CHOOSE":
                option = command["option"]
                if option in retention:
                    return -retention[option]["net"]
                if option in info.get("targets", {}):
                    return self.play_card_value(self.rules.cards[info["targets"][option]], seat, economy)
                if option in info.get("sources", {}):
                    return source_values.get(info["sources"][option], -10)
                return self.choice_value(view, command, region_scores, economy)
            if kind == "END_PHASE":
                if slot_cost and phase in ("Play", "Air", "Discard"):
                    return 0
                return -1
            if kind == "DISCARD_CARD":
                net = retention.get(command["card"], {}).get("net", 0)
                if forced_play_discard:
                    return -net if retention else self.discard_value(
                        self.rules.cards[view["tokens"][command["card"]]["definition_id"]], seat, economy, view)
                return -net if net < 0 and replacement_available else -1
            if kind == "FORFEIT_PLAY":
                return -1
            if kind == "REALLOCATE":
                return plan["total"] if plan else -2
            if kind == "ACTIVATE_CARD":
                return -101  # Choose within the open window, or advance the phase.
            if kind == "USE_AIR":
                return 1 - slot_cost
            token = view["tokens"][command["card"]]
            card = self.rules.cards[token["definition_id"]]
            if mode := command.get("play_as"):
                return self.instructions([{"type": mode}], seat, economy, closeout=True) - slot_cost
            return (self.play_card_value(card, seat, economy) if kind == "PLAY_CARD" else
                    self.card_value(card, seat, economy, active=kind == "USE_STATUS", closeout=True))
        rows = [{"command": command, "rule_score": score(command)} for command in commands]
        for row in rows:
            command = row["command"]
            if plan and command["type"] == "REALLOCATE":
                row["reallocation"] = plan
            key = command.get("option", command.get("card"))
            if key in retention and command["type"] in ("DISCARD_CARD", "CHOOSE"):
                row["retention"] = {**retention[key], "replacement_available": replacement_available,
                                    **({"forced": True} if forced_play_discard else {})}
            if slot_cost and command["type"] in ("PLAY_CARD", "USE_AIR"):
                row["initial_card_score"] = -slot_cost
            if economy and economy.status_weights and command["type"] == "PLAY_CARD":
                card = self.rules.cards[view["tokens"][command["card"]]["definition_id"]]
                if is_status(card) and not command.get("play_as"):
                    row["status_forecast"] = economy.statuses.report(seat, card["id"])
        # Canonical tie-breaks also apply to own permitted choices out of the deck.
        def order(row):
            command = row["command"]
            planned = plan and command.get("option") in plan["payment"]
            return (not planned, -row["rule_score"], sorted(command.items()))
        return sorted(rows, key=order)


class CandidateView:
    def __init__(self, view, commands):
        self.view = view
        self.commands = commands

    def observe(self):
        return self.view

    def legal_commands(self):
        return self.commands


def _search_worker(connection, data, weights):
    from .definitions import RuleSet
    with connection:
        planner = OneStepPlanner(RuleSet(data, projected=True), weights)
        connection.send(("ready", None))
        while True:
            try:
                view, commands, country = connection.recv()
            except EOFError:
                return
            try:
                for row in planner.iter_rank(CandidateView(view, commands), country):
                    connection.send(("candidate", row))
            except CoreError as exc:
                connection.send(("error", type(exc).__name__))
            else:
                connection.send(("done", None))


class DeadlineSearch:
    def __init__(self, rules, weights):
        # The worker cannot obtain the original card instances, journal, RNG or any live state.
        self.data = {**rules.data, "initial_tokens": []}
        self.weights = weights
        self.process = self.connection = None
        self.retired = []
        self.context = multiprocessing.get_context("spawn")
        self.ready = False

    def close(self):
        self._retire()
        for process in self.retired:
            process.join()
            process.close()
        self.retired.clear()

    def _retire(self):
        if self.connection is not None:
            self.connection.close()
        if self.process is not None:
            self.process.terminate()
            self.retired.append(self.process)
        self.process = self.connection = None
        self.ready = False

    def _receive(self, deadline):
        remaining = deadline - perf_counter()
        if remaining <= 0 or not self.connection.poll(remaining):
            raise TimeoutError
        message = self.connection.recv()
        if perf_counter() > deadline:
            raise TimeoutError
        return message

    def run(self, view, commands, country, deadline):
        rows = []
        try:
            if perf_counter() >= deadline:
                return rows, "deadline"
            if self.process is None:
                for process in self.retired[:]:
                    if not process.is_alive():
                        process.close()
                        self.retired.remove(process)
                self.connection, worker = self.context.Pipe()
                with worker:
                    process = self.context.Process(target=_search_worker, args=(worker, self.data, self.weights),
                                                   daemon=True)
                    process.start()
                    self.process = process
            if not self.ready:
                if self._receive(deadline)[0] != "ready":
                    raise RuntimeError("Unexpected search startup message")
                self.ready = True
            # Run labels can encode the experiment's seed. The projection needs neither them nor a journal.
            self.connection.send(({**view, "game_id": "ai-projection", "history": []}, commands, country))
            while True:
                status, payload = self._receive(deadline)
                if status == "candidate":
                    rows.append(payload)
                elif status == "done":
                    return rows, "finished"
                else:
                    return rows, "simulation-" + payload
        except TimeoutError:
            self._retire()
            return rows, "deadline"
        except (OSError, EOFError):
            self._retire()
            return rows, "worker-exited"


class SeatPolicy:
    def __init__(self, rules, *, search=None, evaluator=None, width=3, budget_ms=1500):
        self.rule = RulePolicy(rules, evaluator)
        self.search = search
        self.width = width
        self.budget_ms = budget_ms
        self.plan = []
        self.exchange = None

    def planned_choice(self, view, commands):
        decision = view["decision"]
        if not self.plan or not decision:
            return None
        planned, signature, score = self.plan.pop(0)
        actual = (decision["decision_type"], decision["operation_type"], sorted(decision["legal_options"]))
        # Simulated decision IDs are from a different state. Bind only to the equivalent live window.
        command = {**planned, "decision_id": decision["decision_id"]}
        if signature == actual and command in commands:
            return command, score
        self.plan.clear()
        return None

    def decide(self, client, country):
        started = perf_counter()
        view = client.observe(include_history=False)
        commands = client.legal_commands()
        if not commands or any(c["actor"] != country for c in commands):
            raise CoreError("No legal action for the AI seat")
        ranked = self.rule.rank(view, commands)
        phase = view["turn"]["phase"]
        mandatory = bool(phase == "Play" and self.rule.rules.settings.get("mandatory_turn_play"))
        preferred = paper_play_choices(ranked, mandatory)
        chosen = preferred[0]["command"]
        mode, status, tried, rows, selected = "rules", "rules-only", [], [], None
        if len(commands) == 1:
            mode, status = "forced", "only-legal-command"
        forced_play_discard = mandatory and any(c["type"] == "DISCARD_CARD" for c in commands)
        if (self.rule.evaluator and self.rule.evaluator.hand_slot_cost and
                is_hand_choice(view, country, forced_play_discard=forced_play_discard)):
            status = "hand-retention"
        elif self.search:
            if self.exchange and (not view["decision"] or self.exchange[0] != view["turn"]["turn_id"]):
                self.exchange = None
            info = view.get("reallocation", {})
            options = planned_options(self.rule.rules, info, self.exchange[1]["reallocation"]) if self.exchange else []
            if "payment" in info and not set(options) <= {c.get("option") for c in commands}:
                options = []
            if planned_exchange := next((c for c in commands if c.get("option") in options), None):
                chosen, selected = planned_exchange, self.exchange[1]
                mode, status = "plan", "matched-exchange"
            elif planned := self.planned_choice(view, commands):
                chosen, selected = planned
                mode, status = "plan", "matched-continuation"
            elif view["decision"]:
                status = "pending-choice"
            else:
                self.plan = []
                eligible = [r["command"] for r in preferred
                            if r["command"]["type"] != "ACTIVATE_CARD" and
                            (r["rule_score"] >= -1 or r["command"]["type"] == "REALLOCATE" or
                             (mandatory and r["command"]["type"] in ("PLAY_CARD", "USE_STATUS")))]
                if len(eligible) > 1 or (mandatory and any(c["type"] in ("PLAY_CARD", "USE_STATUS") for c in eligible)):
                    tried = eligible[:self.width]
                    exchange = next((c for c in eligible if c["type"] == "REALLOCATE"), None)
                    if exchange and exchange not in tried and self.width > 1:
                        tried.insert(1, exchange)
                        del tried[self.width:]
                    end = next((c for c in eligible if c["type"] == "END_PHASE"), None)
                    if end is not None and end not in tried:
                        tried.append(end)
                    # Reserve a small part of the total budget for choosing and recording the result.
                    reserve_ms = min(10, self.budget_ms * 0.05)
                    rows, status = self.search.run(view, tried, country, started + (self.budget_ms - reserve_ms) / 1000)
                    completed = [row for row in rows if row["complete"]]
                    rejected = [row["command"] for row in completed if not acceptable_play(row, mandatory=mandatory)]
                    selected = max((row for row in completed if acceptable_play(row, mandatory=mandatory)),
                                   key=candidate_key, default=None)
                    if selected is None and chosen in rejected:
                        chosen = next(row["command"] for row in preferred if row["command"] not in rejected)
                    if selected:
                        mode, chosen = "search", selected["command"]
                        if selected.get("reallocation"):
                            self.exchange = (view["turn"]["turn_id"], selected)
                        steps = zip(selected["continuations"], selected["choice_signatures"])
                        self.plan = [(c, signature, selected) for c, signature in takewhile(
                            lambda step: step[0]["type"] == "CHOOSE" and step[0]["actor"] == country, steps)]
                elif len(commands) > 1:
                    status = "no-comparable-commands"
        # Projected opposing choices are internal to the worker result and never become commands.
        logged_rows = [{k: v for k, v in row.items() if k not in ("continuations", "choice_signatures")}
                       for row in rows]
        for command in tried:
            if not any(row["command"] == command for row in rows):
                logged_rows.append({"command": command, "complete": False, "reason": status,
                                    "q": None, "nodes": 0})
        return chosen, {"version": view["version"], "round": view["turn"]["round_id"],
                        "phase": view["turn"]["phase"], "country": country,
                        "budget_ms": self.budget_ms, "elapsed_ms": (perf_counter() - started) * 1000,
                        "mode": mode, "search_status": status, "command": chosen,
                        "rule_scores": ranked, "candidate_scores": logged_rows,
                        "selected_q": selected["q"] if selected else None}
