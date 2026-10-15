import importlib
import os
import pkgutil
from collections.abc import Iterator

import pytest
from hypothesis import HealthCheck, settings

import zero_server
from tests.harness import Harness

# Every module's definitions run here, at import, outside any test's coverage context, so `tests/compat.py`
# credits a function body to the tests that call it and not to whichever test imported it first.
for module in pkgutil.walk_packages(zero_server.__path__, "zero_server."):
    importlib.import_module(module.name)

# Derandomized so coverage, which the suite holds at 100%, does not move between runs.
settings.register_profile(
    "zero",
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
# Random and longer, for hunting divergences by hand: ZERO_HYPOTHESIS_PROFILE=explore.
settings.register_profile("explore", parent=settings.get_profile("zero"), derandomize=False, max_examples=2000)
settings.load_profile(os.environ.get("ZERO_HYPOTHESIS_PROFILE", "zero"))


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """The differential tests first, so a cached function's one run is credited to them (`tests/compat.py`)."""
    items.sort(key=lambda item: "tests/wire/" not in item.nodeid)


@pytest.fixture(scope="session")
def harness() -> Iterator[Harness]:
    h = Harness()
    yield h
    h.close()
