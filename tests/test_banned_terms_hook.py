import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from hooks.scripts.hook_router import handle_banned_terms_pretool
from teatree import find_project_root
from teatree.hooks import _repo_visibility


@pytest.mark.integration
def test_banned_terms_hook_expands_tilde_config_path(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    config = home / ".teatree.toml"
    config.write_text('[teatree]\nbanned_terms = ["acme"]\n', encoding="utf-8")

    sample = tmp_path / "README.md"
    sample.write_text("acme overlay\n", encoding="utf-8")

    root = find_project_root()
    assert root is not None
    script = root / "scripts" / "hooks" / "check-banned-terms.sh"
    env = dict(os.environ)
    env["HOME"] = str(home)

    result = subprocess.run(
        [str(script), "--config", "~/.teatree.toml", str(sample)],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )

    assert result.returncode == 1
    assert "BANNED TERM" in result.stdout


@pytest.mark.integration
def test_banned_terms_hook_ignores_matches_inside_email_addresses(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    config = home / ".teatree.toml"
    config.write_text('[teatree]\nbanned_terms = ["internalterm"]\n', encoding="utf-8")

    sample = tmp_path / "AGENTS.md"
    sample.write_text("Git author: adrien <adrien.cossa@internalterm.example>\n", encoding="utf-8")

    root = find_project_root()
    assert root is not None
    script = root / "scripts" / "hooks" / "check-banned-terms.sh"
    env = dict(os.environ)
    env["HOME"] = str(home)

    result = subprocess.run(
        [str(script), "--config", "~/.teatree.toml", str(sample)],
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
def test_banned_terms_block_emits_visibility_unknown_note_and_still_denies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A private-LOOKING target whose visibility is UNKNOWN in-hook (no probe
    # tool resolvable, not in the allowlist) must STILL hard-block, and emit a
    # diagnostic stderr NOTE pointing the operator at [teatree] private_repos.
    home = Path(os.environ["HOME"])  # the conftest-isolated HOME
    (home / ".teatree.toml").write_text('[teatree]\nbanned_terms = ["acmewidget"]\n', encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "remote", "add", "origin", "git@github.com:acme/secret-product.git")

    # The probe tool is unreachable in-hook -> visibility "unknown" -> the
    # block stands. Patching the resolver (not PATH) keeps the shell scanner's
    # ``bash``/``grep`` reachable so the banned-term match still fires.
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
    decision = json.loads(captured.out)
    assert decision["permissionDecision"] == "deny"
    assert "banned-terms" in decision["permissionDecisionReason"]
    assert "acme/secret-product" in captured.err
    assert "private_repos" in captured.err


def test_banned_terms_allowed_when_target_resolvable_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # When the target IS allowlisted-private the destination gate SKIPS the
    # scan entirely (#1672 -- ``private_repos`` drives the destination skip),
    # so the post is allowed with no deny and no unknown NOTE. The hint only
    # fires on a genuine unknown-target block.
    home = Path(os.environ["HOME"])  # the conftest-isolated HOME
    (home / ".teatree.toml").write_text(
        '[teatree]\nbanned_terms = ["acmewidget"]\nprivate_repos = ["acme/secret-product"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))

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
    (home / ".teatree.toml").write_text(body, encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
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


def test_live_hook_blocks_bare_commit_with_private_body_file_but_divergent_public_landing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # BLOCKER REGRESSION (#1958 review): a BARE ``git commit -F <abs body file
    # inside a PRIVATE repo>`` whose harness cwd is a DIVERGENT repo that is
    # public-but-UNKNOWN (the common cold-hook state -- no probe tool). The commit
    # lands in the cwd repo, NOT where the body file lives, so the private body
    # file must NEVER vouch for the divergent landing repo's visibility. This must
    # STAY hard-blocked -- downgrading it would widen the leak surface (a private
    # body file laundering a banned term into a public commit).
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

    assert blocked is True
    decision = json.loads(captured.out)
    assert decision["permissionDecision"] == "deny"


def test_live_hook_blocks_substring_term_on_bare_commit_with_body_file_in_public_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # SAFETY (#1958): a bare ``git commit -F <abs body file>`` landing in a PUBLIC
    # repo (cwd == that public repo) must STAY hard-blocked. The carve-out only
    # downgrades a PROVABLY-private landing repo.
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

    assert blocked is True
    decision = json.loads(captured.out)
    assert decision["permissionDecision"] == "deny"


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
