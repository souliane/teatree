"""Static shell parsing for checkout placement decisions."""

from pathlib import Path

import pytest

from teatree.core.gates.worktree_containment import scan_worktree_checkouts


@pytest.mark.parametrize(
    ("command", "target"),
    [
        ("git worktree add /outside/new main", "/outside/new"),
        ("git -C nested -C ../clone worktree add -b feature /outside/new origin/main", "/outside/new"),
        ("git worktree move old /outside/new", "/outside/new"),
        ("env -i timeout 3 nice -n 1 git worktree add /outside/new main", "/outside/new"),
        ("flock /tmp/lock git worktree add /outside/new main", "/outside/new"),
        ("xargs -n 1 git worktree add /outside/new", "/outside/new"),
        ('target=/outside/new; git worktree add "$target" main', "/outside/new"),
        ('for target in /inside /outside/new; do git worktree add "$target"; done', "/outside/new"),
    ],
)
def test_finds_executed_checkout_target(command: str, target: str, tmp_path: Path) -> None:
    calls = scan_worktree_checkouts(command, tmp_path)
    assert any(call.target == Path(target) for call in calls)


@pytest.mark.parametrize(
    "command",
    [
        "echo 'git worktree add /outside/new main'",
        "cat <<'EOF'\ngit worktree add /outside/new main\nEOF",
        "git worktree list",
        "git worktree remove /outside/old",
    ],
)
def test_ignores_non_checkout_text(command: str, tmp_path: Path) -> None:
    assert scan_worktree_checkouts(command, tmp_path) == []


@pytest.mark.parametrize(
    "command",
    [
        'git worktree add "$unknown" main',
        'git worktree add "$(mktemp -d)" main',
        'sh -c "$unknown"',
    ],
)
def test_unknown_destination_is_explicit(command: str, tmp_path: Path) -> None:
    assert any(call.target is None for call in scan_worktree_checkouts(command, tmp_path))


def test_literal_mktemp_directory_is_resolved(tmp_path: Path) -> None:
    calls = scan_worktree_checkouts('git worktree add "$(mktemp -d /tmp/review.XXXXXX)" main', tmp_path)
    assert calls[0].target == Path("/tmp/review.XXXXXX")


def test_cd_and_git_dash_c_choose_source_and_relative_destination(tmp_path: Path) -> None:
    source = tmp_path / "clone"
    source.mkdir()
    command = f"cd {source} && git -C . worktree add ../outside main"
    calls = scan_worktree_checkouts(command, tmp_path)
    assert calls[0].source == source
    assert calls[0].target == tmp_path / "outside"


@pytest.mark.parametrize(
    ("command", "target"),
    [
        ('bash -lc "git worktree add /outside/new"', Path("/outside/new")),
        ('sh -ec "git worktree add /outside/new"', Path("/outside/new")),
        ('eval "git worktree add /outside/new"', Path("/outside/new")),
        ('echo "git worktree add /outside/new" | bash', Path("/outside/new")),
        ('flock /tmp/lock -c "git worktree add /outside/new"', Path("/outside/new")),
        ("git worktree add --orphan /outside/new", Path("/outside/new")),
        ("git worktree add -qfb feature /outside/new", Path("/outside/new")),
        ("git worktree add -fb feature /outside/new", Path("/outside/new")),
        ("VAR=/outside git worktree add $VAR/new", None),
        ("D=$(mktemp -d /tmp/review.XXXX) && git worktree add $D/new", Path("/tmp/review.XXXX/new")),
        ('git worktree add "~/relative"', None),
        ("ls | xargs git worktree add", None),
    ],
)
def test_shell_checkout_variants(command: str, target: Path | None, tmp_path: Path) -> None:
    calls = scan_worktree_checkouts(command, tmp_path)
    assert len(calls) == 1
    if command == 'git worktree add "~/relative"':
        assert calls[0].target == tmp_path / "~" / "relative"
    else:
        assert calls[0].target == target


