"""The CPU-only cases (`bench/cpu.py`) as CodSpeed benchmarks, named as `harness/bench/cpu.ts` names the
TypeScript side, so CodSpeed shows the two next to each other.

    uv run pytest bench --codspeed --no-cov

CodSpeed's simulation mode counts instructions rather than time, so CI catches a regression in any of
these without a tolerance. Locally the command only checks that each case runs.
"""

import asyncio
import inspect
from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from bench.cpu import cases

LOOP = asyncio.new_event_loop()
CASES = LOOP.run_until_complete(cases())


def _drive(fn: Callable[[], Coroutine[Any, Any, Any]]) -> Callable[[], Any]:
    """Run an async case to completion without the event loop, as long as it never waits on anything.

    None of them does: their database answers from memory. Each `run_until_complete` would otherwise
    add two trips through the loop's selector, which outweigh the smaller cases and hide their changes.
    """

    def run() -> Any:
        coroutine = fn()
        try:
            coroutine.send(None)
        except StopIteration as done:
            return done.value
        coroutine.close()
        raise RuntimeError("an async case waited on something, so it needs the event loop")

    return run


@pytest.mark.parametrize("name", CASES)
def test_cpu(benchmark: Any, name: str) -> None:
    fn = CASES[name]
    benchmark(_drive(fn) if inspect.iscoroutinefunction(fn) else fn)
