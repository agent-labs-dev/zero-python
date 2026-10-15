"""The paired wall-clock benchmarks: TypeScript and Python on the same cases, on the same machine.

    uv run python -m bench [--groups GROUP,...] [--filter REGEX] [--rounds 15]

Every case: the CPU-only ones (`bench/cpu.py`, which CodSpeed also gates), then `tx.run`, the mutate
endpoint and `PushProcessor` per Python adapter against postgres.js, then process startup. Each side
is a long-lived worker (`harness/bench/cases.ts`, `bench/cases.py`). For each case the driver warms
both up, then alternates 100 ms bursts between them, so a round's two bursts run milliseconds apart
under the same load. A case's ratio is the median of the rounds' Python/TS ratios.

This is the real gap to npm, which CodSpeed's simulated times distort, but it moves with runner load,
so it is not gated. CI prints the table in the job summary. Output lands in `bench/build/`:
`results.json` (every round's time per case and side) and `report.md` (the table). Needs `npm ci` in
`harness/`; the Postgres groups also need Docker or `ZERO_SERVER_PG`.
"""

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

from bench.report import Rounds, markdown, pair
from tests.postgres import postgres_server
from tests.wire import seed
from tests.wire.conftest import DDL as WIRE_DDL

ROOT = Path(__file__).resolve().parent.parent
HERE = ROOT / "bench"
HARNESS = ROOT / "harness"

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
CPU_GROUPS = ["query", "query_json", "build", "map", "parse", "parse_json", "mutate_cpu", "z2s"]
POSTGRES_GROUPS = ["run", "mutate", "pushproc"]
GROUPS = [*CPU_GROUPS, *POSTGRES_GROUPS, "startup"]
"""A case's group is its name's first part."""


def step(message: str) -> None:
    print(f"==> {message}", file=sys.stderr, flush=True)


async def create_databases(base: str) -> tuple[str, str]:
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


def run_rounds(config_path: Path, args: argparse.Namespace, groups: list[str]) -> Rounds:
    """Time each case in `args.rounds` short bursts per side, alternating sides burst by burst."""
    build: Path = args.out.resolve()
    out: Rounds = {}
    workers = [g for g in groups if g != "startup"]
    if workers:
        ts = Worker(["node", str(HARNESS / "bench" / "cases.ts"), str(config_path)], HARNESS, build / "ts.log")
        py = Worker([sys.executable, "-m", "bench.cases", str(config_path)], ROOT, build / "py.log")
        try:
            for group in workers:
                ts_cases = ts.call(op="setup", group=group)["cases"]
                py_impls: dict[str, list[str]] = {}
                for case, impl in py.call(op="setup", group=group)["cases"]:
                    py_impls.setdefault(case, []).append(impl)
                for case, ts_impl in ts_cases:
                    sides = [(ts, ts_impl)] + [(py, impl) for impl in py_impls.get(case, [])]
                    for worker, impl in sides:
                        worker.call(op="warm", case=case, impl=impl, ms=args.warm_ms)
                    for r in range(args.rounds):
                        for worker, impl in sides if r % 2 == 0 else reversed(sides):
                            us = worker.call(op="time", case=case, impl=impl, ms=args.burst_ms)["us"]
                            out.setdefault((case, impl), []).append(us)
                step(f"{group} done")
        finally:
            ts.close()
            py.close()
    if "startup" in groups:
        # Process starts are slow and steady, so they get fewer rounds.
        compiled = re.compile(args.filter)
        for r in range(args.startup_rounds):
            for result in startup(compiled, args.spawns, flip=r % 2 == 1):
                out.setdefault((result["case"], result["impl"]), []).append(result["median_us"])
        step("startup done")
    return out


def machine() -> str:
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        for line in cpuinfo.read_text().splitlines():
            if line.startswith("model name"):
                return f"{platform.node()}: {line.split(':', 1)[1].strip()} x{os.cpu_count()}"
    return f"{platform.node()}: {platform.machine()} x{os.cpu_count()}"


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m bench", description=__doc__)
    parser.add_argument("--groups", default=",".join(GROUPS), help="comma-separated case groups to time")
    parser.add_argument("--filter", default=".", help="regex over case names")
    parser.add_argument("--rounds", type=int, default=15, help="bursts per case per side")
    parser.add_argument("--burst-ms", type=float, default=100, help="sampling time of one burst")
    parser.add_argument("--warm-ms", type=float, default=300, help="warmup per case per side")
    parser.add_argument("--startup-rounds", type=int, default=5, help="rounds of the startup group")
    parser.add_argument("--spawns", type=int, default=2, help="process starts per startup case per round")
    parser.add_argument("--out", type=Path, default=HERE / "build")
    args = parser.parse_args()
    build: Path = args.out.resolve()
    build.mkdir(parents=True, exist_ok=True)
    groups = [g for g in GROUPS if g in args.groups.split(",")]

    config_path = build / "config.json"
    config = {"filter": args.filter, "queryURL": "", "mutateURL": ""}
    if set(groups) & set(POSTGRES_GROUPS):
        with postgres_server() as base:
            step("seeding Postgres")
            config["mutateURL"], config["queryURL"] = asyncio.run(create_databases(base))
            config_path.write_text(json.dumps(config))
            rounds = run_rounds(config_path, args, groups)
    else:
        config_path.write_text(json.dumps(config))
        rounds = run_rounds(config_path, args, groups)

    rows = [{"case": case, "impl": impl, "us": us} for (case, impl), us in rounds.items()]
    (build / "results.json").write_text(json.dumps({"machine": machine(), "rounds": rows}, indent=1) + "\n")
    table = markdown(pair(rounds))
    (build / "report.md").write_text(f"Python/TypeScript on {machine()}, not gated.\n\n{table}")
    print(table)


if __name__ == "__main__":
    main()
