"""The leak gates add no setting, no bypass and no paid path (rubric criterion 18).

Later changes to the leak gates extend the lists below; a gate module that grows a
switch, an override or a metered call turns one of these red.
"""

import ast
import re
from pathlib import Path

import pytest

from teatree.config.known_settings import ALL_KNOWN_CONFIG_SETTINGS

ROOT = Path(__file__).resolve().parents[2]
PUSH_HOOK = ROOT / "scripts" / "hooks" / "refuse-public-push-with-leak.sh"
PUBLISH_GATE = ROOT / "hooks" / "scripts" / "banned_terms" / "gate.py"

#: Every gate module and script; each is read for the metered-credential names below.
GATE_FILES = (
    PUSH_HOOK,
    PUBLISH_GATE,
    ROOT / "scripts" / "privacy_scan.py",
    ROOT / "src" / "teatree" / "hooks" / "term_refs.py",
)

_LEAK_GATE_SETTING_WORDS = re.compile(
    r"leak|privacy|banned|term_|scan|identity|placeholder|metered|clearance|bypass|skip|fail_open|review_gate"
)
#: The settings that already mention the leak gates, their money or their escapes. A leak-gate change adds none.
_EXISTING_LEAK_GATE_SETTINGS = frozenset(
    {
        "banned_term_registry",
        "banned_terms_gate_enabled",
        "danger_gate_fail_open",
        "metered_spend_window_hours",
        "metered_token_ceiling",
        "scanner_overlay_scope",
        "scanning_news_cadence_hours",
        "scanning_news_skill",
        "user_identity_aliases",
    }
)

#: What the push hook may expand besides its own variables: the runner's push range and the temp dir.
_PUSH_HOOK_ENV = frozenset(
    {
        "PRE_COMMIT_FROM_REF",
        "PRE_COMMIT_LOCAL_BRANCH",
        "PRE_COMMIT_REMOTE_BRANCH",
        "PRE_COMMIT_REMOTE_NAME",
        "PRE_COMMIT_REMOTE_URL",
        "PRE_COMMIT_TO_REF",
        "TMPDIR",
        "ZERO",
    }
)

_METERED_NAMES = (
    "resolve_api_key_credential",
    "AnthropicApiKeyCredential",
    "api_key_lane_available",
    "anthropic_api_key_pass_paths",
    "metered_token_ceiling",
    "ANTHROPIC_API_KEY",
)


def _code_lines(path: Path) -> str:
    return "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")
    )


def test_no_leak_gate_setting_is_added() -> None:
    mentioning_the_gates = {key for key in ALL_KNOWN_CONFIG_SETTINGS if _LEAK_GATE_SETTING_WORDS.search(key)}

    assert mentioning_the_gates == _EXISTING_LEAK_GATE_SETTINGS


def test_the_push_hook_reads_no_environment_variable_that_redirects_or_disables_it() -> None:
    expanded = set(re.findall(r"\$\{?([A-Z][A-Z0-9_]*)", _code_lines(PUSH_HOOK)))

    assert expanded <= _PUSH_HOOK_ENV, sorted(expanded - _PUSH_HOOK_ENV)


@pytest.mark.parametrize("gate_file", GATE_FILES, ids=lambda path: path.name)
def test_no_gate_module_names_a_metered_credential(gate_file: Path) -> None:
    assert gate_file.is_file(), f"{gate_file} is listed as a gate module but does not exist"
    text = gate_file.read_text(encoding="utf-8")

    assert [name for name in _METERED_NAMES if name in text] == []


def test_the_publish_gate_reads_neither_the_master_fail_open_switch_nor_the_false_positive_grant() -> None:
    code = _code_lines(PUBLISH_GATE)

    assert "danger_gate_fail_open" not in code
    assert "fp-confirmed" not in code
    assert "_fail_open_or_deny" not in code


def _names_loaded(tree: ast.AST, name: str) -> list[ast.Name]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load)
    ]


def test_the_kill_switch_only_ever_feeds_the_private_destination_rescue() -> None:
    tree = ast.parse(PUBLISH_GATE.read_text(encoding="utf-8"))
    inner = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_run_banned_terms_pretool"
    )
    rescue = next(
        node
        for node in ast.walk(inner)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "rescued" for target in node.targets)
    )
    reads_of_the_switch = _names_loaded(inner, "gate_off")

    assert len(reads_of_the_switch) == 1
    assert reads_of_the_switch[0] in set(ast.walk(rescue.value))
    assert any(
        isinstance(call, ast.Call) and getattr(call.func, "attr", "") == "gate_skips_for_visibility"
        for call in ast.walk(rescue.value)
    ), "the rescue must be conditioned on a destination the gate proved private"

    callers_of_the_flag_reader = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_banned_terms_gate_enabled"
    ]
    assert len(callers_of_the_flag_reader) == 1


def test_the_outer_handler_only_hands_the_switch_to_the_inner_gate() -> None:
    tree = ast.parse(PUBLISH_GATE.read_text(encoding="utf-8"))
    outer = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "handle_banned_terms_pretool"
    )
    parent = {child: node for node in ast.walk(outer) for child in ast.iter_child_nodes(node)}
    flag_reads = [
        node
        for node in ast.walk(outer)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_banned_terms_gate_enabled"
    ]
    assert len(flag_reads) == 1
    stored = parent[flag_reads[0]]
    while isinstance(stored, ast.UnaryOp):
        stored = parent[stored]
    assert isinstance(stored, ast.Assign), "the flag's value must be stored, not branched on, in the outer handler"
    assert [target.id for target in stored.targets if isinstance(target, ast.Name)] == ["gate_off"]

    for read in _names_loaded(outer, "gate_off"):
        keyword = parent[read]
        assert isinstance(keyword, ast.keyword)
        assert keyword.arg == "gate_off"
        assert getattr(parent[keyword].func, "id", "") == "_run_banned_terms_pretool"
