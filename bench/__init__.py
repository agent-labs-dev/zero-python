"""The benchmark tier: zero-server against the published @rocicorp/zero package, on the same workloads.

Two suites, both driven by `workloads.json`:

- CPU-only cases, as CodSpeed benchmarks: `bench/test_cpu.py` for Python, `harness/bench/cpu.ts` for
  TypeScript, with the same case names. The query endpoint at batches of 1 and 100 (`query`, and
  `query_json` with the JSON parse and serialization an HTTP endpoint adds), parsing 1000-item push
  and transform bodies (`parse`, `parse_json`), the mutate endpoint over an in-memory database
  (`mutate_cpu`), and compiling ZQL to SQL (`z2s`). CI runs them under CodSpeed's CPU simulation,
  which counts instructions, so CodSpeed's check on a pull request flags a Python regression without
  tolerances or retries. The TypeScript side only moves when the pin does; it is there so the gap
  shows on the dashboard.

      uv run pytest bench --codspeed --no-cov   # checks every Python case runs; no numbers locally
      node harness/bench/cpu.ts                 # the same for TypeScript

- Cases that wait on Postgres, which instruction counts leave out: `tx.run` (`run`), the mutate
  endpoint (`mutate`) and `PushProcessor` (`pushproc`) per Python adapter against postgres.js, plus
  process startup. `bench/__main__.py` alternates the two sides on one machine and prints the
  Python/TS ratio per case. CI puts the table in the job summary and never fails on it.

      uv run python -m bench [--groups run,mutate,pushproc,startup] [--filter REGEX]

The CPU benchmarks need neither Node nor Postgres for their setup: `fixtures/` holds the harness's
schemas and the rows Postgres returns for the server-schema query. `uv run python -m bench.capture`
regenerates them, and `tests/wire/test_bench_fixtures.py` fails when they go stale.
"""
