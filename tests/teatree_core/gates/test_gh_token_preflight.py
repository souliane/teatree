"""Unit tests for the GitHub token permission probe (#3405, expanded #3477).

The only boundary faked is the ``gh`` runner (an unstoppable external subprocess /
network); the probe's classification logic runs for real against modelled ``gh
api`` outputs.
"""

from unittest.mock import patch

from teatree.core.gates import gh_token_preflight
from teatree.core.gates.gh_token_preflight import (
    FEATURE_BY_PERMISSION,
    RECOMMENDED_PERMISSION_LABELS,
    REQUIRED_PERMISSION_LABELS,
    GhTokenProbe,
    format_remediation,
    probe_token_permissions,
)

_SLUG = "souliane/teatree"
_NOT_ACCESSIBLE = '{"message":"Resource not accessible by personal access token"}'
_NOT_FOUND = '{"message":"Not Found"}'
_WRITE_LABELS = ("issues: write", "pull_requests: write", "contents: write")
_META_KEY = f"repos/{_SLUG}"


def _classic_headers(scopes: str) -> str:
    """Model a `gh api -i` metadata response for a classic PAT granting *scopes*."""
    return f"HTTP/2.0 200 OK\nX-OAuth-Scopes: {scopes}\nContent-Type: application/json\n\n{{}}"


def _fine_grained_meta(default_branch: str | None = "main") -> str:
    """Model a `gh api -i` metadata response for a fine-grained PAT (no scopes header)."""
    body = f'{{"default_branch":"{default_branch}"}}' if default_branch else "not json"
    return f"HTTP/2.0 200 OK\nContent-Type: application/json\n\n{body}"


def _runner(responses: dict[str, tuple[int, str]], *, default_branch: str = "main"):
    """A fake gh runner keyed by a distinctive substring of the endpoint arg.

    The metadata read (``-i`` present in args) is matched by the reserved
    ``f"repos/{_SLUG}"`` key only, defaulting to a fine-grained response
    carrying *default_branch* when the caller didn't override it explicitly.
    Every other probe (write or read, mutate or graphql) is matched by its own
    resource needle — the two kinds are never distinguished by ``--method``
    presence, matching the real collapsed-to-one-check verdict.
    """

    def run(args: list[str]) -> tuple[int, str]:
        joined = " ".join(args)
        if "-i" in args:
            meta = responses.get(_META_KEY)
            return meta if meta is not None else (0, _fine_grained_meta(default_branch))
        for needle, outcome in responses.items():
            if needle == _META_KEY:
                continue
            if needle in joined:
                return outcome
        return (0, "{}")

    return run


