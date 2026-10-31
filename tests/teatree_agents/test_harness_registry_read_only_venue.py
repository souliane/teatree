"""The ``read_only_venue`` capability shapes a brief; it never admits a write-barring phase."""

from unittest.mock import patch

import pytest

from teatree.agents import harness_registry
from teatree.agents.harness_registry import (
    HarnessBuildContext,
    HarnessCapabilities,
    HarnessSpec,
    NoAvailableHarnessError,
    select_harness,
)


def test_the_read_only_venue_flag_never_admits_a_write_barring_phase() -> None:
    spec = HarnessSpec(
        name="venue_flag_test",
        factory=lambda context: pytest.fail("selection never builds"),
        capabilities=HarnessCapabilities(managed_lane=True, read_only_venue=True),
    )

    with patch.dict(harness_registry._REGISTRY, {spec.name: spec}), pytest.raises(NoAvailableHarnessError) as refused:
        select_harness([spec.name], HarnessBuildContext(phase="planning"))

    assert [rejection.reason for rejection in refused.value.rejected] == [
        "phase 'planning' bars write tools, which a managed-lane harness cannot enforce"
    ]
