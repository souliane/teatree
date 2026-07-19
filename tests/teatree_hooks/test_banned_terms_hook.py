import json
import os
import shutil
import sqlite3
import subprocess
import tomllib
from pathlib import Path

import pytest

from hooks.scripts.hook_router import handle_banned_terms_pretool
from teatree import find_project_root
from teatree.hooks import _repo_visibility, banned_terms_scanner
from teatree.hooks._command_parser import is_fail_closed_sentinel


def _seed_config_db(db_path: Path, rows: dict) -> None:
    """Seed a cold config DB from a parsed ``[teatree]`` table (the DB-home config store)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        for key, value in rows.items():
            conn.execute(
                "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', ?, ?)",
                (key, json.dumps(value)),
            )
        conn.commit()
    finally:
        conn.close()


def _stage_hook_config(tmp_path: Path, terms: list[str]) -> tuple[Path, Path]:
    """Stage the DB-home banned-terms config for the pre-commit hook subprocess.

    Returns ``(home, db)``. ``db`` is the DB-home config store the
    ``check-banned-terms.sh`` CLI resolves via ``T3_CONFIG_DB`` — the only tier
    the CLI consults for the term list.
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    db = home / "config.sqlite3"
    _seed_config_db(db, {"banned_terms": terms})
    return home, db


@pytest.fixture(autouse=True)
def _public_teatree_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The leak gate enforces ONLY on an affirmatively-PUBLIC target (#1415), so a
    # must-BLOCK row toward the genuinely-public ``souliane/teatree`` needs the
    # probe to CONFIRM it public. Delegate every other slug to the real probe so a
    # per-test ``_resolve_probe_tool``/``probe_visibility`` shim still governs it
    # (an unknown/unresolvable target then SKIPS). Isolate the visibility cache.
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "viscache"))
    real_probe = _repo_visibility.probe_visibility
    monkeypatch.setattr(
        _repo_visibility,
        "probe_visibility",
        lambda slug: "PUBLIC" if "souliane/teatree" in slug else real_probe(slug),
    )


@pytest.mark.integration
def test_banned_terms_hook_flags_a_configured_term(tmp_path: Path) -> None:
    home, db = _stage_hook_config(tmp_path, ["acme"])

    sample = tmp_path / "README.md"
    sample.write_text("acme overlay\n", encoding="utf-8")

    root = find_project_root()
    assert root is not None
    script = root / "scripts" / "hooks" / "check-banned-terms.sh"
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["T3_CONFIG_DB"] = str(db)

    result = subprocess.run(
        [str(script), str(sample)],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )

    assert result.returncode == 1
    assert "BANNED TERM" in result.stdout


@pytest.mark.integration
def test_banned_terms_hook_ignores_matches_inside_email_addresses(tmp_path: Path) -> None:
    home, db = _stage_hook_config(tmp_path, ["internalterm"])

    sample = tmp_path / "AGENTS.md"
    sample.write_text("Git author: adrien <adrien.cossa@internalterm.example>\n", encoding="utf-8")

    root = find_project_root()
    assert root is not None
    script = root / "scripts" / "hooks" / "check-banned-terms.sh"
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["T3_CONFIG_DB"] = str(db)

    result = subprocess.run(
        [str(script), str(sample)],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )

    assert result.returncode == 0


def _git(cwd: Path, *args: str) -> None:
    git_bin = shutil.which("git")
    assert git_bin is not None
    subprocess.run(
        [git_bin, *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )


@pytest.mark.integration
def test_banned_terms_fails_closed_on_probe_error_for_resolvable_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # #3442 fail closed: a RESOLVABLE target (the cwd remote resolves the slug)
    # whose visibility the probe cannot confirm (no probe tool resolvable, not in
    # the allowlist) is NOT provably non-public, so the leak gate SCANS and the
    # banned term BLOCKS -- a probe error is not a licence to skip. (A genuinely
    # UNRESOLVABLE target -- no slug at all -- still skips; see
    # ``test_live_hook_skips_unresolvable_target``.)
    home = Path(os.environ["HOME"])  # the conftest-isolated HOME
    _write_home_config(home, '[teatree]\nbanned_terms = ["acmewidget"]\n', monkeypatch, tmp_path)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "remote", "add", "origin", "git@github.com:acme/secret-product.git")

    # The probe tool is unreachable in-hook -> visibility "unknown" -> the gate
    # fails closed and scans. Patching the resolver (not PATH) keeps the shell
    # scanner's ``bash``/``grep`` reachable so the fire finds the term.
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)
    monkeypatch.delenv("GH_REPO", raising=False)

    data = {
        "tool_name": "Bash",
        "tool_input": {"command": 'gh issue create --body "rolling out acmewidget"'},
        "cwd": str(repo),
    }
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is True
    assert json.loads(captured.out)["permissionDecision"] == "deny"


