"""The SQL tier's database: the wire test schema (`harness/schema.ts`) with deterministic rows.

Values come from small pools that the generated queries also draw from, so filters match some rows
and miss others, and every nullable column holds some NULLs.
"""

import json
import random
from typing import Any

DDL = [
    """CREATE TABLE users (
        id TEXT PRIMARY KEY, full_name TEXT NOT NULL, age INTEGER, active BOOLEAN NOT NULL, manager_id TEXT)""",
    """CREATE TABLE issue (
        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, title TEXT NOT NULL, meta JSONB,
        priority DOUBLE PRECISION NOT NULL, open BOOLEAN NOT NULL)""",
    """CREATE TABLE comment (
        id TEXT PRIMARY KEY, issue_id TEXT NOT NULL, author_id TEXT NOT NULL, body TEXT NOT NULL)""",
    "CREATE TABLE label (id TEXT PRIMARY KEY, name TEXT NOT NULL)",
    "CREATE TABLE issue_label (issue_id TEXT, label_id TEXT, PRIMARY KEY (issue_id, label_id))",
    "CREATE TYPE mood AS ENUM ('happy', 'sad')",
    """CREATE TABLE audit (
        id TEXT PRIMARY KEY, at TIMESTAMPTZ NOT NULL, day DATE, tm TIME, ttz TIMETZ, plain TIMESTAMP,
        stamps TIMESTAMPTZ[], tags TEXT[], mood mood)""",
    "CREATE TABLE counter (id TEXT PRIMARY KEY, n BIGINT NOT NULL, history BIGINT[])",
    "CREATE SCHEMA extra",
    """CREATE TABLE extra.gadgets (
        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, serial UUID NOT NULL, ip INET NOT NULL, amount NUMERIC(10, 2),
        code VARCHAR(4), mood mood, moods mood[], seen TIMESTAMPTZ, tag CHAR(3), pad BPCHAR)""",
]

TEXTS = ["", "a", "b", "ab", "A", "é", "x%", "a_b", "Ab", "0", "1"]

# Values for the columns a comparison casts to a Postgres type of its own, which the generated queries
# draw from as well. Each has a form Postgres reads but would print differently.
COLUMN_VALUES: dict[tuple[str, str], list[Any]] = {
    ("gadget", "serial"): [
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-00000000000A",
        "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
        "{a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a12}",
    ],
    ("gadget", "ip"): ["10.0.0.1", "10.0.0.1/32", "192.168.0.0/16", "::1", "10.0.0.2"],
    ("gadget", "amount"): [0, 1.5, 1.25, 10, -3.333, 99999999.99],
    ("gadget", "code"): ["", "ab", "abcd", "abcde", "AB"],
    # `char(3)` pads with spaces and ignores trailing ones when it compares; a LIKE pattern keeps them.
    ("gadget", "tag"): ["ab", "abc", "ab ", "a%", "a_ ", "abcd"],
    ("gadget", "pad"): ["x", "x  ", "xy", "%  "],
    ("gadget", "mood"): ["happy", "sad", "meh"],
    ("gadget", "moods"): [[], ["happy"], ["sad", "happy"], ["meh"]],
    ("gadget", "seen"): [0, 946_684_800_000, 946_684_800_000.5, 1_577_836_800_000],
    ("issue", "meta"): [{"a": 1}, [1, "x"], "s", 2.5, True, {}, {"a": [1, None]}],
}