def test_bare_mktemp_assignment_has_unknown_target(tmp_path: Path) -> None:
    calls = scan_worktree_checkouts("D=$(mktemp -d) && git worktree add $D/new", tmp_path)
    assert len(calls) == 1
    assert calls[0].target is None


def test_assigned_unresolved_variable_stays_unknown(tmp_path: Path) -> None:
    calls = scan_worktree_checkouts("WT=$HOME/stray; git worktree add $WT main", tmp_path)
    assert len(calls) == 1
    assert calls[0].target is None


def test_variable_names_are_replaced_by_exact_spans(tmp_path: Path) -> None:
    calls = scan_worktree_checkouts("A=inside; AB=outside; git worktree add $A/$AB main", tmp_path)
    assert calls[0].target == tmp_path / "inside" / "outside"


def test_assignment_does_not_shift_later_segment_cwds(tmp_path: Path) -> None:
    source = tmp_path / "managed"
    source.mkdir()
    calls = scan_worktree_checkouts(f"X=$(mktemp -d); cd {source} && git worktree add ../stray main", tmp_path)
    assert calls[0].source == source
    assert calls[0].target == tmp_path / "stray"


def test_env_does_not_consume_git_chdir(tmp_path: Path) -> None:
    calls = scan_worktree_checkouts("env git -C sub worktree add ../stray main", tmp_path)
    assert calls[0].source == tmp_path / "sub"
    assert calls[0].target == tmp_path / "stray"


def test_quoted_commit_message_does_not_create_a_pipe(tmp_path: Path) -> None:
    command = "git commit -m 'echo git worktree add /outside/x | sh more words'"
    assert scan_worktree_checkouts(command, tmp_path) == []


def test_mktemp_assignment_inside_quoted_shell_script_is_scanned(tmp_path: Path) -> None:
    command = "bash -c 'D=$(mktemp -d /tmp/review.XXXX); git worktree add $D/new'"
    calls = scan_worktree_checkouts(command, tmp_path)
    assert len(calls) == 1
    assert calls[0].target == Path("/tmp/review.XXXX/new")


def test_git_dir_selects_source_but_relative_target_uses_cwd(tmp_path: Path) -> None:
    calls = scan_worktree_checkouts(f"git --git-dir={tmp_path / 'clone' / '.git'} worktree add target", tmp_path)
    assert calls[0].source == tmp_path / "clone"
    assert calls[0].target == tmp_path / "target"


def test_env_chdir_sets_source_and_target_base(tmp_path: Path) -> None:
    calls = scan_worktree_checkouts("env -C clone git worktree add ../target", tmp_path)
    assert calls[0].source == tmp_path / "clone"
    assert calls[0].target == tmp_path / "target"


@pytest.mark.parametrize(
    "command", ['bash -c "echo $HOME"', "timeout 1800 bash -c 'until [ -f \"$marker\" ]; do sleep 20; done'"]
)
def test_ordinary_dollar_bearing_shell_script_has_no_checkout(command: str, tmp_path: Path) -> None:
    assert scan_worktree_checkouts(command, tmp_path) == []


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    path = tmp_path / "clone"
    (path / ".git").mkdir(parents=True)
    return path


@pytest.mark.parametrize(
    "command",
    [
        "flock /tmp/lock git -c core.x=1 worktree add /outside/stray main",
        "flock -w 5 /tmp/lock git worktree add /outside/stray main",
        "D=/outside/stray; D=/tmp/ok git worktree add $D main",
        "D=/tmp/ok; export D=/outside/stray; git worktree add $D main",
        "D=/tmp; D+=/../outside/stray; git worktree add $D main",
        "X=$(mktemp -d)y git worktree add /outside/stray main",
        "D=/outside/stray; (D=/tmp/ok); git worktree add $D main",
        "D=/tmp/ok; if true; then D=/outside/stray; fi; git worktree add $D main",
        "D=/outside/stray; D=/tmp/ok | true; git worktree add $D main",
    ],
)
def test_bash_semantics_reach_the_executed_target(command: str, tmp_path: Path) -> None:
    calls = scan_worktree_checkouts(command, tmp_path)
    assert Path("/outside/stray") in {call.target for call in calls}


