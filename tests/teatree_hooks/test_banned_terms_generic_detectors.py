"""Hook-level generic-detector policy for forge posts and local commits."""

import json
from pathlib import Path

import pytest

from hooks.scripts import t3_invocation
from hooks.scripts.hook_router import handle_banned_terms_pretool, handle_block_ai_signature
from teatree.hooks import _repo_visibility, banned_term_registry, banned_terms_scanner, generic_forge_gate
from tests.teatree_hooks import test_banned_terms_hook as _banned_terms_hook

_public_teatree_probe = _banned_terms_hook._public_teatree_probe
_seed_config_db = _banned_terms_hook._seed_config_db


@pytest.mark.usefixtures("in_process_banned_terms_scanner")
@pytest.mark.parametrize(
    ("category", "body"),
    [
        ("email", "Contact person@firm.tld"),  # privacy-scan:allow synthetic fixture
        ("home_path", "Read /home/employee/report"),  # privacy-scan:allow synthetic fixture
        ("private_ip", "Connect to 10.24.8.9"),  # privacy-scan:allow synthetic fixture
        ("internal_hostname", "Host build.internal.corp"),  # privacy-scan:allow synthetic fixture
    ],
)
def test_generic_detector_denies_public_bash_forge_write(
    category: str, body: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(
        "TEATREE_TERM_REGISTRY", json.dumps({"leak": ["placeholder-leak"], "prose_collider": [], "tone": []})
    )
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": f'gh issue comment 5 --repo souliane/teatree --body "{body}"'},
        "cwd": str(tmp_path),
    }

    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked
    assert json.loads(captured.out)["permissionDecision"] == "deny"
    assert category in captured.out


@pytest.mark.usefixtures("in_process_banned_terms_scanner")
@pytest.mark.parametrize(("visibility", "expected"), [("PRIVATE", "allow"), (None, "block")])
def test_generic_detector_respects_private_and_unknown_bash_targets(
    visibility: str | None,
    expected: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(
        "TEATREE_TERM_REGISTRY", json.dumps({"leak": ["placeholder-leak"], "prose_collider": [], "tone": []})
    )
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: visibility)
    body = '"person@firm.tld"'  # privacy-scan:allow synthetic fixture
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": f"gh issue comment 5 --repo acme/widget --body {body}"},
        "cwd": str(tmp_path),
    }

    assert handle_banned_terms_pretool(data) is (expected == "block")
    if visibility is None:
        assert "private_repos" in capsys.readouterr().out


@pytest.mark.usefixtures("in_process_banned_terms_scanner")
def test_generic_detector_respects_configured_private_repo_without_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "config.sqlite3"
    _seed_config_db(db, {"private_repos": ["github.com/acme/widget"]})
    monkeypatch.setenv("T3_CONFIG_DB", str(db))
    monkeypatch.setenv(
        "TEATREE_TERM_REGISTRY", json.dumps({"leak": ["placeholder-leak"], "prose_collider": [], "tone": []})
    )
    monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
    body = "Contact person@firm.tld"  # privacy-scan:allow synthetic fixture
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": f'gh issue comment 5 --repo acme/widget --body "{body}"'},
        "cwd": str(tmp_path),
    }

    assert handle_banned_terms_pretool(data) is False


@pytest.mark.usefixtures("in_process_banned_terms_scanner")
def test_generic_detector_blocks_non_trailer_copy_of_trailer_email(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(
        "TEATREE_TERM_REGISTRY", json.dumps({"leak": ["placeholder-leak"], "prose_collider": [], "tone": []})
    )
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
    body_file = tmp_path / "body.md"
    body_file.write_text("Ping xa@b.co\nCo-Authored-By: Claude <xa@b.co>\n")  # privacy-scan:allow synthetic fixture
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": f"gh issue create --repo souliane/teatree --body-file {body_file}"},
        "cwd": str(tmp_path),
    }

    assert handle_banned_terms_pretool(data)
    assert "generic email" in capsys.readouterr().out


@pytest.mark.usefixtures("in_process_banned_terms_scanner")
def test_trailer_email_still_blocks_when_t3_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(
        "TEATREE_TERM_REGISTRY", json.dumps({"leak": ["placeholder-leak"], "prose_collider": [], "tone": []})
    )
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
    monkeypatch.setattr(t3_invocation, "t3_argv", lambda *_args: None)
    body = "Co-Authored-By: Claude <xa@b.co>"  # privacy-scan:allow synthetic fixture
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": f"gh issue comment 5 --repo souliane/teatree --body '{body}'"},
        "cwd": str(tmp_path),
    }

    assert handle_block_ai_signature(data)
    assert "AI-signature" in capsys.readouterr().out


@pytest.mark.usefixtures("in_process_banned_terms_scanner")
def test_generic_detector_distinguishes_form_feed_separated_trailer_and_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(
        "TEATREE_TERM_REGISTRY", json.dumps({"leak": ["placeholder-leak"], "prose_collider": [], "tone": []})
    )
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
    body_file = tmp_path / "body.md"
    body_file.write_text(
        "Co-Authored-By: Claude <xa@b.co>\fPing person@firm.tld"  # privacy-scan:allow synthetic fixture
    )
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": f"gh issue create --repo souliane/teatree --body-file {body_file}"},
        "cwd": str(tmp_path),
    }

    assert handle_banned_terms_pretool(data)
    assert "generic email" in capsys.readouterr().out


