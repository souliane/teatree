"""GitHub access is HTTPS + the ``gh`` credential helper — never SSH (#4447).

The property holds on this deployment (an https ``origin``, a
``credential.https://github.com.helper`` delegating to ``gh auth git-credential``, no key
material anywhere) but nothing asserted it, so an ``origin`` set to an ssh URL in a
provisioning path — or a ``url`` … ``insteadOf`` rewrite pointing github.com at one — would
land unnoticed and quietly make key distribution a hidden dependency the token-based ``gh``
helper cannot serve.

The scan is narrow in two deliberate ways, because this tree PARSES ssh remotes on purpose
(``utils/git_remote.py``, ``core/public_identity.py``, ``core/fleet/wire.py``, and
``entrypoint.sh``'s ``gh_repo_slug``) for arbitrary third-party repos:

*   only a MUTATING shape counts — an ssh GitHub URL within three lines of a ``git clone`` /
    ``remote add`` / ``remote set-url`` / ``remote.origin.url``, or an ``insteadOf`` being
    ASSIGNED. A parser that merely accepts the ssh form never matches, so the scan needs no
    allow-marker on any existing line and costs the tree nothing.
*   ``tests/`` is not scanned: its ~30 ssh-form remotes are FIXTURES standing in for
    third-party repos, not provisioning.

A documentation line that must show the forbidden form verbatim carries an inline
``github-ssh:allow`` marker, the same idiom ``refuse-public-push-with-leak.sh`` uses for the
privacy scan it powers.

A bare mounted ``~/.ssh`` is deliberately NOT a violation here. It is not load-bearing until
an ssh remote or an ``insteadOf`` references it, and both of those ARE caught — here for the
tracked tree, and by ``t3 doctor``'s ``_check_github_remotes_are_https`` for the live
gitconfig that no static scan can see.
"""

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

_SCAN_DIRS = ("agents", "deploy", "dev", "docs", "hooks", "scripts", "skills", "src/teatree")
_SCAN_FILES = ("AGENTS.md", "BLUEPRINT.md", "CLAUDE.md", "README.md")
_SKIP_DIRS = frozenset({".git", ".venv", "__pycache__", "node_modules"})
_SKIP_SUFFIXES = frozenset({".gif", ".ico", ".jpeg", ".jpg", ".pdf", ".png", ".svg", ".woff", ".woff2"})

_ALLOW_MARKER = "github-ssh:allow"

#: An ssh-form GitHub remote, including the ``github.com-<alias>`` ssh Host alias form.
_SSH_GITHUB_RE = re.compile(r"(?:ssh://)?git@github\.com(?:-[\w.-]+)?[:/]", re.IGNORECASE)
_GITHUB_RE = re.compile(r"github\.com", re.IGNORECASE)

