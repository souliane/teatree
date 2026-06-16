"""Tests for :class:`CodexReviewScanner` — auto-dispatch ``/codex:review`` (#1254).

The scanner is the structural fix for the "user has to remember to run
``/codex:review`` after every push" failure mode encoded in the
``feedback_fleet_of_agents_with_codex_doublecheck`` binding. It runs
every tick, walks the configured repo list, and emits one
``codex_review.dispatch`` signal per open self-authored PR whose head SHA
the scanner hasn't seen before — keyed on ``(slug, pr_id, head_sha)``
via :class:`CodexReviewMarker`. Re-ticking on the same SHA is a no-op;
a force-push (new SHA) re-fires.

The scanner is the loop-level enforcement of the fleet-of-agents rule:
the user never has to ask "have you run codex on this?" again.
"""

from dataclasses import dataclass, field

import pytest

from teatree.core.models.codex_review_marker import CodexReviewMarker
from teatree.loop.scanners.base import ScannerError, ScannerErrorClass
from teatree.loop.scanners.codex_review import (
    ADVERSARIAL_REVIEW_VARIANT,
    STANDARD_REVIEW_VARIANT,
    CodexReviewScanner,
    GhCodexPrApi,
    PrSummary,
    _classify_gh_stderr,
    _decode_pr,
)

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


SLUG = "souliane/teatree"
HEAD = "feedfacecafebabe1234567890abcdef12345678"
NEW_HEAD = "1234567890abcdeffeedfacecafebabe87654321"
AUTH_HEAD = "abc1230000000000000000000000000000000000"


def _pr(
    *,
    pr_id: int = 1254,
    head: str = HEAD,
    is_draft: bool = False,
    changed_files: tuple[str, ...] = ("src/teatree/loop/scanners/codex_review.py",),
) -> PrSummary:
    return PrSummary(
        slug=SLUG,
        number=pr_id,
        head_sha=head,
        is_draft=is_draft,
        changed_files=changed_files,
        url=f"https://github.com/{SLUG}/pull/{pr_id}",
        title=f"PR {pr_id}",
    )


@dataclass(slots=True)
class FakeCodexPrApi:
    """Mock ``CodexPrApi`` — captures list-PR calls and returns canned results."""

    prs_by_slug: dict[str, list[PrSummary]] = field(default_factory=dict)
    list_calls: list[str] = field(default_factory=list)

    def list_open_self_prs(self, *, slug: str) -> list[PrSummary]:
        self.list_calls.append(slug)
        return list(self.prs_by_slug.get(slug, ()))


def _scanner(
    *,
    api: FakeCodexPrApi,
    repos: tuple[str, ...] = (SLUG,),
    overlay: str = "teatree",
) -> CodexReviewScanner:
    return CodexReviewScanner(repos=repos, api=api, overlay=overlay)


class TestDispatchOnNewSha:
    def test_new_pr_emits_codex_review_dispatch_signal(self) -> None:
        """An unseen ``(slug, pr_id, head_sha)`` triggers one dispatch signal."""
        api = FakeCodexPrApi(prs_by_slug={SLUG: [_pr()]})
        scanner = _scanner(api=api)

        signals = scanner.scan()

        assert [s.kind for s in signals] == ["codex_review.dispatch"]
        payload = signals[0].payload
        assert payload["slug"] == SLUG
        assert payload["pr_id"] == 1254
        assert payload["head_sha"] == HEAD
        assert payload["pr_url"] == f"https://github.com/{SLUG}/pull/1254"
        assert payload["overlay"] == "teatree"

    def test_marker_row_persisted_after_dispatch(self) -> None:
        """After dispatch, a :class:`CodexReviewMarker` row makes re-ticks a no-op."""
        api = FakeCodexPrApi(prs_by_slug={SLUG: [_pr()]})
        scanner = _scanner(api=api)

        scanner.scan()

        marker = CodexReviewMarker.objects.get(slug=SLUG, pr_id=1254, head_sha=HEAD)
        assert marker.overlay == "teatree"

    def test_second_tick_on_same_head_does_not_redispatch(self) -> None:
        """The hard requirement: re-ticking on the same SHA is silent."""
        api = FakeCodexPrApi(prs_by_slug={SLUG: [_pr()]})
        scanner = _scanner(api=api)

        first = scanner.scan()
        second = scanner.scan()

        assert [s.kind for s in first] == ["codex_review.dispatch"]
        assert second == []

    def test_new_head_sha_after_force_push_redispatches(self) -> None:
        """A new head SHA on the same PR (force-push) fires a fresh dispatch."""
        api = FakeCodexPrApi(prs_by_slug={SLUG: [_pr()]})
        scanner = _scanner(api=api)

        scanner.scan()
        api.prs_by_slug[SLUG] = [_pr(head=NEW_HEAD)]
        signals = scanner.scan()

        assert [s.kind for s in signals] == ["codex_review.dispatch"]
        assert signals[0].payload["head_sha"] == NEW_HEAD


