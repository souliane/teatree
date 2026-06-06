"""Self-pin that the PR5 god-module splits left no dangling legacy paths.

After the campaign PR5 relocations — ``core/merge_execution.py`` →
``core/merge/`` package, ``config.py`` → ``config/`` package, the 17 flat
phase/ship gates → ``core/gates/`` package, ``loop/tick_jobs.py`` →
``job_identity`` / ``scanner_factories`` / ``domain_jobs`` /
``global_scanner_factories`` — every importer and ``mock.patch`` target moved
to the new location. This module is the fitness function that keeps it that
way: a regression that resurrects an old path (a copy-pasted import, a
``patch("teatree.core.merge_execution...")`` target, a doc/skill reference)
turns it red.

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

# Documented PR7-deletable re-export shims (same shape as
# ``teatree.backends.protocols``): a registered overlay consumer still imports
# the old flat path, so the shim keeps it resolving with no cross-repo lockstep.
# These files NAME their old path in the module docstring by design; the scan
# excludes them, but :class:`TestPr7DeletableShims` pins each one to exist + to
# re-export so the exception is tracked, not silent drift. Delete the entry (and
# the shim file) once every overlay consumer has repointed.
_SHIM_FILES = (_REPO_ROOT / "src" / "teatree" / "core" / "merge_guard.py",)
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


class TestPr7DeletableShims:
    """Pin the documented PR7-deletable re-export shims (the cross-repo seam).

    Each shim keeps an old flat path resolving for a registered overlay consumer
    that has not yet repointed, exactly like ``teatree.backends.protocols``. The
    shim must (1) exist and (2) re-export the same object the canonical path
    owns, so a consumer on either path sees one class — and so the exception
    stays visible (delete the shim + this pin once overlays have repointed).
    """

    def test_shim_files_exist(self) -> None:
        for shim in _SHIM_FILES:
            assert shim.is_file(), f"PR7-deletable shim {shim} is gone — repoint consumers before deleting"

    def test_merge_guard_shim_re_exports_the_canonical_class(self) -> None:
        shim = importlib.import_module("teatree.core.merge_guard")
        canonical = importlib.import_module("teatree.core.gates.merge_guard")
        assert shim.MergeGuard is canonical.MergeGuard