class TestProbeTokenPermissions:
    def test_all_present_when_writes_404(self) -> None:
        # A permitted token gets 404 (resource id 0 doesn't exist) on every write.
        run = _runner(
            {
                _META_KEY: (0, "{}"),
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
                _META_KEY: (0, "{}"),
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
                _META_KEY: (0, "{}"),
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
        run = _runner({_META_KEY: (1, _NOT_ACCESSIBLE)})
        with patch("teatree.core.gates.gh_token_preflight.shutil.which", return_value="/usr/bin/gh"):
            probe = probe_token_permissions(_SLUG, run=run)
        assert probe.missing == ("metadata: read",)

    def test_metadata_network_failure_is_indeterminate(self) -> None:
        # A non-permission failure (network) must not be read as a missing scope.
        run = _runner({_META_KEY: (1, "connection reset by peer")})
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


class TestRecommendedPermissions:
    """WARN-tier permissions never affect `ok`/`missing` — only `missing_recommended`."""

    def _base_responses(self) -> dict[str, tuple[int, str]]:
        return {
            _META_KEY: (0, '{"default_branch":"main"}'),
            "issues/0": (1, _NOT_FOUND),
            "pulls/0": (1, _NOT_FOUND),
            "refs/heads": (1, _NOT_FOUND),
            "dispatches": (1, _NOT_FOUND),
            "artifacts": (0, "{}"),
            "check-runs": (0, "{}"),
            "main/status": (0, "{}"),
            "actions/secrets/": (1, _NOT_FOUND),
            "actions/variables/": (1, _NOT_FOUND),
        }

    def test_all_recommended_present_ok_and_no_recommended_gaps_except_workflows(self) -> None:
        run = _runner(self._base_responses())
        with patch("teatree.core.gates.gh_token_preflight.shutil.which", return_value="/usr/bin/gh"):
            probe = probe_token_permissions(_SLUG, run=run)
        assert probe.ok
        # workflows:write is never actively probed for a fine-grained token —
        # always surfaced so remediation tells the operator to verify manually.
        assert probe.missing_recommended == ("workflows: write",)

    def test_missing_actions_write_reported_recommended_never_required(self) -> None:
        responses = self._base_responses()
        responses["dispatches"] = (1, _NOT_ACCESSIBLE)
        run = _runner(responses)
        with patch("teatree.core.gates.gh_token_preflight.shutil.which", return_value="/usr/bin/gh"):
            probe = probe_token_permissions(_SLUG, run=run)
        assert probe.ok  # recommended gaps never flip ok
        assert probe.missing == ()
        assert "actions: write" in probe.missing_recommended

    def test_missing_secrets_write_reported_recommended_never_required(self) -> None:
        """`gh secret set` needs it; a gap must WARN, never fail the never-lockout deploy."""
        responses = self._base_responses()
        responses["actions/secrets/"] = (1, _NOT_ACCESSIBLE)
        run = _runner(responses)
        with patch("teatree.core.gates.gh_token_preflight.shutil.which", return_value="/usr/bin/gh"):
            probe = probe_token_permissions(_SLUG, run=run)
        assert probe.ok
        assert "secrets: write" in probe.missing_recommended
        assert "secrets: write" not in probe.missing

    def test_missing_variables_write_reported(self) -> None:
        responses = self._base_responses()
        responses["actions/variables/"] = (1, _NOT_ACCESSIBLE)
        run = _runner(responses)
        with patch("teatree.core.gates.gh_token_preflight.shutil.which", return_value="/usr/bin/gh"):
            probe = probe_token_permissions(_SLUG, run=run)
        assert probe.ok
        assert "variables: write" in probe.missing_recommended

    def test_the_secret_probe_targets_a_sentinel_that_never_exists(self) -> None:
        """A DELETE probe must never be able to remove a real secret."""
        seen: list[str] = []

        def run(args: list[str]) -> tuple[int, str]:
            seen.append(" ".join(args))
            return (0, _fine_grained_meta()) if "-i" in args else (1, _NOT_FOUND)

        with patch("teatree.core.gates.gh_token_preflight.shutil.which", return_value="/usr/bin/gh"):
            probe_token_permissions(_SLUG, run=run)

        deletes = [call for call in seen if "DELETE" in call]
        assert deletes
        assert all("TEATREE_PREFLIGHT_NONEXISTENT" in call for call in deletes)

    def test_missing_actions_read_reported(self) -> None:
        responses = self._base_responses()
        responses["artifacts"] = (1, _NOT_ACCESSIBLE)
        run = _runner(responses)
        with patch("teatree.core.gates.gh_token_preflight.shutil.which", return_value="/usr/bin/gh"):
            probe = probe_token_permissions(_SLUG, run=run)
        assert probe.ok
        assert "actions: read" in probe.missing_recommended

    def test_missing_checks_read_reported(self) -> None:
        responses = self._base_responses()
        responses["check-runs"] = (1, _NOT_ACCESSIBLE)
        run = _runner(responses)
        with patch("teatree.core.gates.gh_token_preflight.shutil.which", return_value="/usr/bin/gh"):
            probe = probe_token_permissions(_SLUG, run=run)
        assert probe.ok
        assert "checks: read" in probe.missing_recommended

    def test_missing_statuses_read_reported(self) -> None:
        responses = self._base_responses()
        responses["main/status"] = (1, _NOT_ACCESSIBLE)
        run = _runner(responses)
        with patch("teatree.core.gates.gh_token_preflight.shutil.which", return_value="/usr/bin/gh"):
            probe = probe_token_permissions(_SLUG, run=run)
        assert probe.ok
        assert "statuses: read" in probe.missing_recommended

    def test_a_read_probe_404_is_not_missing(self) -> None:
        # A 404 (nonexistent commit/branch/artifact) is the PERMITTED signal for
        # a read probe, never a denial — must not be misreported as missing.
        responses = self._base_responses()
        responses["check-runs"] = (1, _NOT_FOUND)
        run = _runner(responses)
        with patch("teatree.core.gates.gh_token_preflight.shutil.which", return_value="/usr/bin/gh"):
            probe = probe_token_permissions(_SLUG, run=run)
        assert "checks: read" not in probe.missing_recommended

    def test_a_read_probe_network_failure_is_skipped_not_missing(self) -> None:
        responses = self._base_responses()
        responses["check-runs"] = (1, "connection reset by peer")
        run = _runner(responses)
        with patch("teatree.core.gates.gh_token_preflight.shutil.which", return_value="/usr/bin/gh"):
            probe = probe_token_permissions(_SLUG, run=run)
        assert "checks: read" not in probe.missing_recommended

    def test_checks_and_statuses_skipped_without_a_resolvable_default_branch(self) -> None:
        responses = self._base_responses()
        responses[_META_KEY] = (0, "not json at all")
        responses["check-runs"] = (1, _NOT_ACCESSIBLE)  # would-be denial, never reached
        responses["main/status"] = (1, _NOT_ACCESSIBLE)
        run = _runner(responses)
        with patch("teatree.core.gates.gh_token_preflight.shutil.which", return_value="/usr/bin/gh"):
            probe = probe_token_permissions(_SLUG, run=run)
        assert "checks: read" not in probe.missing_recommended
        assert "statuses: read" not in probe.missing_recommended

    def test_token_kind_is_fine_grained_when_no_scopes_header(self) -> None:
        run = _runner(self._base_responses())
        with patch("teatree.core.gates.gh_token_preflight.shutil.which", return_value="/usr/bin/gh"):
            probe = probe_token_permissions(_SLUG, run=run)
        assert probe.token_kind == "fine_grained"


class TestProjectsReadProbe:
    """`projects: read` is probed only when a board is configured (owner + number)."""

    def _base_responses(self) -> dict[str, tuple[int, str]]:
        return {
            _META_KEY: (0, '{"default_branch":"main"}'),
            "issues/0": (1, _NOT_FOUND),
            "pulls/0": (1, _NOT_FOUND),
            "refs/heads": (1, _NOT_FOUND),
            "dispatches": (1, _NOT_FOUND),
            "artifacts": (0, "{}"),
            "check-runs": (0, "{}"),
            "main/status": (0, "{}"),
            "actions/secrets/": (1, _NOT_FOUND),
            "actions/variables/": (1, _NOT_FOUND),
        }

    def test_not_probed_when_board_unconfigured(self) -> None:
        responses = self._base_responses()
        responses["graphql"] = (1, '{"errors":[{"type":"FORBIDDEN"}]}')  # would-be denial, never reached
        run = _runner(responses)
        with patch("teatree.core.gates.gh_token_preflight.shutil.which", return_value="/usr/bin/gh"):
            probe = probe_token_permissions(_SLUG, run=run)
        assert "projects: read" not in probe.missing_recommended

    def test_permitted_project_not_reported_missing(self) -> None:
        responses = self._base_responses()
        responses["graphql"] = (0, '{"data":{"user":{"projectV2":null}},"errors":[{"type":"NOT_FOUND"}]}')
        run = _runner(responses)
        with patch("teatree.core.gates.gh_token_preflight.shutil.which", return_value="/usr/bin/gh"):
            probe = probe_token_permissions(_SLUG, run=run, github_owner="souliane", github_project_number=2)
        assert "projects: read" not in probe.missing_recommended

    def test_denied_project_reported_recommended_never_required(self) -> None:
        responses = self._base_responses()
        responses["graphql"] = (1, '{"data":null,"errors":[{"type":"FORBIDDEN"}]}')
        run = _runner(responses)
        with patch("teatree.core.gates.gh_token_preflight.shutil.which", return_value="/usr/bin/gh"):
            probe = probe_token_permissions(_SLUG, run=run, github_owner="souliane", github_project_number=2)
        assert probe.ok
        assert "projects: read" in probe.missing_recommended


class TestWriteProbeIndeterminate:
    """A write probe that fails to REACH a verdict is INDETERMINATE, not present.

    The per-route write probe reads a genuine grant as a 404 (resource absent) and
    a missing permission as a 403 ``not accessible``. A TRANSIENT/network failure of
    a write probe returns NEITHER string, so the old loop — which only appended to
    ``missing`` on the 403 signal and discarded the return code — silently recorded
    the failed probe as permission PRESENT. A token genuinely missing the permission
    then passed preflight and failed mid-run. The fix treats a probe that reached no
    definitive 403/404 verdict as indeterminate (fail closed to a skip/warn, never a
    false grant).
    """

    def test_transient_write_probe_is_indeterminate_not_present(self) -> None:
        # RED before the fix: the transient issues probe (no `not accessible`, no
        # reached-route signal) was counted as PRESENT, so probe.ok was True.
        run = _runner(
            {
                "repos/souliane/teatree": (0, "{}"),
                "issues/0": (1, "error connecting to api.github.com: connection reset by peer"),
                "pulls/0": (1, _NOT_FOUND),
                "refs/heads": (1, _NOT_FOUND),
            }
        )
        with patch("teatree.core.gates.gh_token_preflight.shutil.which", return_value="/usr/bin/gh"):
            probe = probe_token_permissions(_SLUG, run=run)
        assert probe.missing == ()
        assert probe.indeterminate_reason is not None
        assert "issues: write" in probe.indeterminate_reason
        assert not probe.ok

    def test_genuine_denial_takes_precedence_over_transient(self) -> None:
        # A real 403 denial must be reported (loud FAIL) even when another probe is
        # transient — the denial is a definite gap, not masked by the indeterminate.
        run = _runner(
            {
                "repos/souliane/teatree": (0, "{}"),
                "issues/0": (1, _NOT_ACCESSIBLE),
                "pulls/0": (1, "curl: (6) could not resolve host"),
                "refs/heads": (1, _NOT_FOUND),
            }
        )
        with patch("teatree.core.gates.gh_token_preflight.shutil.which", return_value="/usr/bin/gh"):
            probe = probe_token_permissions(_SLUG, run=run)
        assert probe.missing == ("issues: write",)
        assert not probe.ok

    def test_http_status_marker_counts_as_reached(self) -> None:
        # Real `gh` prints `(HTTP 404)` on a reached route; that is present, not
        # indeterminate, even without the bare `Not Found` JSON body.
        run = _runner(
            {
                "repos/souliane/teatree": (0, "{}"),
                "issues/0": (1, "gh: Sub-issues are disabled (HTTP 422)"),
                "pulls/0": (1, "gh: Not Found (HTTP 404)"),
                "refs/heads": (1, "gh: Not Found (HTTP 404)"),
            }
        )
        with patch("teatree.core.gates.gh_token_preflight.shutil.which", return_value="/usr/bin/gh"):
            probe = probe_token_permissions(_SLUG, run=run)
        assert probe.ok
        assert probe.missing == ()


class TestClassicPatScope:
    """A classic PAT is judged by its ``X-OAuth-Scopes`` header.

    The per-route 403 probe fails OPEN for classic tokens (#3436), so the scope
    header is the source of truth for them — for BOTH tiers.
    """

    def test_classic_pat_with_repo_scope_passes(self) -> None:
        # `ok` never depends on the recommended tier: `read:project` is missing
        # here (only `workflow` is granted alongside `repo`) but `ok` stays True.
        run = _runner({_META_KEY: (0, _classic_headers("repo, workflow, read:org"))})
        with patch("teatree.core.gates.gh_token_preflight.shutil.which", return_value="/usr/bin/gh"):
            probe = probe_token_permissions(_SLUG, run=run)
        assert probe.ok
        assert probe.missing == ()
        assert probe.missing_recommended == ("projects: read",)
        assert probe.token_kind == "classic"

    def test_classic_pat_with_repo_workflow_and_project_scope_is_fully_clean(self) -> None:
        run = _runner({_META_KEY: (0, _classic_headers("repo, workflow, read:project"))})
        with patch("teatree.core.gates.gh_token_preflight.shutil.which", return_value="/usr/bin/gh"):
            probe = probe_token_permissions(_SLUG, run=run)
        assert probe.ok
        assert probe.missing == ()
        assert probe.missing_recommended == ()

    def test_classic_pat_without_repo_scope_blocks_every_write(self) -> None:
        # The bug: without `repo`, the write probes would 404 (fail open). The
        # scope header is the truth — every write is denied.
        run = _runner({_META_KEY: (0, _classic_headers("public_repo, read:org"))})
        with patch("teatree.core.gates.gh_token_preflight.shutil.which", return_value="/usr/bin/gh"):
            probe = probe_token_permissions(_SLUG, run=run)
        assert not probe.ok
        assert probe.missing == _WRITE_LABELS

    def test_classic_repo_status_scope_is_not_repo_write(self) -> None:
        # `repo:status` shares the `repo` prefix but grants no write — an exact
        # scope-token match, never a substring, must reject it.
        run = _runner({_META_KEY: (0, _classic_headers("repo:status, gist"))})
        with patch("teatree.core.gates.gh_token_preflight.shutil.which", return_value="/usr/bin/gh"):
            probe = probe_token_permissions(_SLUG, run=run)
        assert probe.missing == _WRITE_LABELS

    def test_empty_scope_header_is_classic_with_no_write(self) -> None:
        run = _runner({_META_KEY: (0, _classic_headers(""))})
        with patch("teatree.core.gates.gh_token_preflight.shutil.which", return_value="/usr/bin/gh"):
            probe = probe_token_permissions(_SLUG, run=run)
        assert probe.missing == _WRITE_LABELS
        assert probe.missing_recommended == ("workflows: write", "projects: read")

    def test_classic_missing_only_workflow_scope_is_recommended_only(self) -> None:
        run = _runner({_META_KEY: (0, _classic_headers("repo, read:project"))})
        with patch("teatree.core.gates.gh_token_preflight.shutil.which", return_value="/usr/bin/gh"):
            probe = probe_token_permissions(_SLUG, run=run)
        assert probe.ok
        assert probe.missing_recommended == ("workflows: write",)


class TestDefaultRunReachesApi:
    def test_default_run_invokes_gh_api(self) -> None:
        # Regression (#3436): the probe operands must run under `gh api …`, not a
        # bare `gh …` that GitHub's CLI rejects as an unknown command — the latter
        # made every real (unmocked) probe read indeterminate and silently no-op.
        captured: dict[str, list[str]] = {}

        class _Result:
            returncode = 0
            stdout = "{}"
            stderr = ""

        def fake_run(argv: list[str], **_kwargs: object) -> _Result:
            captured["argv"] = argv
            return _Result()

        with patch.object(gh_token_preflight, "run_allowed_to_fail", fake_run):
            gh_token_preflight._default_run(["-i", f"repos/{_SLUG}"])
        assert captured["argv"][:2] == ["gh", "api"]
        assert captured["argv"][-1] == f"repos/{_SLUG}"


class TestParseDefaultBranch:
    def test_parses_from_headers_and_body(self) -> None:
        meta = 'HTTP/2.0 200 OK\nContent-Type: application/json\n\n{"default_branch":"main","name":"x"}'
        assert gh_token_preflight._parse_default_branch(meta) == "main"

    def test_none_when_body_unparseable(self) -> None:
        meta = "HTTP/2.0 200 OK\n\nnot json"
        assert gh_token_preflight._parse_default_branch(meta) is None

    def test_none_when_key_absent(self) -> None:
        meta = "HTTP/2.0 200 OK\n\n{}"
        assert gh_token_preflight._parse_default_branch(meta) is None


class TestRequiredLabels:
    def test_labels_are_the_four_loop_permissions(self) -> None:
        assert REQUIRED_PERMISSION_LABELS == (
            "metadata: read",
            "issues: write",
            "pull_requests: write",
            "contents: write",
        )


class TestRecommendedLabels:
    def test_labels_are_the_warn_tier_permissions(self) -> None:
        assert RECOMMENDED_PERMISSION_LABELS == (
            "workflows: write",
            "actions: write",
            "actions: read",
            "checks: read",
            "statuses: read",
            "projects: read",
            "secrets: write",
            "variables: write",
        )

    def test_never_overlaps_required(self) -> None:
        assert not set(RECOMMENDED_PERMISSION_LABELS) & set(REQUIRED_PERMISSION_LABELS)

    def test_every_label_both_tiers_has_a_feature_note(self) -> None:
        for label in (*REQUIRED_PERMISSION_LABELS, *RECOMMENDED_PERMISSION_LABELS):
            assert label in FEATURE_BY_PERMISSION
            assert FEATURE_BY_PERMISSION[label]


class TestFormatRemediation:
    def test_no_gaps_is_empty(self) -> None:
        assert format_remediation(GhTokenProbe(missing=()), _SLUG) == []

    def test_classic_gap_is_one_line_with_recreate_url(self) -> None:
        probe = GhTokenProbe(missing=(), missing_recommended=("workflows: write",), token_kind="classic")
        lines = format_remediation(probe, _SLUG)
        assert len(lines) == 1
        assert "workflows: write" in lines[0]
        assert "scopes=repo,workflow,read:project" in lines[0]
        assert "https://github.com/settings/tokens/new" in lines[0]

    def test_fine_grained_gap_lists_each_permission_with_its_feature(self) -> None:
        probe = GhTokenProbe(
            missing=(),
            missing_recommended=("actions: write", "checks: read"),
            token_kind="fine_grained",
        )
        lines = format_remediation(probe, _SLUG)
        joined = "\n".join(lines)
        assert "actions: write" in joined
        assert "checks: read" in joined
        assert "needed for" in joined
        assert "https://github.com/settings/personal-access-tokens" in joined
        assert "recreate" in joined.lower()

    def test_fine_grained_required_gap_also_rendered(self) -> None:
        probe = GhTokenProbe(missing=("issues: write",), token_kind="fine_grained")
        lines = format_remediation(probe, _SLUG)
        assert any("issues: write" in line for line in lines)
