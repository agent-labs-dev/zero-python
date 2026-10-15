"""The paired runner's ratio per case and its markdown table."""

import statistics
from dataclasses import dataclass

type Rounds = dict[tuple[str, str], list[float]]
"""Per `(case, impl)`, the time in microseconds from each round."""

TS_IMPLS = {"node", "postgres.js"}


@dataclass
class Row:
    """One Python implementation of a case against the TypeScript one."""

    case: str
    ts_impl: str
    ts: list[float]
    py_impl: str
    py: list[float]

    @property
    def ratios(self) -> list[float]:
        """Each round's Python/TS ratio. A round's two sides ran within milliseconds of each other."""
        return [p / t for p, t in zip(self.py, self.ts, strict=True)]

    @property
    def ratio(self) -> float:
        return statistics.median(self.ratios)


def pair(rounds: Rounds) -> list[Row]:
    ts = {case: (impl, times) for (case, impl), times in rounds.items() if impl in TS_IMPLS}
    return [
        Row(case, ts[case][0], ts[case][1], impl, times)
        for (case, impl), times in rounds.items()
        if impl not in TS_IMPLS and case in ts
    ]


def fmt(us: float) -> str:
    return f"{us / 1000:.2f} ms" if us >= 1000 else f"{us:.1f} µs"


def markdown(rows: list[Row]) -> str:
    lines = [
        "| case | TS | TS median | Python | Python median | Py/TS | rounds' Py/TS |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        spread = f"{min(r.ratios):.2f}-{max(r.ratios):.2f}"
        cells = [r.case, r.ts_impl, fmt(statistics.median(r.ts)), r.py_impl, fmt(statistics.median(r.py))]
        lines.append("| " + " | ".join([*cells, f"{r.ratio:.2f}", spread]) + " |")
    return "\n".join(lines) + "\n"