#: Shapes that SET a remote — shell (``git clone``, ``remote set-url``), an argv list
#: (``"remote", "add"``) and the raw config key. A parser matches none of them.
_MUTATING_RE = re.compile(
    r"""git\s+clone
        | ["']clone["']
        | remote["'\s,]+(?:add|set-url)
        | remote\.origin\.(?:url|pushurl)
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: An ``insteadOf`` being ASSIGNED — a shell/argv ``git config`` write, a gitconfig
#: ``[url "…"]`` section, or a gitconfig assignment. Prose naming ``insteadOf`` is not a rewrite.
_INSTEAD_OF_SET_RE = re.compile(
    r"""git["'\s,]+config[^\n]*insteadof
        | ^[^\S\n]*\[\s*url[\s"']
        | insteadof["'\s]*=
    """,
    re.IGNORECASE | re.VERBOSE | re.MULTILINE,
)

#: How far back a mutating verb may sit from the URL it acts on — enough for a wrapped
#: argv list, short enough that two unrelated statements do not pair up.
_WINDOW = 3


def violations(label: str, text: str) -> list[str]:
    """Every ssh-GitHub-remote violation in *text*, as ``<label>:<line>: <reason>`` strings."""
    lines = text.splitlines()
    found: list[str] = []
    for index, line in enumerate(lines):
        if _ALLOW_MARKER in line:
            continue
        window = "\n".join(lines[max(0, index - _WINDOW + 1) : index + 1])
        if _SSH_GITHUB_RE.search(line) and _MUTATING_RE.search(window):
            found.append(f"{label}:{index + 1}: sets an ssh GitHub remote — {line.strip()}")
        elif _INSTEAD_OF_SET_RE.search(line) and _GITHUB_RE.search(window):
            found.append(f"{label}:{index + 1}: configures an insteadOf rewrite for github.com — {line.strip()}")
    return found


def _scanned_files() -> list[Path]:
    paths = [_REPO_ROOT / name for name in _SCAN_FILES]
    for directory in _SCAN_DIRS:
        for path in sorted((_REPO_ROOT / directory).rglob("*")):
            if not path.is_file() or path.suffix.lower() in _SKIP_SUFFIXES:
                continue
            if _SKIP_DIRS.intersection(path.relative_to(_REPO_ROOT).parts):
                continue
            paths.append(path)
    return paths


class TestTheTrackedTreeReachesGithubOverHttps:
    def test_no_provisioning_path_sets_an_ssh_github_remote(self) -> None:
        found: list[str] = []
        for path in _scanned_files():
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            found.extend(violations(str(path.relative_to(_REPO_ROOT)), text))

        assert not found, (
            "GitHub access is HTTPS + `gh auth git-credential`, never SSH (AGENTS.md, BLUEPRINT.md § 16):\n"
            + "\n".join(found)
        )

    def test_the_scan_covers_a_meaningful_number_of_files(self) -> None:
        assert len(_scanned_files()) > 500


class TestTheScannerGoesRedOnARealViolation:
    """The control. Without it a green tree is indistinguishable from a broken scanner."""

    def test_a_shell_clone_over_ssh(self) -> None:
        assert violations("x.sh", "git clone git@github.com:owner/repo.git ~/workspace/repo")

    def test_a_remote_set_url_over_ssh(self) -> None:
        assert violations("x.sh", 'git remote set-url origin "git@github.com:owner/repo.git"')

    def test_a_wrapped_argv_remote_add(self) -> None:
        text = 'run_git(\n    clone, "remote", "add", "origin",\n    "git@github.com:owner/repo.git",\n)'
        assert violations("x.py", text)

    def test_an_ssh_scheme_url_with_a_port(self) -> None:
        assert violations("x.sh", "git clone ssh://git@github.com:22/owner/repo.git")

    def test_an_ssh_host_alias(self) -> None:
        assert violations("x.sh", "git remote set-url origin git@github.com-work:owner/repo.git")

    def test_a_git_config_instead_of_rewrite(self) -> None:
        text = 'git config --global url."git@github.com:".insteadOf https://github.com/'  # privacy-scan:allow
        assert violations("x.sh", text)

    def test_a_gitconfig_instead_of_section(self) -> None:
        text = '[url "git@github.com:"]\n\tinsteadOf = https://github.com/\n'  # privacy-scan:allow
        assert violations("gitconfig", text)

    def test_the_config_key_form(self) -> None:
        assert violations("x.py", 'run(["git", "config", "remote.origin.url", "git@github.com:owner/repo.git"])')


class TestTheScannerLeavesLegitimateSshHandlingAlone:
    """Narrowness. Parsers, GitLab, and https remotes must never be flagged."""

    def test_a_prefix_stripping_parser(self) -> None:
        text = 'for prefix in ("git@github.com:", "ssh://git@github.com/"):'  # privacy-scan:allow
        assert violations("x.py", text) == []

    def test_a_shell_parameter_expansion_parser(self) -> None:
        text = 'url="${url#ssh://git@github.com/}"\nurl="${url#git@github.com:}"'  # privacy-scan:allow
        assert violations("entrypoint.sh", text) == []

    def test_prose_naming_the_forbidden_form(self) -> None:
        text = "Never point `origin` at `git@github.com:owner/repo` — the `insteadOf` rewrite is banned too."
        assert violations("AGENTS.md", text) == []

    def test_a_gitlab_ssh_remote(self) -> None:
        assert violations("x.sh", "git remote set-url origin git@gitlab.com:owner/repo.git") == []

    def test_an_https_github_clone(self) -> None:
        assert violations("x.sh", "git clone https://github.com/owner/repo.git ~/workspace/repo") == []

    def test_a_line_carrying_the_allow_marker(self) -> None:
        assert violations("x.sh", f"git clone git@github.com:owner/repo.git   # {_ALLOW_MARKER} doc example") == []

    def test_the_real_parser_modules_stay_clean(self) -> None:
        for relative in (
            "src/teatree/utils/git_remote.py",
            "src/teatree/core/public_identity.py",
            "src/teatree/core/fleet/wire.py",
            "src/teatree/cli/doctor/checks_bootstrap.py",
            "deploy/entrypoint.sh",
        ):
            assert violations(relative, (_REPO_ROOT / relative).read_text(encoding="utf-8")) == []
