"""Tests for ``teatree.utils.env.patched_environ`` — scoped os.environ mutation."""

import os
from unittest.mock import patch

import pytest

from teatree.utils.env import patched_environ

_PROBE = "T3_PATCHED_ENVIRON_PROBE"


def test_override_applied_inside_and_restored_after() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop(_PROBE, None)
        with patched_environ({_PROBE: "inside"}):
            assert os.environ[_PROBE] == "inside"
        assert _PROBE not in os.environ


def test_pre_existing_value_is_restored_not_dropped() -> None:
    with patch.dict(os.environ, {_PROBE: "original"}, clear=False):
        with patched_environ({_PROBE: "temporary"}):
            assert os.environ[_PROBE] == "temporary"
        assert os.environ[_PROBE] == "original"


def test_removed_key_is_dropped_inside_and_restored_after() -> None:
    with patch.dict(os.environ, {_PROBE: "present"}, clear=False):
        with patched_environ({}, remove=(_PROBE,)):
            assert _PROBE not in os.environ
        assert os.environ[_PROBE] == "present"


def test_state_restored_even_when_block_raises() -> None:
    boom = "boom"
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop(_PROBE, None)
        with pytest.raises(RuntimeError), patched_environ({_PROBE: "inside"}):
            raise RuntimeError(boom)
        assert _PROBE not in os.environ


def test_unrelated_key_survives() -> None:
    other = "T3_PATCHED_ENVIRON_UNRELATED"
    with patch.dict(os.environ, {other: "kept"}, clear=False):
        with patched_environ({_PROBE: "inside"}, remove=("VIRTUAL_ENV",)):
            assert os.environ[other] == "kept"
        assert os.environ[other] == "kept"
