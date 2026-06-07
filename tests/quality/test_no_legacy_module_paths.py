"""Self-pin that the file-hierarchy campaign splits left no dangling legacy paths.

After the campaign relocations — PR5: ``core/merge_execution.py`` →
``core/merge/`` package, ``config.py`` → ``config/`` package, the 17 flat
phase/ship gates → ``core/gates/`` package, ``loop/tick_jobs.py`` →
``job_identity`` / ``scanner_factories`` / ``domain_jobs`` /
``global_scanner_factories``; PR8: the 24 flat ``backends/<provider>_*.py``
modules → ``backends/gitlab`` / ``backends/slack`` / ``backends/github``
subpackages (prefix stripped); PR9: the 16 flat ``cli/review*.py`` modules →
the ``cli/review`` subpackage (redundant ``review_`` prefix stripped, bare
``cli/review.py`` → ``cli/review/service.py``) — every importer and
``mock.patch`` target moved to the new location. This module is the fitness
function that keeps it that way: a regression that resurrects an old path (a
copy-pasted import, a ``patch("teatree.core.merge_execution...")`` target, a
doc/skill reference) turns it red.

Two halves:

``TestNoLegacyPaths`` greps the whole tracked tree (src / tests / hooks /
skills / docs) for the dead module paths and asserts zero hits — the
#2046/#2048 string-based-patch-target trap made textual, not import, so it
also catches ``patch("...")`` strings.

``TestFacadeImportSmoke`` imports each new package/module and pins the facade
re-export surface, so a split that silently dropped a public symbol fails here
rather than at a downstream call site.
"""

import importlib
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCAN_DIRS = ("src", "tests", "hooks", "skills", "docs")
_SCAN_SUFFIXES = (".py", ".md", ".txt")

# This test file legitimately names the dead paths (in prose + the patterns);
# exclude it so the pin does not flag itself.
_SELF = Path(__file__).resolve()

# PR7-deletable re-export shims: empty — the campaign repointed every consumer
# (teatree's own importers in PR7a, the last overlay consumer in its own follow-up)
# and deleted the final shim (``teatree.core.merge_guard`` → the canonical
# ``teatree.core.gates.merge_guard``) in PR7b. :class:`TestPr7DeletableShims` now
# pins that none remain, so a regression that resurrects a deletable shim turns red.
_SHIM_FILES: tuple[Path, ...] = ()
_SHIM_PATHS = {p.resolve() for p in _SHIM_FILES}

# The 17 relocated gate stems (suffix kept per the PR5 plan §4 decision). Each
# now lives at ``teatree.core.gates.<stem>``; a flat ``teatree.core.<stem>`` is
# the dead path. ``on_behalf`` keeps its flat ``teatree.core.on_behalf`` home
# and is deliberately NOT a gate stem here.
_GATE_STEMS = (
    "anti_vacuity_gate",
    "clone_guard",
    "db_approval_gate",
    "dod_gate",
    "e2e_mandatory_gate",
    "fix_dod_gate",
    "live_post_gate",
    "local_stack_gate",
    "merge_guard",
    "open_questions_gate",
    "orphan_guard",
    "plan_gate",
    "privacy_gate",
    "review_context_gate",
    "review_request_guard",
    "review_skill_gate",
    "schema_guard",
)

# PR8 backends/ grouping: each flat ``teatree.backends.<provider>_<rest>`` moved
# into a ``teatree.backends.<provider>`` subpackage with the redundant prefix
# stripped (e.g. ``teatree.backends.gitlab_api`` → ``teatree.backends.gitlab.api``,
# the bare client ``teatree.backends.gitlab`` → ``teatree.backends.gitlab.client``).
# The flat ``<provider>_<rest>`` paths are now dead; the dotted
# ``teatree.backends.<provider>.<rest>`` paths are live.
_BACKEND_FLAT_STEMS = (
    "gitlab_api",
    "gitlab_ci",
    "gitlab_payloads",
    "gitlab_subissues",
    "gitlab_sync",
    "gitlab_sync_approvals",
    "gitlab_sync_issues",
    "gitlab_sync_prs",
    "gitlab_sync_terminal",
    "slack_bot",
    "slack_bot_errors",
    "slack_http",
    "slack_react_errors",
    "slack_reactions",
    "slack_receiver",
    "slack_review_sync",
    "slack_scopes",
    "slack_token_policy",
    "slack_token_validation",
    "slack_voice_classifier",
    "github_claims",
    "github_payloads",
    "github_projects",
    "github_sync",
)

