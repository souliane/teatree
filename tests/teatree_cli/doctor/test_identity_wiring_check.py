"""`t3 doctor check` must FAIL on the identity wiring, not report it green (#4241 follow-up).

The whole point of the check is that the pre-fix deployment looked healthy: every other check
passed while the merge keystone could admit nobody and the MRs came out under the owner. So the
assertions are on the RETURN — the value that gates the exit code — not only on the printed lines.
"""

from typing import cast
from unittest.mock import MagicMock, patch

from teatree.cli.doctor.checks_identity_wiring import _authoring_faults, _authors_by_ref, check_identity_wiring
from teatree.config.settings import UserSettings
from teatree.core.identity_wiring import IdentityFault
from teatree.core.overlay import OverlayBase, OverlayConfig

_SETTINGS = "teatree.config.get_effective_settings"
_AUTHORING = "teatree.cli.doctor.checks_identity_wiring._authoring_faults"
_OPEN_MRS = "teatree.cli.doctor.checks_identity_wiring._open_mr_fault"


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


class _Config(OverlayConfig):
    """A config whose overlay-wide and per-remote GitLab credentials are both settable."""

    def __init__(self, *, owner: str = "owner-token", scoped: str = "") -> None:
        super().__init__()
        self._owner = owner
        self._scoped = scoped

    def get_gitlab_token(self) -> str:
        return self._owner

    def get_gitlab_token_for_remote(self, remote: str) -> str:
        del remote
        return self._scoped


def _overlay(config: OverlayConfig) -> OverlayBase:
    overlay = MagicMock(spec=OverlayBase)
    overlay.config = config
    return cast("OverlayBase", overlay)


def _faults(
    ambient: OverlayConfig, *, registry: dict[str, OverlayBase], open_mrs: IdentityFault | None = None
) -> list[IdentityFault]:
    # The open-MR arm is stubbed: it reaches the forge, and this helper's repo paths are fictional.
    with (
        patch("teatree.cli.update._collect_repos", return_value=[("a", "/a"), ("b", "/b")]),
        patch("teatree.core.backend_factory.get_overlay", return_value=_overlay(ambient)),
        patch("teatree.utils.git.remote_url", return_value="git@host:x/y.git"),
        patch("teatree.core.authoring_credential.get_all_overlays", return_value=registry),
        patch(_OPEN_MRS, return_value=open_mrs),
    ):
        return _authoring_faults()


class TestAuthoringFaultsAreDeduplicatedByRemote:
    """Two clones of one remote share one credential answer, so they must not report twice."""

    def test_one_fault_per_remote(self) -> None:
        ambient = _Config(scoped="")
        assert len(_faults(ambient, registry={})) == 1


class TestAuthoringFaultsAreRepoKeyedNotAmbient:
    """The probe reads every registered overlay's declaration, not just the ambient one's.

    An ambient-only read reports a clean bill on precisely the repo whose author is wrong: the
    overlay that DECLARES the bot is not the one the entrypoint happens to run under, so its
    unreachable credential is invisible and every MR is opened by the owner — the one identity
    the forge then refuses an approval from.
    """

    def test_another_overlays_unreachable_declaration_is_reported(self) -> None:
        ambient = _Config(scoped="owner-token")
        declaring = _Config(scoped="")
        assert _faults(ambient, registry={"other": _overlay(declaring)})

    def test_another_overlays_resolvable_declaration_is_no_fault(self) -> None:
        ambient = _Config(scoped="owner-token")
        declaring = _Config(scoped="bot-token")
        assert _faults(ambient, registry={"other": _overlay(declaring)}) == []

    def test_a_repo_nobody_declares_is_no_fault(self) -> None:
        ambient = _Config(scoped="owner-token")
        assert _faults(ambient, registry={"other": _overlay(_Config(scoped="owner-token"))}) == []


class TestAlreadyOpenUnapprovableMrsAreReported:
    """The create-time refusal cannot reach an MR that already exists, so the doctor names those.

    An MR opened before that refusal existed — or through the web UI — sits open looking healthy
    and answers an approval with 401. That is the state this whole check exists to surface early.
    """

    _RESOLVABLE = _Config(scoped="bot-token")

    def test_a_declared_remote_with_an_owner_authored_open_mr_fails(self) -> None:
        fault = IdentityFault(summary="git@host:x/y.git has 1 OPEN merge request(s)", remedy="re-open it")
        faults = _faults(_Config(scoped="owner-token"), registry={"other": _overlay(self._RESOLVABLE)}, open_mrs=fault)

        assert faults == [fault]

    def test_a_declared_remote_with_no_unapprovable_mr_is_clean(self) -> None:
        faults = _faults(_Config(scoped="owner-token"), registry={"other": _overlay(self._RESOLVABLE)}, open_mrs=None)

        assert faults == []

    def test_a_remote_nobody_declares_never_reaches_the_forge(self) -> None:
        # An ordinary repo the owner authors himself would report every one of its MRs.
        with patch(_OPEN_MRS, side_effect=AssertionError("the open-MR read must not run here")):
            assert _authoring_faults_for_undeclared_remote() == []


def _authoring_faults_for_undeclared_remote() -> list[IdentityFault]:
    ambient = _Config(scoped="owner-token")
    with (
        patch("teatree.cli.update._collect_repos", return_value=[("a", "/a")]),
        patch("teatree.core.backend_factory.get_overlay", return_value=_overlay(ambient)),
        patch("teatree.utils.git.remote_url", return_value="git@host:x/y.git"),
        patch("teatree.core.authoring_credential.get_all_overlays", return_value={}),
    ):
        return _authoring_faults()


class TestAuthorsByRef:
    """The forge payload is reduced to ``{reference: author}``; a payload without one is dropped."""

    def test_the_web_url_is_the_reference_when_the_payload_carries_one(self) -> None:
        payloads = [{"web_url": "https://forge/x/y/-/merge_requests/7", "author": {"username": "the-owner"}}]

        assert _authors_by_ref(payloads) == {"https://forge/x/y/-/merge_requests/7": "the-owner"}

    def test_the_iid_is_the_fallback_reference(self) -> None:
        assert _authors_by_ref([{"iid": 9, "author": {"username": "the-bot"}}]) == {"!9": "the-bot"}

    def test_a_payload_with_no_readable_author_is_dropped(self) -> None:
        assert _authors_by_ref([{"iid": 9}, {"iid": 10, "author": {}}, {"iid": 11, "author": "nope"}]) == {}