def test_banned_terms_allowed_when_target_resolvable_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # When the target IS allowlisted-private the destination gate SKIPS the
    # scan entirely (#1672 -- ``private_repos`` drives the destination skip),
    # so the post is allowed with no deny and no unknown NOTE. The hint only
    # fires on a genuine unknown-target block.
    home = Path(os.environ["HOME"])  # the conftest-isolated HOME
    _write_home_config(
        home,
        '[teatree]\nbanned_terms = ["acmewidget"]\nprivate_repos = ["acme/secret-product"]\n',
        monkeypatch,
        tmp_path,
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "remote", "add", "origin", "git@github.com:acme/secret-product.git")
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)
    monkeypatch.delenv("GH_REPO", raising=False)

    data = {
        "tool_name": "Bash",
        "tool_input": {"command": 'gh issue create --body "rolling out acmewidget"'},
        "cwd": str(repo),
    }
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is False
    assert captured.out == ""  # no deny JSON
    assert "visibility unknown" not in captured.err


def _write_home_config(home: Path, body: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db = home / "config.sqlite3"
    _seed_config_db(db, tomllib.loads(body).get("teatree", {}))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("T3_CONFIG_DB", str(db))
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))


def _public_clone(tmp_path: Path) -> Path:
    repo = tmp_path / "public-clone"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "remote", "add", "origin", "git@github.com:souliane/teatree.git")
    return repo


def test_live_hook_allows_customer_term_to_probe_private_target_from_public_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # LIVE entry-point regression: the harness cwd is the PUBLIC teatree clone,
    # the post targets a PROVABLY-private ``--repo`` the allowlist does not name.
    # The over-block was the live path classifying the destination from the
    # ambient cwd; the gate must resolve the target FROM THE COMMAND and consult
    # the probe, then SKIP the scan for the private target.
    home = Path(os.environ["HOME"])
    _write_home_config(home, '[teatree]\nbanned_terms = ["acmewidget"]\n', monkeypatch, tmp_path)
    monkeypatch.setattr(
        _repo_visibility,
        "probe_visibility",
        lambda slug: "PRIVATE" if "privowner/private-svc" in slug else None,
    )
    monkeypatch.delenv("GH_REPO", raising=False)

    data = {
        "tool_name": "Bash",
        "tool_input": {"command": 'gh issue comment 5 --repo privowner/private-svc --body "rolling out acmewidget"'},
        "cwd": str(_public_clone(tmp_path)),
    }
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is False
    assert captured.out == ""  # no deny JSON


_PUBLIC_ONLY_CONFIG = '[teatree]\nbanned_terms = ["customercorp"]\nprivate_repos = ["customer-org"]\n'


def test_live_hook_blocks_customer_term_to_public_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # LIVE entry-point must-DENY: a customer term toward the genuinely-public
    # repo (the harness cwd) is a leak and the live path DENIES.
    home = Path(os.environ["HOME"])
    _write_home_config(home, _PUBLIC_ONLY_CONFIG, monkeypatch, tmp_path)
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)
    monkeypatch.delenv("GH_REPO", raising=False)

    data = {
        "tool_name": "Bash",
        "tool_input": {"command": 'gh issue comment 5 --repo souliane/teatree --body "customercorp leak"'},
        "cwd": str(_public_clone(tmp_path)),
    }
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is True
    decision = json.loads(captured.out)
    assert decision["permissionDecision"] == "deny"


# The banned term is the org PREFIX (``customercorp``); the configured private repo
# is the full ``<org>-engineering`` namespace. A body citing the customer repo's own
# work-item URL tokenizes ``customercorp`` out of that URL -- the address of the repo,
# not a leak -- so a PUBLIC post whose ONLY occurrence is inside that URL must downgrade.
_OWN_URL_CONFIG = '[teatree]\nbanned_terms = ["customercorp"]\nprivate_repos = ["customercorp-engineering"]\n'
_OWN_REPO_URL = "https://gitlab.com/customercorp-engineering/their-svc/-/issues/8223"


def test_live_hook_allows_customer_term_only_inside_own_repo_url_to_public_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # LIVE entry-point must-WARN: a PUBLIC teatree post whose body's only occurrence
    # of the customer term is inside the customer repo's own work-item URL is the
    # ADDRESS of that repo, not a leak. The gate downgrades to a stderr warning.
    home = Path(os.environ["HOME"])
    _write_home_config(home, _OWN_URL_CONFIG, monkeypatch, tmp_path)
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)
    monkeypatch.delenv("GH_REPO", raising=False)

    data = {
        "tool_name": "Bash",
        "tool_input": {
            "command": f'gh issue comment 5 --repo souliane/teatree --body "Tracked upstream — see {_OWN_REPO_URL}"'
        },
        "cwd": str(_public_clone(tmp_path)),
    }
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is False
    assert captured.out == ""  # no deny JSON
    assert "own configured repo" in captured.err