class TestSkipPaths:
    def test_draft_pr_is_skipped(self) -> None:
        """Draft PRs are not auto-reviewed — the user is still iterating."""
        api = FakeCodexPrApi(prs_by_slug={SLUG: [_pr(is_draft=True)]})
        scanner = _scanner(api=api)

        signals = scanner.scan()

        assert signals == []
        assert not CodexReviewMarker.objects.filter(slug=SLUG, pr_id=1254).exists()

    def test_no_repos_no_signals_no_api_calls(self) -> None:
        """An empty repo list keeps the scanner silent on every tick."""
        api = FakeCodexPrApi()
        scanner = _scanner(api=api, repos=())

        signals = scanner.scan()

        assert signals == []
        assert api.list_calls == []


class TestAdversarialClassifier:
    def test_security_path_routes_to_adversarial_review(self) -> None:
        """Touching ``permissions/`` selects ``codex:adversarial-review``."""
        api = FakeCodexPrApi(
            prs_by_slug={
                SLUG: [_pr(head=AUTH_HEAD, changed_files=("src/teatree/permissions/policy.py",))],
            },
        )
        scanner = _scanner(api=api)

        signals = scanner.scan()

        assert signals[0].payload["variant"] == "codex:adversarial-review"

    def test_default_diff_routes_to_standard_review(self) -> None:
        """A run-of-the-mill diff stays on ``codex:review``."""
        api = FakeCodexPrApi(prs_by_slug={SLUG: [_pr()]})
        scanner = _scanner(api=api)

        signals = scanner.scan()

        assert signals[0].payload["variant"] == "codex:review"


class TestSignalAttribution:
    def test_signal_carries_overlay_for_multi_overlay_loop(self) -> None:
        """Multi-overlay loops attribute signals back to the originating overlay."""
        api = FakeCodexPrApi(prs_by_slug={SLUG: [_pr()]})
        scanner = _scanner(api=api, overlay="my-client-overlay")

        signals = scanner.scan()

        assert signals[0].payload["overlay"] == "my-client-overlay"


@dataclass(slots=True)
class _FakeCompleted:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class TestSafeListErrorHandling:
    """``_safe_list`` propagates ``ScannerError`` but isolates other exceptions."""

    def test_scanner_error_propagates_to_dispatcher(self) -> None:
        class _AuthFailingApi:
            def list_open_self_prs(self, *, slug: str) -> list[PrSummary]:
                _ = slug
                raise ScannerError(
                    scanner="codex_review",
                    error_class=ScannerErrorClass.AUTH,
                    detail="gh auth login required",
                )

        scanner = CodexReviewScanner(repos=(SLUG,), api=_AuthFailingApi(), overlay="t")
        with pytest.raises(ScannerError) as excinfo:
            scanner.scan()
        assert excinfo.value.error_class == ScannerErrorClass.AUTH

    def test_unexpected_exception_is_isolated_to_empty_list(self) -> None:
        class _BoomApi:
            def list_open_self_prs(self, *, slug: str) -> list[PrSummary]:
                _ = slug
                msg = "unexpected"
                raise RuntimeError(msg)

        scanner = CodexReviewScanner(repos=(SLUG,), api=_BoomApi(), overlay="t")
        assert scanner.scan() == []


