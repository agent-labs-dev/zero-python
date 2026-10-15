"""`bench/fixtures/` still matches the harness and Postgres. Regenerate with `uv run python -m bench.capture`."""

from bench.capture import server_schema_rows
from bench.workloads import fixture, schema, wire_schema
from tests.harness import Harness
from tests.wire.conftest import SqlDB, WireDB


def test_schemas_match_the_harness(harness: Harness) -> None:
    assert fixture("schema.json") == harness.call("schema")
    assert fixture("wire_schema.json") == harness.call("wireSchema")


def test_server_schema_rows_match_postgres(sql_db: SqlDB, wire_db: WireDB) -> None:
    assert sql_db.run(server_schema_rows(sql_db.url, schema())) == fixture("query_server_schema_rows.json")
    assert wire_db.run(server_schema_rows(wire_db.ts_url, wire_schema())) == fixture("mutate_server_schema_rows.json")