def test_live_hook_blocks_bare_customer_term_alongside_own_repo_url_to_public_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # LIVE entry-point must-DENY: the same URL is present, but the customer term
    # ALSO appears as a bare word outside it. A bare occurrence is a genuine leak,
    # so the URL carve-out must NOT vouch for it -- the public post hard-blocks.
    home = Path(os.environ["HOME"])
    _write_home_config(home, _OWN_URL_CONFIG, monkeypatch, tmp_path)
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)
    monkeypatch.delenv("GH_REPO", raising=False)

    data = {
        "tool_name": "Bash",
        "tool_input": {
            "command": (
                "gh issue comment 5 --repo souliane/teatree "
                f'--body "Rolling out the customercorp integration — see {_OWN_REPO_URL}"'
            )
        },
        "cwd": str(_public_clone(tmp_path)),
    }
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is True
    decision = json.loads(captured.out)
    assert decision["permissionDecision"] == "deny"


def test_live_hook_allows_customer_term_to_colleague_private_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # LIVE entry-point must-ALLOW: a customer term toward the customer's own
    # (colleague) private repo is allowed -- the leak gate enforces on PUBLIC
    # targets only. The target is resolved FROM THE COMMAND (--repo), not the
    # ambient public-clone cwd.
    home = Path(os.environ["HOME"])
    _write_home_config(home, _PUBLIC_ONLY_CONFIG, monkeypatch, tmp_path)
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)
    monkeypatch.delenv("GH_REPO", raising=False)

    data = {
        "tool_name": "Bash",
        "tool_input": {"command": 'gh issue comment 5 --repo customer-org/their-svc --body "customercorp note"'},
        "cwd": str(_public_clone(tmp_path)),
    }
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is False
    assert captured.out == ""  # no deny JSON


def test_live_hook_allows_customer_term_on_git_c_commit_to_private_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # LIVE entry-point must-ALLOW (commit path): a customer term in a
    # ``git -C <worktree> commit`` subject whose repo resolves FROM THE COMMAND
    # to a private repo is allowed, even though the harness cwd is the public
    # clone -- the cwd->target resolution part of the fix.
    home = Path(os.environ["HOME"])
    _write_home_config(home, _PUBLIC_ONLY_CONFIG, monkeypatch, tmp_path)
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)

    worktree = tmp_path / "wt"
    worktree.mkdir()
    _git(worktree, "init", "-b", "main")
    _git(worktree, "remote", "add", "origin", "git@gitlab.com:customer-org/their-svc.git")

    data = {
        "tool_name": "Bash",
        "tool_input": {"command": f'git -C {worktree} commit -m "customercorp feature"'},
        "cwd": str(_public_clone(tmp_path)),
    }
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is False
    assert captured.out == ""  # no deny JSON


# #2067: the private_repos entry is HOST-QUALIFIED (the form `slug_for_cwd`
# emits and the carve-out doc states -- ``host/owner/repo``), while
# ``gh pr create --repo`` supplies a BARE ``owner/repo`` slug. The carve-out
# must still recognise the bare flag slug as that private repo.
_HOST_QUALIFIED_CONFIG = (
    '[teatree]\nbanned_terms = ["customercorp"]\nprivate_repos = ["github.com/customer-org/their-svc"]\n'
)


def test_live_hook_allows_customer_term_to_host_qualified_private_repo_via_bare_repo_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # #2067 must-WARN: a host-qualified `private_repos` entry must downgrade a
    # `gh pr create --repo owner/name` (bare flag) post on the repo's own term.
    home = Path(os.environ["HOME"])
    _write_home_config(home, _HOST_QUALIFIED_CONFIG, monkeypatch, tmp_path)
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)
    monkeypatch.delenv("GH_REPO", raising=False)

    data = {
        "tool_name": "Bash",
        "tool_input": {
            "command": 'gh pr create --repo customer-org/their-svc --title fix --body "customercorp rollout"'
        },
        "cwd": str(_public_clone(tmp_path)),
    }
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is False
    assert captured.out == ""  # no deny JSON


def test_live_hook_blocks_customer_term_to_public_repo_under_host_qualified_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # #2067 must-BLOCK: a host-qualified private allowlist must NOT weaken the
    # public surface -- a customer term toward the public repo still hard-blocks.
    home = Path(os.environ["HOME"])
    _write_home_config(home, _HOST_QUALIFIED_CONFIG, monkeypatch, tmp_path)
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)
    monkeypatch.delenv("GH_REPO", raising=False)

    data = {
        "tool_name": "Bash",
        "tool_input": {"command": 'gh pr create --repo souliane/teatree --title fix --body "customercorp leak"'},
        "cwd": str(_public_clone(tmp_path)),
    }
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is True
    decision = json.loads(captured.out)
    assert decision["permissionDecision"] == "deny"


# A banned term that is a SUBSTRING TOKEN of the own private-repo slug (the org
# prefix of ``acmecorp-engineering``), so the work-item URL ``host/acmecorp-
# engineering/.../-/issues/N`` tokenizes ``acmecorp`` out of it. The own-slug
# downgrade (#1951) keys on token-CONTAINMENT, so this prefix qualifies as the
# repo's own identity -- but ONLY when the commit lands in that private repo.
_SUBSTRING_TERM_CONFIG = '[teatree]\nbanned_terms = ["acmecorp"]\nprivate_repos = ["acmecorp-engineering"]\n'


