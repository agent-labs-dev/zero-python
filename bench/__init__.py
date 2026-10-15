"""The benchmark tier: zero-server against the published @rocicorp/zero package, on the same workloads.

    uv run python -m bench [--rounds 15] [--filter REGEX] [--no-gate]   # measure one shard, gate on it
    uv run python -m bench --gate SHARD...                              # gate on shards measured elsewhere
    uv run python -m bench --update-baseline SHARD... [--force]         # rebuild baseline.json

A SHARD is a `results.json` or a directory holding some. Measuring needs what `uv run pytest` needs:
`npm ci` in `harness/`, and Docker or `ZERO_SERVER_PG`.

`harness/bench/cases.ts` times the TypeScript package and `bench/cases.py` times Python, case for case,
both driven by `workloads.json`. Each runs as a long-lived worker. For each case the driver warms both
up, then alternates short bursts between them (100 ms, 15 rounds), so the two bursts of a round run
milliseconds apart and share whatever load the machine is under. A burst's time is the median of its
samples; a case's ratio is the median over rounds of each round's Python/TS ratio.

Cases:

- `query`, `query_json`: `handle_query_request` on batches of 1, 10 and 100 simple and complex queries
  (`related`, `exists`), with `_json` adding the request's JSON parse and the response's serialization.
- `parse`, `parse_json`: validating 1000-item push and transform bodies, given as objects or as text.
- `mutate_cpu`: the mutate endpoint over an in-memory fake database, so only SDK time counts.
- `mutate`, `pushproc`: the mutate endpoint and `PushProcessor` against Postgres, per Python adapter,
  against postgres.js.
- `z2s`: compiling ZQL to SQL. `run`: `tx.run` end to end, decode included, at 1, 100 and 10k rows.
- `startup`: starting the interpreter, then importing the server SDK and one adapter.

The gate compares ratios, never absolute times. CI measures three shards on separate runners, then
`bench-gate` takes each case's median shard ratio. A case fails when that exceeds its entry in
`baseline.json` by more than its tolerance, and still does when the gate runner times it again alone
with twice the rounds. A case with no entry passes, which is how a new case starts.

The baseline holds each case's worst shard ratio over at least three CI runs, so a lucky run cannot set
it. `--update-baseline` refuses fewer than three shards and lowers a ratio only when every shard beat
it. After a speedup, download the shards of three CI runs on the PR and rebuild:

    gh run download RUN_ID -p 'bench-shard-*' -D bench/build/runs/RUN_ID   # for each of three runs
    uv run python -m bench --update-baseline bench/build/runs

`--force` replaces every ratio, for a regression the PR accepts on purpose.

TOLERANCES

Output lands in `build/`: `results.json` (one shard: every round's time per case and side) and
`report.md` (the gate's table).
"""
