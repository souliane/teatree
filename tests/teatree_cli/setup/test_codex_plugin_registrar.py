import io
import json
from collections.abc import Callable, Iterator
from contextlib import redirect_stdout
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from unittest.mock import patch

import pytest

from teatree.cli.setup.codex_plugin_registrar import CodexPluginRegistrar

type Reply = CompletedProcess[str] | BaseException | Callable[[], CompletedProcess[str]]


def _ok(payload: object = None) -> CompletedProcess[str]:
    return CompletedProcess(["codex"], 0, json.dumps(payload if payload is not None else {}), "")


def _failed(stderr: str, returncode: int = 1) -> CompletedProcess[str]:
    return CompletedProcess(["codex"], returncode, "", stderr)


def _with_codex_manifest(root: Path) -> Path:
    (root / ".agents" / "plugins").mkdir(parents=True)
    (root / ".agents" / "plugins" / "marketplace.json").write_text(
        json.dumps(
            {"name": "souliane", "plugins": [{"name": "t3", "source": {"source": "local", "path": "./plugins/t3"}}]}
        )
    )
    (root / ".codex-plugin").mkdir()
    (root / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "t3", "skills": "./skills/", "mcpServers": "./.mcp.json"})
    )
    (root / "skills" / "code").mkdir(parents=True)
    (root / "skills" / "code" / "SKILL.md").write_text("# code\n")
    (root / ".mcp.json").write_text("{}\n")
    return root


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))

    def _get_data_dir(namespace: str) -> Path:
        path = tmp_path / "data" / namespace
        path.mkdir(parents=True, exist_ok=True)
        return path

    with (
        patch("teatree.cli.setup.codex_plugin_registrar.get_data_dir", _get_data_dir),
        patch("teatree.cli.setup.codex_plugin_registrar.shutil.which", return_value="/usr/bin/codex"),
    ):
        yield tmp_path


def _slim_root(tmp_path: Path) -> Path:
    return (tmp_path / "data" / "codex-plugin").resolve()


def _install(registrar: CodexPluginRegistrar, replies: list[Reply]) -> tuple[str | None, list[tuple[str, ...]]]:
    """Run ``install()``; return ``None`` on success, else its WARN output, plus the CLI calls."""
    calls: list[tuple[str, ...]] = []
    queue = list(replies)

    def _run(command: list[str], **kwargs: object) -> CompletedProcess[str]:
        assert kwargs == {"expected_codes": None, "timeout": 120}
        calls.append(tuple(command[1:]))
        reply = queue.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply if isinstance(reply, CompletedProcess) else reply()

    output = io.StringIO()
    with (
        patch("teatree.cli.setup.codex_plugin_registrar.run_allowed_to_fail", side_effect=_run),
        redirect_stdout(output),
    ):
        installed = registrar.install()
    return (None if installed else output.getvalue()), calls


def _fresh_install_calls(slim: Path) -> list[tuple[str, ...]]:
    return [
        ("plugin", "marketplace", "list", "--json"),
        ("plugin", "marketplace", "add", str(slim), "--json"),
        ("plugin", "list", "--json"),
        ("plugin", "add", "t3@souliane", "--json"),
    ]


def test_registers_a_slim_root_from_the_vendored_manifest_root_without_touching_the_checkout(env: Path) -> None:
    fork = env / "host-project"
    vendor = _with_codex_manifest(fork / "vendor" / "teatree")

    reason, calls = _install(
        CodexPluginRegistrar(fork),
        [_ok({"marketplaces": []}), _ok(), _ok({"installed": []}), _ok()],
    )

    assert reason is None
    assert calls == _fresh_install_calls(_slim_root(env))
    assert not (vendor / "plugins").exists()


