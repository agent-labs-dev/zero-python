"""Run the benchmark tier: see `bench/__init__.py`."""

import argparse
import asyncio
import json
import os
import platform
import random
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import asyncpg

from bench.report import Combined, Rounds, Row, combine, gate, markdown, pair, update
from tests.harness import Harness
from tests.postgres import postgres_server
from tests.wire import seed
from tests.wire.conftest import DDL as WIRE_DDL
from zero_server.schema import load_schema
from zero_server.server.server_schema import get_server_schema
from zero_server.server.transaction import Row as DBRow

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
    captured: list[DBRow] = []

    class Recording:
        wrapped_transaction = None

        async def query(self, sql: str, params: Any = ()) -> list[DBRow]:
            rows = [dict(r) for r in await conn.fetch(sql, *params)]
            captured[:] = rows
            return rows

    await get_server_schema(Recording(), load_schema((build / "wire_schema.json").read_text()))
    await conn.close()
    (build / "server_schema_rows.json").write_text(json.dumps(captured))
    return mutate_url, query_url


class Worker:
    """One side's long-lived timing process (`harness/bench/cases.ts` or `bench/cases.py`)."""

    def __init__(self, cmd: list[str], cwd: Path, log: Path) -> None:
        self._log = log.open("w")
        self._process = subprocess.Popen(
            cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._log, text=True, bufsize=1
        )

    def call(self, **request: Any) -> Any:
        stdin, stdout = self._process.stdin, self._process.stdout
        assert stdin is not None
        assert stdout is not None
        stdin.write(json.dumps(request) + "\n")
        stdin.flush()
        line = stdout.readline()
        if not line:
            raise SystemExit(f"bench worker died; see {self._log.name}")
        return json.loads(line)

    def close(self) -> None:
        if self._process.stdin is not None:
            self._process.stdin.close()
        self._process.wait(timeout=30)
        self._log.close()


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


def run_rounds(config_path: Path, args: argparse.Namespace, filt: str, groups: list[str], rounds: int) -> Rounds:
    """Time each case in `rounds` short bursts per side, alternating sides burst by burst.

    The two sides of one round run within a burst or two of each other, so a stretch of load from a
    neighbour on a shared runner lands on both, and the round's ratio cancels it.
    """
    build: Path = args.out.resolve()
    ts = Worker(["node", str(HARNESS / "bench" / "cases.ts"), str(config_path)], HARNESS, build / "ts.log")
    py = Worker([sys.executable, "-m", "bench.cases", str(config_path)], ROOT, build / "py.log")
    out: Rounds = {}
    try:
        for group in groups:
            ts_cases = ts.call(op="setup", group=group)["cases"]
            py_impls: dict[str, list[str]] = {}
            for case, impl in py.call(op="setup", group=group)["cases"]:
                py_impls.setdefault(case, []).append(impl)
            for case, ts_impl in ts_cases:
                sides = [(ts, ts_impl)] + [(py, impl) for impl in py_impls.get(case, [])]
                for worker, impl in sides:
                    worker.call(op="warm", case=case, impl=impl, ms=args.warm_ms)
                for r in range(rounds):
                    for worker, impl in sides if r % 2 == 0 else reversed(sides):
                        us = worker.call(op="time", case=case, impl=impl, ms=args.burst_ms)["us"]
                        out.setdefault((case, impl), []).append(us)
            step(f"{group} done")
    finally:
        ts.close()
        py.close()
    compiled = re.compile(filt)
    for r in range(rounds if any(compiled.search(c) for c in STARTUP) else 0):
        for result in startup(compiled, args.spawns, flip=r % 2 == 1):
            out.setdefault((result["case"], result["impl"]), []).append(result["median_us"])
    step("startup done")
    return out


def measure(args: argparse.Namespace, filt: str, groups: list[str], rounds: int) -> Rounds:
    """Seed a Postgres and time `rounds` rounds of `groups` on both sides."""
    build: Path = args.out.resolve()
    harness = Harness()
    (build / "schema.json").write_text(json.dumps(harness.call("schema")))
    (build / "wire_schema.json").write_text(json.dumps(harness.call("wireSchema")))
    harness.close()
    with postgres_server() as base:
        step("seeding Postgres")
        mutate_url, query_url = asyncio.run(create_databases(base, build))
        config = {
            "filter": filt,
            "workloads": str(HERE / "workloads.json"),
            "schema": str(build / "schema.json"),
            "wireSchema": str(build / "wire_schema.json"),
            "serverSchemaRows": str(build / "server_schema_rows.json"),
            "queryURL": query_url,
            "mutateURL": mutate_url,
        }
        config_path = build / "config.json"
        config_path.write_text(json.dumps(config))
        return run_rounds(config_path, args, filt, groups, rounds)


