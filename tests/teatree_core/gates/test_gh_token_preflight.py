"""Unit tests for the GitHub token permission probe (#3405).

The only boundary faked is the ``gh`` runner (an unstoppable external subprocess /
network); the probe's classification logic runs for real against modelled ``gh
api`` outputs.
"""

from unittest.mock import patch

from teatree.core.gates.gh_token_preflight import REQUIRED_PERMISSION_LABELS, probe_token_permissions

_SLUG = "souliane/teatree"
_NOT_ACCESSIBLE = '{"message":"Resource not accessible by personal access token"}'
_NOT_FOUND = '{"message":"Not Found"}'


def _runner(responses: dict[str, tuple[int, str]]):
    """A fake gh runner keyed by a distinctive substring of the endpoint arg.

    The metadata read (no ``--method``) is matched by its ``repos/<slug>`` needle;
    write probes (``--method`` present) are matched by their resource needle so
    the metadata needle — a substring of every write endpoint — never shadows them.
    """

    def run(args: list[str]) -> tuple[int, str]:
        joined = " ".join(args)
        is_write = "--method" in args
        for needle, outcome in responses.items():
            if needle not in joined:
                continue
            if is_write == (needle != f"repos/{_SLUG}"):
                return outcome
        return (0, "{}")

    return run


class TestProbeTokenPermissions:
    def test_all_present_when_writes_404(self) -> None:
        # A permitted token gets 404 (resource id 0 doesn't exist) on every write.
        run = _runner(
            {
                "repos/souliane/teatree": (0, "{}"),
                "issues/0": (1, _NOT_FOUND),
                "pulls/0": (1, _NOT_FOUND),
                "refs/heads": (1, _NOT_FOUND),
            }
        )
        with patch("teatree.core.gates.gh_token_preflight.shutil.which", return_value="/usr/bin/gh"):
            probe = probe_token_permissions(_SLUG, run=run)
        assert probe.ok
        assert probe.missing == ()

    def test_missing_issues_write_detected(self) -> None:
        run = _runner(
            {
                "repos/souliane/teatree": (0, "{}"),
                "issues/0": (1, _NOT_ACCESSIBLE),
                "pulls/0": (1, _NOT_FOUND),
                "refs/heads": (1, _NOT_FOUND),
            }
        )
        with patch("teatree.core.gates.gh_token_preflight.shutil.which", return_value="/usr/bin/gh"):
            probe = probe_token_permissions(_SLUG, run=run)
        assert probe.missing == ("issues: write",)
        assert not probe.ok

    def test_multiple_missing_reported_in_order(self) -> None:
        run = _runner(
            {
                "repos/souliane/teatree": (0, "{}"),
                "issues/0": (1, _NOT_ACCESSIBLE),
                "pulls/0": (1, _NOT_ACCESSIBLE),
                "refs/heads": (1, _NOT_FOUND),
            }
        )
        with patch("teatree.core.gates.gh_token_preflight.shutil.which", return_value="/usr/bin/gh"):
            probe = probe_token_permissions(_SLUG, run=run)
        assert probe.missing == ("issues: write", "pull_requests: write")

    def test_metadata_denied_short_circuits(self) -> None:
        # A token that cannot even read the repo: the write probes are meaningless.
        run = _runner({"repos/souliane/teatree": (1, _NOT_ACCESSIBLE)})
        with patch("teatree.core.gates.gh_token_preflight.shutil.which", return_value="/usr/bin/gh"):
            probe = probe_token_permissions(_SLUG, run=run)
        assert probe.missing == ("metadata: read",)

    def test_metadata_network_failure_is_indeterminate(self) -> None:
        # A non-permission failure (network) must not be read as a missing scope.
        run = _runner({"repos/souliane/teatree": (1, "connection reset by peer")})
        with patch("teatree.core.gates.gh_token_preflight.shutil.which", return_value="/usr/bin/gh"):
            probe = probe_token_permissions(_SLUG, run=run)
        assert probe.missing == ()
        assert probe.indeterminate_reason is not None
        assert not probe.ok

    def test_gh_absent_is_indeterminate(self) -> None:
        with patch("teatree.core.gates.gh_token_preflight.shutil.which", return_value=None):
            probe = probe_token_permissions(_SLUG, run=_runner({}))
        assert probe.indeterminate_reason is not None
        assert "gh" in probe.indeterminate_reason.lower()


class TestRequiredLabels:
    def test_labels_are_the_four_loop_permissions(self) -> None:
        assert REQUIRED_PERMISSION_LABELS == (
            "metadata: read",
            "issues: write",
            "pull_requests: write",
            "contents: write",
        )