# PR9 cli/ review grouping: each flat ``teatree.cli.review_<rest>`` moved into
# the ``teatree.cli.review`` subpackage with the redundant ``review_`` prefix
# stripped (e.g. the flat ``review_diff`` module → ``teatree.cli.review.diff``), and
# the bare ``teatree.cli.review`` module (``ReviewService`` + ``review_app``)
# became ``teatree.cli.review.service``. The flat ``review_<rest>`` paths are now
# dead; the dotted ``teatree.cli.review.<rest>`` paths (incl. ``service``) are
# live. ``teatree.cli.review`` itself is the live package facade, so it is NOT a
# dead path — only the underscore-joined ``review_<rest>`` forms are.
_CLI_REVIEW_FLAT_STEMS = (
    "approval",
    "audit",
    "authorize",
    "commands",
    "default_draft",
    "diff",
    "drafts",
    "evidence_gate",
    "live_approval",
    "on_behalf",
    "post_impl",
    "request",
    "run",
    "shape_gate",
    "todo_gate",
)

# Submodule names inside ``teatree.cli.review`` after the move (the live dotted
# paths). ``service`` carries the former bare ``review.py`` surface.
_CLI_REVIEW_SUBMODULES = (*_CLI_REVIEW_FLAT_STEMS, "service")


def _legacy_patterns() -> dict[str, re.Pattern[str]]:
    patterns = {
        "teatree.core.merge_execution": re.compile(r"\bteatree\.core\.merge_execution\b"),
        "core import merge_execution": re.compile(r"\bcore\s+import\s+merge_execution\b"),
        "teatree.loop.tick_jobs": re.compile(r"\bteatree\.loop\.tick_jobs\b"),
        "loop.tick_jobs path": re.compile(r"\bloop/tick_jobs\.py\b|\bloop\.tick_jobs\b"),
    }
    for stem in _GATE_STEMS:
        # Flat ``teatree.core.<gate>`` — but NOT the live ``teatree.core.gates.<gate>``.
        patterns[f"teatree.core.{stem}"] = re.compile(rf"\bteatree\.core\.{stem}\b")
    for stem in _BACKEND_FLAT_STEMS:
        # Flat ``teatree.backends.<provider>_<rest>`` — the dotted subpackage path
        # ``teatree.backends.<provider>.<rest>`` is live and never matches this.
        patterns[f"teatree.backends.{stem}"] = re.compile(rf"\bteatree\.backends\.{stem}\b")
    for stem in _CLI_REVIEW_FLAT_STEMS:
        # Flat ``teatree.cli.review_<rest>`` — the dotted subpackage path
        # ``teatree.cli.review.<rest>`` is live and never matches this.
        patterns[f"teatree.cli.review_{stem}"] = re.compile(rf"\bteatree\.cli\.review_{stem}\b")
    return patterns


def _scan_files() -> list[Path]:
    files: list[Path] = []
    for rel in _SCAN_DIRS:
        root = _REPO_ROOT / rel
        if not root.is_dir():
            continue
        for suffix in _SCAN_SUFFIXES:
            files.extend(p for p in root.rglob(f"*{suffix}") if p.resolve() not in _SHIM_PATHS | {_SELF})
    return files


def _hits(pattern: re.Pattern[str]) -> list[str]:
    out: list[str] = []
    for path in _scan_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                # ``teatree.core.gates.<stem>`` contains ``teatree.core.<stem>``
                # only via the substring ``gates.`` — the \b-anchored gate
                # pattern already excludes it, but guard the merge/loop ones too.
                if "teatree.core.gates." in line and "merge_execution" not in line and "tick_jobs" not in line:
                    continue
                out.append(f"{path.relative_to(_REPO_ROOT)}:{lineno}: {line.strip()}")
    return out


class TestNoLegacyPaths:
    @pytest.mark.parametrize("label", list(_legacy_patterns()))
    def test_legacy_path_has_zero_hits(self, label: str) -> None:
        hits = _hits(_legacy_patterns()[label])
        assert not hits, f"legacy path '{label}' still referenced:\n" + "\n".join(hits)


