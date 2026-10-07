"""A forge write that carries free text is a publish the leak gates scan; a read is not.

The banned-term rows run through the router's own registered handler against a
destination pinned PUBLIC, the secret rows through the real router subprocess
(secrets block regardless of destination), and every block row has a clean twin and
a read control carrying the same planted term, so the harness is shown both to
block and to allow.
"""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import handle_banned_terms_pretool
from teatree.hooks import _repo_visibility, banned_terms_scanner
from teatree.hooks._command_parser import is_publish_command
from teatree.hooks._parser_primitives import is_fail_closed_sentinel
from tests._uv_stub import working_uv

_HOOK_ROUTER = Path(__file__).resolve().parents[2] / "hooks" / "scripts" / "hook_router.py"
_TERM = "acmecorp"
_SECRET = "AKIA" + "IOSFODNN7EXAMPLE"
_PUB = "-R souliane/teatree"
_GL_PUB = "-R gitlab.com/souliane/teatree"
_GH_COMMENTS = "https://api.github.com/repos/souliane/teatree/issues/1/comments"


def _free_text_writes(text: str, content_file: Path, clean_file: Path) -> list[str]:
    return [
        f"gh release create v9.9.9 {_PUB} --notes '{text}'",
        f"gh release edit v9.9.9 {_PUB} --notes '{text}'",
        f"gh gist create --desc '{text}' {clean_file}",
        f"gh gist create --public {content_file}",
        f"gh repo edit souliane/teatree --description '{text}'",
        f"gh label create probe {_PUB} --description '{text}'",
        f"gh issue close 1 {_PUB} --comment '{text}'",
        f"gh pr close 1 {_PUB} --comment '{text}'",
        f"gh issue reopen 1 {_PUB} -c '{text}'",
        f"glab release create v9.9.9 {_GL_PUB} --notes '{text}'",
        f"glab snippet create {_GL_PUB} --title '{text}' {clean_file}",
        f"git tag -a v9.9.9 -m '{text}'",
        f"git tag --message '{text}' v9.9.9",
        f"""curl -X POST {_GH_COMMENTS} -d '{{"body":"{text}"}}'""",
        f"""gh api graphql -f query='mutation {{ addComment(input:{{subjectId:"X", body:"{text}"}}) {{ id }} }}'""",
        f"gh pr create {_PUB} -t '{text}' --body 'clean body'",
        f"gh api repos/souliane/teatree/issues/1/labels -f 'labels[]={text}'",
    ]


_ROW_IDS = [f"free-text-write-{n:02d}" for n in range(1, 18)]


def _seed_config_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'banned_term_registry', ?)",
            (json.dumps({"leak": [_TERM], "prose_collider": [_TERM]}),),
        )
        conn.commit()
    finally:
        conn.close()


def _bash(command: str) -> dict[str, object]:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def _payload(command: str) -> str | None:
    return banned_terms_scanner.extract_publish_payload("Bash", {"command": command})


@pytest.fixture
def files(tmp_path: Path) -> tuple[Path, Path]:
    content = tmp_path / "content.txt"
    content.write_text(f"notes {_TERM}\n", encoding="utf-8")
    clean = tmp_path / "clean.txt"
    clean.write_text("routine notes\n", encoding="utf-8")
    return content, clean


@pytest.fixture
def seeded_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    _seed_config_db(tmp_path / "config.sqlite3")
    monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "config.sqlite3"))
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("T3_UV", str(working_uv(tmp_path / "bin" / "uv")))
    return monkeypatch


@pytest.fixture
def public_gate(seeded_gate: pytest.MonkeyPatch) -> None:
    seeded_gate.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")


@pytest.fixture
def private_gate(seeded_gate: pytest.MonkeyPatch) -> None:
    seeded_gate.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PRIVATE")