def test_the_registered_root_excludes_checkout_junk_and_symlinks(env: Path) -> None:
    checkout = _with_codex_manifest(env / "checkout")
    (checkout / ".git").mkdir()
    (checkout / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (checkout / ".venv").mkdir()
    (checkout / ".venv" / "lib.so").write_bytes(b"\0" * (5 * 1024 * 1024))
    (checkout / "plugins").mkdir()
    (checkout / "plugins" / "t3").symlink_to("..")

    reason, calls = _install(
        CodexPluginRegistrar(checkout),
        [_ok({"marketplaces": []}), _ok(), _ok({"installed": []}), _ok()],
    )

    registered = Path(calls[1][3])
    files = {str(path.relative_to(registered)) for path in registered.rglob("*") if path.is_file()}
    assert reason is None
    assert registered != checkout.resolve()
    assert files == {
        ".agents/plugins/marketplace.json",
        "plugins/t3/.codex-plugin/plugin.json",
        "plugins/t3/skills/code/SKILL.md",
        "plugins/t3/.mcp.json",
    }
    assert not [path for path in registered.rglob("*") if path.is_symlink()]
    assert sum(path.stat().st_size for path in registered.rglob("*") if path.is_file()) < 5 * 1024 * 1024


def test_a_manifestless_checkout_is_refused_before_the_cli(env: Path) -> None:
    repo = env / "teatree"
    repo.mkdir()

    reason, calls = _install(CodexPluginRegistrar(repo), [])

    assert calls == []
    assert reason is not None
    assert "Codex plugin manifests" in reason


@pytest.mark.parametrize(
    ("manifest", "payload"),
    [
        (".agents/plugins/marketplace.json", "{broken"),
        (".codex-plugin/plugin.json", "{broken"),
        (".agents/plugins/marketplace.json", '{"name":"other","plugins":[]}'),
        (".codex-plugin/plugin.json", '{"name":"other"}'),
    ],
)
def test_an_invalid_manifest_is_refused_before_the_cli(env: Path, manifest: str, payload: str) -> None:
    repo = _with_codex_manifest(env / "teatree")
    (repo / manifest).write_text(payload)

    reason, calls = _install(CodexPluginRegistrar(repo), [])

    assert calls == []
    assert reason is not None
    assert "Codex plugin manifests" in reason


def test_a_refused_payload_never_reaches_the_cli(env: Path) -> None:
    repo = _with_codex_manifest(env / "teatree")
    (repo / ".codex-plugin" / "plugin.json").write_text(json.dumps({"name": "t3", "skills": "./"}))

    reason, calls = _install(CodexPluginRegistrar(repo), [])

    assert calls == []
    assert reason is not None
    assert "payload" in reason


def test_already_registered_slim_root_is_a_noop(env: Path) -> None:
    repo = _with_codex_manifest(env / "teatree")
    slim = _slim_root(env)
    marketplace = {"marketplaces": [{"name": "souliane", "root": str(slim)}]}
    installed = {
        "installed": [
            {
                "pluginId": "t3@souliane",
                "installed": True,
                "enabled": True,
                "source": {"source": "local", "path": str(slim / "plugins" / "t3")},
            }
        ]
    }

    reason, calls = _install(CodexPluginRegistrar(repo), [_ok(marketplace), _ok(installed)])

    assert reason is None
    assert calls == [("plugin", "marketplace", "list", "--json"), ("plugin", "list", "--json")]


def test_a_disabled_plugin_is_reinstalled(env: Path) -> None:
    repo = _with_codex_manifest(env / "teatree")
    slim = _slim_root(env)
    marketplace = {"marketplaces": [{"name": "souliane", "root": str(slim)}]}
    disabled = {
        "installed": [
            {
                "pluginId": "t3@souliane",
                "installed": True,
                "enabled": False,
                "source": {"path": str(slim / "plugins" / "t3")},
            }
        ]
    }

    reason, calls = _install(CodexPluginRegistrar(repo), [_ok(marketplace), _ok(disabled), _ok(), _ok()])

    assert reason is None
    assert calls == [
        ("plugin", "marketplace", "list", "--json"),
        ("plugin", "list", "--json"),
        ("plugin", "remove", "t3@souliane", "--json"),
        ("plugin", "add", "t3@souliane", "--json"),
    ]


def test_a_checkout_root_registration_migrates_to_the_slim_root(env: Path) -> None:
    repo = _with_codex_manifest(env / "teatree")
    marketplace = {"marketplaces": [{"name": "souliane", "root": str(repo.resolve())}]}
    installed = {
        "installed": [
            {
                "pluginId": "t3@souliane",
                "installed": True,
                "enabled": True,
                "source": {"source": "local", "path": str(repo.resolve() / "plugins" / "t3")},
            }
        ]
    }

    reason, calls = _install(
        CodexPluginRegistrar(repo),
        [_ok(marketplace), _ok(), _ok(), _ok(installed), _ok(), _ok()],
    )

    assert reason is None
    assert calls == [
        ("plugin", "marketplace", "list", "--json"),
        ("plugin", "marketplace", "remove", "souliane", "--json"),
        ("plugin", "marketplace", "add", str(_slim_root(env)), "--json"),
        ("plugin", "list", "--json"),
        ("plugin", "remove", "t3@souliane", "--json"),
        ("plugin", "add", "t3@souliane", "--json"),
    ]


@pytest.mark.parametrize(
    ("replies", "purpose"),
    [
        ([_failed("marketplace db locked")], "inspect Codex marketplaces"),
        ([_ok({"marketplaces": []}), _failed("marketplace db locked")], "add the TeaTree Codex marketplace"),
        ([_ok({"marketplaces": []}), _ok(), _failed("marketplace db locked")], "inspect Codex plugins"),
        (
            [_ok({"marketplaces": []}), _ok(), _ok({"installed": []}), _failed("marketplace db locked")],
            "install the TeaTree Codex plugin",
        ),
    ],
)
def test_each_cli_failure_reports_its_exit_code_and_stderr(env: Path, replies: list[Reply], purpose: str) -> None:
    repo = _with_codex_manifest(env / "teatree")

    reason, _calls = _install(CodexPluginRegistrar(repo), replies)

    assert reason is not None
    assert purpose in reason
    assert "exited 1" in reason
    assert "marketplace db locked" in reason


@pytest.mark.parametrize("target", ["marketplace", "plugin"])
def test_a_failed_stale_registration_removal_is_reported(env: Path, target: str) -> None:
    repo = _with_codex_manifest(env / "teatree")
    slim = _slim_root(env)
    stale_marketplace = {"marketplaces": [{"name": "souliane", "root": str(env / "old")}]}
    stale_plugin = {"installed": [{"pluginId": "t3@souliane", "installed": True, "source": {"path": str(env / "old")}}]}
    registered_marketplace = {"marketplaces": [{"name": "souliane", "root": str(slim)}]}
    replies: list[Reply] = (
        [_ok(stale_marketplace), _failed("busy")]
        if target == "marketplace"
        else [_ok(registered_marketplace), _ok(stale_plugin), _failed("busy")]
    )

    reason, _calls = _install(CodexPluginRegistrar(repo), replies)

    assert reason is not None
    assert f"replace the TeaTree Codex {target}" in reason


def _add_leaving_staging(home: Path, reply: Reply) -> Callable[[], CompletedProcess[str]]:
    def _reply() -> CompletedProcess[str]:
        (home / "plugins" / "cache" / "souliane" / "plugin-install-new").mkdir(parents=True)
        if isinstance(reply, BaseException):
            raise reply
        return reply if isinstance(reply, CompletedProcess) else reply()

    return _reply


@pytest.mark.parametrize(
    ("add_reply", "expected"),
    [
        (_failed("No space left on device"), "No space left on device"),
        (TimeoutExpired(["codex"], 120), "timed out after 120s"),
    ],
)
def test_a_failed_plugin_add_leaves_no_new_staging_and_keeps_older_staging(
    env: Path, add_reply: Reply, expected: str
) -> None:
    repo = _with_codex_manifest(env / "teatree")
    cache = env / "codex-home" / "plugins" / "cache" / "souliane"
    (cache / "plugin-install-older").mkdir(parents=True)

    reason, _calls = _install(
        CodexPluginRegistrar(repo),
        [
            _ok({"marketplaces": []}),
            _ok(),
            _ok({"installed": []}),
            _add_leaving_staging(env / "codex-home", add_reply),
        ],
    )

    assert reason is not None
    assert expected in reason
    assert sorted(path.name for path in cache.iterdir()) == ["plugin-install-older"]


def test_missing_codex_is_nonfatal(env: Path) -> None:
    with patch("teatree.cli.setup.codex_plugin_registrar.shutil.which", return_value=None):
        reason, calls = _install(CodexPluginRegistrar(env), [])

    assert calls == []
    assert reason is not None
    assert "`codex` not on PATH" in reason


def test_a_codex_binary_that_cannot_start_is_reported(env: Path) -> None:
    repo = _with_codex_manifest(env / "teatree")

    reason, _calls = _install(CodexPluginRegistrar(repo), [OSError("exec format error")])

    assert reason is not None
    assert "did not start" in reason
    assert "exec format error" in reason


@pytest.mark.parametrize("stdout", ["not-json", "{broken", "[]"])
def test_output_without_a_json_object_is_reported(env: Path, stdout: str) -> None:
    repo = _with_codex_manifest(env / "teatree")

    reason, _calls = _install(CodexPluginRegistrar(repo), [CompletedProcess(["codex"], 0, stdout, "")])

    assert reason is not None
    assert "inspect Codex marketplaces" in reason


def test_a_trailing_json_object_after_cli_warnings_is_accepted(env: Path) -> None:
    repo = _with_codex_manifest(env / "teatree")
    noisy = CompletedProcess(["codex"], 0, 'Warning: ignored invalid metadata\n{"marketplaces": []}\n', "")

    reason, calls = _install(CodexPluginRegistrar(repo), [noisy, _ok(), _ok({"installed": []}), _ok()])

    assert reason is None
    assert calls == _fresh_install_calls(_slim_root(env))


def test_stderr_in_the_reason_is_bounded(env: Path) -> None:
    repo = _with_codex_manifest(env / "teatree")

    reason, _calls = _install(CodexPluginRegistrar(repo), [_failed("e" * 50_000)])

    assert reason is not None
    assert len(reason) < 2_000


def test_registration_schema_probes_reject_invalid_rows_and_find_later_match(tmp_path: Path) -> None:
    root = tmp_path / "slim"
    expected_plugin = root / "plugins" / "t3"

    assert CodexPluginRegistrar._marketplace_present({"marketplaces": {}}) is False
    assert CodexPluginRegistrar._marketplace_registered({"marketplaces": {}}, root) is False
    assert (
        CodexPluginRegistrar._marketplace_registered(
            {"marketplaces": [None, {"name": "other"}, {"name": "souliane", "root": 7}]}, root
        )
        is False
    )
    assert (
        CodexPluginRegistrar._marketplace_registered(
            {"marketplaces": [None, {"name": "souliane", "root": str(root)}]}, root
        )
        is True
    )
    assert CodexPluginRegistrar._plugin_present({"installed": {}}) is False
    assert CodexPluginRegistrar._plugin_registered({"installed": {}}, root) is False
    assert (
        CodexPluginRegistrar._plugin_registered(
            {
                "installed": [
                    None,
                    {"pluginId": "other", "installed": True},
                    {"pluginId": "t3@souliane", "installed": True, "source": None},
                    {
                        "pluginId": "t3@souliane",
                        "installed": True,
                        "enabled": True,
                        "source": {"path": str(expected_plugin)},
                    },
                ]
            },
            root,
        )
        is True
    )
