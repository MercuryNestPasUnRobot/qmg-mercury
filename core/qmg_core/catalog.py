"""Read-only SQLite catalog compiled from versioned, declarative rule data."""
from argparse import ArgumentParser
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from .definitions import RuleSet
from .model import CoreError, canonical
from .rules import initialize


APPLICATION_ID = 0x514D4743
SCHEMA_VERSION = 1
DEFINITION_GROUPS = ("countries", "regions", "connections", "units", "score_sites")
SCHEMA = """
CREATE TABLE catalog (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    ruleset_version TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    settings_json TEXT NOT NULL
);
CREATE TABLE definitions (
    kind TEXT NOT NULL CHECK (kind IN ('countries', 'regions', 'connections', 'units', 'score_sites')),
    id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    attributes TEXT NOT NULL,
    PRIMARY KEY (kind, id),
    UNIQUE (kind, ordinal)
);
CREATE TABLE cards (
    id TEXT PRIMARY KEY NOT NULL,
    ordinal INTEGER UNIQUE NOT NULL CHECK (ordinal >= 0),
    country TEXT NOT NULL,
    card_type TEXT NOT NULL CHECK (card_type IN ('EVENT', 'STATUS', 'RESPONSE')),
    attributes TEXT NOT NULL
);
CREATE INDEX cards_by_country_type ON cards(country, card_type);
CREATE TABLE abilities (
    id TEXT PRIMARY KEY NOT NULL,
    ordinal INTEGER UNIQUE NOT NULL CHECK (ordinal >= 0),
    attributes TEXT NOT NULL
);
CREATE TABLE card_abilities (
    card_id TEXT NOT NULL REFERENCES cards(id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    ability_id TEXT NOT NULL,
    PRIMARY KEY (card_id, ordinal)
);
CREATE TABLE card_effects (
    owner_id TEXT NOT NULL REFERENCES cards(id),
    stage TEXT NOT NULL CHECK (stage IN ('effects', 'on_settle')),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    operation TEXT NOT NULL,
    parameters TEXT NOT NULL,
    PRIMARY KEY (owner_id, stage, ordinal)
);
CREATE TABLE ability_effects (
    owner_id TEXT NOT NULL REFERENCES abilities(id),
    stage TEXT NOT NULL CHECK (stage IN ('effects', 'on_settle')),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    operation TEXT NOT NULL,
    parameters TEXT NOT NULL,
    PRIMARY KEY (owner_id, stage, ordinal)
);
CREATE TABLE initial_tokens (
    token_id TEXT PRIMARY KEY NOT NULL,
    ordinal INTEGER UNIQUE NOT NULL CHECK (ordinal >= 0),
    attributes TEXT NOT NULL
);
"""


def _fields(record, *omitted):
    return {key: value for key, value in record.items() if key not in omitted}


def _attributes(definition, *columns, sequences):
    result = _fields(definition, *columns)
    # Preserve the distinction between an omitted sequence and an explicit empty one.
    for key in sequences:
        if key in result:
            result[key] = []
    return canonical(result)


def _write_effects(db, table, definitions):
    db.executemany(
        f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?)",
        ((definition["id"], stage, ordinal, op["type"], canonical(_fields(op, "type")))
         for definition in definitions for stage in ("effects", "on_settle")
         for ordinal, op in enumerate(definition.get(stage, ()))),
    )


def _write_catalog(db, rules):
    data = rules.data
    db.execute("PRAGMA foreign_keys = ON")
    db.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    db.executescript(SCHEMA)
    with db:
        stored_fields = (*DEFINITION_GROUPS, "cards", "abilities", "initial_tokens", "ruleset_version")
        db.execute("INSERT INTO catalog VALUES (1, ?, ?, ?)",
                   (rules.version, rules.fingerprint, canonical(_fields(data, *stored_fields))))
        db.executemany(
            "INSERT INTO definitions VALUES (?, ?, ?, ?)",
            ((kind, row["id"], i, canonical(_fields(row, "id")))
             for kind in DEFINITION_GROUPS for i, row in enumerate(data[kind])),
        )
        db.executemany(
            "INSERT INTO cards VALUES (?, ?, ?, ?, ?)",
            ((row["id"], i, row["country"], row["type"],
              _attributes(row, "id", "country", "type", sequences=("effects", "on_settle", "abilities")))
             for i, row in enumerate(data["cards"])),
        )
        db.executemany("INSERT INTO abilities VALUES (?, ?, ?)",
                       ((row["id"], i, _attributes(row, "id", sequences=("effects", "on_settle")))
                        for i, row in enumerate(data["abilities"])))
        # RuleSet validates linked abilities after expanding legacy reset declarations.
        db.executemany(
            "INSERT INTO card_abilities VALUES (?, ?, ?)",
            ((row["id"], i, ability) for row in data["cards"] for i, ability in enumerate(row.get("abilities", ()))),
        )
        _write_effects(db, "card_effects", data["cards"])
        _write_effects(db, "ability_effects", data["abilities"])
        db.executemany(
            "INSERT INTO initial_tokens VALUES (?, ?, ?)",
            ((row["token_id"], i, canonical(_fields(row, "token_id"))) for i, row in enumerate(data["initial_tokens"])),
        )