class TestFreeTextForgeWriteIsScannedForBannedTerms:
    @pytest.mark.parametrize("index", range(17), ids=_ROW_IDS)
    @pytest.mark.usefixtures("public_gate")
    def test_planted_term_is_denied_and_named(
        self, index: int, files: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        content, clean = files
        command = _free_text_writes(_TERM, content, clean)[index]
        assert handle_banned_terms_pretool(_bash(command)) is True
        decision = json.loads(capsys.readouterr().out)
        assert decision["permissionDecision"] == "deny"
        assert _TERM in decision["permissionDecisionReason"]

    @pytest.mark.parametrize("index", range(17), ids=_ROW_IDS)
    @pytest.mark.usefixtures("public_gate")
    def test_clean_twin_is_allowed(self, index: int, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        clean = tmp_path / "clean.txt"
        clean.write_text("routine notes\n", encoding="utf-8")
        command = _free_text_writes("routine update", clean, clean)[index]
        assert is_publish_command(command) is True
        assert handle_banned_terms_pretool(_bash(command)) is False
        assert capsys.readouterr().out == ""

    def test_handler_is_the_routing_table_entry(self) -> None:
        assert handle_banned_terms_pretool in router._HANDLERS["PreToolUse"]


_READ_CONTROLS = [
    f"gh release view {_TERM} {_PUB}",
    f"gh api repos/souliane/teatree/issues -X GET -f labels={_TERM}",
    f"gh search issues {_TERM}",
    f"gh pr checkout 5 -b {_TERM}",
    f"curl https://api.github.com/search/issues?q={_TERM}",
    f"""curl -X POST http://localhost:8000/api/v1/notes -d '{{"body":"{_TERM}"}}'""",
    f"glab config get {_TERM}",
    f"gh issue close {_TERM} {_PUB} --reason 'not planned'",
    f"git tag -d {_TERM}",
    f"curl -G https://api.github.com/search/issues -d q={_TERM}",
    f"gh secret set TOKEN {_PUB} --body {_TERM}",
]


class TestReadsAndNonPublishesStayUnscanned:
    @pytest.mark.parametrize("command", _READ_CONTROLS, ids=[f"read-control-{n}" for n in range(len(_READ_CONTROLS))])
    @pytest.mark.usefixtures("public_gate")
    def test_read_carrying_the_term_is_not_a_publish(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert is_publish_command(command) is False
        assert handle_banned_terms_pretool(_bash(command)) is False
        assert capsys.readouterr().out == ""

    @pytest.mark.usefixtures("public_gate")
    def test_graphql_read_document_is_not_scanned(self, capsys: pytest.CaptureFixture[str]) -> None:
        command = f"""gh api graphql -f query='query {{ search(query:"{_TERM}", type:ISSUE) {{ issueCount }} }}'"""
        assert _TERM not in (_payload(command) or "")
        assert handle_banned_terms_pretool(_bash(command)) is False
        assert capsys.readouterr().out == ""


class TestPrivateTargetStillSkips:
    @pytest.mark.parametrize(
        "command",
        [
            f"gh release create v1 -R acme-private/tracker --notes '{_TERM}'",
            (
                "curl -X POST https://gitlab.com/api/v4/projects/acme-private%2Ftracker/issues/1/notes "
                f"""-d '{{"body":"{_TERM}"}}'"""
            ),
        ],
        ids=["cli-write-private-flag", "rest-write-private-url"],
    )
    @pytest.mark.usefixtures("private_gate")
    def test_write_to_private_target_is_allowed(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert is_publish_command(command) is True
        assert handle_banned_terms_pretool(_bash(command)) is False
        assert capsys.readouterr().out == ""

    @pytest.mark.usefixtures("private_gate")
    def test_rest_write_naming_a_non_forge_url_too_is_scanned(self, capsys: pytest.CaptureFixture[str]) -> None:
        command = (
            "curl -X POST https://gitlab.com/api/v4/projects/acme-private%2Ftracker/issues/1/notes "
            f"""https://paste.example.com/new -d '{{"body":"{_TERM}"}}'"""
        )
        assert handle_banned_terms_pretool(_bash(command)) is True
        assert _TERM in json.loads(capsys.readouterr().out)["permissionDecisionReason"]

    @pytest.mark.usefixtures("public_gate")
    def test_rest_write_to_public_gitlab_project_is_denied(self, capsys: pytest.CaptureFixture[str]) -> None:
        command = (
            "curl -X POST https://gitlab.com/api/v4/projects/souliane%2Fteatree/issues/1/notes "
            f"""-d '{{"body":"{_TERM}"}}'"""
        )
        assert handle_banned_terms_pretool(_bash(command)) is True
        assert _TERM in json.loads(capsys.readouterr().out)["permissionDecisionReason"]


_SECRET_WRITES = [
    f"gh issue close 1 {_PUB} --comment '{_SECRET}'",
    f"gh release create v9.9.9 {_PUB} --notes '{_SECRET}'",
    f"gh issue reopen 1 {_PUB} -c '{_SECRET}'",
    f"gh gist create --desc '{_SECRET}' notes.md",
    f"gh label create probe {_PUB} --description '{_SECRET}'",
    f"""curl -X POST {_GH_COMMENTS} -d '{{"body":"{_SECRET}"}}'""",
    (
        "gh api graphql -f query='mutation($b:String!){ addComment(input:{subjectId:\"X\", body:$b}) { id } }' "
        f"-f b='{_SECRET}'"
    ),
]


class TestSecretInFreeTextWriteBlocksEndToEnd:
    @pytest.fixture
    def hook_env(self, tmp_path: Path) -> dict[str, str]:
        _seed_config_db(tmp_path / "config.sqlite3")
        return {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "T3_UV": str(working_uv(tmp_path / "bin" / "uv")),
            "T3_CONFIG_DB": str(tmp_path / "config.sqlite3"),
            "T3_DATA_DIR": str(tmp_path / "data"),
            "TEATREE_CLAUDE_STATUSLINE_STATE_DIR": str(tmp_path / "state"),
        }

    def _run(self, command: str, env: dict[str, str]) -> tuple[int, str]:
        proc = subprocess.run(
            [sys.executable, str(_HOOK_ROUTER), "--event", "PreToolUse"],
            input=json.dumps(_bash(command)),
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        return proc.returncode, proc.stdout

    @pytest.mark.parametrize("command", _SECRET_WRITES, ids=[f"secret-write-{n}" for n in range(len(_SECRET_WRITES))])
    def test_secret_is_denied(self, command: str, hook_env: dict[str, str]) -> None:
        rc, out = self._run(command, hook_env)
        assert rc == 2
        assert "secret" in json.loads(out)["permissionDecisionReason"].lower()

    def test_clean_free_text_write_passes(self, hook_env: dict[str, str]) -> None:
        rc, out = self._run(f"gh issue close 1 {_PUB} --comment 'shipped in the next release'", hook_env)
        assert (rc, out) == (0, "")


class TestFreeTextExtractionEdges:
    @pytest.mark.parametrize(
        "command",
        [
            f"gh pr close 5 -d -c '{_TERM}'",
            f"gh release create v1 -d --notes '{_TERM}'",
            f"gh release create v1 --notes '- item {_TERM}'",
            f"gh release create v1 --notes='{_TERM}'",
            f"gh release create v1 -t'{_TERM}'",
            f"gh issue -R o/r close 5 --comment '{_TERM}'",
            f"xargs gh release create v1 --notes '{_TERM}'",
            f"/usr/bin/gh release create v1 --notes '{_TERM}'",
            f"git -C /tmp tag -a v1 -m '{_TERM}'",
            f"printf 'notes {_TERM}' | gh gist create",
            f"glab release create v1 -N '{_TERM}'",
            f"gh api graphql -F query=@missing.graphql -f subject='{_TERM}'",
            f"""curl -X POST api.github.com/repos/o/r/issues/1/comments -d '{{"body":"{_TERM}"}}'""",
            f"""curl --request=PATCH --url=https://gitlab.com/api/v4/projects/1/notes/2 --data 'body={_TERM}'""",
        ],
        ids=[f"edge-{n:02d}" for n in range(14)],
    )
    def test_term_reaches_the_scanned_payload(self, command: str) -> None:
        payload = _payload(command)
        assert payload is not None
        assert _TERM in payload

    def test_content_file_operand_is_read(self, files: tuple[Path, Path]) -> None:
        content, _ = files
        assert _TERM in (_payload(f"gh gist create -d 'desc' -f name.txt {content}") or "")

    @pytest.mark.parametrize("add", ["--add {path}", "--add={path}"])
    def test_added_gist_file_is_read(self, add: str, files: tuple[Path, Path]) -> None:
        content, _ = files
        assert _TERM in (_payload(f"gh gist edit 0123abcd {add.format(path=content)}") or "")

    def test_unreadable_content_file_fails_closed(self, tmp_path: Path) -> None:
        assert is_fail_closed_sentinel(_payload(f"gh gist create {tmp_path / 'absent.txt'}") or "")

    def test_rest_write_body_from_a_file_substitution_is_read(self, files: tuple[Path, Path]) -> None:
        content, _ = files
        assert _TERM in (_payload(f'curl -X POST {_GH_COMMENTS} -d "$(cat {content})"') or "")

    def test_rest_write_body_from_an_unset_variable_fails_closed(self) -> None:
        assert is_fail_closed_sentinel(_payload(f'curl -X POST {_GH_COMMENTS} --data="$T3_ABSENT_CURL_BODY"') or "")

    def test_rest_write_with_an_inert_substitution_is_scanned_not_blocked(self) -> None:
        payload = _payload(f"""curl -X POST {_GH_COMMENTS} -d '{{"body":"ran $(date) {_TERM}"}}'""") or ""
        assert _TERM in payload
        assert not is_fail_closed_sentinel(payload)

    def test_boolean_short_flag_does_not_swallow_the_next_free_text_flag(self) -> None:
        payload = _payload(f"gh pr create {_PUB} -d -t '{_TERM}'") or ""
        assert _TERM in payload