def _private_worktree(tmp_path: Path, name: str = "wt") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "remote", "add", "origin", "git@gitlab.com:acmecorp-engineering/acmecorp-product.git")
    return repo


def _commit_msg_file(repo: Path) -> Path:
    msg = repo / "COMMIT_MSG.txt"
    msg.write_text(
        "feat: deadline work\n\nSee https://gitlab.com/acmecorp-engineering/acmecorp-client-workspace/-/issues/8223\n",
        encoding="utf-8",
    )
    return msg


def test_live_hook_allows_substring_term_on_bare_commit_in_private_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The legitimate #1958 case: a BARE ``git commit -F <abs body file>`` whose
    # harness cwd IS the private worktree (where git actually runs and the commit
    # lands). The substring own-slug term in the body must WARN, not hard-block --
    # the commit lands in the repo's own private worktree.
    home = Path(os.environ["HOME"])
    _write_home_config(home, _SUBSTRING_TERM_CONFIG, monkeypatch, tmp_path)
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)

    worktree = _private_worktree(tmp_path)
    msg = _commit_msg_file(worktree)
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": f"git commit -F {msg}"},
        "cwd": str(worktree),
    }
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is False
    assert captured.out == ""  # no deny JSON


def test_live_hook_downgrades_bare_commit_with_readable_body_file_divergent_public_landing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # #1415 Case A: a BARE ``git commit -F <abs readable body file>`` whose harness
    # cwd is a public-but-UNKNOWN repo (the common cold-hook state -- no probe tool).
    # The commit lands in the cwd repo. Since the readable-body commit path now
    # matches the unreadable-body path, this DOWNGRADES to warn: a commit is LOCAL,
    # so the banned term reaches no public surface until a push, and the #703
    # pre-push gate re-scans commit messages before they reach a public remote. The
    # public-surface anti-vacuity is the chained-public-post guard (below) and the
    # pure ``gh``/``glab`` public post path.
    home = Path(os.environ["HOME"])
    _write_home_config(home, _SUBSTRING_TERM_CONFIG, monkeypatch, tmp_path)
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)

    private_worktree = _private_worktree(tmp_path)
    msg = _commit_msg_file(private_worktree)
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": f"git commit -F {msg}"},
        "cwd": str(_public_clone(tmp_path)),
    }
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is False  # downgraded to warn, not denied
    assert captured.out == ""  # no deny JSON


def test_live_hook_downgrades_bare_commit_with_readable_body_file_in_public_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # #1415 Case A: a bare ``git commit -F <abs readable body file>`` landing in a
    # PUBLIC repo (cwd == that public repo) DOWNGRADES to warn. A commit is LOCAL and
    # the #703 pre-push gate re-scans commit messages before a public push, so the
    # readable-body commit path matches the unreadable-body path.
    home = Path(os.environ["HOME"])
    _write_home_config(home, _SUBSTRING_TERM_CONFIG, monkeypatch, tmp_path)
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)

    public_repo = _public_clone(tmp_path)
    msg = _commit_msg_file(public_repo)
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": f"git commit -F {msg}"},
        "cwd": str(public_repo),
    }
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is False  # downgraded to warn, not denied
    assert captured.out == ""  # no deny JSON


def test_live_hook_blocks_private_commit_chained_to_public_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # SAFETY (#1958, chain-proof): a bare commit landing in the private worktree
    # cwd, chained to a PUBLIC ``gh issue create`` carrying the term, must NOT
    # downgrade. ``is_git_commit_command`` matches the FIRST segment only, so the
    # per-segment chain proof must still defeat the downgrade.
    home = Path(os.environ["HOME"])
    _write_home_config(home, _SUBSTRING_TERM_CONFIG, monkeypatch, tmp_path)
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)

    worktree = _private_worktree(tmp_path)
    msg = _commit_msg_file(worktree)
    chained = f'git commit -F {msg} && gh issue create --repo souliane/teatree --title x --body "acmecorp leak"'
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": chained},
        "cwd": str(worktree),
    }
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is True
    decision = json.loads(captured.out)
    assert decision["permissionDecision"] == "deny"


# ── #1415 misfire regression suite ──────────────────────────────────────────
#
# Four reported over-block surfaces. FM2 (SSH-alias) was genuinely RED on the
# pre-fix code and is closed by the ``slug_for_cwd`` dotless-alias normalization
# in ``_repo_visibility``. FM1/FM3/FM4 are kept here as live-hook regression
# LOCKS over the exact reported command shapes: they assert the gate does NOT
# fire on a branch-name-only term, a clean/own-private body-file, or a no-body
# help invocation, so a future change to the body-walker / destination-skip /
# publish-detection cannot silently re-introduce the misfire. Each allow case is
# paired with an anti-vacuity must-block guard.

