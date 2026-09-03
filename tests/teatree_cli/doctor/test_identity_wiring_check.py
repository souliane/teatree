"""`t3 doctor check` must FAIL on the identity wiring, not report it green (#4241 follow-up).

The whole point of the check is that the pre-fix deployment looked healthy: every other check
passed while the merge keystone could admit nobody and the MRs came out under the owner. So the
assertions are on the RETURN — the value that gates the exit code — not only on the printed lines.
"""

from unittest.mock import patch

from teatree.cli.doctor.checks_identity_wiring import _authoring_faults, check_identity_wiring
from teatree.config.settings import UserSettings
from teatree.core.identity_wiring import AuthoringIdentity, IdentityFault

_SETTINGS = "teatree.config.get_effective_settings"
_AUTHORING = "teatree.cli.doctor.checks_identity_wiring._authoring_faults"


class TestReviewerAdmissionHalf:
    """An empty allowlist is the state this deployment shipped in, and it must fail the run."""

    def test_fails_when_both_allowlists_are_empty(self, capsys) -> None:
        with (
            patch(_SETTINGS, return_value=UserSettings()),
            patch(_AUTHORING, return_value=[]),
        ):
            assert check_identity_wiring() is False
        assert "merge keystone" in capsys.readouterr().out

    def test_passes_once_an_identity_is_configured(self) -> None:
        settings = UserSettings(independent_reviewer_identities=["owner-handle"])
        with patch(_SETTINGS, return_value=settings), patch(_AUTHORING, return_value=[]):
            assert check_identity_wiring() is True

    def test_the_owner_alias_tier_satisfies_it_too(self) -> None:
        settings = UserSettings(user_identity_aliases=["owner-handle"])
        with patch(_SETTINGS, return_value=settings), patch(_AUTHORING, return_value=[]):
            assert check_identity_wiring() is True


class TestAuthoringHalf:
    """A declared bot credential this venue cannot resolve fails the run on its own."""

    def test_an_unresolvable_authoring_credential_fails(self, capsys) -> None:
        settings = UserSettings(independent_reviewer_identities=["owner-handle"])
        fault = IdentityFault(summary="git@host:x/y.git cannot author", remedy="provision it")
        with patch(_SETTINGS, return_value=settings), patch(_AUTHORING, return_value=[fault]):
            assert check_identity_wiring() is False
        assert "cannot author" in capsys.readouterr().out

    def test_both_faults_are_reported_together(self, capsys) -> None:
        fault = IdentityFault(summary="git@host:x/y.git cannot author", remedy="provision it")
        with patch(_SETTINGS, return_value=UserSettings()), patch(_AUTHORING, return_value=[fault]):
            assert check_identity_wiring() is False
        out = capsys.readouterr().out
        assert "merge keystone" in out
        assert "cannot author" in out


class TestCrashPosture:
    """A broken probe must not masquerade as a configuration fault the operator would chase."""

    def test_a_raising_probe_warns_and_does_not_fail_the_run(self, capsys) -> None:
        with patch(_SETTINGS, side_effect=RuntimeError("control db unreachable")):
            assert check_identity_wiring() is True
        assert "WARN" in capsys.readouterr().out


class TestAuthoringFaultsAreDeduplicatedByRemote:
    """Two clones of one remote share one credential answer, so they must not report twice."""

    def test_one_fault_per_remote(self) -> None:
        config = type(
            "_Config", (), {"authoring_identity_on": staticmethod(lambda _remote: AuthoringIdentity.UNRESOLVABLE)}
        )()
        overlay = type("_Overlay", (), {"config": config})()
        with (
            patch("teatree.cli.update._collect_repos", return_value=[("a", "/a"), ("b", "/b")]),
            patch("teatree.core.backend_factory.get_overlay", return_value=overlay),
            patch("teatree.utils.git.remote_url", return_value="git@host:x/y.git"),
        ):
            assert len(_authoring_faults()) == 1
