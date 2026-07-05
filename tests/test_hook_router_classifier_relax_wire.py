"""Subprocess wire-test harness for the classifier-relax PreToolUse verdict (#3, #4).

The existing classifier-relax tests call ``handle_allow_classifier_relax_settings_write``
in-process and read the stdout payload. They NEVER exercise ``main()``'s exit-code
translation — which is exactly why #3 shipped: the sanctioned allow emitted a bare
legacy ``{"permissionDecision": "allow"}`` and returned ``True``, and ``main()``
translated that ``True`` into ``sys.exit(2)`` (a BLOCK). The human approval acted as a
block and the blocked attempt burned the one-shot consent — invisible to an in-process
test that only reads the emitted JSON.

This harness runs ``hook_router.py --event PreToolUse`` as a real subprocess so the
process exit code propagates through ``sys.exit``, and asserts BOTH the exit code and
the emitted envelope shape. It is the wire-level anti-vacuity proof for #3/#4 and the
reusable harness GM-2 extends for the Lane-B hard-deny parity.
"""

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

HOOK_ROUTER = Path(__file__).resolve().parent.parent / "hooks" / "scripts" / "hook_router.py"


def run_hook_router(event: str, payload: dict, *, home: str) -> subprocess.CompletedProcess[str]:
    """Run ``hook_router.py --event <event>`` as a subprocess with a controlled HOME.

    ``home`` isolates ``~/.claude/settings.json`` (the classifier-relax target) and
    ``~/.teatree.toml`` (the fail-open kill-switch read) from the developer's real
    config, so the deny/allow assertions depend only on the payload + transcript.
    ``Path.home()`` honours ``HOME`` on POSIX.
    """
    env = {**os.environ, "HOME": home, "USERPROFILE": home}
    return subprocess.run(
        [sys.executable, str(HOOK_ROUTER), "--event", event],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
        env=env,
    )


# ── Transcript builders (mirror test_classifier_relax_pretooluse_hook.py) ──


def _assistant(text: str, tool_uses: list[dict] | None = None) -> dict:
    content: list[dict] = []
    if text:
        content.append({"type": "text", "text": text})
    content.extend({"type": "tool_use", "name": tu["name"], "input": tu.get("input", {})} for tu in tool_uses or [])
    return {"type": "assistant", "message": {"role": "assistant", "content": content}}


def _user(text: str = "go") -> dict:
    return {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _ask_relax_question() -> dict:
    return {
        "name": "AskUserQuestion",
        "input": {
            "questions": [
                {"question": "?", "options": ["Allow it (relax classifier)", "Keep the denial (do it differently)"]}
            ]
        },
    }


@contextmanager
def _sanctioned_home() -> Iterator[tuple[str, str, str]]:
    """Yield ``(home, settings_path, transcript_path)`` with a Step-3 approval on record.

    The transcript carries the sanctioned classifier-relax AskUserQuestion + an
    affirmative user turn and NO settings.json write since, so the allow gate's
    consume-once consent is live.
    """
    with tempfile.TemporaryDirectory() as home:
        settings = str(Path(home) / ".claude" / "settings.json")
        transcript = Path(home) / "transcript.jsonl"
        entries = [
            _user("file the issue"),
            _assistant("The command was denied. Choose:", tool_uses=[_ask_relax_question()]),
            _user("Allow it (relax classifier)"),
        ]
        transcript.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
        yield home, settings, str(transcript)


class TestSanctionedAllowExitsZeroWithNestedEnvelope:
    """#3: a sanctioned classifier-relax allow must exit 0 with the nested allow shape.

    RED before the fix: the handler emitted flat ``{"permissionDecision": "allow"}``
    and returned ``True``, so ``main()`` exited 2 (a BLOCK) and there was no
    ``hookSpecificOutput`` envelope for the harness to read.
    """

    def test_valid_write_allow_exits_0_and_nested_envelope(self) -> None:
        with _sanctioned_home() as (home, settings, transcript):
            payload = {
                "tool_name": "Write",
                "tool_input": {
                    "file_path": settings,
                    "content": '{"permissions": {"allow": ["Bash(gh issue create *)"]}}',
                },
                "transcript_path": transcript,
            }
            result = run_hook_router("PreToolUse", payload, home=home)

        assert result.returncode == 0, (
            f"a sanctioned allow must exit 0, not the deny exit 2 (#3); got {result.returncode}; "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        out = json.loads(result.stdout)
        nested = out.get("hookSpecificOutput")
        assert nested is not None, "the allow must carry the nested hookSpecificOutput envelope the harness reads"
        assert nested.get("hookEventName") == "PreToolUse"
        assert nested.get("permissionDecision") == "allow"


class TestSanctionedDenyExitsTwo:
    """A schema-invalid write WITH the approval must still deny — and deny exits 2."""

    def test_malformed_write_denied_exits_2(self) -> None:
        with _sanctioned_home() as (home, settings, transcript):
            payload = {
                "tool_name": "Write",
                "tool_input": {"file_path": settings, "content": "{ broken json"},
                "transcript_path": transcript,
            }
            result = run_hook_router("PreToolUse", payload, home=home)

        assert result.returncode == 2, (
            f"a malformed sanctioned write must deny (exit 2); got {result.returncode}; stdout={result.stdout!r}"
        )
        out = json.loads(result.stdout)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


class TestJsonKeyEditIsNotFalseBlocked:
    """#4: an Edit whose new_string carries JSON KEYS (``"allow":``) must NOT be blocked.

    RED before the fix: ``_new_string_adds_blanket_rule`` extracted every quoted token,
    so the JSON key ``allow`` matched the bare-tool blanket-rule pattern and the write
    was DENIED (exit 2) — burning the consume-once approval on a false-block.
    """

    def test_edit_adding_scoped_rule_under_allow_key_exits_0(self) -> None:
        with _sanctioned_home() as (home, settings, transcript):
            # No settings.json seeded → the applied-content path is unreadable, so the
            # raw-fragment fallback runs. Its regex must exclude the ``"allow":`` key.
            payload = {
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": settings,
                    "old_string": "",
                    "new_string": '  "permissions": {\n    "allow": [\n      "Bash(uv run pytest:*)",',
                },
                "transcript_path": transcript,
            }
            result = run_hook_router("PreToolUse", payload, home=home)

        assert result.returncode == 0, (
            f'an Edit adding a scoped rule under the "allow": key must not be blocked (#4); '
            f"got {result.returncode}; stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        out = json.loads(result.stdout)
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


class TestBareToolListEntryStillRefused:
    """#4 not-weakened: a bare-tool list entry (``"Bash",``) after a key is STILL refused.

    The regex fix excludes JSON keys, but a whole-tool blanket grant that IS a list
    entry (followed by ``,``/``]``, not ``:``) must keep failing — the smallest-rule
    protocol is not relaxed.
    """

    def test_edit_adding_bare_bash_rule_denied_exits_2(self) -> None:
        with _sanctioned_home() as (home, settings, transcript):
            payload = {
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": settings,
                    "old_string": "",
                    "new_string": '  "permissions": {\n    "allow": [\n      "Bash",',
                },
                "transcript_path": transcript,
            }
            result = run_hook_router("PreToolUse", payload, home=home)

        assert result.returncode == 2, (
            f"a bare-tool blanket rule must still be refused; got {result.returncode}; stdout={result.stdout!r}"
        )
        out = json.loads(result.stdout)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