_FM_CONFIG = '[teatree]\nbanned_terms = ["acmewidget"]\n'
_FM_PRIVATE_CONFIG = '[teatree]\nbanned_terms = ["acmewidget"]\nprivate_repos = ["owner-org"]\n'

_FM1_HEAD_ONLY = 'gh pr create --head ac-acmewidget-feature --title "Add feature" --body "clean public body"'
_FM1_REF_FLAGS = "gh pr create -H acmewidget-topic --base acmewidget-main --title T --body clean"
_FM1_BODY_TERM = 'gh pr create --head clean-branch --title T --body "rolling out acmewidget"'
_FM3_CLEAN_BODY_FILE = 'gh pr create --title "Release" --body-file {body}'
_FM3_PRIVATE_UNRESOLVABLE = "gh pr create --repo owner-org/private-product --title T --body-file /no/such/body.md"
_FM3_PUBLIC_UNRESOLVABLE = "gh pr create --repo souliane/teatree --title T --body-file /no/such/body.md"


def test_fm1_term_only_in_head_branch_name_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # FM1 (#1415): a banned term that appears ONLY in the ``--head`` branch name
    # of a ``gh pr create`` -- never in the published title/body -- must NOT block.
    # The body walker is an allowlist of body-bearing flags, so a non-body flag
    # value (branch ref) never enters the scanned payload.
    home = Path(os.environ["HOME"])
    _write_home_config(home, _FM_CONFIG, monkeypatch, tmp_path)
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)

    data = {"tool_name": "Bash", "tool_input": {"command": _FM1_HEAD_ONLY}, "cwd": str(_public_clone(tmp_path))}
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is False
    assert captured.out == ""  # no deny JSON


def test_fm1_term_in_base_and_short_head_flags_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # FM1 variant: the ``-H`` / ``--base`` ref flags carry the term, body clean.
    home = Path(os.environ["HOME"])
    _write_home_config(home, _FM_CONFIG, monkeypatch, tmp_path)
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)

    data = {"tool_name": "Bash", "tool_input": {"command": _FM1_REF_FLAGS}, "cwd": str(_public_clone(tmp_path))}
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is False
    assert captured.out == ""


def test_fm1_term_in_actual_body_still_blocks_on_public_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # FM1 anti-vacuity guard: the SAME public command WITH the term in the real
    # ``--body`` must STILL block. This proves the FM1 allow tests are not green
    # merely because the gate stopped firing on this surface entirely.
    home = Path(os.environ["HOME"])
    _write_home_config(home, _FM_CONFIG, monkeypatch, tmp_path)
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)

    data = {"tool_name": "Bash", "tool_input": {"command": _FM1_BODY_TERM}, "cwd": str(_public_clone(tmp_path))}
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is True
    assert json.loads(captured.out)["permissionDecision"] == "deny"


def test_fm2_useratalias_ssh_remote_own_private_repo_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # FM2 (#1415) LIVE: a clone whose ``origin`` uses a per-account dotless SSH
    # config alias (``git@gh-acct:owner-org/private-product``) is the user's OWN
    # known-private repo. The post must downgrade/skip via the offline allowlist
    # with no probe tool -- before the slug fix the dotless ``gh-acct`` segment
    # broke the allowlist match and the own private repo over-blocked.
    home = Path(os.environ["HOME"])
    _write_home_config(home, _FM_PRIVATE_CONFIG, monkeypatch, tmp_path)
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)
    monkeypatch.delenv("GH_REPO", raising=False)

    repo = tmp_path / "alias-clone"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "remote", "add", "origin", "git@gh-acct:owner-org/private-product.git")

    data = {
        "tool_name": "Bash",
        "tool_input": {"command": 'gh issue create --body "rolling out acmewidget"'},
        "cwd": str(repo),
    }
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is False
    assert captured.out == ""  # no deny JSON


def test_fm2_useratalias_ssh_remote_public_repo_still_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # FM2 anti-vacuity guard: a clone using a dotless SSH alias but pointing at a
    # PUBLIC repo NOT in the allowlist must STILL block a customer term -- the
    # alias normalization must not blanket-downgrade every aliased remote.
    home = Path(os.environ["HOME"])
    _write_home_config(home, _FM_PRIVATE_CONFIG, monkeypatch, tmp_path)
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)
    monkeypatch.delenv("GH_REPO", raising=False)

    repo = tmp_path / "alias-public"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "remote", "add", "origin", "git@gh-acct:souliane/teatree.git")

    data = {
        "tool_name": "Bash",
        "tool_input": {"command": 'gh issue create --body "rolling out acmewidget"'},
        "cwd": str(repo),
    }
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is True
    assert json.loads(captured.out)["permissionDecision"] == "deny"


def test_fm3_clean_body_file_to_public_repo_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # FM3 (#1415): a ``--body-file`` pointing at an EXISTING, clean file on a
    # public repo must NOT fail-closed -- the file is read at scan time and the
    # clean body produces no match.
    home = Path(os.environ["HOME"])
    _write_home_config(home, _FM_CONFIG, monkeypatch, tmp_path)
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)

    body = tmp_path / "body.md"
    body.write_text("A perfectly clean public release note.\n", encoding="utf-8")

    data = {
        "tool_name": "Bash",
        "tool_input": {"command": _FM3_CLEAN_BODY_FILE.format(body=body)},
        "cwd": str(_public_clone(tmp_path)),
    }
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is False
    assert captured.out == ""


