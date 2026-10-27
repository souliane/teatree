import io
import json
import os
import shutil
from collections.abc import Callable, Iterator
from contextlib import redirect_stdout
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from unittest.mock import patch

import pytest

import teatree
import teatree.cli.setup.codex_plugin_registrar as registrar_mod
from teatree.cli.setup.codex_plugin_payload import CodexPluginPayload
from teatree.cli.setup.codex_plugin_registrar import CodexPluginRegistrar
from teatree.core.mcp_registration import PLUGIN_ARGS

type Reply = CompletedProcess[str] | BaseException | Callable[[], CompletedProcess[str]]


def _ok(payload: object = None) -> CompletedProcess[str]:
    return CompletedProcess(["codex"], 0, json.dumps(payload if payload is not None else {}), "")


def _failed(stderr: str, returncode: int = 1) -> CompletedProcess[str]:
    return CompletedProcess(["codex"], returncode, "", stderr)


_STDIO_TEATREE = {"type": "stdio", "command": "t3", "args": list(PLUGIN_ARGS)}
_TEATREE_ENTRY = {"name": "teatree", "enabled": True, "transport": _STDIO_TEATREE}
_MCP_RESOLVED = _ok(_TEATREE_ENTRY)
_MCP_GET = ("mcp", "get", "teatree", "--json")
_PLUGIN_OFF_MCP_LIST = ("-c", 'plugins={"t3@souliane"={enabled=false}}', "mcp", "list", "--json")


def _resolved_with(**transport: object) -> dict[str, object]:
    return {**_TEATREE_ENTRY, "transport": {**_STDIO_TEATREE, **transport}}


def _with_codex_manifest(root: Path) -> Path:
    (root / ".agents" / "plugins").mkdir(parents=True)
    (root / ".agents" / "plugins" / "marketplace.json").write_text(
        json.dumps(
            {"name": "souliane", "plugins": [{"name": "t3", "source": {"source": "local", "path": "./plugins/t3"}}]}
        )
    )
    (root / ".codex-plugin").mkdir()
    (root / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "t3", "version": "0.0.1", "skills": "./skills/", "mcpServers": "./.mcp.json"})
    )
    (root / "skills" / "code").mkdir(parents=True)
    (root / "skills" / "code" / "SKILL.md").write_text("# code\n")
    (root / ".mcp.json").write_text("{}\n")
    return root


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    def _get_data_dir(namespace: str) -> Path:
        path = tmp_path / "data" / namespace
        path.mkdir(parents=True, exist_ok=True)
        return path

    with (
        patch("teatree.cli.setup.codex_plugin_registrar.get_data_dir", _get_data_dir),
        patch("teatree.cli.setup.codex_plugin_registrar.shutil.which", return_value="/usr/bin/codex"),
        patch("teatree.cli.setup.codex_plugin_registrar.container_is_the_sandbox", return_value=False),
    ):
        yield tmp_path


def _slim_root(tmp_path: Path) -> Path:
    return (tmp_path / "data" / "codex-plugin").resolve()


def _manifest_version(repo: Path) -> str:
    manifest = json.loads((repo / ".codex-plugin" / "plugin.json").read_text())
    return manifest.get("version", "local")


def _served_copy(env: Path, repo: Path) -> Path:
    payload = CodexPluginPayload(repo, _slim_root(env)).materialize()
    served = env / "codex-home" / "plugins" / "cache" / "souliane" / "t3" / _manifest_version(repo)
    shutil.copytree(payload / "plugins" / "t3", served)
    return served


def _registered_plugin(slim: Path, *, version: str = "0.0.1") -> dict[str, object]:
    return {
        "installed": [
            {
                "pluginId": "t3@souliane",
                "version": version,
                "installed": True,
                "enabled": True,
                "source": {"source": "local", "path": str(slim / "plugins" / "t3")},
            }
        ]
    }


def _post_add_plugins(version: str = "0.0.1") -> CompletedProcess[str]:
    return _ok({"installed": [{"pluginId": "t3@souliane", "installed": True, "version": version}]})


