"""The ``ComposeEngine`` a roll drives, over the docker CLI.

Each generation is brought up with its OWN compose topology — an image generation's is
docker-compose.yml plus the image-only generation override, both read out of that image's
baked tree; the legacy stack's is the deploy checkout's source-mounted docker-compose.yml —
so rolling back restores exactly what was serving. Stop, inspect and the admin probe
address containers by their compose labels and need no topology.
"""

import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from teatree.deploy.roll import ContainerState, RollError
from teatree.generation import generation_image
from teatree.utils.run import CompletedProcess, run_allowed_to_fail

REVISION_LABEL = "org.opencontainers.image.revision"
DEFAULT_PROJECT = "teatree"
DEFAULT_PROMOTED_TAG = "teatree-headless:latest"
DEFAULT_ADMIN_PORT = 8000
DEFAULT_LEGACY_CLONE_DIR = "/home/teatree/teatree"
_BASE_FILE = "docker-compose.yml"
_GENERATION_FILE = "docker-compose.generation.yml"
_HOST_IDENTITY_FILE = "docker-compose.host-identity.yml"
_OUTPUT_TAIL_LINES = 40


@dataclass(frozen=True, slots=True)
class DockerComposeEngine:
    deploy_dir: Path
    env: Mapping[str, str]
    project: str = DEFAULT_PROJECT
    promoted_tag: str = DEFAULT_PROMOTED_TAG
    admin_port: int = DEFAULT_ADMIN_PORT
    legacy_clone_dir: str = DEFAULT_LEGACY_CLONE_DIR

    @classmethod
    def from_environment(cls) -> "DockerComposeEngine":
        env = dict(os.environ)
        checkout = env.get("TEATREE_DEPLOY_CHECKOUT", "").strip()
        if not checkout:
            msg = "TEATREE_DEPLOY_CHECKOUT is unset — run the roller through deploy/roll.sh"
            raise RollError(msg)
        port = env.get("TEATREE_ADMIN_PORT", "").strip() or str(DEFAULT_ADMIN_PORT)
        if not port.isdigit():
            msg = f"TEATREE_ADMIN_PORT must be a port number, got {port!r}"
            raise RollError(msg)
        return cls(
            deploy_dir=Path(checkout) / "deploy",
            env=env,
            project=env.get("TEATREE_COMPOSE_PROJECT", "").strip() or DEFAULT_PROJECT,
            promoted_tag=env.get("TEATREE_PROMOTED_TAG", "").strip() or DEFAULT_PROMOTED_TAG,
            admin_port=int(port),
            legacy_clone_dir=env.get("TEATREE_LEGACY_CLONE_DIR", "").strip() or DEFAULT_LEGACY_CLONE_DIR,
        )

    @property
    def admin_probe_url(self) -> str:
        return f"http://127.0.0.1:{self.admin_port}/admin/login/"

    def image_revision(self, image: str) -> str:
        result = self._docker("image", "inspect", "--format", _label_template(), image)
        revision = result.stdout.strip() if result.returncode == 0 else ""
        return "" if revision == "<no value>" else revision

    def run_init(self, generation: str) -> None:
        self._compose(
            generation,
            "up", "--no-deps", "--force-recreate", "--exit-code-from", "teatree-init", "teatree-init",
            action="teatree-init",
        )  # fmt: skip

    def up(self, generation: str, services: Sequence[str]) -> None:
        self._compose(generation, "up", "-d", "--no-deps", *services, action=f"up {' '.join(services)}")

    def stop(self, services: Sequence[str]) -> None:
        ids = [container for service in services for container in self._containers(service, running_only=True)]
        if ids:
            self._checked(self._docker("stop", *ids), action=f"stop {' '.join(services)}")

    def inspect(self, services: Sequence[str]) -> dict[str, ContainerState]:
        return {service: self._state_of(service) for service in services}

    def admin_answers(self) -> bool:
        admins = self._containers("teatree-admin", running_only=True)
        probe = ("curl", "-fsS", "-o", "/dev/null", "--max-time", "5", self.admin_probe_url)
        return bool(admins) and self._docker("exec", admins[0], *probe).returncode == 0

    def promote(self, generation: str) -> None:
        image = generation_image(generation)
        self._checked(self._docker("tag", image, self.promoted_tag), action=f"tag {image} as {self.promoted_tag}")

    def _state_of(self, service: str) -> ContainerState:
        containers = self._containers(service, running_only=False)
        if not containers:
            return ContainerState(running=False, revision="", present=False)
        template = "{{.State.Running}} {{.RestartCount}} {{.State.StartedAt}} " + _label_template()
        result = self._docker("inspect", "--format", template, containers[0])
        running, restarts, started_at, revision = [*result.stdout.strip().split(" ", 3), "", "", ""][:4]
        return ContainerState(
            running=running == "true",
            revision="" if revision == "<no value>" else revision,
            restarts=int(restarts) if restarts.isdigit() else 0,
            started_at=started_at,
        )

    def _containers(self, service: str, *, running_only: bool) -> list[str]:
        filters = (
            f"label=com.docker.compose.project={self.project}",
            f"label=com.docker.compose.service={service}",
            "label=com.docker.compose.oneoff=False",
        )
        args = ["ps", "-q", *(arg for label in filters for arg in ("--filter", label))]
        if not running_only:
            args.insert(1, "-a")
        return self._docker(*args).stdout.split()

    def _compose(self, generation: str, *args: str, action: str) -> None:
        with tempfile.TemporaryDirectory(prefix="teatree-roll-") as scratch:
            files = self._topology(generation, Path(scratch))
            argv = ["docker", "compose", "-p", self.project, "--project-directory", str(self.deploy_dir)]
            argv += [flag for path in files for flag in ("-f", str(path))]
            self._checked(
                run_allowed_to_fail([*argv, *args], expected_codes=None, env=self._compose_env(generation)),
                action=action,
            )

    def _compose_env(self, generation: str) -> dict[str, str]:
        if generation:
            return {**self.env, "TEATREE_IMAGE": generation_image(generation)}
        return {**self.env, "TEATREE_IMAGE": self.promoted_tag, "TEATREE_CLONE_DIR": self.legacy_clone_dir}

    def _topology(self, generation: str, scratch: Path) -> list[Path]:
        if not generation:
            names = [_BASE_FILE, *self._host_identity()]
            return [self.deploy_dir / name for name in names]
        # The generation override replaces whole volume lists, so host identity must merge on top of it.
        names = [_BASE_FILE, _GENERATION_FILE, *self._host_identity()]
        image = generation_image(generation)
        files = []
        for name in names:
            read = f'cat "$TEATREE_CLONE_DIR/deploy/{name}"'
            baked = self._docker("run", "--rm", "--pull", "never", "--entrypoint", "sh", image, "-c", read)
            self._checked(baked, action=f"read {name} from {image}")
            (scratch / name).write_text(baked.stdout, encoding="utf-8")
            files.append(scratch / name)
        return files

    def _host_identity(self) -> list[str]:
        host_home = self.env.get("TEATREE_HOST_HOME", "").strip()
        return [_HOST_IDENTITY_FILE] if host_home and host_home != str(Path.home()) else []

    def _docker(self, *args: str) -> CompletedProcess[str]:
        return run_allowed_to_fail(["docker", *args], expected_codes=None, env=dict(self.env))

    @staticmethod
    def _checked(result: CompletedProcess[str], *, action: str) -> None:
        if result.returncode == 0:
            return
        tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-_OUTPUT_TAIL_LINES:])
        msg = f"{action} exited {result.returncode}: {tail}"
        raise RollError(msg)


def _label_template() -> str:
    return f'{{{{ index .Config.Labels "{REVISION_LABEL}" }}}}'