@pytest.mark.parametrize(
    "command",
    [
        "D=/tmp/ok; read D; git worktree add $D main",
        "f() { D=/outside/stray; }; D=/tmp/ok; f; git worktree add $D main",
        "D=/tmp/ok; declare -n D=OTHER; git worktree add $D main",
        "D=/tmp/ok; unset D; git worktree add $D main",
    ],
)
def test_a_rebinding_the_scan_cannot_follow_leaves_the_target_unknown(command: str, tmp_path: Path) -> None:
    assert None in {call.target for call in scan_worktree_checkouts(command, tmp_path)}


def test_git_dir_env_prefix_selects_the_source(clone: Path, tmp_path: Path) -> None:
    calls = scan_worktree_checkouts(f"GIT_DIR={clone}/.git git worktree add /outside/stray main", tmp_path)
    assert {call.source for call in calls} == {clone}


def test_relative_git_dir_resolves_against_the_dash_c_directory(clone: Path, tmp_path: Path) -> None:
    calls = scan_worktree_checkouts(f"git -C {clone} --git-dir=.git worktree add /outside/stray main", tmp_path)
    assert {call.source for call in calls} == {clone}


def test_exported_git_dir_reaches_a_nested_shell(clone: Path, tmp_path: Path) -> None:
    command = f"export GIT_DIR={clone}/.git; bash -c 'git worktree add /outside/stray main'"
    assert clone in {call.source for call in scan_worktree_checkouts(command, tmp_path)}


@pytest.mark.parametrize(
    "command",
    ["timeout 5 env -C /outside git worktree add foo", "flock /tmp/lock env -C /outside git worktree add foo"],
)
def test_env_chdir_behind_another_wrapper_moves_the_target_base(command: str, tmp_path: Path) -> None:
    calls = scan_worktree_checkouts(command, tmp_path)
    assert [call.target for call in calls] == [Path("/outside/foo")]


def test_unknown_user_tilde_is_unresolved_and_later_calls_are_still_scanned(tmp_path: Path) -> None:
    calls = scan_worktree_checkouts("git worktree add ~nouser5247/x; git worktree add /outside/stray main", tmp_path)
    assert [call.target for call in calls] == [None, Path("/outside/stray")]


def test_cd_inside_a_subshell_chooses_the_source_and_ends_with_it(clone: Path, tmp_path: Path) -> None:
    calls = scan_worktree_checkouts(f"(cd {clone} && git worktree add /outside/x); git worktree add rel", tmp_path)
    assert [(call.source, call.target) for call in calls] == [
        (clone, Path("/outside/x")),
        (tmp_path, tmp_path / "rel"),
    ]


@pytest.mark.parametrize(
    "command",
    [
        "cd {source} && x=$(git worktree add /outside/stray)",
        "cd {source} && echo 'git worktree add /outside/stray' | sh",
    ],
)
def test_nested_payloads_run_in_the_directory_an_earlier_cd_chose(command: str, clone: Path, tmp_path: Path) -> None:
    calls = scan_worktree_checkouts(command.format(source=clone), tmp_path)
    assert (clone, Path("/outside/stray")) in {(call.source, call.target) for call in calls}


def test_quoted_tilde_elsewhere_does_not_suppress_the_target_expansion(tmp_path: Path) -> None:
    calls = scan_worktree_checkouts("git worktree add --reason '~' ~/stray", tmp_path)
    assert [call.target for call in calls] == [Path.home().resolve() / "stray"]


def test_case_pattern_keeps_its_arm_command_visible(tmp_path: Path) -> None:
    calls = scan_worktree_checkouts("case $x in a) git worktree add /outside/stray;; esac", tmp_path)
    assert [call.target for call in calls] == [Path("/outside/stray")]


def test_unquoted_mktemp_substitution_with_a_template_resolves(tmp_path: Path) -> None:
    calls = scan_worktree_checkouts("git worktree add $(mktemp -d /tmp/review.XXXX) main", tmp_path)
    assert [call.target for call in calls] == [Path("/tmp/review.XXXX")]
