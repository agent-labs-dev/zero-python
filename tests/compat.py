"""The compatibility proof: what the differential tests exercised, on both sides, after `pytest` has run.

    uv run pytest && uv run python -m tests.compat

Two checks, both of which must come out complete:

- **Upstream.** Every line and branch of every upstream module the port names in a docstring ran inside the
  published `@rocicorp/zero` during the differential tests, less the regions `tests/compat_scope.py`
  excludes with a reason. The harness records V8 coverage of the npm package (`tests/harness.py`), and c8
  maps it back to the TypeScript through the package's source maps.
- **Port.** Every line and branch of `zero_server` ran during a differential test (`tests/wire`), less the
  Python-only modules `tests/compat_scope.py` lists and the lines marked `# python-only: <reason>`. The unit
  tests' coverage does not count here.

An upstream branch nothing exercised is behaviour no test compared. A Python branch only a unit test reached
is behaviour no test compared either. Either fails the check with the uncovered source.
"""

import bisect
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field

from coverage import Coverage

from tests.compat_scope import NOT_PORTED, PYTHON_ONLY
from tests.harness import HARNESS, V8_COVERAGE

ROOT = HARNESS.parent
SRC = ROOT / "src" / "zero_server"
OUT = HARNESS / "node_modules" / "@rocicorp" / "zero" / "out"
REPORT = HARNESS / "build" / "c8"
NODE_MODULES = HARNESS / "node_modules"
# A backticked upstream path, `packages/zql/src/query/query-registry.ts`, or a sibling named after one,
# `validate-input.ts`, which is in the same directory.
CITATION = re.compile(r"`(?:packages/([a-z0-9-]+/src/[\w/.-]+?\.ts)|([\w.-]+\.ts))`")
# A dependency of the npm package and one of its files: `@badrap/valita` 0.3.11 (`src/index.ts`).
DEPENDENCY = re.compile(r"`(@[\w-]+/[\w.-]+)`\s+[\d.]+\s+\(`([\w/.-]+)`\)")
DIFFERENTIAL = "tests/wire/"


def cited_modules() -> set[str]:
    """Upstream modules the port says it was ported from, as `package/src/path.ts`."""
    cited: set[str] = set()
    for path in SRC.rglob("*.py"):
        directory = None
        for full, sibling in CITATION.findall(path.read_text()):
            if full:
                cited.add(full)
                directory = full.rsplit("/", 1)[0]
            elif directory is not None:
                cited.add(f"{directory}/{sibling}")
        cited.update(f"{package}/{file}" for package, file in DEPENDENCY.findall(path.read_text()))
    return cited


def is_dependency(module: str) -> bool:
    return module.startswith("@")


def upstream_source(module: str) -> list[str]:
    if is_dependency(module):
        return (NODE_MODULES / module).read_text().split("\n")
    source_map = json.loads((OUT / module).with_suffix(".js.map").read_text())
    return source_map["sourcesContent"][0].split("\n")


@dataclass
class Gaps:
    """Uncovered lines and branches of one module, each a 1-based line number."""

    lines: set[int] = field(default_factory=set)
    branches: set[int] = field(default_factory=set)

    def __bool__(self) -> bool:
        return bool(self.lines or self.branches)


def remap() -> dict[str, dict]:
    """c8's report of the harness's V8 coverage, keyed by `package/src/path.ts`."""
    if not V8_COVERAGE.exists() or not any(V8_COVERAGE.iterdir()):
        sys.exit(f"no V8 coverage in {V8_COVERAGE.relative_to(ROOT)}: run pytest first")
    subprocess.run(
        [
            "node",
            "node_modules/c8/bin/c8.js",
            "report",
            f"--temp-directory={V8_COVERAGE}",
            f"--reports-dir={REPORT}",
            "--reporter=json",
            "--include=node_modules/@rocicorp/zero/**",
            "--include=node_modules/@badrap/**",
            "--include=node_modules/@databases/**",
            "--exclude-node-modules=false",
            "--exclude-after-remap=false",
        ],
        cwd=HARNESS,
        check=True,
    )
    report = json.loads((REPORT / "coverage-final.json").read_text())
    # The source maps resolve to `node_modules/@rocicorp/<package>/src/...`, one level above `zero/out`;
    # a dependency is keyed by its own path in `node_modules`.
    return {key.split("node_modules/", 1)[1].removeprefix("@rocicorp/"): data for key, data in report.items()}


