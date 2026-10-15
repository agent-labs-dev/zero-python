"""Ratios from rounds, the gate against `baseline.json`, the baseline update, and the markdown table.

A shard is one full run on one machine: several rounds, each timing every case on both sides. CI runs
shards on separate runners and gates on their combination.
"""

import statistics
from dataclasses import dataclass
from typing import Any

type Rounds = dict[tuple[str, str], list[float]]
"""Per `(case, impl)`, the median time in microseconds from each round."""

TS_IMPLS = {"node", "postgres.js"}


@dataclass
class Row:
    """One Python implementation of a case against the TypeScript one, in one shard."""

    case: str
    ts_impl: str
    ts: list[float]
    py_impl: str
    py: list[float]

    @property
    def key(self) -> str:
        return f"{self.case} {self.py_impl}"

    @property
    def ratio(self) -> float:
        """The median over rounds of each round's ratio: a round's two sides ran seconds apart."""
        return statistics.median(p / t for p, t in zip(self.py, self.ts, strict=True))


def pair(rounds: Rounds) -> list[Row]:
    ts = {case: (impl, times) for (case, impl), times in rounds.items() if impl in TS_IMPLS}
    return [
        Row(case, ts[case][0], ts[case][1], impl, times)
        for (case, impl), times in rounds.items()
        if impl not in TS_IMPLS and case in ts
    ]


@dataclass
class Combined:
    """A case across shards. Its ratio is the median of the shards' ratios."""

    shards: list[Row]

    @property
    def key(self) -> str:
        return self.shards[0].key

    @property
    def ratio(self) -> float:
        return statistics.median(r.ratio for r in self.shards)


def combine(shards: list[list[Row]]) -> list[Combined]:
    by_key: dict[str, list[Row]] = {}
    for rows in shards:
        for row in rows:
            by_key.setdefault(row.key, []).append(row)
    return [Combined(rows) for rows in by_key.values()]


def tolerance(baseline: dict[str, Any], key: str) -> float:
    """The longest matching prefix in `baseline["tolerance"]` wins; `""` is the default."""
    table: dict[str, float] = baseline["tolerance"]
    return table[max((p for p in table if key.startswith(p)), key=len)]


@dataclass
class Verdict:
    case: Combined
    baseline: float | None
    limit: float | None

    @property
    def failed(self) -> bool:
        return self.limit is not None and self.case.ratio > self.limit


def gate(cases: list[Combined], baseline: dict[str, Any]) -> list[Verdict]:
    """A case fails when its ratio exceeds its baseline by more than its tolerance. A case with no
    baseline passes, so a new case, or a first run on a new runner class, records rather than fails."""
    out = []
    for case in cases:
        base = baseline["ratios"].get(case.key)
        limit = None if base is None else base * (1 + tolerance(baseline, case.key))
        out.append(Verdict(case, base, limit))
    return out


MIN_SHARDS = 3


def update(baseline: dict[str, Any], shards: list[list[Row]], force: bool = False) -> dict[str, Any]:
    """The baseline from at least `MIN_SHARDS` shards, taking each case's worst shard ratio.

    A case's ratio only goes down, and only when every shard beat it: one lucky run cannot lower it. A
    case the baseline lacks is added. `force` replaces every ratio, for a deliberate regression.
    """
    if len(shards) < MIN_SHARDS:
        raise ValueError(f"a baseline needs at least {MIN_SHARDS} shards, got {len(shards)}")
    ratios = dict(baseline["ratios"])
    for case in combine(shards):
        worst = round(max(r.ratio for r in case.shards), 4)
        old = ratios.get(case.key)
        if force or old is None or worst < old:
            ratios[case.key] = worst
    return {**baseline, "ratios": dict(sorted(ratios.items()))}


def fmt(us: float) -> str:
    return f"{us / 1000:.2f} ms" if us >= 1000 else f"{us:.1f} µs"


def markdown(verdicts: list[Verdict]) -> str:
    """Times are the median over every round of every shard; Py/TS lists each shard's ratio too."""
    lines = [
        "| case | TS | TS median | Python | Python median | Py/TS | per shard | baseline | limit | gate |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for v in verdicts:
        first = v.case.shards[0]
        ts = statistics.median(t for r in v.case.shards for t in r.ts)
        py = statistics.median(t for r in v.case.shards for t in r.py)
        cells = [
            first.case,
            first.ts_impl,
            fmt(ts),
            first.py_impl,
            fmt(py),
            f"{v.case.ratio:.2f}",
            " ".join(f"{r.ratio:.2f}" for r in v.case.shards),
            "new" if v.baseline is None else f"{v.baseline:.2f}",
            "" if v.limit is None else f"{v.limit:.2f}",
            "FAIL" if v.failed else "ok",
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"
