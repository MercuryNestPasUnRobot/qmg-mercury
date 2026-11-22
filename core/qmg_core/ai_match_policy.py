"""Seat-local rule policy and time-bounded, observation-only candidate search."""
import multiprocessing
from itertools import takewhile
from time import perf_counter

from .evaluation import OneStepPlanner, candidate_key
from .model import CoreError


ACTION_PRIORITY = {"GAIN_VP": 2.0, "BUILD_ARMY": 1.6, "RECRUIT": 1.6, "BUILD_NAVY": 1.4,
                   "LAND_BATTLE": 1.3, "SEA_BATTLE": 1.3, "DRAW": 0.45}
EFFECT_PRIORITY = {"choose-space": 1.55, "recruit": 1.55, "build": 1.55, "battle": 1.3,
                   "battle-series": 1.3, "choose-unit": 1.2, "air-action": 1.0,
                   "discard-top": 0.65, "play-from-zone": 0.6, "search-deck": 0.35}


def effect_priority(effect, team):
    if effect["type"] == "points":
        return effect["amount"] * (2 if effect["side"] in ("self", team) else -2)
    if effect["type"] == "choose-one":
        return max((sum(effect_priority(e, team) for e in option["effects"])
                    for option in effect["options"]), default=0)
    return EFFECT_PRIORITY.get(effect["type"], 0.2)


class RulePolicy:
    """One policy serves as both the comparator and the live fall-back."""
    def __init__(self, rules):
        self.rules = rules
        self.sites = {}
        for site in rules.sites.values():
            region = site["region"]
            self.sites[region] = self.sites.get(region, 0) + site["vp"]

    def instructions(self, steps, team):
        value = 0
        for step in steps:
            op = step.get("operation", step)
            if op["type"] == "CARD_EFFECT":
                value += effect_priority(op["effect"], team)
            else:
                value += ACTION_PRIORITY.get(op["type"], 0.25) * op.get("amount", 1)
        return value

    def card_value(self, card, seat, *, active=False):
        abilities = (self.rules.abilities[a]["effects"] for a in card.get("abilities", []))
        team = self.rules.team(seat)
        if active:
            return self.instructions(self.rules.abilities[card["active_ability"]]["effects"], team)
        return self.instructions(card["effects"], team) + 0.35 * sum(self.instructions(e, team) for e in abilities)

    def choice_value(self, view, command):
        option = command["option"]
        decision = view["decision"]
        kind, op = decision["decision_type"], decision["operation_type"]
        if option in ("skip", "decline", "pass", "no", "done", "cancel"):
            return -100
        if kind in ("ABILITY", "RESPONSE_ORDER"):
            source = view["tokens"].get(decision.get("response_sources", {}).get(option))
            return 1 + self.card_value(self.rules.cards[source["definition_id"]], command["actor"]) if source else 1
        if option in ("confirm", "air", "discard-top"):
            return 100
        if token := view["tokens"].get(option):
            if token["token_type"] == "CARD":
                value = self.card_value(self.rules.cards[token["definition_id"]], command["actor"])
                cost = op in ("DISCARD", "COST_SELECTED") or any(
                    word in kind for word in ("COST", "DISCARD", "PAY", "REALLOCATE"))
                return -value if cost or view["turn"]["phase"] in ("Setup", "Discard") else value
            if token["token_type"] == "UNIT":
                return self.sites.get(token["position_id"], 0) * (1 if kind == "BATTLE_VICTIM" else -1)
        if option in self.rules.regions:
            site_value = self.sites.get(option, 0)
            occupants = view["positions"][option].get("tokens", [])
            return site_value + 0.25 * bool(occupants)
        return ACTION_PRIORITY.get(option, 0)

    def rank(self, view, commands):
        normal_play = any(c["type"] in ("PLAY_CARD", "USE_STATUS") for c in commands)
        def score(command):
            kind = command["type"]
            if kind == "CHOOSE":
                return self.choice_value(view, command)
            if kind == "END_PHASE":
                return -1
            if kind == "REALLOCATE":
                return 0.1 if not normal_play and view["turn"]["plays_remaining"] else -2
            if kind == "ACTIVATE_CARD":
                return -101  # Choose within the open window, or advance the phase.
            if kind == "USE_AIR":
                return 1
            if mode := command.get("play_as"):
                return ACTION_PRIORITY.get(mode, 0.4)
            token = view["tokens"][command["card"]]
            return self.card_value(self.rules.cards[token["definition_id"]], command["actor"],
                                   active=kind == "USE_STATUS")
        rows = [{"command": command, "rule_score": score(command)} for command in commands]
        # Canonical tie-breaks also apply to own permitted choices out of the deck.
        return sorted(rows, key=lambda r: (-r["rule_score"], sorted(r["command"].items())))


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
    def __init__(self, rules, *, search=None, width=3, budget_ms=1500):
        self.rule = RulePolicy(rules)
        self.search = search
        self.width = width
        self.budget_ms = budget_ms
        self.plan = []

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
        chosen = ranked[0]["command"]
        mode, status, tried, rows, selected = "rules", "rules-only", [], [], None
        if len(commands) == 1:
            mode, status = "forced", "only-legal-command"
        if self.search:
            if planned := self.planned_choice(view, commands):
                chosen, selected = planned
                mode, status = "plan", "matched-continuation"
            elif view["decision"]:
                status = "pending-choice"
            else:
                self.plan = []
                eligible = [r["command"] for r in ranked
                            if r["command"]["type"] != "ACTIVATE_CARD" and r["rule_score"] >= -1]
                if len(eligible) > 1:
                    tried = eligible[:self.width]
                    end = next((c for c in eligible if c["type"] == "END_PHASE"), None)
                    if end is not None and end not in tried:
                        tried.append(end)
                    # Reserve a small part of the total budget for choosing and recording the result.
                    reserve_ms = min(10, self.budget_ms * 0.05)
                    rows, status = self.search.run(view, tried, country, started + (self.budget_ms - reserve_ms) / 1000)
                    selected = max((row for row in rows if row["complete"]), key=candidate_key, default=None)
                    if selected:
                        mode, chosen = "search", selected["command"]
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