def _run_install(
    registrar: CodexPluginRegistrar, replies: list[Reply], *, add_copies_payload: bool | None = True
) -> tuple[bool, str, list[tuple[str, ...]]]:
    calls: list[tuple[str, ...]] = []
    queue = list(replies)

    def _run(command: list[str], **kwargs: object) -> CompletedProcess[str]:
        assert kwargs == {"expected_codes": None, "timeout": 120}
        calls.append(tuple(command[1:]))
        reply = queue.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        result = reply if isinstance(reply, CompletedProcess) else reply()
        if result.returncode == 0 and command[1:3] in (["plugin", "remove"], ["plugin", "add"]):
            served = (
                Path(os.environ["CODEX_HOME"])
                / "plugins"
                / "cache"
                / "souliane"
                / "t3"
                / _manifest_version(registrar.manifest_root)
            )
            if command[2] == "remove":
                shutil.rmtree(served, ignore_errors=True)
            elif add_copies_payload:
                source = registrar_mod.get_data_dir("codex-plugin") / "plugins" / "t3"
                shutil.copytree(source, served, dirs_exist_ok=True)
            elif add_copies_payload is False:
                (served / "skills" / "code").mkdir(parents=True)
                (served / "skills" / "code" / "SKILL.md").write_text("# stale\n")
        return result

    output = io.StringIO()
    with (
        patch("teatree.cli.setup.codex_plugin_registrar.run_allowed_to_fail", side_effect=_run),
        redirect_stdout(output),
    ):
        installed = registrar.install()
    return installed, output.getvalue(), calls


def _install(
    registrar: CodexPluginRegistrar, replies: list[Reply], *, add_copies_payload: bool | None = True
) -> tuple[str | None, list[tuple[str, ...]]]:
    """Run ``install()``; return ``None`` on success, else its WARN output, plus the CLI calls."""
    installed, output, calls = _run_install(registrar, replies, add_copies_payload=add_copies_payload)
    return (None if installed else output), calls


def _fresh_install_calls(slim: Path) -> list[tuple[str, ...]]:
    return [
        ("plugin", "marketplace", "list", "--json"),
        ("plugin", "marketplace", "add", str(slim), "--json"),
        ("plugin", "list", "--json"),
        ("plugin", "add", "t3@souliane", "--json"),
        ("plugin", "list", "--json"),
        _MCP_GET,
    ]


def _fresh_install_replies() -> list[Reply]:
    return [_ok({"marketplaces": []}), _ok(), _ok({"installed": []}), _ok(), _post_add_plugins()]


