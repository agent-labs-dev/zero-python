"""The benchmark tier's ratio gate and baseline ratchet (`bench/report.py`)."""

from bench.report import gate, markdown, pair, ratchet

ROUNDS = {
    ("q", "node"): [10.0, 12.0, 11.0],
    ("q", "python"): [20.0, 23.0, 22.0],
    ("run", "postgres.js"): [100.0],
    ("run", "asyncpg"): [90.0],
    ("run", "psycopg"): [130.0],
    ("orphan", "python"): [1.0],
}
BASELINE = {"tolerance": {"": 0.1, "run": 0.5}, "ratios": {"q python": 1.9, "run psycopg": 1.0}}


def test_pairs_each_python_impl_with_the_ts_one() -> None:
    rows = {r.key: r.ratio for r in pair(ROUNDS)}
    assert rows == {"q python": 2.0, "run asyncpg": 0.9, "run psycopg": 1.3}


def test_gate_uses_the_longest_tolerance_prefix_and_passes_new_cases() -> None:
    verdicts = {v.row.key: v for v in gate(pair(ROUNDS), BASELINE)}
    assert not verdicts["q python"].failed  # 2.0 <= 1.9 * 1.1
    assert not verdicts["run psycopg"].failed  # 1.3 <= 1.0 * 1.5
    assert verdicts["run asyncpg"].baseline is None
    assert not verdicts["run asyncpg"].failed
    worse = {**BASELINE, "ratios": {**BASELINE["ratios"], "q python": 1.8}}
    assert [v.row.key for v in gate(pair(ROUNDS), worse) if v.failed] == ["q python"]
    assert "| FAIL |" in markdown(gate(pair(ROUNDS), worse))


def test_ratchet_only_lowers_unless_forced() -> None:
    rows = pair(ROUNDS)
    assert ratchet(rows, BASELINE)["ratios"] == {"q python": 1.9, "run asyncpg": 0.9, "run psycopg": 1.0}
    assert ratchet(rows, BASELINE, force=True)["ratios"] == {"q python": 2.0, "run asyncpg": 0.9, "run psycopg": 1.3}