def _read_effects(db, table, definitions):
    for owner, stage, operation, parameters in db.execute(
        f"SELECT owner_id, stage, operation, parameters FROM {table} ORDER BY owner_id, stage, ordinal"
    ):
        definitions[owner][stage].append({**json.loads(parameters), "type": operation})


class ContentCatalog:
    @staticmethod
    def build(rules, path):
        """Publish a validated catalog atomically and preserve any existing destination."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".catalog-", dir=target.parent) as directory:
            staging = Path(directory) / "content.sqlite3"
            with closing(sqlite3.connect(staging)) as db:
                _write_catalog(db, rules)
            checked = ContentCatalog.load(staging)
            if checked.fingerprint != rules.fingerprint:
                raise CoreError("Catalog did not preserve the source rules")
            if os.name == "nt":
                # Windows rename atomically fails if the destination already exists.
                staging.rename(target)
            else:
                os.link(staging, target)
        return target

    @staticmethod
    def load(path):
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        try:
            with closing(sqlite3.connect(uri, uri=True)) as db:
                db.execute("PRAGMA query_only = ON")
                db.execute("BEGIN")
                if (db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
                        or db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION):
                    raise CoreError("Unsupported rules catalog format")
                if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise CoreError("Catalog contains broken data references")
                version, fingerprint, metadata = db.execute(
                    "SELECT ruleset_version, fingerprint, settings_json FROM catalog WHERE singleton = 1"
                ).fetchone()
                data = {**json.loads(metadata), "ruleset_version": version, **{kind: [] for kind in DEFINITION_GROUPS}}
                for kind, key, body in db.execute("SELECT kind, id, attributes FROM definitions ORDER BY kind, ordinal"):
                    data[kind].append({**json.loads(body), "id": key})
                cards = {key: {**json.loads(body), "id": key, "country": country, "type": card_type}
                         for key, country, card_type, body in db.execute(
                             "SELECT id, country, card_type, attributes FROM cards ORDER BY ordinal")}
                abilities = {key: {**json.loads(body), "id": key} for key, body in db.execute(
                    "SELECT id, attributes FROM abilities ORDER BY ordinal")}
                for card, ability in db.execute("SELECT card_id, ability_id FROM card_abilities ORDER BY card_id, ordinal"):
                    cards[card]["abilities"].append(ability)
                _read_effects(db, "card_effects", cards)
                _read_effects(db, "ability_effects", abilities)
                data["cards"], data["abilities"] = list(cards.values()), list(abilities.values())
                data["initial_tokens"] = [{**json.loads(body), "token_id": key} for key, body in db.execute(
                    "SELECT token_id, attributes FROM initial_tokens ORDER BY ordinal")]
            rules = RuleSet(data)
            if rules.fingerprint != fingerprint:
                raise CoreError("Catalog content fingerprint mismatch")
            initialize(rules)
            return rules
        except CoreError:
            raise
        except (sqlite3.Error, KeyError, TypeError, ValueError, AttributeError) as error:
            raise CoreError(f"Could not load rules catalog {path}: {error}") from error

    @staticmethod
    def cached(source, directory):
        rules = RuleSet.load(source)
        target = Path(directory) / f"{rules.fingerprint}.sqlite3"
        if not target.is_file():
            try:
                ContentCatalog.build(rules, target)
            except FileExistsError:
                # A second process may have published the same immutable content.
                pass
        stored = ContentCatalog.load(target)
        if stored.fingerprint != rules.fingerprint:
            raise CoreError("Cached catalog does not match its source")
        return stored


def main():
    parser = ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    build = commands.add_parser("build")
    build.add_argument("source", type=Path)
    build.add_argument("output", type=Path)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("catalog", type=Path)
    inspect.add_argument("--card")
    args = parser.parse_args()
    try:
        if args.action == "build":
            rules = RuleSet.load(args.source)
            ContentCatalog.build(rules, args.output)
            print(json.dumps({"catalog": str(args.output), "rules_digest": rules.fingerprint}))
        else:
            rules = ContentCatalog.load(args.catalog)
            if args.card is not None and args.card not in rules.cards:
                raise CoreError(f"Unknown card definition: {args.card}")
            result = {"ruleset_version": rules.version, "rules_digest": rules.fingerprint,
                      "card_count": len(rules.cards), "ability_count": len(rules.abilities)}
            if args.card is not None:
                result["card"] = rules.cards[args.card]
                result["abilities"] = [rules.abilities[key] for key in sorted(rules.bound_abilities[args.card])]
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except (CoreError, OSError, sqlite3.Error, KeyError, TypeError, ValueError) as error:
        parser.exit(1, f"Catalog error: {error}\n")


if __name__ == "__main__":
    main()