def test_fm3_unresolvable_body_file_to_own_private_repo_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # FM3 (#1415): an UNRESOLVABLE ``--body-file`` whose destination is the user's
    # OWN allowlisted-private repo must NOT fail-closed -- the destination skip
    # precedes the body scan, so an own/private post is never blocked on an
    # unreadable body.
    home = Path(os.environ["HOME"])
    _write_home_config(home, _FM_PRIVATE_CONFIG, monkeypatch, tmp_path)
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)

    data = {
        "tool_name": "Bash",
        "tool_input": {"command": _FM3_PRIVATE_UNRESOLVABLE},
        "cwd": str(_public_clone(tmp_path)),
    }
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is False
    assert captured.out == ""


def test_fm3_unresolvable_body_file_to_public_repo_still_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # FM3 anti-vacuity guard: an UNRESOLVABLE ``--body-file`` to a PUBLIC repo
    # must STILL fail closed (an unscanned body must not slip onto a public
    # surface) -- the FM3 allow cases must not have weakened the real protection.
    home = Path(os.environ["HOME"])
    _write_home_config(home, _FM_CONFIG, monkeypatch, tmp_path)
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)

    data = {
        "tool_name": "Bash",
        "tool_input": {"command": _FM3_PUBLIC_UNRESOLVABLE},
        "cwd": str(_public_clone(tmp_path)),
    }
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is True
    assert json.loads(captured.out)["permissionDecision"] == "deny"


def test_fm4_help_invocation_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # FM4 (#1415): a ``--help`` invocation of a publish subcommand carries no
    # publish body, so the gate must not fire on it (an empty payload is a no-op,
    # not a fail-closed unresolvable body).
    home = Path(os.environ["HOME"])
    _write_home_config(home, _FM_CONFIG, monkeypatch, tmp_path)
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)

    cwd = str(_public_clone(tmp_path))
    for command in ("glab mr note --help", "gh pr create --help", "gh issue comment -h"):
        data = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}
        blocked = handle_banned_terms_pretool(data)
        captured = capsys.readouterr()
        assert blocked is False, command
        assert captured.out == "", command


# ── #1415: unreadable commit body on a PRIVATE repo must downgrade ────────────
#
# An UNREADABLE ``git commit -F <file>`` body (the file does not exist at cold-
# hook scan time -- the agent's standard "write the body file, commit it in the
# next call" idiom, or a relative path the reset hook cwd cannot reach) produced
# the fail-closed sentinel and HARD-BLOCKED with "The publish body could not be
# read", even when the commit lands in a known-PRIVATE repo. A private-repo
# commit is not a public surface -- the body lands in private history regardless
# of whether the gate could read it -- so an unread body cannot leak and must
# downgrade to warn. The payload-driven carve-out fails closed on the sentinel,
# so the fix routes the unreadable-body marker through a body-INDEPENDENT
# private-destination check. The PUBLIC-surface fail-closed protection is
# preserved by the paired anti-vacuity guards above (FM3) and below.


def test_live_hook_allows_unreadable_commit_body_file_in_private_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # RED→GREEN (#1415): a ``git commit -F <nonexistent file>`` whose harness cwd
    # IS a known-private worktree must DOWNGRADE to warn, not hard-block on
    # "publish body could not be read". The body is unreadable at scan time, but
    # the commit lands in the repo's own private history -- not a public leak.
    home = Path(os.environ["HOME"])
    _write_home_config(home, _SUBSTRING_TERM_CONFIG, monkeypatch, tmp_path)
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)

    worktree = _private_worktree(tmp_path)
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": "git commit -F /no/such/COMMIT_MSG.txt"},
        "cwd": str(worktree),
    }
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is False
    assert captured.out == ""  # no deny JSON


def test_live_hook_downgrades_unreadable_commit_body_file_in_provably_public_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # #1415: the SAME unreadable ``git commit -F <nonexistent file>`` landing in a
    # PROBE-CONFIRMED-PUBLIC repo now DOWNGRADES to warn. A ``git commit`` is LOCAL
    # regardless of the landing repo's visibility, and the pre-push gate (#703)
    # re-scans every commit message before a public push -- so the commit-time gate
    # must not hard-block an ordinary commit merely because its body is unreadable
    # at scan time (the over-block that stuck multiple coders mid-commit). The
    # public-surface protection lives in the #703 pre-push gate and the chained
    # public ``gh`` post guard (``...chained...``), both of which still hard-block.
    # Since #1415 Case A the readable-term commit path downgrades too, so the
    # readable and unreadable body paths for a LOCAL commit are consistent.
    home = Path(os.environ["HOME"])
    _write_home_config(home, _SUBSTRING_TERM_CONFIG, monkeypatch, tmp_path)
    monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")

    public_repo = _public_clone(tmp_path)
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": "git commit -F /no/such/COMMIT_MSG.txt"},
        "cwd": str(public_repo),
    }
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is False  # downgraded to warn, not denied
    assert captured.out == ""  # no deny JSON on stdout


