"""Run the benchmark tier: see `bench/__init__.py`."""

import argparse
import asyncio
import json
import random
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import asyncpg

from bench.report import Rounds, gate, markdown, pair, ratchet
from tests.harness import Harness
from tests.postgres import postgres_server
from tests.wire import seed
from tests.wire.conftest import DDL as WIRE_DDL
from zero_server.schema import load_schema
from zero_server.server.server_schema import get_server_schema
from zero_server.server.transaction import Row

ROOT = Path(__file__).resolve().parent.parent
HERE = ROOT / "bench"
HARNESS = ROOT / "harness"
BASELINE = HERE / "baseline.json"

NODE_IMPORT = (
    "await import('@rocicorp/zero/server'); await import('@rocicorp/zero/server/adapters/postgresjs'); "
    "await import('@rocicorp/zero');"
)
PY_IMPORT = "import zero_server.server.process_queries, zero_server.server.process_mutations, "
STARTUP: dict[str, dict[str, list[str]]] = {
    "startup.bare": {"node": ["node", "-e", "0"], "python": [sys.executable, "-c", "pass"]},
    "startup.import": {
        "node": ["node", "--input-type=module", "-e", NODE_IMPORT],
        **{
            adapter: [sys.executable, "-c", f"{PY_IMPORT}zero_server.server.adapters.{adapter}"]
            for adapter in ("asyncpg", "psycopg", "sqlalchemy")
        },
    },
}


def step(message: str) -> None:
    print(f"==> {message}", file=sys.stderr, flush=True)


async def create_databases(base: str, build: Path) -> tuple[str, str]:
    """`bench_mut` holds the wire tier's tables; `bench_q` the SQL tier's, scaled up to 10k issues."""
    admin = await asyncpg.connect(f"{base}/postgres")
    for name in ("bench_mut", "bench_q"):
        await admin.execute(f"DROP DATABASE IF EXISTS {name}")
        await admin.execute(f"CREATE DATABASE {name}")
    await admin.close()

    mutate_url, query_url = f"{base}/bench_mut", f"{base}/bench_q"
    conn = await asyncpg.connect(mutate_url)
    for statement in WIRE_DDL:
        await conn.execute(statement)
    await conn.close()

    conn = await asyncpg.connect(query_url)
    for statement in seed.DDL:
        await conn.execute(statement)
    rng = random.Random(1)
    users = [(f"u{i}", f"User {i}", rng.randint(18, 80), rng.random() < 0.5) for i in range(200)]
    await conn.copy_records_to_table("users", records=users, columns=["id", "full_name", "age", "active"])
    issues = [
        (
            f"i{i:05}",
            f"u{rng.randrange(200)}",
            f"Issue title number {i} lorem ipsum",
            json.dumps({"k": i, "tags": ["a", "b"]}) if i % 3 else None,
            float(rng.randint(0, 5)),
            rng.random() < 0.6,
        )
        for i in range(10_000)
    ]
    await conn.executemany("INSERT INTO issue VALUES ($1, $2, $3, $4::jsonb, $5, $6)", issues)
    comments = [
        (f"c{n:06}", f"i{rng.randrange(10_000):05}", f"u{rng.randrange(200)}", f"comment body {n}")
        for n in range(30_000)
    ]
    await conn.copy_records_to_table("comment", records=comments, columns=["id", "issue_id", "author_id", "body"])
    await conn.copy_records_to_table(
        "label", records=[(f"l{i}", f"label{i}") for i in range(20)], columns=["id", "name"]
    )
    issue_labels = {(f"i{rng.randrange(10_000):05}", f"l{rng.randrange(20)}") for _ in range(20_000)}
    await conn.copy_records_to_table("issue_label", records=sorted(issue_labels), columns=["issue_id", "label_id"])
    for statement in (
        "CREATE INDEX ON comment (issue_id)",
        "CREATE INDEX ON issue (owner_id)",
        "CREATE INDEX ON issue_label (label_id)",
        "ANALYZE",
    ):
        await conn.execute(statement)
    await conn.close()

    # What the server-schema query returns for the wire schema, for the mutate endpoint's fake database.
    conn = await asyncpg.connect(mutate_url)
    captured: list[Row] = []

    class Recording:
        wrapped_transaction = None

        async def query(self, sql: str, params: Any = ()) -> list[Row]:
            rows = [dict(r) for r in await conn.fetch(sql, *params)]
            captured[:] = rows
            return rows

    await get_server_schema(Recording(), load_schema((build / "wire_schema.json").read_text()))
    await conn.close()
    (build / "server_schema_rows.json").write_text(json.dumps(captured))
    return mutate_url, query_url


def run_side(cmd: list[str], cwd: Path) -> list[dict[str, Any]]:
    done = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if done.returncode != 0:
        sys.stderr.write(done.stderr[-5000:])
        raise SystemExit(f"{cmd[0]} side failed")
    return json.loads(done.stdout.strip().splitlines()[-1])


