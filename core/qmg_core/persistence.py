"""Private, versioned checkpoints and an append-only transactional JSONL journal."""
from copy import deepcopy
import json
import os
from pathlib import Path

from .model import CoreError, GameState, canonical, digest
from .rules import validate_state


class OperationLogger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._last = None
        self._size = None

    def append(self, record):
        data = (canonical(record) + "\n").encode("utf-8")
        with self.path.open("a+b") as stream:
            stream.seek(0, 2)
            offset = stream.tell()
            if self._size is not None and offset != self._size:
                raise CoreError("Journal changed outside its single writer")
            if offset:
                stream.seek(-1, 2)
                if stream.read(1) != b"\n":
                    raise CoreError("Refusing to append after an incomplete journal tail")
                if self._size is None:
                    stream.seek(0)
                    last_line = b""
                    for last_line in stream:
                        pass
                    try:
                        self._last = json.loads(last_line)
                    except ValueError as e:
                        raise CoreError("Invalid existing journal tail") from e
                    if digest({k: v for k, v in self._last.items() if k != "record_digest"}) != self._last.get("record_digest"):
                        raise CoreError("Existing journal tail checksum mismatch")
                stream.seek(0, 2)
            if self._last is not None and (record["previous_record"] != self._last["record_digest"] or record["sequence_id"] != self._last["sequence_id"] + 1 or record["game_id"] != self._last["game_id"] or record["rules_digest"] != self._last["rules_digest"]):
                raise CoreError("Refusing to mix journal chains, games or rulesets")
            try:
                if stream.write(data) != len(data):
                    raise OSError("Incomplete journal write")
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                stream.seek(offset)
                stream.truncate()
                stream.flush()
                raise
            self._last = {k: record[k] for k in ("record_digest", "sequence_id", "game_id", "rules_digest")}
            self._size = offset + len(data)


class SaveRepository:
    SCHEMA = 1

    @staticmethod
    def save(path, processor):
        payload = {"schema": SaveRepository.SCHEMA, "rules_digest": processor.rules.fingerprint,
                   "state": processor.state.to_dict(), "record_head": processor.record_head}
        envelope = {"payload": payload, "sha256": digest(payload)}
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(target.suffix + ".tmp")
        with temp.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(canonical(envelope) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)

    @staticmethod
    def recover(path, rules, journal=None, tolerate_truncated_tail=False):
        from .engine import CommandProcessor
        envelope = json.loads(Path(path).read_text(encoding="utf-8"))
        p = envelope["payload"]
        if digest(p) != envelope["sha256"] or p["schema"] != SaveRepository.SCHEMA or p["rules_digest"] != rules.fingerprint:
            raise CoreError("Snapshot checksum, schema or rules identity mismatch")
        state = GameState.from_dict(p["state"])
        validate_state(rules, state)
        processor = CommandProcessor(rules, state, record_head=p["record_head"])
        if journal is not None and Path(journal).exists():
            lines = Path(journal).read_bytes().splitlines(keepends=True)
            previous_sequence, previous_digest = None, None
            for i, line in enumerate(lines):
                if not line.endswith(b"\n") and i == len(lines) - 1:
                    if tolerate_truncated_tail:
                        break
                    raise CoreError("Incomplete journal tail")
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeDecodeError) as e:
                    raise CoreError("Corrupt journal record") from e
                body = {k: v for k, v in record.items() if k != "record_digest"}
                if digest(body) != record["record_digest"]:
                    raise CoreError("Journal checksum mismatch")
                if record["rules_digest"] != rules.fingerprint or record["game_id"] != state.game_id:
                    raise CoreError("Journal belongs to another game/ruleset")
                seq = record["sequence_id"]
                if previous_sequence is not None and seq != previous_sequence + 1:
                    raise CoreError("Duplicate, missing or reordered record")
                if previous_digest is not None and record["previous_record"] != previous_digest:
                    raise CoreError("Broken journal record chain")
                previous_sequence = seq
                previous_digest = record["record_digest"]
                if seq <= state.version:
                    if seq == state.version and (record["record_digest"] != processor.record_head or record["after_hash"] != digest(state.to_dict())):
                        raise CoreError("Snapshot and journal disagree")
                    processor.records.append(record)
                    continue
                if seq != state.version + 1 or record["previous_record"] != processor.record_head or record["before_hash"] != digest(state.to_dict()) or record["rules_digest"] != rules.fingerprint or record["game_id"] != state.game_id:
                    raise CoreError("Journal identity or state chain mismatch")
                data = state.to_dict()
                data.update(deepcopy(record["state_patch"]))
                next_state = GameState.from_dict(data)
                validate_state(rules, next_state)
                if next_state.version != seq or digest(next_state.to_dict()) != record["after_hash"]:
                    raise CoreError("Replay state mismatch")
                state = next_state
                processor._state = state
                processor.record_head = record["record_digest"]
                processor.records.append(record)
        return processor