class TestGhCodexPrApi:
    """``GhCodexPrApi`` — the ``gh``-backed implementation of ``CodexPrApi``."""

    def test_returns_decoded_prs_on_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = (
            '[{"number": 1, "headRefOid": "abc", "isDraft": false, "url": "u", '
            '"title": "t", "files": [{"path": "src/a.py"}, {"path": "src/b.py"}]}]'
        )

        def _stub_run(cmd: list[str], **kwargs: object) -> _FakeCompleted:
            _ = (cmd, kwargs)
            return _FakeCompleted(returncode=0, stdout=payload, stderr="")

        monkeypatch.setattr("teatree.loop.scanners.codex_review.run_allowed_to_fail", _stub_run)
        api = GhCodexPrApi(token="ghp_x")
        prs = api.list_open_self_prs(slug=SLUG)
        assert len(prs) == 1
        assert prs[0].slug == SLUG
        assert prs[0].number == 1
        assert prs[0].head_sha == "abc"
        assert prs[0].is_draft is False
        assert prs[0].changed_files == ("src/a.py", "src/b.py")
        assert prs[0].url == "u"
        assert prs[0].title == "t"

    def test_gh_not_installed_rc_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _stub_run(cmd: list[str], **kwargs: object) -> _FakeCompleted:
            _ = (cmd, kwargs)
            return _FakeCompleted(returncode=127, stdout="", stderr="gh: command not found")

        monkeypatch.setattr("teatree.loop.scanners.codex_review.run_allowed_to_fail", _stub_run)
        api = GhCodexPrApi(token="")
        assert api.list_open_self_prs(slug=SLUG) == []

    def test_file_not_found_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _stub_run(cmd: list[str], **kwargs: object) -> _FakeCompleted:
            _ = (cmd, kwargs)
            msg = "gh"
            raise FileNotFoundError(msg)

        monkeypatch.setattr("teatree.loop.scanners.codex_review.run_allowed_to_fail", _stub_run)
        api = GhCodexPrApi(token="x")
        assert api.list_open_self_prs(slug=SLUG) == []

    def test_gh_auth_failure_raises_scanner_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _stub_run(cmd: list[str], **kwargs: object) -> _FakeCompleted:
            _ = (cmd, kwargs)
            return _FakeCompleted(
                returncode=1,
                stdout="",
                stderr="gh auth login required",
            )

        monkeypatch.setattr("teatree.loop.scanners.codex_review.run_allowed_to_fail", _stub_run)
        api = GhCodexPrApi(token="")
        with pytest.raises(ScannerError) as excinfo:
            api.list_open_self_prs(slug=SLUG)
        assert excinfo.value.error_class == ScannerErrorClass.AUTH

    def test_gh_rate_limit_failure_raises_scanner_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _stub_run(cmd: list[str], **kwargs: object) -> _FakeCompleted:
            _ = (cmd, kwargs)
            return _FakeCompleted(
                returncode=1,
                stdout="",
                stderr="API rate limit exceeded for user.",
            )

        monkeypatch.setattr("teatree.loop.scanners.codex_review.run_allowed_to_fail", _stub_run)
        api = GhCodexPrApi(token="x")
        with pytest.raises(ScannerError) as excinfo:
            api.list_open_self_prs(slug=SLUG)
        assert excinfo.value.error_class == ScannerErrorClass.RATE_LIMIT

    def test_gh_network_failure_raises_scanner_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _stub_run(cmd: list[str], **kwargs: object) -> _FakeCompleted:
            _ = (cmd, kwargs)
            return _FakeCompleted(
                returncode=1,
                stdout="",
                stderr="dial tcp: no such host",
            )

        monkeypatch.setattr("teatree.loop.scanners.codex_review.run_allowed_to_fail", _stub_run)
        api = GhCodexPrApi(token="x")
        with pytest.raises(ScannerError) as excinfo:
            api.list_open_self_prs(slug=SLUG)
        assert excinfo.value.error_class == ScannerErrorClass.NETWORK

    def test_gh_unknown_failure_raises_unknown_error_class(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _stub_run(cmd: list[str], **kwargs: object) -> _FakeCompleted:
            _ = (cmd, kwargs)
            return _FakeCompleted(returncode=1, stdout="", stderr="some other failure")

        monkeypatch.setattr("teatree.loop.scanners.codex_review.run_allowed_to_fail", _stub_run)
        api = GhCodexPrApi(token="x")
        with pytest.raises(ScannerError) as excinfo:
            api.list_open_self_prs(slug=SLUG)
        assert excinfo.value.error_class == ScannerErrorClass.UNKNOWN

    def test_empty_stdout_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _stub_run(cmd: list[str], **kwargs: object) -> _FakeCompleted:
            _ = (cmd, kwargs)
            return _FakeCompleted(returncode=0, stdout="   \n", stderr="")

        monkeypatch.setattr("teatree.loop.scanners.codex_review.run_allowed_to_fail", _stub_run)
        api = GhCodexPrApi(token="x")
        assert api.list_open_self_prs(slug=SLUG) == []

    def test_malformed_json_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _stub_run(cmd: list[str], **kwargs: object) -> _FakeCompleted:
            _ = (cmd, kwargs)
            return _FakeCompleted(returncode=0, stdout="not json", stderr="")

        monkeypatch.setattr("teatree.loop.scanners.codex_review.run_allowed_to_fail", _stub_run)
        api = GhCodexPrApi(token="x")
        assert api.list_open_self_prs(slug=SLUG) == []

    def test_non_list_json_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _stub_run(cmd: list[str], **kwargs: object) -> _FakeCompleted:
            _ = (cmd, kwargs)
            return _FakeCompleted(returncode=0, stdout='{"oops": "object not array"}', stderr="")

        monkeypatch.setattr("teatree.loop.scanners.codex_review.run_allowed_to_fail", _stub_run)
        api = GhCodexPrApi(token="x")
        assert api.list_open_self_prs(slug=SLUG) == []

    def test_token_is_exported_as_gh_token_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def _stub_run(cmd: list[str], **kwargs: object) -> _FakeCompleted:
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env")
            return _FakeCompleted(returncode=0, stdout="[]", stderr="")

        monkeypatch.setattr("teatree.loop.scanners.codex_review.run_allowed_to_fail", _stub_run)
        api = GhCodexPrApi(token="ghp_secret")
        api.list_open_self_prs(slug=SLUG)
        env = captured["env"]
        assert isinstance(env, dict)
        assert env.get("GH_TOKEN") == "ghp_secret"

    def test_no_token_does_not_set_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def _stub_run(cmd: list[str], **kwargs: object) -> _FakeCompleted:
            captured["env"] = kwargs.get("env")
            return _FakeCompleted(returncode=0, stdout="[]", stderr="")

        monkeypatch.setattr("teatree.loop.scanners.codex_review.run_allowed_to_fail", _stub_run)
        api = GhCodexPrApi(token="")
        api.list_open_self_prs(slug=SLUG)
        assert captured["env"] is None


class TestDecodePr:
    """``_decode_pr`` skips PRs with a missing/non-int number, and coerces other missing fields to safe defaults."""

    def test_missing_number_returns_none(self) -> None:
        """A payload with no number field must be skipped — pr_id=0 poisons the marker table."""
        assert _decode_pr(slug=SLUG, raw={}) is None

    def test_non_int_number_returns_none(self) -> None:
        """A non-integer number (e.g. stringified) is not a valid PR id — skip."""
        assert _decode_pr(slug=SLUG, raw={"number": "not-an-int"}) is None

    def test_valid_number_with_missing_optional_fields_uses_safe_defaults(self) -> None:
        pr = _decode_pr(slug=SLUG, raw={"number": 42})
        assert pr is not None
        assert pr.slug == SLUG
        assert pr.number == 42
        assert pr.head_sha == ""
        assert pr.is_draft is False
        assert pr.changed_files == ()
        assert pr.url == ""
        assert pr.title == ""

    def test_non_list_files_falls_back_to_empty(self) -> None:
        pr = _decode_pr(slug=SLUG, raw={"number": 1, "files": "not-a-list"})
        assert pr is not None
        assert pr.changed_files == ()

    def test_non_dict_file_entry_is_skipped(self) -> None:
        pr = _decode_pr(slug=SLUG, raw={"number": 1, "files": ["bare-string", {"path": "src/a.py"}]})
        assert pr is not None
        assert pr.changed_files == ("src/a.py",)

    def test_blank_path_in_file_entry_is_skipped(self) -> None:
        pr = _decode_pr(slug=SLUG, raw={"number": 1, "files": [{"path": ""}, {"path": "src/a.py"}]})
        assert pr is not None
        assert pr.changed_files == ("src/a.py",)


class TestClassifyGhStderr:
    """``_classify_gh_stderr`` returns the expected ``ScannerErrorClass``."""

    @pytest.mark.parametrize(
        ("stderr", "expected"),
        [
            ("API rate limit exceeded", ScannerErrorClass.RATE_LIMIT),
            ("secondary rate limit", ScannerErrorClass.RATE_LIMIT),
            ("Please run gh auth login", ScannerErrorClass.AUTH),
            ("Bad credentials", ScannerErrorClass.AUTH),
            ("HTTP 401", ScannerErrorClass.AUTH),
            ("dial tcp: lookup api.github.com: no such host", ScannerErrorClass.NETWORK),
            ("could not resolve host", ScannerErrorClass.NETWORK),
            ("network is unreachable", ScannerErrorClass.NETWORK),
            ("absolutely unexpected error", ScannerErrorClass.UNKNOWN),
        ],
    )
    def test_classifier_matches_known_markers(self, stderr: str, expected: ScannerErrorClass) -> None:
        assert _classify_gh_stderr(stderr) == expected


class TestVariantConstants:
    """The ``codex:review`` / ``codex:adversarial-review`` variant names match the agent zones."""

    def test_standard_variant_name_matches_slash_command(self) -> None:
        assert STANDARD_REVIEW_VARIANT == "codex:review"

    def test_adversarial_variant_name_matches_slash_command(self) -> None:
        assert ADVERSARIAL_REVIEW_VARIANT == "codex:adversarial-review"
