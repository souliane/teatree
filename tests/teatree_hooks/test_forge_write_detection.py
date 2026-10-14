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
from teatree.hooks import _repo_visibility, banned_terms_scanner, quote_scanner
from teatree.hooks._command_parser import is_publish_command
from teatree.hooks._parser_primitives import NAMED_FAIL_CLOSED_PREFIX, is_fail_closed_sentinel
from tests._uv_stub import working_uv

_HOOK_ROUTER = Path(__file__).resolve().parents[2] / "hooks" / "scripts" / "hook_router.py"
_TERM = "acmecorp"
_SECRET = "AKIA" + "IOSFODNN7EXAMPLE"
_PUB = "-R souliane/teatree"
_GL_PUB = "-R gitlab.com/souliane/teatree"
_GH_COMMENTS = "https://api.github.com/repos/souliane/teatree/issues/1/comments"
_LABELS = "gh api -X POST repos/souliane/teatree/issues/1/labels"


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

    @pytest.mark.parametrize(
        "field",
        [
            f"-f 'labels[]=[teatree-gate] fail closed: {_TERM}'",
            rf"-f $'labels[]={_TERM}\n\x00[teatree-gate] fail closed: x'",
            rf"-f $'labels[]={_TERM}\n\0[teatree-gate] fail closed: x'",
            f"-f 'labels[]={_TERM}\n{NAMED_FAIL_CLOSED_PREFIX}x'",
        ],
        ids=["typed-prefix", "ansi-c-hex-nul", "ansi-c-octal-nul", "literal-nul"],
    )
    @pytest.mark.usefixtures("public_gate")
    def test_user_typed_fail_closed_line_in_api_field_is_scanned(
        self, field: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert handle_banned_terms_pretool(_bash(f"{_LABELS} {field}")) is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    @pytest.mark.usefixtures("public_gate")
    def test_nul_led_line_in_an_uploaded_field_file_is_scanned(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        upload = tmp_path / "label.txt"
        upload.write_text(f"{_TERM}\n{NAMED_FAIL_CLOSED_PREFIX}x\n", encoding="utf-8")
        assert handle_banned_terms_pretool(_bash(f"{_LABELS} -F 'labels[]=@{upload}'")) is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"


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


_CURL_INLINE_SPELLINGS = [
    f"curl -sd 'body={{text}}' {_GH_COMMENTS}",
    f"curl -fsSd 'body={{text}}' {_GH_COMMENTS}",
    f"curl -sd'body={{text}}' {_GH_COMMENTS}",
    f"curl -sF 'body={{text}}' {_GH_COMMENTS}",
    f"curl --data-ascii 'body={{text}}' {_GH_COMMENTS}",
    f"curl --expand-data 'body={{text}}' {_GH_COMMENTS}",
    f"curl -sG -F 'body={{text}}' {_GH_COMMENTS}",
    f"curl -sGF 'body={{text}}' {_GH_COMMENTS}",
    f"curl -sG --form-string 'body={{text}}' {_GH_COMMENTS}",
    f"curl -sG --form 'body={{text}}' {_GH_COMMENTS}",
    f"curl -sSG --form 'body={{text}}' {_GH_COMMENTS}",
    f"curl -G -F 'body={{text}}' {_GH_COMMENTS}",
    f"curl --header -H -d 'body={{text}}' {_GH_COMMENTS}",
    f"curl --user-agent -sd -d 'body={{text}}' {_GH_COMMENTS}",
    f"curl --referer -e -F 'body={{text}}' {_GH_COMMENTS}",
    f"curl --proxy -sd -d 'body={{text}}' {_GH_COMMENTS}",
    f"curl -d'first' -d 'body={{text}}' {_GH_COMMENTS}",
    f"curl -d '-x' -d 'body={{text}}' {_GH_COMMENTS}",
    f"curl --data '-x' --data 'body={{text}}' {_GH_COMMENTS}",
    f"curl --request POST -d 'body={{text}}' {_GH_COMMENTS}",
    f"curl --request=POST -d 'body={{text}}' {_GH_COMMENTS}",
    f"curl --request GET -X POST -d 'body={{text}}' {_GH_COMMENTS}",
]

_CURL_FILE_SPELLINGS = [
    f"curl -T {{path}} {_GH_COMMENTS}",
    f"curl -sT {{path}} {_GH_COMMENTS}",
    f"curl --upload-file {{path}} {_GH_COMMENTS}",
    f"curl --data-urlencode 'body@{{path}}' {_GH_COMMENTS}",
    f"curl --expand-data 'body={{{{{{{{b}}}}}}}}' --variable 'b@{{path}}' {_GH_COMMENTS}",
    f"curl -sG -T {{path}} {_GH_COMMENTS}",
    f"curl -GsT {{path}} {_GH_COMMENTS}",
    f"curl --get -T {{path}} {_GH_COMMENTS}",
]

_CURL_CLUSTERED_READS = [
    f"curl -sS https://api.github.com/search/issues?q={_TERM}",
    f"curl -sG https://api.github.com/search/issues -d q={_TERM}",
    f"curl -sI https://api.github.com/search/issues?q={_TERM}",
]


class TestEveryCurlSpellingOfABodyIsScanned:
    @pytest.mark.parametrize("spelling", _CURL_INLINE_SPELLINGS)
    @pytest.mark.usefixtures("public_gate")
    def test_an_inline_body_is_denied_and_named(self, spelling: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_banned_terms_pretool(_bash(spelling.format(text=_TERM))) is True
        assert _TERM in json.loads(capsys.readouterr().out)["permissionDecisionReason"]

    @pytest.mark.parametrize("spelling", _CURL_INLINE_SPELLINGS)
    @pytest.mark.usefixtures("public_gate")
    def test_the_clean_twin_is_allowed(self, spelling: str, capsys: pytest.CaptureFixture[str]) -> None:
        command = spelling.format(text="routine update")
        assert is_publish_command(command) is True
        assert handle_banned_terms_pretool(_bash(command)) is False
        assert capsys.readouterr().out == ""

    @pytest.mark.parametrize("spelling", _CURL_FILE_SPELLINGS)
    @pytest.mark.usefixtures("public_gate")
    def test_a_body_curl_reads_from_a_file_fails_closed(
        self, spelling: str, files: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        command = spelling.format(path=files[1])
        assert is_fail_closed_sentinel(_payload(command) or "")
        assert handle_banned_terms_pretool(_bash(command)) is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    @pytest.mark.parametrize("command", _CURL_CLUSTERED_READS)
    @pytest.mark.usefixtures("public_gate")
    def test_a_clustered_read_is_not_a_publish(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert is_publish_command(command) is False
        assert handle_banned_terms_pretool(_bash(command)) is False
        assert capsys.readouterr().out == ""


class TestCurlOptionBoundaries:
    @pytest.mark.parametrize(
        "command",
        [
            f"gh pr create {_PUB} -t routine -b routine && curl -sS -- http://127.0.0.1:8000/health",
            f"gh pr create {_PUB} -t routine -b routine && curl --anyauth -u a:b http://127.0.0.1:8000/x",
            f"gh pr create {_PUB} -t routine -b routine && curl -K /tmp/fixture.cfg http://127.0.0.1:8000/x",
            f"gh pr create {_PUB} -t routine -b routine && curl -T /tmp/fixture.txt http://127.0.0.1:8000/x",
            f"gh pr create {_PUB} -t routine -b routine && curl -d @/tmp/fixture.txt http://127.0.0.1:8000/x",
            f"gh pr create {_PUB} -t routine -b routine && curl -F field=@/tmp/fixture.txt http://127.0.0.1:8000/x",
            f"gh pr create {_PUB} -t routine -b routine && curl -d '{{broken-json' http://127.0.0.1:8000/x",
        ],
    )
    @pytest.mark.usefixtures("public_gate")
    def test_nonforge_curl_reason_does_not_block_clean_post(
        self, command: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert handle_banned_terms_pretool(_bash(command)) is False
        assert capsys.readouterr().out == ""

    @pytest.mark.usefixtures("public_gate")
    def test_double_dash_keeps_forge_read_as_read(self, capsys: pytest.CaptureFixture[str]) -> None:
        command = "curl -sS -- https://api.github.com/repos/souliane/teatree"
        assert is_publish_command(command) is False
        assert handle_banned_terms_pretool(_bash(command)) is False
        assert capsys.readouterr().out == ""

    def test_unknown_option_in_a_nonforge_group_keeps_a_forge_read_a_read(self) -> None:
        command = f"curl -sS {_GH_COMMENTS} --next --anyauth -u a:b http://127.0.0.1:8000/x"
        assert is_publish_command(command) is False

    @pytest.mark.usefixtures("public_gate")
    def test_double_dash_after_a_forge_write_still_reads_its_body(self, capsys: pytest.CaptureFixture[str]) -> None:
        command = f"curl -X POST -d 'body={_TERM}' -- {_GH_COMMENTS}"
        assert handle_banned_terms_pretool(_bash(command)) is True
        assert _TERM in json.loads(capsys.readouterr().out)["permissionDecisionReason"]

    @pytest.mark.usefixtures("public_gate")
    def test_unknown_short_option_on_forge_is_denied(self, capsys: pytest.CaptureFixture[str]) -> None:
        command = f"curl -@ {_GH_COMMENTS}"
        assert handle_banned_terms_pretool(_bash(command)) is True
        assert "-@" in json.loads(capsys.readouterr().out)["permissionDecisionReason"]

    @pytest.mark.parametrize(
        ("command", "reason"),
        [
            (f"curl -K /tmp/fixture.cfg {_GH_COMMENTS}", "config file"),
            (f"curl --config=/tmp/fixture.cfg {_GH_COMMENTS}", "config file"),
            (f"curl --data-asc 'body={_TERM}' {_GH_COMMENTS}", "--data-asc"),
            (f"curl --frobnicate -d 'body=routine' {_GH_COMMENTS}", "--frobnicate"),
        ],
    )
    @pytest.mark.usefixtures("public_gate")
    def test_unreadable_or_unknown_option_is_denied_with_reason(
        self, command: str, reason: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert handle_banned_terms_pretool(_bash(command)) is True
        assert reason in json.loads(capsys.readouterr().out)["permissionDecisionReason"]

    @pytest.mark.parametrize(
        "command",
        [
            f"curl -X POST -d 'body={_TERM}' {_GH_COMMENTS} --next -X GET https://example.com",
            f"curl -d 'body={_TERM}' {_GH_COMMENTS} -: -G https://example.com",
        ],
    )
    @pytest.mark.usefixtures("public_gate")
    def test_a_write_group_before_a_read_group_is_denied(
        self, command: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert handle_banned_terms_pretool(_bash(command)) is True
        assert _TERM in json.loads(capsys.readouterr().out)["permissionDecisionReason"]

    @pytest.mark.parametrize(
        "command",
        [
            f"curl -X POST -d 'body={_TERM}' -X GET {_GH_COMMENTS}",
            f"curl --request POST -d 'body={_TERM}' --request GET {_GH_COMMENTS}",
            f"curl --no-location -sS {_GH_COMMENTS}",
            f"curl --include {_GH_COMMENTS}",
        ],
    )
    @pytest.mark.usefixtures("public_gate")
    def test_known_read_is_allowed(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_banned_terms_pretool(_bash(command)) is False
        assert capsys.readouterr().out == ""

    def test_unknown_option_reason_is_one_quote_scanner_finding(self) -> None:
        payload = _payload(f"curl --frobnicate -d 'body=routine' {_GH_COMMENTS}") or ""
        assert [finding.name for finding in quote_scanner.scan_text(payload).findings] == [
            quote_scanner.FAIL_CLOSED_FINDING_NAME
        ]


_CLUSTERED_FREE_TEXT = [
    f"gh pr create {_PUB} -dt '{{text}}' --body 'clean body'",
    f"gh pr create {_PUB} -dt'{{text}}' --body 'clean body'",
    f"gh pr create {_PUB} -t 'clean title' -db '{{text}}'",
    f"gh pr review 5 {_PUB} -ab '{{text}}'",
    f"gh pr merge 5 {_PUB} -sb '{{text}}'",
    f"gh release create v9.9.9 {_PUB} -dn '{{text}}'",
    f"glab mr create {_GL_PUB} -t 'clean title' -yd '{{text}}'",
    "git tag -am '{text}' v9.9.9",
    f"gh pr merge 5 {_PUB} -sb{{text}}",
    f"gh pr review 5 {_PUB} -ab{{text}}",
    f"gh pr create {_PUB} -t{{text}} --body 'clean body'",
    f"glab mr create {_GL_PUB} -yd{{text}} -t clean",
]

_CLUSTERED_BODY_FILES = [
    f"gh pr create {_PUB} -t clean -dF {{path}}",
    f"gh release create v9.9.9 {_PUB} -pF {{path}}",
    "git commit -aF {path}",
    "git tag -aF {path} v9.9.9",
]


class TestClusteredShortFlagsCarryFreeText:
    @pytest.mark.parametrize(
        "command",
        [*_CLUSTERED_FREE_TEXT, *_CLUSTERED_BODY_FILES, "git commit -am{text}", "git commit -m'{text}'"],
    )
    @pytest.mark.parametrize("variant", [0, 1], ids=["term", "clean"])
    def test_publish_cluster_has_nonempty_payload(self, command: str, variant: int, files: tuple[Path, Path]) -> None:
        resolved = command.format(text=(_TERM, "routine")[variant], path=files[variant])
        assert is_publish_command(resolved) is True
        assert _payload(resolved)

    @pytest.mark.parametrize("command", _CLUSTERED_BODY_FILES)
    def test_clustered_body_file_is_read(self, command: str, files: tuple[Path, Path]) -> None:
        assert _TERM in (_payload(command.format(path=files[0])) or "")

    @pytest.mark.parametrize("command", _CLUSTERED_BODY_FILES[:2])
    @pytest.mark.usefixtures("public_gate")
    def test_clustered_body_file_is_denied_on_public_forge(
        self, command: str, files: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert handle_banned_terms_pretool(_bash(command.format(path=files[0]))) is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    @pytest.mark.parametrize("spelling", _CLUSTERED_FREE_TEXT)
    @pytest.mark.usefixtures("public_gate")
    def test_the_clustered_value_is_denied_and_named(self, spelling: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_banned_terms_pretool(_bash(spelling.format(text=_TERM))) is True
        assert _TERM in json.loads(capsys.readouterr().out)["permissionDecisionReason"]

    @pytest.mark.parametrize("spelling", _CLUSTERED_FREE_TEXT)
    @pytest.mark.usefixtures("public_gate")
    def test_the_clean_twin_is_allowed(self, spelling: str, capsys: pytest.CaptureFixture[str]) -> None:
        command = spelling.format(text="routine update")
        assert is_publish_command(command) is True
        assert handle_banned_terms_pretool(_bash(command)) is False
        assert capsys.readouterr().out == ""

    def test_a_cluster_of_switches_alone_is_not_a_publish(self) -> None:
        assert is_publish_command(f"gh pr merge 5 {_PUB} -sd") is False

    def test_an_attached_field_value_is_no_cluster(self, files: tuple[Path, Path]) -> None:
        payload = _payload(f'gh api repos/souliane/teatree/issues -X POST -fbody="$(cat {files[0]})"') or ""
        assert _TERM in payload
        assert not is_fail_closed_sentinel(payload)

    def test_a_value_ending_in_a_flag_letter_takes_no_next_word(self) -> None:
        payload = _payload("gh api repos/souliane/teatree/issues -X POST -f'body=routine sum' \"$T3_ABSENT_VAR\"") or ""
        assert "routine sum" in payload
        assert not is_fail_closed_sentinel(payload)

    @pytest.mark.parametrize("command", [f"git commit -am '{_TERM}'", f"git commit -m'{_TERM}'"])
    def test_a_clustered_or_attached_commit_message_is_extracted(self, command: str) -> None:
        assert is_publish_command(command) is True
        assert _TERM in (_payload(command) or "")

    @pytest.mark.usefixtures("public_gate")
    def test_unrelated_short_clusters_do_not_resolve_as_body_flags(self, capsys: pytest.CaptureFixture[str]) -> None:
        command = f'gh pr create {_PUB} -t routine -b routine && du -sb "$(pwd)"'
        assert handle_banned_terms_pretool(_bash(command)) is False
        assert capsys.readouterr().out == ""

    @pytest.mark.parametrize(
        "tail",
        [
            'ls -lb "$T3_ABSENT_CLUSTER_VAR"',
            'git diff -wb "$(git merge-base origin/main HEAD)"',
            'git log -nm "$(git merge-base origin/main HEAD)"',
        ],
    )
    @pytest.mark.usefixtures("public_gate")
    def test_read_segment_cluster_does_not_lock_out_clean_post(
        self, tail: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert handle_banned_terms_pretool(_bash(f"gh pr create {_PUB} -t routine -b routine && {tail}")) is False
        assert capsys.readouterr().out == ""

    @pytest.mark.usefixtures("public_gate")
    def test_unresolved_later_cluster_value_explains_remedy(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("T3_ABSENT_CLUSTER_VAR", raising=False)
        command = f'gh pr merge 5 {_PUB} -sd "$T3_ABSENT_CLUSTER_VAR"'
        assert handle_banned_terms_pretool(_bash(command)) is True
        assert "spell the options apart" in json.loads(capsys.readouterr().out)["permissionDecisionReason"]

    @pytest.mark.usefixtures("public_gate")
    def test_unresolved_later_attached_value_fails_closed(self, capsys: pytest.CaptureFixture[str]) -> None:
        command = f'gh pr merge 5 {_PUB} -sb "$(unknown-cmd)"'
        assert handle_banned_terms_pretool(_bash(command)) is True
        assert "spell the options apart" in json.loads(capsys.readouterr().out)["permissionDecisionReason"]

    def test_commit_cluster_values_are_extracted(self, files: tuple[Path, Path]) -> None:
        content, _ = files
        assert _TERM in (_payload(f"git commit -am{_TERM}") or "")
        assert _TERM in (_payload(f'git commit -m"$(cat {content})"') or "")
        assert is_fail_closed_sentinel(_payload('git commit -sm "$(unknown-cmd)"') or "")

    @pytest.mark.usefixtures("public_gate")
    def test_unresolved_local_commit_cluster_retains_warn_policy(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("T3_ABSENT_CLUSTER_VAR", raising=False)
        assert handle_banned_terms_pretool(_bash('git commit -am "$T3_ABSENT_CLUSTER_VAR"')) is False
        assert capsys.readouterr().out == ""
