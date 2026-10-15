"""Aggregating rounds, the ratio gate against `baseline.json`, and the markdown table."""

import statistics
from dataclasses import dataclass
from typing import Any

type Rounds = dict[tuple[str, str], list[float]]
"""Per `(case, impl)`, the median time in microseconds from each round."""

TS_IMPLS = {"node", "postgres.js"}


@dataclass
class Row:
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
        """The median over rounds of each round's ratio: a round's two sides ran under the same load."""
        return statistics.median(p / t for p, t in zip(self.py, self.ts, strict=True))


def pair(rounds: Rounds) -> list[Row]:
    """One row per Python implementation of a case, against the TypeScript one."""
    ts = {case: (impl, times) for (case, impl), times in rounds.items() if impl in TS_IMPLS}
    rows = []
    for (case, impl), times in rounds.items():
        if impl not in TS_IMPLS and case in ts:
            rows.append(Row(case, ts[case][0], ts[case][1], impl, times))
    return rows


def tolerance(baseline: dict[str, Any], key: str) -> float:
    """The longest matching prefix in `baseline["tolerance"]` wins; `""` is the default."""
    table: dict[str, float] = baseline["tolerance"]
    return table[max((p for p in table if key.startswith(p)), key=len)]


@dataclass
class Verdict:
    row: Row
    baseline: float | None
    limit: float | None

    @property
    def failed(self) -> bool:
        return self.limit is not None and self.row.ratio > self.limit


def gate(rows: list[Row], baseline: dict[str, Any]) -> list[Verdict]:
    """A case fails when its Python/TS ratio exceeds its baseline by more than its tolerance. A case with
    no baseline passes, so a new case, or a first run on a new runner, records rather than fails."""
    out = []
    for row in rows:
        base = baseline["ratios"].get(row.key)
        limit = None if base is None else base * (1 + tolerance(baseline, row.key))
        out.append(Verdict(row, base, limit))
    return out


def ratchet(rows: list[Row], baseline: dict[str, Any], force: bool = False) -> dict[str, Any]:
    """The baseline with each case's ratio lowered to this run's where it improved, added where missing,
    and, with `force`, replaced outright."""
    ratios = dict(baseline["ratios"])
    for row in rows:
        old = ratios.get(row.key)
        if force or old is None or row.ratio < old:
            ratios[row.key] = round(row.ratio, 4)
    return {**baseline, "ratios": dict(sorted(ratios.items()))}


def fmt(us: float) -> str:
    return f"{us / 1000:.2f} ms" if us >= 1000 else f"{us:.1f} µs"


def spread(times: list[float]) -> str:
    return f"{fmt(statistics.median(times))} ({fmt(min(times))}-{fmt(max(times))})"


def markdown(verdicts: list[Verdict]) -> str:
    lines = [
        "| case | TS | TS median (min-max) | Python | Python median (min-max) | Py/TS | baseline | gate |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for v in verdicts:
        r = v.row
        base = "new" if v.baseline is None else f"{v.baseline:.2f}"
        status = "FAIL" if v.failed else "ok"
        cells = [r.case, r.ts_impl, spread(r.ts), r.py_impl, spread(r.py), f"{r.ratio:.2f}", base, status]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"
