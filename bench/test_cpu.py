"""The CPU-only cases (`bench/cpu.py`) as CodSpeed benchmarks, named as `harness/bench/cpu.ts` names the
TypeScript side, so CodSpeed shows the two next to each other.

    uv run pytest bench --codspeed --no-cov

CodSpeed's simulation mode counts instructions rather than time, so CI catches a regression in any of
these without a tolerance. Locally the command only checks that each case runs.
"""

import asyncio
import inspect
from typing import Any

import pytest

from bench.cpu import cases

LOOP = asyncio.new_event_loop()
CASES = LOOP.run_until_complete(cases())


@pytest.mark.parametrize("name", CASES)
def test_cpu(benchmark: Any, name: str) -> None:
    fn = CASES[name]
    benchmark((lambda: LOOP.run_until_complete(fn())) if inspect.iscoroutinefunction(fn) else fn)
