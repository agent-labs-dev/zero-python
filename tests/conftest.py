import os
from collections.abc import Iterator

import pytest
from hypothesis import HealthCheck, settings

from tests.harness import Harness

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


@pytest.fixture(scope="session")
def harness() -> Iterator[Harness]:
    h = Harness()
    yield h
    h.close()