def test_registers_a_slim_root_from_the_vendored_manifest_root_without_touching_the_checkout(env: Path) -> None:
    fork = env / "host-project"
    vendor = _with_codex_manifest(fork / "vendor" / "teatree")

    reason, calls = _install(
        CodexPluginRegistrar(fork),
        [*_fresh_install_replies(), _MCP_RESOLVED],
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
        [*_fresh_install_replies(), _MCP_RESOLVED],
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
    _served_copy(env, repo)
    marketplace = {"marketplaces": [{"name": "souliane", "root": str(slim)}]}
    installed = _registered_plugin(slim)

    reason, calls = _install(CodexPluginRegistrar(repo), [_ok(marketplace), _ok(installed), _MCP_RESOLVED])

    assert reason is None
    assert calls == [("plugin", "marketplace", "list", "--json"), ("plugin", "list", "--json"), _MCP_GET]


def test_codex_reported_version_selects_the_served_cache(env: Path) -> None:
    repo = _with_codex_manifest(env / "teatree")
    plugin_version = "9.7.2"
    assert teatree.__version__ != plugin_version
    manifest = repo / ".codex-plugin" / "plugin.json"
    metadata = json.loads(manifest.read_text())
    metadata["version"] = plugin_version
    manifest.write_text(json.dumps(metadata))
    slim = _slim_root(env)
    source = CodexPluginPayload(repo, slim).materialize() / "plugins" / "t3"
    served = env / "codex-home" / "plugins" / "cache" / "souliane" / "t3" / plugin_version
    shutil.copytree(source, served)
    marketplace = {"marketplaces": [{"name": "souliane", "root": str(slim)}]}
    installed = _registered_plugin(slim, version=plugin_version)

    reason, calls = _install(
        CodexPluginRegistrar(repo), [_ok(marketplace), _ok(installed), _MCP_RESOLVED, _ok(), _ok()]
    )

    assert calls == [("plugin", "marketplace", "list", "--json"), ("plugin", "list", "--json"), _MCP_GET]
    assert reason is None


def test_fresh_install_checks_the_version_codex_reports_after_add(env: Path) -> None:
    repo = _with_codex_manifest(env / "teatree")
    manifest = repo / ".codex-plugin" / "plugin.json"
    metadata = json.loads(manifest.read_text())
    metadata["version"] = "9.7.2"
    manifest.write_text(json.dumps(metadata))

    reason, calls = _install(
        CodexPluginRegistrar(repo), [*_fresh_install_replies()[:-1], _post_add_plugins("9.7.2"), _MCP_RESOLVED]
    )

    assert reason is None
    assert calls == _fresh_install_calls(_slim_root(env))
    assert (env / "codex-home" / "plugins" / "cache" / "souliane" / "t3" / "9.7.2").is_dir()


def test_missing_codex_version_after_add_is_reported(env: Path) -> None:
    repo = _with_codex_manifest(env / "teatree")

    reason, calls = _install(
        CodexPluginRegistrar(repo),
        [*_fresh_install_replies()[:-1], _ok({"installed": [{"pluginId": "t3@souliane", "installed": True}]})],
    )

    assert reason is not None
    assert "did not report a version" in reason
    assert _MCP_GET not in calls


def test_a_stale_served_copy_is_removed_and_re_added(env: Path) -> None:
    repo = _with_codex_manifest(env / "teatree")
    served = _served_copy(env, repo)
    (served / "skills" / "code" / "SKILL.md").write_text("# stale\n")
    slim = _slim_root(env)
    marketplace = {"marketplaces": [{"name": "souliane", "root": str(slim)}]}

    reason, calls = _install(
        CodexPluginRegistrar(repo),
        [_ok(marketplace), _ok(_registered_plugin(slim)), _ok(), _ok(), _post_add_plugins(), _MCP_RESOLVED],
    )

    assert calls[2:4] == [("plugin", "remove", "t3@souliane", "--json"), ("plugin", "add", "t3@souliane", "--json")]
    assert reason is None
    assert (served / "skills" / "code" / "SKILL.md").read_text() == "# code\n"


@pytest.mark.parametrize("drift", ["extra", "symlink", "directory"])
def test_a_served_copy_with_a_different_tree_is_reinstalled(env: Path, drift: str) -> None:
    repo = _with_codex_manifest(env / "teatree")
    served = _served_copy(env, repo)
    skill = served / "skills" / "code" / "SKILL.md"
    if drift == "extra":
        (served / "extra").write_text("stale")
    elif drift == "symlink":
        skill.unlink()
        skill.symlink_to(repo / "skills" / "code" / "SKILL.md")
    else:
        skill.unlink()
        skill.mkdir()
    slim = _slim_root(env)
    marketplace = {"marketplaces": [{"name": "souliane", "root": str(slim)}]}

    reason, calls = _install(
        CodexPluginRegistrar(repo),
        [_ok(marketplace), _ok(_registered_plugin(slim)), _ok(), _ok(), _post_add_plugins(), _MCP_RESOLVED],
    )

    assert reason is None
    assert calls[2:4] == [("plugin", "remove", "t3@souliane", "--json"), ("plugin", "add", "t3@souliane", "--json")]
    assert skill.read_text() == "# code\n"


def test_an_unreadable_served_copy_is_not_ready(env: Path) -> None:
    repo = _with_codex_manifest(env / "teatree")
    served = _served_copy(env, repo)
    slim = _slim_root(env)
    marketplace = {"marketplaces": [{"name": "souliane", "root": str(slim)}]}
    original_cmp = registrar_mod.filecmp.cmp

    def compare(original: Path, copy: Path, *, shallow: bool) -> bool:
        if copy == served / "skills" / "code" / "SKILL.md":
            reason = "unreadable served skill"
            raise OSError(reason)
        return original_cmp(original, copy, shallow=shallow)

    with patch.object(registrar_mod.filecmp, "cmp", compare):
        reason, calls = _install(
            CodexPluginRegistrar(repo),
            [_ok(marketplace), _ok(_registered_plugin(slim)), _ok(), _ok(), _post_add_plugins()],
        )

    assert reason is not None
    assert "still differs" in reason
    assert _MCP_GET not in calls


def test_an_identical_served_copy_is_left_alone(env: Path) -> None:
    repo = _with_codex_manifest(env / "teatree")
    served = _served_copy(env, repo)
    slim = _slim_root(env)
    marketplace = {"marketplaces": [{"name": "souliane", "root": str(slim)}]}

    reason, calls = _install(
        CodexPluginRegistrar(repo), [_ok(marketplace), _ok(_registered_plugin(slim)), _MCP_RESOLVED]
    )

    assert reason is None
    assert served.is_dir()
    assert calls == [("plugin", "marketplace", "list", "--json"), ("plugin", "list", "--json"), _MCP_GET]


def test_a_served_copy_still_stale_after_add_is_not_ready(env: Path) -> None:
    repo = _with_codex_manifest(env / "teatree")
    served = _served_copy(env, repo)
    (served / "skills" / "code" / "SKILL.md").write_text("# stale\n")
    slim = _slim_root(env)
    marketplace = {"marketplaces": [{"name": "souliane", "root": str(slim)}]}

    reason, calls = _install(
        CodexPluginRegistrar(repo),
        [_ok(marketplace), _ok(_registered_plugin(slim)), _ok(), _ok(), _post_add_plugins(), _MCP_RESOLVED],
        add_copies_payload=False,
    )

    assert reason is not None
    assert "still differs" in reason
    assert calls[2:4] == [("plugin", "remove", "t3@souliane", "--json"), ("plugin", "add", "t3@souliane", "--json")]
    assert _MCP_GET not in calls


def test_the_replace_path_is_verified_after_add(env: Path) -> None:
    repo = _with_codex_manifest(env / "teatree")
    slim = _slim_root(env)
    marketplace = {"marketplaces": [{"name": "souliane", "root": str(slim)}]}
    disabled = {"installed": [{"pluginId": "t3@souliane", "installed": True, "enabled": False}]}

    reason, calls = _install(
        CodexPluginRegistrar(repo),
        [_ok(marketplace), _ok(disabled), _ok(), _ok(), _post_add_plugins(), _MCP_RESOLVED],
        add_copies_payload=False,
    )

    assert reason is not None
    assert "still differs" in reason
    assert calls[2:4] == [("plugin", "remove", "t3@souliane", "--json"), ("plugin", "add", "t3@souliane", "--json")]
    assert _MCP_GET not in calls


def test_a_missing_served_copy_after_add_is_not_ready(env: Path) -> None:
    repo = _with_codex_manifest(env / "teatree")
    slim = _slim_root(env)
    marketplace = {"marketplaces": [{"name": "souliane", "root": str(slim)}]}

    reason, calls = _install(
        CodexPluginRegistrar(repo),
        [_ok(marketplace), _ok({"installed": []}), _ok(), _post_add_plugins()],
        add_copies_payload=None,
    )

    assert reason is not None
    assert "still differs" in reason
    assert _MCP_GET not in calls


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

    reason, calls = _install(
        CodexPluginRegistrar(repo), [_ok(marketplace), _ok(disabled), _ok(), _ok(), _post_add_plugins(), _MCP_RESOLVED]
    )

    assert reason is None
    assert calls == [
        ("plugin", "marketplace", "list", "--json"),
        ("plugin", "list", "--json"),
        ("plugin", "remove", "t3@souliane", "--json"),
        ("plugin", "add", "t3@souliane", "--json"),
        ("plugin", "list", "--json"),
        _MCP_GET,
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
        [_ok(marketplace), _ok(), _ok(), _ok(installed), _ok(), _ok(), _post_add_plugins(), _MCP_RESOLVED],
    )

    assert reason is None
    assert calls == [
        ("plugin", "marketplace", "list", "--json"),
        ("plugin", "marketplace", "remove", "souliane", "--json"),
        ("plugin", "marketplace", "add", str(_slim_root(env)), "--json"),
        ("plugin", "list", "--json"),
        ("plugin", "remove", "t3@souliane", "--json"),
        ("plugin", "add", "t3@souliane", "--json"),
        ("plugin", "list", "--json"),
        _MCP_GET,
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


def test_a_codex_that_cannot_find_the_teatree_mcp_server_is_not_ready(env: Path) -> None:
    repo = _with_codex_manifest(env / "teatree")

    reason, _calls = _install(
        CodexPluginRegistrar(repo),
        [*_fresh_install_replies(), _failed("Error: No MCP server named 'teatree' found.")],
    )

    assert reason is not None
    assert "confirm the teatree MCP server in Codex" in reason
    assert "exited 1" in reason
    assert "No MCP server named 'teatree' found." in reason


@pytest.mark.parametrize(
    ("resolved", "named"),
    [
        ({**_TEATREE_ENTRY, "enabled": False}, '"enabled": false'),
        (_resolved_with(args=["mcp", "serve", "--read-only"]), "--read-only"),
        (_resolved_with(command="/usr/local/bin/t3"), "/usr/local/bin/t3"),
        (_resolved_with(args=None), '"args": null'),
        (_resolved_with(args="mcp serve"), '"args": "mcp serve"'),
        ({"name": "teatree", "enabled": True}, '{"name": "teatree", "enabled": true}'),
    ],
    ids=["disabled", "read-only-args", "absolute-command", "null-args", "string-args", "no-transport"],
)
def test_a_teatree_mcp_server_codex_resolves_differently_is_not_ready(
    env: Path, resolved: dict[str, object], named: str
) -> None:
    repo = _with_codex_manifest(env / "teatree")

    reason, _calls = _install(CodexPluginRegistrar(repo), [*_fresh_install_replies(), _ok(resolved)])

    assert reason is not None
    assert "confirm the teatree MCP server in Codex" in reason
    assert named in reason


@pytest.mark.parametrize(
    ("listed", "warned"),
    [([], None), ([_TEATREE_ENTRY], "still loads teatree"), ({}, "printed no JSON array")],
    ids=["worker-lane-loads-nothing", "worker-lane-still-loads-teatree", "worker-lane-list-is-not-an-array"],
)
def test_inside_the_sandbox_the_plugin_off_mcp_list_gates_readiness(
    env: Path, listed: list[object] | dict[str, object], warned: str | None
) -> None:
    repo = _with_codex_manifest(env / "teatree")

    with patch("teatree.cli.setup.codex_plugin_registrar.container_is_the_sandbox", return_value=True):
        reason, calls = _install(CodexPluginRegistrar(repo), [*_fresh_install_replies(), _MCP_RESOLVED, _ok(listed)])

    assert calls == [*_fresh_install_calls(_slim_root(env)), _PLUGIN_OFF_MCP_LIST]
    if warned is None:
        assert reason is None
    else:
        assert reason is not None
        assert warned in reason


def test_the_ready_line_claims_only_what_codex_resolved(env: Path) -> None:
    repo = _with_codex_manifest(env / "teatree")

    installed, output, _calls = _run_install(CodexPluginRegistrar(repo), [*_fresh_install_replies(), _MCP_RESOLVED])

    assert installed is True
    assert output == (
        f"OK    TeaTree Codex plugin registered from {_slim_root(env)}; "
        "Codex resolves its `teatree` MCP server (t3 mcp serve --interactive).\n"
    )


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

    reason, calls = _install(
        CodexPluginRegistrar(repo), [noisy, _ok(), _ok({"installed": []}), _ok(), _post_add_plugins(), _MCP_RESOLVED]
    )

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