def test_live_hook_downgrades_unreadable_commit_body_file_in_unknown_visibility_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # #1415 task #62: an unreadable commit body whose landing repo is NEITHER
    # allowlisted NOR probe-resolvable (the common cold-hook unknown state) now
    # DOWNGRADES to warn. A commit is LOCAL; the pre-push public-leak gate
    # (refuse-public-push-with-leak.sh) re-scans commit messages before they reach
    # a public remote, so the commit-time gate must not hard-block an ordinary
    # commit whose repo the in-hook probe cannot classify. Previously this forced
    # ALLOW_BANNED_TERM=1 on every ordinary commit in an undeclared checkout.
    home = Path(os.environ["HOME"])
    _write_home_config(home, _SUBSTRING_TERM_CONFIG, monkeypatch, tmp_path)
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)

    repo = tmp_path / "unknown-vis"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "remote", "add", "origin", "git@github.com:someorg/not-allowlisted.git")
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": "git commit -F /no/such/COMMIT_MSG.txt"},
        "cwd": str(repo),
    }
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is False  # downgraded to warn, not denied
    assert captured.out == ""


def test_live_hook_allows_workitem_url_inline_commit_in_private_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # #1415 (the reported inline shape): a ``git commit -m`` whose message holds a
    # private-repo work-item URL (``host/<org>/<repo>/-/work_items/N``) and lands
    # in that own private worktree must NOT hard-block -- the org slug in the URL
    # is the repo's own identity, not a foreign leak.
    home = Path(os.environ["HOME"])
    _write_home_config(home, _SUBSTRING_TERM_CONFIG, monkeypatch, tmp_path)
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)

    worktree = _private_worktree(tmp_path)
    url = "https://gitlab.com/acmecorp-engineering/acmecorp-client-workspace/-/work_items/8223"
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": f'git commit -m "fix(foo): see {url}"'},
        "cwd": str(worktree),
    }
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is False
    assert captured.out == ""  # no deny JSON


# ── #1415: kill-switch ───────────────────────────────────────────────────────
#
# A ``[teatree] banned_terms_gate_enabled = false`` config line disables the
# whole gate (NEVER-LOCKOUT). With it set, a command that would otherwise
# hard-block -- a banned term toward a PUBLIC repo -- is allowed straight
# through, and the gate emits no deny.


_GATE_DISABLED_CONFIG = '[teatree]\nbanned_terms = ["acmewidget"]\nbanned_terms_gate_enabled = false\n'


def test_kill_switch_off_allows_a_would_be_blocked_public_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The SAME public-repo banned-term post that hard-blocks with the gate ON
    # must be ALLOWED with ``banned_terms_gate_enabled = false`` -- the kill-
    # switch returns before any scan, so no deny is emitted.
    home = Path(os.environ["HOME"])
    _write_home_config(home, _GATE_DISABLED_CONFIG, monkeypatch, tmp_path)
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)

    data = {
        "tool_name": "Bash",
        "tool_input": {"command": 'gh issue comment 5 --repo souliane/teatree --body "rolling out acmewidget"'},
        "cwd": str(_public_clone(tmp_path)),
    }
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is False
    assert captured.out == ""  # no deny JSON


def test_kill_switch_default_on_still_blocks_a_public_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # ANTI-VACUITY guard: with the kill-switch absent (default ON), the SAME
    # public-repo banned-term post still hard-blocks -- proving the off-test
    # above measures the switch, not an unconditionally-allowed command.
    home = Path(os.environ["HOME"])
    _write_home_config(home, '[teatree]\nbanned_terms = ["acmewidget"]\n', monkeypatch, tmp_path)
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)

    data = {
        "tool_name": "Bash",
        "tool_input": {"command": 'gh issue comment 5 --repo souliane/teatree --body "rolling out acmewidget"'},
        "cwd": str(_public_clone(tmp_path)),
    }
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is True
    assert json.loads(captured.out)["permissionDecision"] == "deny"


# ── #1415 visibility scoping (affirmative-public) ────────────────────────────
#
# The reported over-block fired on a publish to a PRIVATE internal remote the
# user had not declared. The leak gate enforces ONLY on an affirmatively-PUBLIC
# target: a target named in ``internal_publish_namespaces`` SKIPS, and so does an
# unknown/unresolvable one (bias hard toward not firing). ONLY a target the probe
# CONFIRMS public -- the public teatree repo, a USER-OWNED non-teatree PUBLIC repo
# -- SCANS. A private/internal/unknown repo must never be falsely blocked.

_DENYLIST_CONFIG = '[teatree]\nbanned_terms = ["customercorp"]\ninternal_publish_namespaces = ["internal-eng"]\n'


