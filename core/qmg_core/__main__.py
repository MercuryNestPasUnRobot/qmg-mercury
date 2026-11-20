"""Small headless integration demonstration; deliberately not a full QMG game."""
from dataclasses import asdict
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from . import CoreAPI, CommandProcessor, OneStepPlanner, OperationLogger, RuleSet, SaveRepository, ScoreEvaluator


def main():
    data = Path(__file__).resolve().parent.parent / "data"
    rules = RuleSet.load(data / "architecture_fixture.json")
    weights = json.loads((data / "evaluation_weights.json").read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="qmg-core-") as directory:
        root = Path(directory)
        processor = CommandProcessor.create(rules, seed=1, logger=OperationLogger(root / "operations.jsonl"))
        SaveRepository.save(root / "initial.json", processor)
        gui = CoreAPI(processor, ("germany",))
        gui.submit(next(c for c in gui.legal_commands() if c.get("card") == "g-chain"))
        waiting = gui.observe()["decision"]
        SaveRepository.save(root / "waiting.json", processor)
        gui.submit(next(c for c in gui.legal_commands() if c["option"] == "B"))
        recovered = SaveRepository.recover(root / "initial.json", rules, root / "operations.jsonl")
        fresh = CoreAPI(CommandProcessor.create(rules), ("germany",))
        ranked = OneStepPlanner(rules, weights).rank(fresh, "germany")
        print(json.dumps({"fixture_only": True, "waiting_decision": waiting,
                          "completed_scores": gui.observe()["scores"], "operation_records": len(processor.records),
                          "replay_matches": recovered.state.to_dict() == processor.state.to_dict(),
                          "position_value": asdict(ScoreEvaluator(rules, weights).evaluate(fresh, "germany")),
                          "ai_best_candidate": ranked[0]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
