"""Tests for the PreToolUse skill-loading gate's fail-open on stale skills.

The skill-loading gate (``handle_enforce_skill_loading``) blocks
Bash/Edit/Write until every suggested-but-unloaded skill is loaded. A
suggestion comes from the supplementary keyword config
(``~/.teatree-skills.yml``) or from lifecycle/intent detection, and lands
in ``<session>.pending``.

The lockout class this guards against: a ``~/.teatree-skills.yml`` entry
maps a keyword to a skill *name that no longer resolves* (renamed or
removed skill). The gate would then demand a skill the ``Skill`` tool
cannot load ("Unknown skill"), blocking ALL Bash/Edit/Write for the whole
session with no in-session self-rescue.

The fix: before blocking on a required skill, the gate verifies the name
resolves to a loadable skill (a ``<skill>/SKILL.md`` under one of the
skill search dirs). An unresolvable name does NOT block — the gate emits
a one-line warning naming the stale skill + the config file and lets the
tool through. A real-but-unloaded skill still enforces load-first.

Integration-style: the real handler, real ``STATE_DIR`` on ``tmp_path``,
real skill dirs seeded under the temp ``HOME``.
"""

import json
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import _skill_resolves, handle_enforce_skill_loading


def _seed_skill(skills_dir: Path, name: str) -> None:
    """Create a loadable ``<skills_dir>/<name>/SKILL.md`` fixture skill."""
    skill = skills_dir / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")