def test_live_hook_allows_customer_term_to_denylisted_internal_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # MUST-NOT-FIRE (the reported over-block, fixed via the denylist): a banned
    # term in a publish to a PRIVATE internal GitLab namespace named in
    # ``internal_publish_namespaces`` is ALLOWED -- it is provably internal.
    home = Path(os.environ["HOME"])
    _write_home_config(home, _DENYLIST_CONFIG, monkeypatch, tmp_path)
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)
    monkeypatch.delenv("GH_REPO", raising=False)

    data = {
        "tool_name": "Bash",
        "tool_input": {"command": 'glab mr note 5 --repo internal-eng/internal-product --message "customercorp note"'},
        "cwd": str(_public_clone(tmp_path)),
    }
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is False
    assert captured.out == ""  # no deny JSON


def test_live_hook_blocks_customer_term_to_user_owned_non_teatree_public_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # F3 / MUST-FIRE (the leak path the review caught): a USER-OWNED non-teatree
    # PUBLIC repo (e.g. a blog repo) is NOT in the denylist and the probe confirms
    # it PUBLIC, so a banned term toward it must STILL hard-block. Reverting the
    # fail-closed default (treating non-denylisted as skip) makes this go RED.
    home = Path(os.environ["HOME"])
    _write_home_config(home, _DENYLIST_CONFIG, monkeypatch, tmp_path)
    monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
    monkeypatch.delenv("GH_REPO", raising=False)

    data = {
        "tool_name": "Bash",
        "tool_input": {"command": 'gh issue create --repo ourorg/other-public-repo --body "customercorp leak"'},
        "cwd": str(_public_clone(tmp_path)),
    }
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is True
    decision = json.loads(captured.out)
    assert decision["permissionDecision"] == "deny"


def test_live_hook_fails_closed_on_probe_error_for_non_denylisted_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # #3442 fail closed / MUST-FIRE: a non-denylisted target with a resolvable
    # ``--repo`` slug whose visibility the in-hook probe cannot confirm is NOT
    # provably non-public, so the leak gate SCANS and BLOCKS -- a probe error must
    # never route a leak out unscanned, mirroring the bash pre-push gate. Declare a
    # genuinely-private repo in ``private_repos`` to keep it skip-eligible offline.
    home = Path(os.environ["HOME"])
    _write_home_config(home, _DENYLIST_CONFIG, monkeypatch, tmp_path)
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)
    monkeypatch.delenv("GH_REPO", raising=False)

    data = {
        "tool_name": "Bash",
        "tool_input": {"command": 'gh issue create --repo someowner/mystery --body "customercorp note"'},
        "cwd": str(_public_clone(tmp_path)),
    }
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is True
    assert json.loads(captured.out)["permissionDecision"] == "deny"


def test_live_hook_skips_unresolvable_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # MUST-SKIP: a publish whose target cannot be resolved from the command (a
    # flagless create whose cwd has NO git remote -> destination None) is not
    # affirmatively public, so the gate SKIPS it. Bias hard toward not firing:
    # an unresolvable ``gh``/``glab`` publish is never treated as a public leak.
    home = Path(os.environ["HOME"])
    _write_home_config(home, _DENYLIST_CONFIG, monkeypatch, tmp_path)
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)
    monkeypatch.delenv("GH_REPO", raising=False)

    no_remote = tmp_path / "no-remote"
    no_remote.mkdir()
    _git(no_remote, "init", "-b", "main")

    data = {
        "tool_name": "Bash",
        "tool_input": {"command": 'gh issue create --body "customercorp leak"'},
        "cwd": str(no_remote),
    }
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is False
    assert captured.out == ""  # no deny JSON


def test_posting_body_file_resolves_against_cwd(tmp_path: Path) -> None:
    """The gh-posting path resolves a RELATIVE --body-file against the harness cwd.

    Anti-vacuous pair (#1415/#1213): a clean relative body resolves (no
    fail-closed sentinel — the over-block FP is gone), AND a real banned term in
    the cwd-resolved body is scanned and reported — the posting path neither
    over-blocks a clean body nor goes blind to a real term.
    """
    (tmp_path / "clean.md").write_text("## What\nclean body\n", encoding="utf-8")
    clean = banned_terms_scanner.extract_publish_payload(
        "Bash", {"command": "gh pr edit 5 --body-file clean.md"}, tmp_path
    )
    assert clean is not None
    assert not is_fail_closed_sentinel(clean)
    assert "clean body" in clean

    (tmp_path / "leak.md").write_text("## What\nmentions acmewidget here\n", encoding="utf-8")
    leaked = banned_terms_scanner.extract_publish_payload(
        "Bash", {"command": "gh pr edit 5 --body-file leak.md"}, tmp_path
    )
    cfg = tmp_path / "cfg.sqlite3"
    _seed_config_db(cfg, {"banned_terms": ["acmewidget"]})
    assert banned_terms_scanner.scan_text(leaked, config_path=cfg) == "acmewidget"