def from_v8(module: str) -> dict | None:
    """Coverage of a plain JavaScript file straight from V8, in c8's shape, for a file c8 drops because its
    source map names TypeScript the package does not ship.

    Every non-blank line is a statement, counted by the innermost block around its first character, and
    every block within a function is a branch.
    """
    path = NODE_MODULES / module
    url = f"file://{path}"
    functions = [
        fn
        for dump in V8_COVERAGE.glob("*.json")
        for script in json.loads(dump.read_text())["result"]
        if script["url"] == url
        for fn in script["functions"]
    ]
    if not functions:
        return None
    text = path.read_text()
    starts = [0, *(i + 1 for i, char in enumerate(text) if char == "\n")]

    def line(offset: int) -> int:
        return bisect.bisect_right(starts, offset)

    def count(offset: int) -> int:
        enclosing = [r for fn in functions for r in fn["ranges"] if r["startOffset"] <= offset < r["endOffset"]]
        return min(enclosing, key=lambda r: r["endOffset"] - r["startOffset"])["count"] if enclosing else 1

    data: dict = {"s": {}, "statementMap": {}, "b": {}, "branchMap": {}, "fnMap": {}}
    for n, start in enumerate(starts):
        source = text[start : starts[n + 1] if n + 1 < len(starts) else len(text)]
        # A line of only punctuation, such as a closing brace, is not a statement.
        if source.strip(" \t\n{}()[];,") and not source.strip().startswith(("//", "*", "/*")):
            first = start + len(source) - len(source.lstrip())
            data["s"][str(n)] = count(first)
            data["statementMap"][str(n)] = {"start": {"line": n + 1}}
    for i, fn in enumerate(functions):
        whole = fn["ranges"][0]
        loc = {"start": {"line": line(whole["startOffset"])}, "end": {"line": line(whole["endOffset"] - 1)}}
        data["fnMap"][str(i)] = {"name": fn["functionName"], "loc": loc}
        for j, block in enumerate(fn["ranges"][1:]):
            data["b"][f"{i}.{j}"] = [block["count"]]
            data["branchMap"][f"{i}.{j}"] = {"loc": {"start": {"line": line(block["startOffset"])}}}
    return data


def uncovered(data: dict) -> Gaps:
    gaps = Gaps()
    for i, count in data["s"].items():
        if count == 0:
            gaps.lines.add(data["statementMap"][i]["start"]["line"])
    for i, counts in data["b"].items():
        if 0 in counts:
            gaps.branches.add(data["branchMap"][i]["loc"]["start"]["line"])
    return gaps


def regions(module: str, data: dict | None) -> dict[str, tuple[int, int]]:
    """Each excluded region of `module` as a line range: a function by name, or `"12-30"`."""
    functions: dict[str, list[tuple[int, int]]] = {}
    for fn in (data or {}).get("fnMap", {}).values():
        functions.setdefault(fn["name"], []).append((fn["loc"]["start"]["line"], fn["loc"]["end"]["line"]))
    resolved: dict[str, tuple[int, int]] = {}
    for region in NOT_PORTED.get(module, {}):
        if match := re.fullmatch(r"(\d+)-(\d+)", region):
            resolved[region] = (int(match[1]), int(match[2]))
        elif len(functions.get(region, [])) == 1:
            resolved[region] = functions[region][0]
        else:
            sys.exit(f"{module}: {region!r} names {len(functions.get(region, []))} functions, not one")
    return resolved


def check_upstream() -> list[str]:
    report = remap()
    failures: list[str] = []
    scope = cited_modules()
    for module in sorted(set(NOT_PORTED) - scope):
        failures.append(f"{module}: excluded in compat_scope.py but no port cites it")
    for module in sorted(scope):
        if not is_dependency(module) and not (OUT / module).with_suffix(".js").exists():
            continue  # types only: nothing runs
        data = report.get(module)
        if data is None and is_dependency(module):
            data = from_v8(module)
        if "*" in NOT_PORTED.get(module, {}):
            continue
        if data is None:
            failures.append(f"{module}: never loaded by the differential tests")
            continue
        gaps = uncovered(data)
        source = upstream_source(module)
        for region, (start, end) in regions(module, data).items():
            inside = set(range(start, end + 1))
            if not inside & (gaps.lines | gaps.branches):
                failures.append(f"{module}: excluded region {region} ({start}-{end}) is covered; drop the exclusion")
            gaps.lines -= inside
            gaps.branches -= inside
        if gaps:
            lines = sorted(gaps.lines | gaps.branches)
            shown = "\n".join(f"    {n:5} {'B' if n in gaps.branches else ' '} {source[n - 1]}" for n in lines)
            failures.append(f"{module}: {len(gaps.lines)} lines and {len(gaps.branches)} branches uncovered\n{shown}")
    return failures


PYTHON_ONLY_MARK = r"#\s*python-only: \S"
"""Marks a line, and the block it opens, as behaviour with no upstream counterpart. The reason follows."""


def check_port() -> list[str]:
    cov = Coverage(data_file=ROOT / ".coverage")
    cov.exclude(PYTHON_ONLY_MARK)
    cov.load()
    if not any(DIFFERENTIAL in c for c in cov.get_data().measured_contexts()):
        sys.exit("no per-test contexts in .coverage: run pytest, whose addopts set --cov-context=test")
    report = REPORT.parent / "port.json"
    cov.json_report(
        outfile=str(report),
        # The empty context is import time, which runs definitions, not behaviour.
        contexts=[re.escape(DIFFERENTIAL), "^$"],
        omit=[str(SRC / module) for module in PYTHON_ONLY],
        show_contexts=False,
    )
    failures: list[str] = []
    for path, data in json.loads(report.read_text())["files"].items():
        lines, branches = data["missing_lines"], data["missing_branches"]
        if lines or branches:
            failures.append(f"{path}: lines {lines} and branches {branches} reached by no differential test")
    for module in sorted(PYTHON_ONLY):
        if not (SRC / module).exists():
            failures.append(f"{module}: listed in PYTHON_ONLY but does not exist")
    return failures


def main() -> None:
    failures = [*check_upstream(), *check_port()]
    for failure in failures:
        print(failure, file=sys.stderr)
    if failures:
        sys.exit(1)
    print(f"compat: {len(cited_modules())} upstream modules and every port module fully exercised differentially")


if __name__ == "__main__":
    main()
