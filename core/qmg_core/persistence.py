"""Private, versioned checkpoints and an append-only transactional JSONL journal."""
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
            previous_sequence, previous_digest, previous_state_hash = None, None, None
            with Path(journal).open("rb") as stream:
                for line in stream:
                    if not line.endswith(b"\n"):
                        if tolerate_truncated_tail:
                            break
                        raise CoreError("Incomplete journal tail")
                    record = SaveRepository._read_record(line, rules, state.game_id)
                    sequence = record["sequence_id"]
                    if previous_sequence is not None and (
                        sequence != previous_sequence + 1
                        or record["previous_record"] != previous_digest
                        or record["before_hash"] != previous_state_hash
                    ):
                        raise CoreError("Duplicate, missing, reordered or disconnected journal record")
                    previous_sequence = sequence
                    previous_digest = record["record_digest"]
                    previous_state_hash = record["after_hash"]
                    if sequence == state.version:
                        if previous_digest != processor.record_head or previous_state_hash != digest(state.to_dict()):
                            raise CoreError("Snapshot and journal disagree")
                    elif sequence > state.version:
                        state = SaveRepository._apply_record(processor, state, record)
                    processor.records.append(record)
            if previous_sequence is not None and previous_sequence < state.version:
                raise CoreError("Journal ends before its checkpoint")
        return processor

    @staticmethod
    def _read_record(line, rules, game_id):
        try:
            record = json.loads(line)
            body = {key: value for key, value in record.items() if key != "record_digest"}
            if digest(body) != record["record_digest"]:
                raise CoreError("Journal checksum mismatch")
            sequence = record["sequence_id"]
            if type(sequence) is not int or sequence < 1 or record["before_version"] != sequence - 1 or record["after_version"] != sequence:
                raise CoreError("Invalid journal sequence or versions")
            if record["rules_digest"] != rules.fingerprint or record["game_id"] != game_id:
                raise CoreError("Journal belongs to another game/ruleset")
            return record
        except CoreError:
            raise
        except (ValueError, AttributeError, KeyError, TypeError) as e:
            raise CoreError("Corrupt journal record") from e

    @staticmethod
    def _apply_record(processor, state, record):
        sequence = record["sequence_id"]
        before = state.to_dict()
        if sequence != state.version + 1 or record["previous_record"] != processor.record_head or record["before_hash"] != digest(before):
            raise CoreError("Journal identity or state chain mismatch")
        before.update(record["state_patch"])
        next_state = GameState.from_dict(before)
        validate_state(processor.rules, next_state)
        if next_state.version != sequence or digest(next_state.to_dict()) != record["after_hash"]:
            raise CoreError("Replay state mismatch")
        processor._state = next_state
        processor.record_head = record["record_digest"]
        return next_state

    @staticmethod
    def resume(path, rules, journal):
        """Recover a verified prefix and continue writing to the same journal."""
        processor = SaveRepository.recover(path, rules, journal)
        processor.logger = OperationLogger(journal)
        processor.checkpoint_path = path
        processor.advance()
        return processor
