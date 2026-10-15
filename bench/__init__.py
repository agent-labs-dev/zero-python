"""The benchmark tier: zero-server against the published @rocicorp/zero package, on the same workloads.

    uv run python -m bench [--rounds 5] [--budget-ms 300] [--filter REGEX] [--no-gate]
    uv run python -m bench --update-baseline [--force]

Needs what `uv run pytest` needs: `npm ci` in `harness/`, and Docker or `ZERO_SERVER_PG`.

`harness/bench/cases.ts` times the TypeScript package and `bench/cases.py` times Python, case for case,
both driven by `workloads.json`. Each round runs one group of cases at a time (`query`, `run`, ...) as a
fresh process per side, alternating which side goes first, then times process startup. A case's time in
a round is the median of its samples after a warmup. The report shows the median of the round medians
with their min and max, and the ratio is the median of each round's Python/TS ratio.

Cases:

- `query`, `query_json`: `handle_query_request` on batches of 1, 10 and 100 simple and complex queries
  (`related`, `exists`), with `_json` adding the request's JSON parse and the response's serialization.
- `parse`, `parse_json`: validating 1000-item push and transform bodies, given as objects or as text.
- `mutate_cpu`: the mutate endpoint over an in-memory fake database, so only SDK time counts.
- `mutate`, `pushproc`: the mutate endpoint and `PushProcessor` against Postgres, per Python adapter,
  against postgres.js.
- `z2s`: compiling ZQL to SQL. `run`: `tx.run` end to end, decode included, at 1, 100 and 10k rows.
- `startup`: starting the interpreter, then importing the server SDK and one adapter.

The gate compares each case's Python/TS ratio, never an absolute time: both sides share the machine and
its load, so the ratio moves far less between runs than either time. A case fails when its ratio exceeds
its entry in `baseline.json` by more than its tolerance, after one retry of the failing cases. A case
with no entry passes and is recorded, which is how a new case or a new runner class starts.

The tolerances in `baseline.json` come from three full runs on a loaded 16-core Mac. The largest spread
of a case's ratio between runs was 15% for the CPU-only groups, 18% for `run`, 30% for `mutate`, 36% for
`pushproc` and 4% for `startup`. Each tolerance is 1.4 to 2.5 times that, so the CPU-only cases catch a
25% regression and the Postgres cases only a large one. Tighten them once CI has a history.

Ratios differ by platform, so the committed baseline comes from CI, and until one is committed every case
is new and passes. Every run writes `build/baseline.json`, the committed baseline with improved or new
ratios lowered in, and CI uploads it. After a speedup, commit that file, or run `--update-baseline` on
the CI runner class. `--force` replaces every ratio, for a deliberate regression.

Output lands in `build/`: `results.md` (the table), `results.json` (every round's medians).
"""