def startup(filt: re.Pattern[str], spawns: int, flip: bool) -> list[dict[str, Any]]:
    out = []
    for case, impls in STARTUP.items():
        if not filt.search(case):
            continue
        times: dict[str, list[float]] = {impl: [] for impl in impls}
        order = list(impls.items())
        for _ in range(spawns):
            for impl, cmd in reversed(order) if flip else order:
                t0 = time.perf_counter()
                subprocess.run(cmd, cwd=HARNESS, check=True, capture_output=True)
                times[impl].append((time.perf_counter() - t0) * 1e6)
        out += [{"case": case, "impl": impl, "median_us": statistics.median(t)} for impl, t in times.items()]
    return out


GROUPS = ["query", "query_json", "parse", "parse_json", "mutate_cpu", "z2s", "run", "mutate", "pushproc"]
"""Each round runs one group at a time on each side, so the two measure a case seconds apart, not minutes."""


def run_rounds(config_path: Path, config: dict[str, Any], groups: list[str], rounds: int, spawns: int) -> Rounds:
    filt = re.compile(config["filter"])
    out: Rounds = {}
    for r in range(rounds):
        results = []
        for group in groups:
            # Alternate which side goes first, so drift in machine load does not favour one.
            sides = [
                (["node", str(HARNESS / "bench" / "cases.ts"), str(config_path), group], HARNESS),
                ([sys.executable, "-m", "bench.cases", str(config_path), group], ROOT),
            ]
            for cmd, cwd in sides if r % 2 == 0 else reversed(sides):
                results += run_side(cmd, cwd)
        results += startup(filt, spawns, flip=r % 2 == 1)
        for result in results:
            out.setdefault((result["case"], result["impl"]), []).append(result["median_us"])
        step(f"round {r + 1}/{rounds} done")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m bench", description=__doc__)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--budget-ms", type=float, default=300, help="sampling time per case per round")
    parser.add_argument("--spawns", type=int, default=5, help="process starts per startup case per round")
    parser.add_argument("--filter", default=".", help="regex over case names")
    parser.add_argument("--out", type=Path, default=HERE / "build")
    parser.add_argument("--no-gate", action="store_true", help="report only, never fail")
    parser.add_argument("--update-baseline", action="store_true", help="lower baseline ratios this run improved")
    parser.add_argument("--force", action="store_true", help="with --update-baseline, replace every ratio")
    args = parser.parse_args()

    build: Path = args.out.resolve()
    build.mkdir(parents=True, exist_ok=True)
    step("schemas from the harness")
    harness = Harness()
    (build / "schema.json").write_text(json.dumps(harness.call("schema")))
    (build / "wire_schema.json").write_text(json.dumps(harness.call("wireSchema")))
    harness.close()

    with postgres_server() as base:
        step("seeding Postgres")
        mutate_url, query_url = asyncio.run(create_databases(base, build))
        config = {
            "filter": args.filter,
            "budgetMs": args.budget_ms,
            "workloads": str(HERE / "workloads.json"),
            "schema": str(build / "schema.json"),
            "wireSchema": str(build / "wire_schema.json"),
            "serverSchemaRows": str(build / "server_schema_rows.json"),
            "queryURL": query_url,
            "mutateURL": mutate_url,
        }
        config_path = build / "config.json"
        config_path.write_text(json.dumps(config))
        rounds = run_rounds(config_path, config, GROUPS, args.rounds, args.spawns)

        baseline = json.loads(BASELINE.read_text())
        verdicts = gate(pair(rounds), baseline)
        failed = [v.row for v in verdicts if v.failed]
        if failed and not args.no_gate:
            # One retry of the failing cases alone, so a burst of load on a shared runner does not fail it.
            names = sorted({r.case for r in failed})
            step(f"retrying {len(names)} case(s) over the gate: {', '.join(names)}")
            retry = {**config, "filter": "^(" + "|".join(re.escape(n) for n in names) + ")$"}
            config_path.write_text(json.dumps(retry))
            groups = sorted({n.split(".")[0] for n in names} - {"startup"})
            rounds.update(run_rounds(config_path, retry, groups, args.rounds, args.spawns))
            verdicts = gate(pair(rounds), baseline)

    rows = [v.row for v in verdicts]
    table = markdown(verdicts)
    (build / "results.md").write_text(table)
    (build / "results.json").write_text(
        json.dumps(
            [
                {
                    "case": v.row.case,
                    "ts_impl": v.row.ts_impl,
                    "ts_us": v.row.ts,
                    "py_impl": v.row.py_impl,
                    "py_us": v.row.py,
                    "ratio": v.row.ratio,
                    "baseline": v.baseline,
                    "limit": v.limit,
                    "failed": v.failed,
                }
                for v in verdicts
            ],
            indent=1,
        )
    )
    # What --update-baseline would write: CI uploads it, so a runner's ratios can be committed.
    (build / "baseline.json").write_text(json.dumps(ratchet(rows, baseline), indent=2) + "\n")
    print(table)
    if args.update_baseline:
        BASELINE.write_text(json.dumps(ratchet(rows, baseline, force=args.force), indent=2) + "\n")
        step(f"wrote {BASELINE.relative_to(ROOT)}")
    failures = [v for v in verdicts if v.failed]
    if failures and not args.no_gate:
        for v in failures:
            step(f"FAIL {v.row.key}: Py/TS {v.row.ratio:.2f} > {v.limit:.2f} (baseline {v.baseline:.2f})")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