@pytest.mark.usefixtures("in_process_banned_terms_scanner")
def test_generic_deny_names_non_trailer_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(
        "TEATREE_TERM_REGISTRY", json.dumps({"leak": ["placeholder-leak"], "prose_collider": [], "tone": []})
    )
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
    body_file = tmp_path / "body.md"
    body_file.write_text(
        "Co-Authored-By: Claude <xa@b.co>\nRead /home/employee/report\n"  # privacy-scan:allow synthetic fixture
    )
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": f"gh issue create --repo souliane/teatree --body-file {body_file}"},
        "cwd": str(tmp_path),
    }

    assert handle_banned_terms_pretool(data)
    captured = capsys.readouterr()
    assert "generic home_path" in captured.out


@pytest.mark.usefixtures("in_process_banned_terms_scanner")
def test_chained_commit_and_pr_create_scans_the_forge_title(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "TEATREE_TERM_REGISTRY", json.dumps({"leak": ["placeholder-leak"], "prose_collider": [], "tone": []})
    )
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
    data = {
        "tool_name": "Bash",
        "tool_input": {
            "command": "git commit -m safe && gh pr create --repo souliane/teatree "
            "--title 'Contact person@firm.tld' --body safe"  # privacy-scan:allow synthetic fixture
        },
        "cwd": str(tmp_path),
    }  # privacy-scan:allow synthetic fixture

    assert handle_banned_terms_pretool(data)


@pytest.mark.usefixtures("in_process_banned_terms_scanner")
def test_generic_detector_warns_on_local_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(
        "TEATREE_TERM_REGISTRY", json.dumps({"leak": ["placeholder-leak"], "prose_collider": [], "tone": []})
    )
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": 'git commit -m "Contact person@firm.tld"'},  # privacy-scan:allow synthetic fixture
        "cwd": str(tmp_path),
    }

    assert handle_banned_terms_pretool(data) is False
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "generic email" in captured.err
    assert "person@firm.tld" not in captured.err  # privacy-scan:allow synthetic fixture


def test_generic_detector_warns_on_local_commit_when_allowlist_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(
        "TEATREE_TERM_REGISTRY", json.dumps({"leak": ["placeholder-leak"], "prose_collider": [], "tone": []})
    )
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")

    def unreadable_allowlist(db_path: Path | None = None) -> tuple[str, ...]:
        del db_path
        message = "unreadable"
        raise banned_term_registry.BannedTermsUnsetError(message)

    monkeypatch.setattr(banned_terms_scanner, "scan_text", lambda _payload: None)
    monkeypatch.setattr(generic_forge_gate, "allowlist_terms", unreadable_allowlist)
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": 'git commit -m "Contact person@firm.tld"'},  # privacy-scan:allow synthetic fixture
        "cwd": str(tmp_path),
    }

    assert handle_banned_terms_pretool(data) is False
    assert "generic email" in capsys.readouterr().err


@pytest.mark.usefixtures("in_process_banned_terms_scanner")
def test_generic_detector_honors_exact_allow_but_not_an_extended_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pins generic_payload_finding's own allowlist_terms lookup.
    monkeypatch.setenv(
        "TEATREE_TERM_REGISTRY",
        json.dumps(
            {
                "leak": ["placeholder-leak"],
                "prose_collider": [],
                "tone": [],
                "allow": ["person@firm.tld", "firm"],  # privacy-scan:allow synthetic fixture
            }
        ),
    )  # privacy-scan:allow synthetic fixture
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")

    def post(body: str) -> bool:
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": f'gh issue comment 5 --repo souliane/teatree --body "{body}"'},
            "cwd": str(tmp_path),
        }
        return handle_banned_terms_pretool(data)

    assert post("person@firm.tld") is False  # privacy-scan:allow synthetic fixture
    assert post("person@firm.tld.biz") is True  # privacy-scan:allow synthetic fixture
    assert post("other@firm.tld") is True  # privacy-scan:allow synthetic fixture


@pytest.mark.usefixtures("in_process_banned_terms_scanner")
def test_generic_detector_does_not_scan_slack_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(
        "TEATREE_TERM_REGISTRY", json.dumps({"leak": ["placeholder-leak"], "prose_collider": [], "tone": []})
    )
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))
    data = {
        "tool_name": "Bash",
        "tool_input": {
            "command": (
                "curl -X POST -d "
                "'text=/home/employee/report' "  # privacy-scan:allow synthetic fixture
                "https://slack.com/api/chat.postMessage"
            )
        },
        "cwd": str(tmp_path),
    }

    assert handle_banned_terms_pretool(data) is False
    assert capsys.readouterr().out == ""


@pytest.mark.usefixtures("in_process_banned_terms_scanner")
def test_generic_detector_exempts_public_ci_home_path_on_forge_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pins generic_payload_finding's forge_write=not local_commit call.
    monkeypatch.setenv(
        "TEATREE_TERM_REGISTRY", json.dumps({"leak": ["placeholder-leak"], "prose_collider": [], "tone": []})
    )
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
    data = {
        "tool_name": "Bash",
        "tool_input": {
            "command": (
                "gh issue comment 5 --repo souliane/teatree --body "
                '"CI at /home/runner/work/project"'  # privacy-scan:allow synthetic fixture
            )
        },
        "cwd": str(tmp_path),
    }

    assert handle_banned_terms_pretool(data) is False


@pytest.mark.usefixtures("in_process_banned_terms_scanner")
def test_generic_detector_does_not_honor_push_scanner_marker_on_forge_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "TEATREE_TERM_REGISTRY", json.dumps({"leak": ["placeholder-leak"], "prose_collider": [], "tone": []})
    )
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
    data = {
        "tool_name": "Bash",
        "tool_input": {
            "command": 'gh issue comment 5 --repo souliane/teatree --body "person@firm.tld # privacy-scan:allow"'
        },  # privacy-scan:allow synthetic fixture
        "cwd": str(tmp_path),
    }

    assert handle_banned_terms_pretool(data) is True
