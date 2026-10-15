# Contributing

zero-server is a port, so a change is right when it behaves like `@rocicorp/zero` at the version in `ZERO_VERSION`. Read [`docs/plan.md`](docs/plan.md) before porting anything.

## Setup

You need Python 3.12+, [uv](https://docs.astral.sh/uv/), Node 24 and Docker.

```sh
uv sync --group e2e
(cd harness && npm ci)
(cd e2e && npm ci)
```

The tests start Postgres in Docker. To use your own instead, set `ZERO_SERVER_PG` to a connection string such as `postgresql://postgres:postgres@localhost:5432`.

## Checks

CI runs these, and a PR merges only when they pass:

```sh
uv run ruff check
uv run ruff format --check
uv run ty check
uv run pytest
uv run --group e2e python e2e/run.py
```

## Rules

- A behaviour change needs a differential test that runs the same input through the npm package and through Python. A test that asserts what the TypeScript source appears to do proves nothing.
- Code ported from [rocicorp/mono](https://github.com/rocicorp/mono) keeps its Apache-2.0 notice, and its docstring names the upstream path and commit.
- Commits must be signed. `main` rejects unsigned ones.
- PRs are squash-merged, so the PR title becomes the commit message. Use a [Conventional Commits](https://www.conventionalcommits.org/) title, like `feat: ...` or `fix: ...`.

## Bumping Zero

Change `ZERO_VERSION`, the `@rocicorp/zero` version in `harness/package.json` and `e2e/package.json`, and the `rocicorp/zero` image in `e2e/docker-compose.yml`. Then run the full suite and fix what it finds.