def rows() -> dict[str, list[dict[str, Any]]]:
    rng = random.Random(7)
    users = [f"u{i}" for i in range(8)]
    issues = [f"i{i}" for i in range(25)]
    labels = [f"l{i}" for i in range(5)]

    def maybe(value: Any) -> Any:
        return None if rng.random() < 0.25 else value

    return {
        "users": [
            {
                "id": u,
                "full_name": rng.choice(TEXTS),
                "age": maybe(rng.randint(-5, 5)),
                "active": rng.random() < 0.5,
                # Someone is their own manager.
                "manager_id": maybe(rng.choice([*users, "ghost"])),
            }
            for u in users
        ],
        "issue": [
            {
                "id": i,
                "owner_id": rng.choice([*users, "ghost"]),
                "title": rng.choice(TEXTS),
                "meta": maybe(json.dumps(rng.choice([{"a": 1}, [1, "x"], "s", 2.5, True, None]))),
                "priority": rng.choice([-5, -1, 0, 0.5, 1, 2, 3, 5]),
                "open": rng.random() < 0.6,
            }
            for i in issues
        ],
        "comment": [
            {
                "id": f"c{n}",
                "issue_id": rng.choice(issues),
                "author_id": rng.choice(users),
                "body": rng.choice(TEXTS),
            }
            for n in range(40)
        ],
        "label": [{"id": label, "name": rng.choice(TEXTS)} for label in labels],
        "issue_label": [
            {"issue_id": i, "label_id": label}
            for i, label in {(rng.choice(issues), rng.choice(labels)) for _ in range(30)}
        ],
        "audit": [
            {
                "id": f"a{n}",
                "at": 946_684_800 + rng.randint(0, 10**8),
                "day": maybe(f"20{rng.randint(10, 30)}-0{rng.randint(1, 9)}-1{rng.randint(0, 9)}"),
                "tm": maybe(f"{rng.randint(0, 23):02}:{rng.randint(0, 59):02}:00"),
                "ttz": maybe(f"{rng.randint(0, 23):02}:30:00+0{rng.randint(0, 5)}"),
                "plain": maybe(f"2020-01-0{rng.randint(1, 9)} 12:00:00"),
                "stamps": maybe(["2020-01-01 00:00:00+00", "2021-06-01 12:00:00+02"][: rng.randint(0, 2)]),
                "tags": maybe(rng.sample(TEXTS, rng.randint(0, 3))),
                "mood": maybe(rng.choice(["happy", "sad"])),
            }
            for n in range(6)
        ],
        # The last value is past 2^53, which upstream refuses to return rather than round.
        # k8's unsafe integer is in an array, after rows whose arrays hold none.
        "counter": [{"id": f"k{n}", "n": n, "history": [str(n)]} for n in range(3)]
        + [{"id": "k8", "n": 8, "history": ["1", str(2**60)]}, {"id": "k9", "n": 2**60, "history": None}],
        "extra.gadgets": [
            {
                "id": f"g{n}",
                "owner_id": rng.choice(users),
                "serial": rng.choice(COLUMN_VALUES["gadget", "serial"]),
                "ip": rng.choice(COLUMN_VALUES["gadget", "ip"][:-1]),
                "amount": maybe(str(rng.choice(COLUMN_VALUES["gadget", "amount"]))),
                "code": maybe(rng.choice(COLUMN_VALUES["gadget", "code"][:-2])),
                "mood": maybe(rng.choice(["happy", "sad"])),
                "moods": maybe(rng.choice(COLUMN_VALUES["gadget", "moods"][:-1])),
                "seen": maybe(rng.choice(COLUMN_VALUES["gadget", "seen"]) / 1000),
                "tag": maybe(rng.choice(COLUMN_VALUES["gadget", "tag"][:3])),
                "pad": maybe(rng.choice(COLUMN_VALUES["gadget", "pad"][:3])),
            }
            for n in range(8)
        ],
    }


def inserts() -> list[tuple[str, dict[str, Any]]]:
    """Each row as a statement and its parameters, in an order that respects nothing: there are no FKs."""
    out: list[tuple[str, dict[str, Any]]] = []
    for table, table_rows in rows().items():
        for row in table_rows:
            columns = ", ".join(row)
            values = ", ".join(_placeholder(table, c) for c in row)
            out.append((f"INSERT INTO {table} ({columns}) VALUES ({values})", row))
    return out


_CASTS = {
    ("issue", "meta"): "CAST(:meta AS jsonb)",
    ("audit", "at"): "to_timestamp(:at)",
    # Bound as text and cast in SQL, so asyncpg does not ask for datetime objects.
    ("audit", "day"): "CAST(CAST(:day AS text) AS date)",
    ("audit", "tm"): "CAST(CAST(:tm AS text) AS time)",
    ("audit", "ttz"): "CAST(CAST(:ttz AS text) AS timetz)",
    ("audit", "plain"): "CAST(CAST(:plain AS text) AS timestamp)",
    ("audit", "stamps"): "CAST(CAST(:stamps AS text[]) AS timestamptz[])",
    ("audit", "mood"): "CAST(CAST(:mood AS text) AS mood)",
    ("counter", "history"): "CAST(CAST(:history AS text[]) AS bigint[])",
    ("extra.gadgets", "serial"): "CAST(CAST(:serial AS text) AS uuid)",
    ("extra.gadgets", "ip"): "CAST(CAST(:ip AS text) AS inet)",
    ("extra.gadgets", "amount"): "CAST(CAST(:amount AS text) AS numeric)",
    ("extra.gadgets", "mood"): "CAST(CAST(:mood AS text) AS mood)",
    ("extra.gadgets", "moods"): "CAST(CAST(:moods AS text[]) AS mood[])",
    ("extra.gadgets", "seen"): "to_timestamp(:seen)",
}


def _placeholder(table: str, column: str) -> str:
    return _CASTS.get((table, column), f":{column}")
