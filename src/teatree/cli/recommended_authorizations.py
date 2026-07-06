"""Recommended auto-mode authorizations — detection only, never applied.

Teatree deliberately ships **no** ``autoMode``/``permissions`` allow-list of
its own (BLUEPRINT §11.4 — *Plugin config is not self-modifiable by the
agent*; the classifier whitelist is the user's final say). Classifier rules
must always remain **per-user**.

What teatree *can* do is document the generic, parameterized set of
authorizations that make a teatree session friction-free, and detect which of
them are absent from the user's resolved ``~/.claude/settings.json`` so
``t3 doctor`` / ``t3 setup`` can *suggest* (never auto-apply) them.

Everything here is read-only. Nothing in this module writes the user's
settings file. User-specific items (VPS hosts, dev-DB credentials, exact
repo/paths) are deliberately **not** part of the recommended generic set —
those are the user's to add.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import typer


@dataclass(frozen=True)
class RecommendedAuthorization:
    """One recommended, generic, parameterized auto-mode authorization.

    ``sentence`` is the natural-language statement a user can paste into the
    ``autoMode.allow`` array of their own ``~/.claude/settings.json``.
    ``keyphrases`` are stable lowercase substrings used for heuristic
    presence detection: an existing ``autoMode.allow`` entry counts as
    *covering* this recommendation when it contains **every** keyphrase
    (AND semantics — each keyphrase pins one load-bearing concept so a
    loosely-worded but genuinely-covering rule still matches while an
    unrelated rule does not).
    """

    key: str
    sentence: str
    keyphrases: tuple[str, ...]

    def is_covered_by(self, existing_entries: list[str]) -> bool:
        """True when any existing entry contains all of this rec's keyphrases."""
        for entry in existing_entries:
            lowered = entry.lower()
            if all(phrase in lowered for phrase in self.keyphrases):
                return True
        return False


# The recommended generic set. Wording is teatree-generic and parameterized
# (``<overlay>``, ``<id>``, ``<phase>``) — never a specific user's hosts,
# paths, or credentials. Keep each sentence copy-pasteable as-is.
RECOMMENDED_AUTHORIZATIONS: tuple[RecommendedAuthorization, ...] = (
    RecommendedAuthorization(
        key="manage-claude-settings-and-hooks",
        sentence=(
            "Allow editing ~/.claude/settings.json and files under ~/.claude/hooks/ — "
            "teatree manages its own hooks and settings registration there."
        ),
        keyphrases=("~/.claude/settings.json", "~/.claude/hooks"),
    ),
    RecommendedAuthorization(
        key="worktree-file-writes",
        sentence=(
            "Allow editing, writing, and deleting files inside t3 worktrees "
            "as part of the normal lifecycle (implementation, cleanup)."
        ),
        keyphrases=("worktree", "deleting"),
    ),
    RecommendedAuthorization(
        key="local-dev-lifecycle-commands",
        sentence=(
            "Allow local-dev lifecycle and verification commands "
            "(pkill, docker, docker compose, pipenv, playwright, npm, npx, curl, sed) "
            "when run as part of a t3 lifecycle or verification step."
        ),
        keyphrases=("docker", "lifecycle"),
    ),
    RecommendedAuthorization(
        key="gh-pr-merge-green-only",
        sentence=(
            "Allow `gh pr merge` ONLY when the PR's required CI checks are all green "
            "(mergeStateStatus CLEAN); otherwise it stays gated."
        ),
        keyphrases=("gh pr merge", "green"),
    ),
    RecommendedAuthorization(
        key="lifecycle-visit-phase-attestation",
        sentence=(
            "Allow `t3 <overlay> lifecycle visit-phase <id> <phase> --agent-id ...` for recording phase attestation."
        ),
        keyphrases=("lifecycle visit-phase", "--agent-id"),
    ),
    RecommendedAuthorization(
        key="sanctioned-merge-path",
        sentence=(
            "Allow the sanctioned merge path — `t3 <overlay> ticket clear ...` "
            "(orchestrator, independent reviewer identity) followed by "
            "`t3 <overlay> ticket merge <clear_id>` (agent executes) — for all "
            "blast classes including substrate/self-improvement, with safety "
            "enforced in the MergeClear preconditions (independent reviewer != "
            "loop, SHA-bound, live-green required checks, substrate needs a "
            "recorded human approver). Raw `gh pr merge` / `glab mr merge` is "
            "NOT authorized by this rule."
        ),
        keyphrases=("ticket clear", "ticket merge"),
    ),
)


def _settings_path() -> Path:
    """Return the resolved ``~/.claude/settings.json`` path (follows symlinks).

    The file is frequently a dotfiles symlink; resolving means a downstream
    reader sees the real target. When the file is absent the unresolved path
    is returned so callers can report it.
    """
    path = Path.home() / ".claude" / "settings.json"
    return path.resolve() if path.is_file() else path


def load_automode_allow(settings_path: Path | None = None) -> list[str]:
    """Return the user's ``autoMode.allow`` entries (strings only).

    Degrades gracefully to ``[]`` when the file is missing, unreadable,
    not JSON, or has no ``autoMode.allow`` array. Never raises, never
    writes.
    """
    path = settings_path if settings_path is not None else _settings_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    automode = data.get("autoMode")
    if not isinstance(automode, dict):
        return []
    allow = automode.get("allow")
    if not isinstance(allow, list):
        return []
    return [entry for entry in allow if isinstance(entry, str)]


def find_missing_authorizations(
    settings_path: Path | None = None,
) -> list[RecommendedAuthorization]:
    """Return recommended authorizations absent from the user's settings.

    Read-only heuristic detection. The user's settings file is never
    modified by this function or its callers.
    """
    existing = load_automode_allow(settings_path)
    return [rec for rec in RECOMMENDED_AUTHORIZATIONS if not rec.is_covered_by(existing)]


def report_missing_authorizations(
    echo: Callable[[str], object],
    settings_path: Path | None = None,
) -> bool:
    """Print a suggestion for every absent recommended authorization.

    Read-only. Teatree never edits the user's settings — this only
    *suggests* the paste-ready sentence for each missing rule. Always
    returns ``True``: a missing recommendation is advisory, not a failed
    check. ``echo`` is injected (e.g. ``typer.echo``) so the pure module
    stays decoupled from the CLI framework.
    """
    missing = find_missing_authorizations(settings_path)
    if not missing:
        echo("OK    All recommended auto-mode authorizations present")
        return True

    echo(
        f"WARN  {len(missing)} recommended auto-mode authorization(s) absent from ~/.claude/settings.json.",
    )
    echo("      Teatree never edits your settings. To work friction-free, add the following")
    echo('      sentence(s) to the "autoMode.allow" array of your own ~/.claude/settings.json:')
    for rec in missing:
        echo("")
        echo(f"      [{rec.key}]")
        echo(f"        {rec.sentence}")
    echo("")
    echo("      User-specific items (VPS hosts, dev-DB credentials, exact repo paths) are")
    echo("      deliberately NOT recommended generically — those are yours to add.")
    return True


def authorizations() -> bool:
    """Suggest absent recommended auto-mode authorizations; re-test cached scope failures.

    Read-only for settings. As the "am I authorized" re-check surface, it also
    resets the in-process token-scope-failure cache (PR-19): once the operator
    re-runs this after fixing a token's scopes, the next call re-tests the scope
    live instead of short-circuiting on a stale cached miss.
    """
    from teatree.core.intake.scope_cache import reset_scope_cache  # noqa: PLC0415 — deferred: keep import-light

    reset_scope_cache()
    return report_missing_authorizations(typer.echo)
