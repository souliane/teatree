"""A Codex-run ``t3 push`` authenticates with the token its dispatch routed, and with nothing else."""

import os
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner, Result

from teatree import forge_credentials
from teatree.agents._runner_env import with_routed_github_token
from teatree.agents.codex_app_server_env import codex_process_env
from teatree.cli.push import push
from teatree.config.credential_pass_key import PassKeyResolution, PassKeySource
from teatree.core.forge_push_verdict import PUSH_EXIT_CODES, PushFailure
from teatree.core.overlays.forge_credential_provider import build_and_register
from teatree.forge_credentials import ROUTED_GH_KEY_ENV, register_forge_credential_provider
from teatree.utils.git_run import run_with_status
from teatree.utils.secrets import read_pass
from tests._git_repo import make_git_repo, run_git

_TOKEN = "gh" + "p_" + "r" * 36
_GITHUB_KEY = "github/acme/pat"
_GITLAB_KEY = "gitlab/acme/pat"
_REFUSED = PUSH_EXIT_CODES[PushFailure.CREDENTIAL]

_app = typer.Typer()
_app.command()(push)


@dataclass(frozen=True)
class _Routes:
    keys: dict[str, str]

    def resolve_pass_key(self, credential: str) -> PassKeyResolution:
        return PassKeyResolution(f"{credential}_pass_key", self.keys.get(credential, ""), PassKeySource.OVERLAY_DB)


@dataclass(frozen=True)
class _Overlay:
    config: _Routes


@dataclass
class _ForgeNetwork:
    tip: str
    pushes: list[dict[str, str]] = field(default_factory=list)

    def push(
        self, cmd: list[str], *, env: dict[str, str] | None = None, **_: object
    ) -> subprocess.CompletedProcess[str]:
        self.pushes.append(dict(env or {}))
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    def read(self, *, repo: str, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[0] == "ls-remote":
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=f"{self.tip}\t{args[2]}\n", stderr="")
        return run_with_status(repo=repo, args=args, **kwargs)


@dataclass(frozen=True)
class _CodexPush:
    result: Result
    network: _ForgeNetwork
    store_read: str


class TestCodexRunPush:
    @pytest.fixture(autouse=True)
    def _acme_owns_both_forges(self) -> Iterator[None]:
        previous = forge_credentials._provider
        overlay = _Overlay(_Routes({"github_token": _GITHUB_KEY, "gitlab_token": _GITLAB_KEY}))
        build_and_register(
            get_overlay=lambda _name=None: overlay,
            get_overlay_for_repo=lambda _repo: overlay,
            infer_overlay_for_url=lambda url: "acme" if "/acme/" in url else None,
            all_overlay_names=lambda: ["acme"],
            owned_repos=lambda _name: {},
        )
        yield
        register_forge_credential_provider(previous)

    @pytest.fixture(autouse=True)
    def _dirs(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.codex_home = tmp_path / "codex-home"
        self.worker_home = tmp_path / "worker-home"

    def _clone(self, origin: str) -> Path:
        clone = make_git_repo(self.tmp_path / "clone")
        run_git(clone, "remote", "add", "origin", origin)
        run_git(clone, "checkout", "-q", "-b", "feature")
        return clone

    @staticmethod
    def _routed_dispatch_env() -> dict[str, str]:
        with patch("teatree.core.overlays.forge_credential_provider.read_pass", return_value=_TOKEN):
            env = with_routed_github_token(None, overlay="acme")
        assert env is not None
        return env

    def _child_env(self, dispatch_env: dict[str, str], **ambient: str) -> dict[str, str]:
        worker_ambient = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.worker_home),
            "PASSWORD_STORE_DIR": str(self.worker_home / ".password-store"),
            "GNUPGHOME": str(self.worker_home / ".gnupg"),
            **ambient,
        }
        return codex_process_env(self.codex_home, dispatch_env, ambient=worker_ambient)

    @staticmethod
    def _push_inside(child: dict[str, str], clone: Path) -> _CodexPush:
        network = _ForgeNetwork(tip=run_git(clone, "rev-parse", "HEAD"))
        with (
            patch.dict(os.environ, child, clear=True),
            patch("teatree.core.forge_push.run_bounded_group", network.push),
            patch("teatree.core.forge_push.run_with_status", side_effect=network.read),
        ):
            store_read = read_pass(_GITHUB_KEY)
            result = CliRunner().invoke(_app, ["--repo", str(clone)])
        return _CodexPush(result, network, store_read)

    def test_the_dispatch_names_the_pass_key_its_token_came_from(self) -> None:
        assert self._routed_dispatch_env() == {"GH_TOKEN": _TOKEN, ROUTED_GH_KEY_ENV: _GITHUB_KEY}

    def test_the_routed_token_pushes_while_the_store_stays_out_of_reach(self) -> None:
        child = self._child_env(self._routed_dispatch_env())

        pushed = self._push_inside(child, self._clone("https://github.com/acme/widget.git"))

        assert not {"PASSWORD_STORE_DIR", "GNUPGHOME"} & child.keys()
        assert child["HOME"] == str(self.codex_home)
        assert pushed.store_read == ""
        assert pushed.result.exit_code == 0, pushed.result.output
        assert [env["GH_TOKEN"] for env in pushed.network.pushes] == [_TOKEN]

    def test_a_token_without_the_marker_is_refused(self) -> None:
        child = self._child_env({"GH_TOKEN": _TOKEN})

        pushed = self._push_inside(child, self._clone("https://github.com/acme/widget.git"))

        assert pushed.result.exit_code == _REFUSED, pushed.result.output
        assert pushed.network.pushes == []

    def test_a_marker_naming_another_pass_key_is_refused(self) -> None:
        child = self._child_env({"GH_TOKEN": _TOKEN, ROUTED_GH_KEY_ENV: "github/other/pat"})

        pushed = self._push_inside(child, self._clone("https://github.com/acme/widget.git"))

        assert pushed.result.exit_code == _REFUSED, pushed.result.output
        assert pushed.network.pushes == []

    def test_a_gitlab_route_never_borrows_the_github_token(self) -> None:
        child = self._child_env({"GH_TOKEN": _TOKEN, ROUTED_GH_KEY_ENV: _GITLAB_KEY})

        pushed = self._push_inside(child, self._clone("https://gitlab.com/acme/widget.git"))

        assert pushed.result.exit_code == _REFUSED, pushed.result.output
        assert pushed.network.pushes == []

    def test_a_marker_only_in_the_workers_own_environment_never_reaches_codex(self) -> None:
        child = self._child_env({"GH_TOKEN": _TOKEN}, **{ROUTED_GH_KEY_ENV: _GITHUB_KEY})

        pushed = self._push_inside(child, self._clone("https://github.com/acme/widget.git"))

        assert ROUTED_GH_KEY_ENV not in child
        assert pushed.result.exit_code == _REFUSED, pushed.result.output
