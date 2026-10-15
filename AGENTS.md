# AGENTS.md

zero-server is a community Python port of Rocicorp Zero's **server SDK**: what `@rocicorp/zero/server` gives a
TypeScript backend. It is not affiliated with Rocicorp; say so wherever the project describes itself.

Before any porting work, read [`docs/plan.md`](docs/plan.md): scope, the oracle that defines "compatible", the
phases and their completion criteria, and the decisions already made.

## Conventions

- Python 3.12+, uv, ruff, ty, pytest, Hypothesis. Typed throughout; wire models are Pydantic.
- The pinned Zero version lives in one file, `ZERO_VERSION`. Every claim of compatibility names that version.
- Compatibility is proven by **differential** tests against the published npm package, never by reading the TS
  and asserting what it should do. A Python behaviour with no differential test is unverified.
- Ported code keeps Apache-2.0 attribution to rocicorp/mono; record each ported module's upstream path and
  commit in its docstring.
- All commits signed (`git commit -S`). One PR per phase against `main`; watch CI until green.