@pytest.fixture
def gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point ``STATE_DIR`` and ``T3_SKILL_SEARCH_DIRS`` at temp fixture trees.

    The gate's resolver scans the dirs returned by the canonical
    ``_skill_search_dirs``, which honours the ``T3_SKILL_SEARCH_DIRS``
    override. Pointing it at a temp dir exercises the real resolution path
    (rather than relying on host skill installs) and seeds one real,
    loadable skill (``ac-reviewing-codebase``) plus a lifecycle skill
    (``code``) so tests can distinguish "real but unloaded" from "stale".

    Returns the fixture skills dir so tests can add more skills if needed.
    """
    original_state = router.STATE_DIR
    router.STATE_DIR = tmp_path / "state"
    router.STATE_DIR.mkdir(parents=True, exist_ok=True)

    skills_dir = tmp_path / "skills"
    _seed_skill(skills_dir, "ac-reviewing-codebase")
    _seed_skill(skills_dir, "code")
    monkeypatch.setenv("T3_SKILL_SEARCH_DIRS", str(skills_dir))

    yield skills_dir

    router.STATE_DIR = original_state


def _write_pending(session_id: str, skills: list[str]) -> None:
    (router.STATE_DIR / f"{session_id}.pending").write_text("\n".join(skills) + "\n", encoding="utf-8")


def _write_loaded(session_id: str, skills: list[str]) -> None:
    (router.STATE_DIR / f"{session_id}.skills").write_text("\n".join(skills) + "\n", encoding="utf-8")


def _as_code_work(data: dict) -> dict:
    """Normalize the gate input so it targets Python code work.

    The gate is scoped to genuine Python/Django work (a ``.py`` Edit/Write or a
    Python-tooling Bash command). These tests exercise the orthogonal
    resolution / canonical-matching / escape logic, so each call is rewritten to
    a code-work shape while preserving any embedded ``[skill-load-ok:]`` token
    and a no-input shape (``{"tool_name": "Bash"}``) is given a code-work
    command. A test that already supplies a ``.py`` file or a Python command is
    left untouched.
    """
    tool_input = dict(data.get("tool_input") or {})
    tool_name = data.get("tool_name", "")
    if tool_name in {"Edit", "Write"} and not str(tool_input.get("file_path", "")).endswith((".py", ".pyi")):
        tool_input.setdefault("new_string", tool_input.get("new_string", "x = 1"))
        tool_input["file_path"] = "src/teatree/core/probe.py"
    elif tool_name == "Bash":
        command = str(tool_input.get("command", ""))
        if command.startswith("echo "):
            # Token-cap test: keep the long body but make the leading verb a
            # Python tool so the call counts as code work.
            tool_input["command"] = "uv run python -c '' && " + command
        elif "[skill-load-ok:" in command:
            token = command[command.find("# [skill-load-ok:") :]
            tool_input["command"] = f"uv run pytest -q  {token}".rstrip()
        else:
            tool_input["command"] = "uv run pytest -q"
    return {**data, "tool_input": tool_input}


def _run(data: dict) -> tuple[bool, dict | None, str]:
    """Invoke the gate, capturing its deny payload (stdout) and warning (stderr)."""
    out = StringIO()
    err = StringIO()
    with patch("sys.stdout", out), patch("sys.stderr", err):
        blocked = handle_enforce_skill_loading(_as_code_work(data))
    payload = None
    raw = out.getvalue().strip()
    if raw:
        payload = json.loads(raw)
    return blocked, payload, err.getvalue()


class TestPerCallSkillLoadOkEscape:
    """``[skill-load-ok: <reason>]`` in the tool call unblocks a real demand (#1567).

    The escape is the structural guarantee that a false skill-trigger can
    never wedge an autonomous loop: a genuine, resolvable, unloaded skill
    still blocks every Bash/Edit/Write that does NOT carry the token, but a
    single call carrying the token proceeds. An empty reason is rejected.
    """

    def test_token_in_bash_command_unblocks(self, gate: Path) -> None:
        _write_pending("sess-tok-bash", ["ac-reviewing-codebase"])
        blocked, payload, err = _run(
            {
                "session_id": "sess-tok-bash",
                "tool_name": "Bash",
                "tool_input": {"command": "git status  # [skill-load-ok: unrelated loop work]"},
            }
        )
        assert blocked is False
        assert payload is None
        assert "skill-load-ok" in err

    def test_token_in_edit_new_string_unblocks(self, gate: Path) -> None:
        _write_pending("sess-tok-edit", ["ac-reviewing-codebase"])
        blocked, payload, _ = _run(
            {
                "session_id": "sess-tok-edit",
                "tool_name": "Edit",
                "tool_input": {"new_string": "x = 1  # [skill-load-ok: false trigger]"},
            }
        )
        assert blocked is False
        assert payload is None

    def test_token_in_write_content_unblocks(self, gate: Path) -> None:
        _write_pending("sess-tok-write", ["ac-reviewing-codebase"])
        blocked, payload, _ = _run(
            {
                "session_id": "sess-tok-write",
                "tool_name": "Write",
                "tool_input": {"content": "# [skill-load-ok: scaffolding]\nprint('hi')\n"},
            }
        )
        assert blocked is False
        assert payload is None

    def test_empty_reason_does_not_unblock(self, gate: Path) -> None:
        _write_pending("sess-tok-empty", ["ac-reviewing-codebase"])
        blocked, payload, _ = _run(
            {
                "session_id": "sess-tok-empty",
                "tool_name": "Bash",
                "tool_input": {"command": "git status  # [skill-load-ok: ]"},
            }
        )
        assert blocked is True
        assert payload is not None
        assert payload["permissionDecision"] == "deny"

    def test_no_token_still_blocks(self, gate: Path) -> None:
        _write_pending("sess-tok-none", ["ac-reviewing-codebase"])
        blocked, payload, _ = _run(
            {
                "session_id": "sess-tok-none",
                "tool_name": "Bash",
                "tool_input": {"command": "git status"},
            }
        )
        assert blocked is True
        assert payload is not None

    def test_token_beyond_512_chars_does_not_unblock(self, gate: Path) -> None:
        _write_pending("sess-tok-far", ["ac-reviewing-codebase"])
        buried = "echo " + ("a" * 600) + " [skill-load-ok: buried]"
        blocked, payload, _ = _run(
            {
                "session_id": "sess-tok-far",
                "tool_name": "Bash",
                "tool_input": {"command": buried},
            }
        )
        assert blocked is True
        assert payload is not None


class TestStaleSkillFailsOpen:
    """A pending skill whose name does not resolve must NOT block tools."""

    @pytest.mark.parametrize("tool_name", ["Bash", "Edit", "Write"])
    def test_unresolvable_skill_does_not_block(self, gate: Path, tool_name: str) -> None:
        _write_pending("sess-stale", ["ac-auditing-repos"])
        blocked, payload, _ = _run({"session_id": "sess-stale", "tool_name": tool_name})
        assert blocked is False
        assert payload is None

    def test_unresolvable_skill_warns_with_name_and_config(self, gate: Path) -> None:
        _write_pending("sess-stale2", ["ac-auditing-repos"])
        _, _, warning = _run({"session_id": "sess-stale2", "tool_name": "Bash"})
        assert "ac-auditing-repos" in warning
        assert ".teatree-skills.yml" in warning


class TestRealUnloadedSkillStillEnforced:
    """A pending skill that DOES resolve but is unloaded still blocks (load-first)."""

    def test_real_unloaded_skill_blocks(self, gate: Path) -> None:
        _write_pending("sess-real", ["ac-reviewing-codebase"])
        blocked, payload, _ = _run({"session_id": "sess-real", "tool_name": "Bash"})
        assert blocked is True
        assert payload is not None
        assert payload["permissionDecision"] == "deny"
        assert "ac-reviewing-codebase" in payload["permissionDecisionReason"]

    def test_real_loaded_skill_passes(self, gate: Path) -> None:
        _write_pending("sess-loaded", ["ac-reviewing-codebase"])
        _write_loaded("sess-loaded", ["ac-reviewing-codebase"])
        blocked, payload, _ = _run({"session_id": "sess-loaded", "tool_name": "Bash"})
        assert blocked is False
        assert payload is None


class TestCanonicalNamespaceMatching:
    """A demand and its loaded form are matched by their fully-qualified canonical.

    ``<session>.skills`` / ``<session>.pending`` record a skill VERBATIM in
    whatever shape arrived: the Skill-tool PostToolUse records the
    NAMESPACED form (``t3:code``) while InstructionsLoaded / the loader's
    pending writer record the BARE form (``code``). Matching normalizes UP
    to the qualified canonical (``code`` → ``t3:code`` for a plugin-owned
    skill) so a bare demand is satisfied by its namespaced loaded form and
    vice versa — WITHOUT conflating distinct skills across namespaces
    (``t3:review`` ≠ ``other:review``). An unresolvable namespace fails open.

    ``code``/``rules``/``review`` are real plugin-owned lifecycle skills, so
    they canonicalize to ``t3:*``; ``ac-*`` names are not plugin-owned and
    stay bare.
    """

    def test_bare_pending_satisfied_by_namespaced_loaded(self, gate: Path) -> None:
        # The deadlock reproduction: pending demand is bare ``code`` (the
        # loader's form), loaded set has ONLY the namespaced ``t3:code`` (the
        # Skill tool's form). Pre-fix verbatim membership never matches, so
        # the gate blocks forever. Canonicalizing UP satisfies the demand.
        _write_pending("sess-bare-pending", ["code"])
        _write_loaded("sess-bare-pending", ["t3:code"])
        blocked, payload, _ = _run({"session_id": "sess-bare-pending", "tool_name": "Bash"})
        assert blocked is False
        assert payload is None

    def test_namespaced_pending_satisfied_by_bare_loaded(self, gate: Path) -> None:
        # Symmetric: pending ``t3:code`` (namespaced demand), loaded has the
        # bare ``code`` (InstructionsLoaded form) → both canonicalize to
        # ``t3:code`` and match.
        _write_pending("sess-ns-pending", ["t3:code"])
        _write_loaded("sess-ns-pending", ["code"])
        blocked, payload, _ = _run({"session_id": "sess-ns-pending", "tool_name": "Edit"})
        assert blocked is False
        assert payload is None

    def test_distinct_namespaces_are_not_conflated(self, gate: Path) -> None:
        # The qualified name is the identity: a demand for ``t3:code`` is NOT
        # satisfied by a loaded ``other:code`` (different plugin). The
        # bare-strip approach would wrongly match these; canonicalizing UP
        # keeps them distinct, so the gate still blocks. ``code`` is a real
        # plugin-owned skill (also seeded as resolvable in the fixture), so
        # the demand canonicalizes to ``t3:code``.
        _write_pending("sess-distinct", ["code"])
        _write_loaded("sess-distinct", ["other:code"])
        blocked, payload, _ = _run({"session_id": "sess-distinct", "tool_name": "Bash"})
        assert blocked is True
        assert payload is not None
        assert payload["permissionDecision"] == "deny"
        assert "/code" in payload["permissionDecisionReason"]

    def test_legacy_mixed_state_with_both_spellings(self, gate: Path) -> None:
        # Today's legacy ``.skills`` may carry the SAME skill under both the
        # bare and namespaced spelling. A bare pending demand still matches —
        # canonicalization collapses both loaded forms onto ``t3:rules``.
        _write_pending("sess-legacy", ["rules"])
        _write_loaded("sess-legacy", ["rules", "t3:rules", "t3:code"])
        blocked, payload, _ = _run({"session_id": "sess-legacy", "tool_name": "Bash"})
        assert blocked is False
        assert payload is None

    def test_genuinely_unloaded_skill_still_blocks(self, gate: Path) -> None:
        # The fix must not defang the gate: a demand with NO matching loaded
        # form (bare or namespaced) still hard-blocks.
        _write_pending("sess-still-blocks", ["code"])
        _write_loaded("sess-still-blocks", ["rules"])
        blocked, payload, _ = _run({"session_id": "sess-still-blocks", "tool_name": "Bash"})
        assert blocked is True
        assert payload is not None
        assert payload["permissionDecision"] == "deny"
        assert "/code" in payload["permissionDecisionReason"]


class TestSnapshotSymmetry:
    """The matcher resolves ``(owned, namespace)`` ONCE and threads it through both sides.

    The demand side and the loaded side must canonicalize against the SAME
    ``owned`` snapshot. If each name re-scanned the filesystem, a flaky read
    (``OSError`` returning an empty set on the second call) would have the
    demand side canonicalize ``code`` → ``t3:code`` while the loaded side
    canonicalizes ``t3:code`` → ``t3:code`` against a different (empty)
    snapshot — an asymmetric match. Resolving once kills that asymmetry: with
    a single snapshot, a flaky read can only make the WHOLE invocation strict
    (re-block, recoverable), never produce a one-sided over-match.
    """

    def test_owned_scan_called_once_per_invocation(self, gate: Path) -> None:
        _write_pending("sess-snap-once", ["code"])
        _write_loaded("sess-snap-once", ["t3:code"])

        calls = {"n": 0}
        real = router._plugin_owned_skills

        def counting() -> set[str]:
            calls["n"] += 1
            return real()

        with patch.object(router, "_plugin_owned_skills", counting):
            blocked, payload, _ = _run({"session_id": "sess-snap-once", "tool_name": "Bash"})

        assert blocked is False
        assert payload is None
        assert calls["n"] == 1, f"owned-set scanned {calls['n']} times; must resolve ONE snapshot per invocation"

    def test_flaky_scan_does_not_produce_asymmetric_overmatch(self, gate: Path) -> None:
        # If the owned-set read succeeds for the demand side then raises for
        # the loaded side, a per-name resolution would over-match (demand
        # canonicalizes to ``t3:code``, loaded falls back to verbatim
        # ``t3:code``) — clearing the gate on a flaky read. With ONE snapshot
        # the loaded ``t3:code`` (verbatim) demand ``code`` either both match
        # (snapshot populated) or both stay strict (snapshot empty); a
        # mid-invocation failure can never satisfy demand A with loaded B.
        _write_pending("sess-snap-flaky", ["code"])
        _write_loaded("sess-snap-flaky", ["rules"])  # genuinely distinct from ``code``

        calls = {"n": 0}

        def flaky() -> set[str]:
            calls["n"] += 1
            if calls["n"] >= 2:
                msg = "second scan fails"
                raise OSError(msg)
            return {"code", "rules"}

        with patch.object(router, "_plugin_owned_skills", flaky):
            blocked, payload, _ = _run({"session_id": "sess-snap-flaky", "tool_name": "Bash"})

        # A demand for ``code`` with only ``rules`` loaded must still block; a
        # second-call failure must not flip it open.
        assert blocked is True
        assert payload is not None
        assert "/code" in payload["permissionDecisionReason"]


class TestMixedResolvability:
    """A mix of a stale name and a real-unloaded name blocks only on the real one."""

    def test_blocks_on_real_warns_on_stale(self, gate: Path) -> None:
        _write_pending("sess-mixed", ["ac-auditing-repos", "ac-reviewing-codebase"])
        blocked, payload, warning = _run({"session_id": "sess-mixed", "tool_name": "Bash"})
        assert blocked is True
        assert payload is not None
        assert "ac-reviewing-codebase" in payload["permissionDecisionReason"]
        # The stale name must not appear as a load-me demand.
        assert "ac-auditing-repos" not in payload["permissionDecisionReason"]
        assert "ac-auditing-repos" in warning


class TestResolutionEdgeCases:
    """Resolution scans the canonical search dirs and the name's final segment."""

    def test_lifecycle_bare_name_resolves_and_blocks(self, gate: Path) -> None:
        # ``code`` is a lifecycle skill seeded in the fixture skills dir —
        # an unloaded lifecycle suggestion must still enforce load-first.
        _write_pending("sess-life", ["code"])
        blocked, payload, _ = _run({"session_id": "sess-life", "tool_name": "Edit"})
        assert blocked is True
        assert payload is not None
        assert "/code" in payload["permissionDecisionReason"]

    def test_bare_namespaced_name_resolves_only_verbatim(self, gate: Path) -> None:
        # A bare ``plugin:skill`` resolves ONLY when a verbatim ``t3:code``
        # skill dir exists — never by discarding the namespace onto an
        # installed bare ``code`` (that collision is the lockout class).
        _seed_skill(gate, "t3:code")
        _write_pending("sess-ns-ok", ["t3:code"])
        blocked, payload, _ = _run({"session_id": "sess-ns-ok", "tool_name": "Bash"})
        assert blocked is True
        assert payload is not None

    def test_bare_namespaced_collision_fails_open(self, gate: Path) -> None:
        # Stale bare ``old:code`` (no ``old:code`` dir) must NOT resolve onto
        # the installed bare ``code`` — fail open, do not lock out.
        _write_pending("sess-ns-stale", ["old:code"])
        blocked, payload, warning = _run({"session_id": "sess-ns-stale", "tool_name": "Bash"})
        assert blocked is False
        assert payload is None
        assert "old:code" in warning

    def test_override_dirs_are_actually_scanned(self, gate: Path) -> None:
        # Proves the gate uses the canonical T3_SKILL_SEARCH_DIRS-driven
        # resolver: a skill that exists ONLY in the override dir resolves.
        _seed_skill(gate, "freshly-seeded-skill")
        _write_pending("sess-fresh", ["freshly-seeded-skill"])
        blocked, _, _ = _run({"session_id": "sess-fresh", "tool_name": "Bash"})
        assert blocked is True

    def test_overlay_skill_path_shape_resolves(self, gate: Path) -> None:
        # An overlay suggestion is a path (``skills/<skill>/SKILL.md``), not
        # a bare name. A genuinely-installed overlay skill must still
        # enforce load-first rather than silently fail open.
        _seed_skill(gate, "t3:acme")
        _write_pending("sess-overlay", ["t3:acme/SKILL.md"])
        blocked, payload, _ = _run({"session_id": "sess-overlay", "tool_name": "Bash"})
        assert blocked is True
        assert payload is not None

    def test_generated_overlay_skill_path_spelling_resolves(self, gate: Path) -> None:
        # The overlay generator emits the exact spelling ``skills/<skill>/
        # SKILL.md``. With the skill installed at that literal path under a
        # search dir, the gate must enforce load-first.
        _seed_skill(gate / "skills", "t3:acme")
        _write_pending("sess-gen", ["skills/t3:acme/SKILL.md"])
        blocked, payload, _ = _run({"session_id": "sess-gen", "tool_name": "Bash"})
        assert blocked is True
        assert payload is not None

    def test_stale_overlay_skill_path_fails_open(self, gate: Path) -> None:
        # A path-shaped suggestion for an uninstalled overlay skill must
        # fail open (warn, not block) — the lockout-prevention contract.
        _write_pending("sess-overlay-stale", ["skills/t3:gone/SKILL.md"])
        blocked, payload, warning = _run({"session_id": "sess-overlay-stale", "tool_name": "Bash"})
        assert blocked is False
        assert payload is None
        assert "t3:gone" in warning

    def test_stale_overlay_path_does_not_collide_with_bare_skill(self, gate: Path) -> None:
        # A stale overlay path whose dir name's post-colon suffix collides
        # with an installed BARE skill (``code``) must NOT resolve onto it —
        # the overlay dir is matched verbatim (``t3:code``), so the gate
        # fails open instead of locking out on the renamed-away path.
        _write_pending("sess-collide", ["skills/t3:code/SKILL.md"])
        blocked, payload, warning = _run({"session_id": "sess-collide", "tool_name": "Bash"})
        assert blocked is False
        assert payload is None
        assert "t3:code" in warning

    def test_empty_segment_is_not_enforceable(self, gate: Path) -> None:
        # Degenerate names must never resolve — proven against a NON-empty
        # search dir (the fixture seeds real skills) so the guard does not
        # depend on an empty dir list.
        dirs = [gate]
        assert _skill_resolves("", dirs) is False
        assert _skill_resolves("SKILL.md", dirs) is False
        assert _skill_resolves("/SKILL.md", dirs) is False
        assert _skill_resolves("ns:", dirs) is False