def machine() -> str:
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        for line in cpuinfo.read_text().splitlines():
            if line.startswith("model name"):
                return f"{platform.node()}: {line.split(':', 1)[1].strip()} x{os.cpu_count()}"
    return f"{platform.node()}: {platform.machine()} x{os.cpu_count()}"


def save_shard(path: Path, rounds: Rounds) -> None:
    rows = [{"case": case, "impl": impl, "us": us} for (case, impl), us in rounds.items()]
    path.write_text(json.dumps({"machine": machine(), "rounds": rows}, indent=1) + "\n")


def load_shards(paths: list[Path]) -> list[list[Row]]:
    """Every `results.json` under `paths`, each one shard."""
    files = sorted(f for p in paths for f in ([p] if p.is_file() else p.rglob("results.json")))
    shards = []
    for f in files:
        data = json.loads(f.read_text())
        shards.append(pair({(r["case"], r["impl"]): r["us"] for r in data["rounds"]}))
        step(f"shard {f} ({data['machine']})")
    return shards


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m bench", description=__doc__)
    parser.add_argument("--rounds", type=int, default=15, help="bursts per case per side")
    parser.add_argument("--burst-ms", type=float, default=100, help="sampling time of one burst")
    parser.add_argument("--warm-ms", type=float, default=300, help="warmup per case per side")
    parser.add_argument("--spawns", type=int, default=2, help="process starts per startup case per round")
    parser.add_argument("--filter", default=".", help="regex over case names")
    parser.add_argument("--out", type=Path, default=HERE / "build")
    parser.add_argument("--no-gate", action="store_true", help="measure and report, never fail")
    parser.add_argument("--gate", nargs="+", type=Path, metavar="SHARD", help="gate on these shards' results")
    parser.add_argument(
        "--update-baseline", nargs="+", type=Path, metavar="SHARD", help="rebuild the baseline from these shards"
    )
    parser.add_argument("--force", action="store_true", help="with --update-baseline, replace every ratio")
    args = parser.parse_args()
    build: Path = args.out.resolve()
    build.mkdir(parents=True, exist_ok=True)
    baseline = json.loads(BASELINE.read_text())

    if args.update_baseline:
        updated = update(baseline, load_shards(args.update_baseline), force=args.force)
        for key in sorted(updated["ratios"]):
            old, new = baseline["ratios"].get(key), updated["ratios"][key]
            if old != new:
                step(f"{key}: {old} -> {new}")
        BASELINE.write_text(json.dumps(updated, indent=2) + "\n")
        step(f"wrote {BASELINE.relative_to(ROOT)}")
        return

    if args.gate:
        shards = load_shards(args.gate)
    else:
        rounds = measure(args, args.filter, GROUPS, args.rounds)
        save_shard(build / "results.json", rounds)
        shards = [pair(rounds)]

    cases = combine(shards)
    verdicts = gate(cases, baseline)
    failed = {v.case.key for v in verdicts if v.failed}
    if failed and not args.no_gate:
        # One retry of the failing cases alone, on this machine and with twice the rounds, so a burst
        # of load on a shared runner does not fail the build.
        names = sorted({key.split(" ")[0] for key in failed})
        step(f"retrying {len(names)} case(s) over the gate: {', '.join(names)}")
        filt = "^(" + "|".join(re.escape(n) for n in names) + ")$"
        groups = [g for g in GROUPS if g in {n.split(".")[0] for n in names}]
        rounds = measure(args, filt, groups, args.rounds * 2)
        save_shard(build / "retry.json", rounds)
        retried = {row.key: row for row in pair(rounds)}
        verdicts = gate([Combined([retried[c.key]]) if c.key in failed else c for c in cases], baseline)

    table = markdown(verdicts)
    (build / "report.md").write_text(table)
    print(table)
    failures = [v for v in verdicts if v.failed]
    if failures and not args.no_gate:
        for v in failures:
            step(f"FAIL {v.case.key}: Py/TS {v.case.ratio:.2f} > {v.limit:.2f} (baseline {v.baseline:.2f})")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