class TestFacadeImportSmoke:
    def test_merge_package_facade_re_exports(self) -> None:
        merge = importlib.import_module("teatree.core.merge")
        for name in (
            "merge_ticket_pr",
            "execute_bound_merge",
            "assert_merge_preconditions",
            "resolve_pr_repo_slug",
            "fetch_pr_merge_state",
            "MergePreconditionError",
            "MergeHeadMovedError",
        ):
            assert hasattr(merge, name), f"teatree.core.merge missing {name}"

    def test_config_package_facade_re_exports(self) -> None:
        config = importlib.import_module("teatree.config")
        for name in ("load_config", "UserSettings", "CONFIG_PATH", "discover_overlays"):
            assert hasattr(config, name), f"teatree.config missing {name}"

    def test_gates_package_modules_import(self) -> None:
        for stem in _GATE_STEMS:
            importlib.import_module(f"teatree.core.gates.{stem}")

    def test_loop_split_modules_import(self) -> None:
        for name in ("job_identity", "scanner_factories", "domain_jobs", "global_scanner_factories"):
            importlib.import_module(f"teatree.loop.{name}")

    def test_loop_tick_re_exports_moved_surface(self) -> None:
        tick = importlib.import_module("teatree.loop.tick")
        for name in ("build_default_jobs", "build_default_scanners", "jobs_for_domain", "_ScannerJob", "Domain"):
            assert hasattr(tick, name), f"teatree.loop.tick missing re-export {name}"

    @pytest.mark.parametrize("stem", _BACKEND_FLAT_STEMS)
    def test_backend_subpackage_modules_import(self, stem: str) -> None:
        provider, _, rest = stem.partition("_")
        importlib.import_module(f"teatree.backends.{provider}.{rest}")

    def test_gitlab_package_facade_re_exports(self) -> None:
        gitlab = importlib.import_module("teatree.backends.gitlab")
        for name in ("GitLabCodeHost", "get_client"):
            assert hasattr(gitlab, name), f"teatree.backends.gitlab missing {name}"

    def test_slack_package_facade_re_exports(self) -> None:
        slack = importlib.import_module("teatree.backends.slack")
        for name in (
            "post_webhook_message",
            "search_review_permalinks",
            "read_recent_review_matches",
            "SlackReviewSearchRequest",
        ):
            assert hasattr(slack, name), f"teatree.backends.slack missing {name}"

    def test_github_package_facade_re_exports(self) -> None:
        github = importlib.import_module("teatree.backends.github")
        for name in ("GitHubCodeHost", "ProjectItem", "fetch_project_items", "issue_repo_short"):
            assert hasattr(github, name), f"teatree.backends.github missing {name}"

    @pytest.mark.parametrize("stem", _CLI_REVIEW_SUBMODULES)
    def test_cli_review_subpackage_modules_import(self, stem: str) -> None:
        importlib.import_module(f"teatree.cli.review.{stem}")

    def test_cli_review_package_facade_re_exports(self) -> None:
        review = importlib.import_module("teatree.cli.review")
        for name in ("ReviewService", "review_app", "review_request_app"):
            assert hasattr(review, name), f"teatree.cli.review missing {name}"


class TestPr7DeletableShims:
    """Pin that the PR7 shim-deletion campaign is complete — no deletable shims remain.

    The cross-repo seam (``teatree.core.merge_guard`` →
    ``teatree.core.gates.merge_guard``) existed so the PR5 god-module split could
    land without cross-repo lockstep. PR7a repointed teatree's own importers, the
    overlay repoint removed the last external consumer, and PR7b deleted the final
    shim. This pin keeps :data:`_SHIM_FILES` empty so a regression that reintroduces
    a deletable shim turns red instead of slipping in silently — and the flat path is
    now an ordinary dead path enforced by :class:`TestNoLegacyPaths`.
    """

    def test_no_deletable_shims_remain(self) -> None:
        assert _SHIM_FILES == (), (
            "PR7 shim-deletion campaign is complete — a new deletable re-export shim "
            "appeared; repoint its consumers and delete it rather than tracking it here."
        )
