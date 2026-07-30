"""The ``T3_EVAL_IN_CONTAINER`` marker (``teatree.utils.eval_container``).

The foundation-layer home both the interface-layer eval CLI (which SETS the
marker) and the domain-layer credential factory (which READS it) depend on —
see ``teatree.cli.eval.metered_routing`` and ``teatree.credential_config``.
"""

from unittest.mock import patch

from teatree.utils.eval_container import IN_CONTAINER_ENV_VAR, in_container

_MODULE = "teatree.utils.eval_container"


class TestInContainer:
    def test_true_when_marker_set(self) -> None:
        with patch(f"{_MODULE}.os.environ", {IN_CONTAINER_ENV_VAR: "1"}):
            assert in_container() is True

    def test_false_when_marker_absent(self) -> None:
        with patch(f"{_MODULE}.os.environ", {}):
            assert in_container() is False

    def test_false_when_marker_empty(self) -> None:
        with patch(f"{_MODULE}.os.environ", {IN_CONTAINER_ENV_VAR: ""}):
            assert in_container() is False
