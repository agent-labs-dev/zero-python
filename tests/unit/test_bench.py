"""The benchmark tier's gate and baseline update (`bench/report.py`)."""

import pytest

from bench.report import combine, gate, markdown, pair, update


def shard(q: float, run: float) -> list:
    """A shard whose `q` and `run psycopg` ratios are `q` and `run` in every round."""
    return pair(
        {
            ("q", "node"): [10.0, 12.0, 11.0],
            ("q", "python"): [10.0 * q, 12.0 * q, 11.0 * q],
            ("run", "postgres.js"): [100.0],
            ("run", "psycopg"): [100.0 * run],
            ("orphan", "python"): [1.0],
        }
    )


BASELINE = {"tolerance": {"": 0.1, "run": 0.5}, "ratios": {"q python": 2.0}}


def test_pairs_each_python_impl_with_the_ts_one_and_takes_the_median_of_round_ratios() -> None:
    rows = pair({("q", "node"): [10.0, 10.0, 10.0], ("q", "python"): [20.0, 90.0, 21.0]})
    assert [(r.key, r.ratio) for r in rows] == [("q python", 2.1)]


def test_the_gate_takes_the_median_shard_and_the_longest_tolerance_prefix() -> None:
    shards = [shard(2.1, 1.0), shard(9.0, 1.0), shard(2.2, 1.0)]
    verdicts = {v.case.key: v for v in gate(combine(shards), BASELINE)}
    assert verdicts["q python"].case.ratio == pytest.approx(2.2)
    assert not verdicts["q python"].failed  # 2.2 <= 2.0 * 1.1
    assert verdicts["run psycopg"].baseline is None
    assert not verdicts["run psycopg"].failed

    worse = gate(combine([shard(2.3, 1.0)]), BASELINE)
    assert [v.case.key for v in worse if v.failed] == ["q python"]
    assert "| FAIL |" in markdown(worse)


def test_update_lowers_a_ratio_only_when_every_shard_beat_it() -> None:
    lucky = [shard(1.5, 1.0), shard(2.5, 1.0), shard(1.6, 1.0)]
    assert update(BASELINE, lucky)["ratios"] == {"q python": 2.0, "run psycopg": 1.0}
    better = [shard(1.5, 1.0), shard(1.8, 1.2), shard(1.6, 1.1)]
    assert update(BASELINE, better)["ratios"] == {"q python": 1.8, "run psycopg": 1.2}
    assert update(BASELINE, lucky, force=True)["ratios"] == {"q python": 2.5, "run psycopg": 1.0}


def test_update_needs_three_shards() -> None:
    with pytest.raises(ValueError, match="at least 3 measurements of each case: q python, run psycopg"):
        update(BASELINE, [shard(1.0, 1.0)])
