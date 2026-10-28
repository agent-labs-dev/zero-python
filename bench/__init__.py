"""The benchmark tier: zero-server against the published @rocicorp/zero package, on the same workloads.

Every case is written once per side, from `workloads.json`:

- CPU-only cases: `bench/cpu.py` and `harness/bench/cpu_cases.ts`, with the same case names. The
  query endpoint at batches of 1 and 100 (`query`, and `query_json` with the JSON parse and
  serialization an HTTP endpoint adds), its two stages on their own (building a query, `build`, and
  mapping its AST to server names, `map`), parsing 1000-item push and transform bodies (`parse`,
  `parse_json`), the mutate endpoint over an in-memory database (`mutate_cpu`), and compiling ZQL to
  SQL (`z2s`).
- Cases that wait on Postgres: `tx.run` (`run`), the mutate endpoint (`mutate`) and `PushProcessor`
  (`pushproc`) per Python adapter against postgres.js, in `bench/cases.py` and `harness/bench/cases.ts`.
  Plus process startup (`startup`).

Two ways to run them:

- CodSpeed, for the CPU-only cases: `bench/test_cpu.py` and `harness/bench/cpu.ts`. CI runs them under
  CodSpeed's CPU simulation, which counts instructions, so CodSpeed's check on a pull request flags a
  Python regression without tolerances or retries. Simulated times distort the gap between the two
  sides, so read the gap from the wall-clock table below.

      uv run pytest bench --codspeed --no-cov   # checks every Python case runs; no numbers locally
      node harness/bench/cpu.ts                 # the same for TypeScript

- The wall clock, for every case: `python -m bench` alternates the two sides on one machine and
  prints the Python/TS ratio per case. This is the real gap to npm. It moves with runner load, so CI
  puts the table in the job summary and never fails on it.

      uv run python -m bench [--groups GROUP,...] [--filter REGEX]

The CPU-only cases need neither Node nor Postgres for their setup: `fixtures/` holds the harness's
schemas and the rows Postgres returns for the server-schema query. `uv run python -m bench.capture`
regenerates them, and `tests/wire/test_bench_fixtures.py` fails when they go stale.
"""
