"""The raw-review-post sibling: single canonical identity, re-export reachability, cold import.

The raw-review-post deny gate (#1164) was extracted whole out of ``hook_router``
into ``hooks/scripts/raw_review_post_guard.py`` (the #2384 Wave-2 router split,
PR6). These tests pin the EXTRACTION contract — the behavioural gate tests (which
commands deny vs allow, the deny message) live in
``tests/test_hook_router_raw_review_post.py`` and exercise the handler via the
router re-export unchanged.

The contract:

* the sibling has a SINGLE canonical package identity ``hooks.scripts.raw_review_post_guard``
    (no bare-name alias after the package-relative refactor), so a test patching a
    helper here and the handler the router invokes are the same object;
* the router re-exports ``handle_block_raw_review_post`` (and the
    ``is_raw_review_write`` helper + the ``REVIEW_POST_ENDPOINT_RE`` constant the
    transcript-conformance test reads as ``router._REVIEW_POST_ENDPOINT_RE``)
    under their original router names, so ``router.handle_block_raw_review_post``
    resolves to the SAME object the sibling defines (and ``_HANDLERS['PreToolUse']``
    registers it unchanged);
* the effective-HTTP-method regexes the gate SHARES with router-resident handlers
    (``_GLAB_GH_API_RE`` / ``_REVIEW_POST_METHOD_RE`` / ``_REVIEW_POST_BODY_FLAG_RE``,
    read by ``_effective_method_is_write`` and the out-of-band-merge gate) stay
    defined ONLY in the router — the sibling back-imports them lazily, so there is
    exactly one definition each;
* the sibling cold-imports with stdlib only — no Django, no ``teatree`` at module
    top (the live PreToolUse hook is a bare ``python3`` subprocess with no Django
    configured); the deny path's ``emit_pretooluse_deny`` chokepoint stays in the
    router and is back-imported lazily inside the handler body.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
import hooks.scripts.raw_review_post_guard as rrp

_SCRIPTS_DIR = Path(router.__file__).resolve().parent


def _bash_event(command: str) -> dict:
    return {"session_id": "sib", "tool_name": "Bash", "tool_input": {"command": command}}


class TestCanonicalIdentity:
    def test_module_has_one_canonical_package_identity(self) -> None:
        # The package-relative refactor gives the sibling a SINGLE canonical
        # identity (``hooks.scripts.raw_review_post_guard``) — the old bare-name alias is gone —
        # so the module a test patches is the one the router imports.
        assert sys.modules["hooks.scripts.raw_review_post_guard"] is rrp


class TestRouterReExportReachable:
    """Handler, helper, and the moved constant resolve to ONE object across the sibling and the router."""

    def test_handler_reexport_is_the_same_object(self) -> None:
        assert router.handle_block_raw_review_post is rrp.handle_block_raw_review_post

    def test_helper_reexport_is_the_same_object(self) -> None:
        assert router._is_raw_review_write is rrp.is_raw_review_write

    def test_endpoint_regex_reexport_is_the_same_object(self) -> None:
        assert router._REVIEW_POST_ENDPOINT_RE is rrp.REVIEW_POST_ENDPOINT_RE

    def test_shared_method_regexes_have_one_definition_in_the_router(self) -> None:
        """The effective-method regexes are NOT redefined in the sibling — one definition, in the router."""
        for name in ("_GLAB_GH_API_RE", "_REVIEW_POST_METHOD_RE", "_REVIEW_POST_BODY_FLAG_RE"):
            assert hasattr(router, name), name
            assert not hasattr(rrp, name), name

    def test_router_handler_denies_a_real_review_write(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Non-vacuous: the router-driven handler denies a raw review POST via the sibling + lazy back-imports."""
        command = "glab api projects/42/merge_requests/7/discussions -X POST -f body=hi"
        assert router.handle_block_raw_review_post(_bash_event(command)) is True
        deny = json.loads(capsys.readouterr().out.strip())
        assert deny["permissionDecision"] == "deny"

    def test_patching_sibling_helper_is_seen_through_the_router(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Patching the sibling's detection helper affects the handler the router drives — one module object."""
        monkeypatch.setattr(rrp, "is_raw_review_write", lambda _command: False)
        command = "glab api projects/42/merge_requests/7/discussions -X POST -f body=hi"
        assert router.handle_block_raw_review_post(_bash_event(command)) is not True


class TestColdImport:
    def test_imports_with_stdlib_only_no_django(self) -> None:
        """A fresh interpreter imports the sibling without Django configured or teatree loaded."""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; sys.path.insert(0, sys.argv[1]); "
                    "import raw_review_post_guard as s; "
                    "assert 'django' not in sys.modules, 'django imported at module top'; "
                    "assert not any(m == 'teatree' or m.startswith('teatree.') for m in sys.modules), "
                    "'teatree imported at module top'; "
                    "print(s.handle_block_raw_review_post({'tool_name': 'Edit', 'tool_input': {}}))"
                ),
                str(_SCRIPTS_DIR),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
            env={"PATH": "/usr/bin:/bin"},
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "False"
