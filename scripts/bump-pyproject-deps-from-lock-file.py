#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "typer>=0.17.4",
#   "tomlkit>=0.13.3",
# ]
# ///

# Bump all dependencies in pyproject.toml to >= latest versions from uv.lock,
# preserving formatting, comments, and upper/lower version constraints.
import re
import tomllib
from pathlib import Path

import tomlkit
import typer

app = typer.Typer()


class BumpPyprojectDepsFromLockFile:
    PYPROJECT_PATH = Path("pyproject.toml")
    UV_LOCK_PATH = Path("uv.lock")
    DEP_REGEX = re.compile(r"^(?P<name>[\w\-]+)(?P<constraints>(?:[><=!~]=?.*)?)$")

    def __init__(self) -> None:
        with self.PYPROJECT_PATH.open() as fp:
            self.pyproject = tomlkit.parse(fp.read())

        with self.UV_LOCK_PATH.open("rb") as fp:
            self.uv_lock = tomllib.load(fp)

        self.locked_versions = {
            pkg["name"].lower().replace("_", "-"): pkg["version"] for pkg in self.uv_lock.get("package", [])
        }

    def update_deps(self, deps: list[str], group_name: str) -> None:
        typer.echo(f"[{group_name}]")

        for index, current_dep in enumerate(deps):
            match = self.DEP_REGEX.match(current_dep.strip())
            if not match:
                continue

            name = match.group("name").lower().replace("_", "-")
            constraints = match.group("constraints").strip()

            if name in self.locked_versions:
                latest_version = self.locked_versions[name]

                new_constraints_parts: list[str] = []
                for part in re.split(r",\s*", constraints):
                    if part.startswith((">=", "==")):
                        continue
                    if part:
                        new_constraints_parts.append(part)
                new_dep = f"{name}>={latest_version}"
                if new_constraints_parts:
                    new_dep += "," + ",".join(new_constraints_parts)

                if new_dep != current_dep:
                    deps[index] = new_dep
                    typer.echo(f"{current_dep} -> {new_dep}")

        typer.echo()

    def run(self) -> None:
        if project_table := self.pyproject.get("project"):
            deps_list = project_table.get("dependencies")
            if isinstance(deps_list, list):
                self.update_deps(deps_list, group_name="main")

        dep_groups = self.pyproject.get("dependency-groups")
        if hasattr(dep_groups, "items"):
            for group, group_deps in dep_groups.items():
                if isinstance(group_deps, list):
                    self.update_deps(group_deps, group_name=group)

        with self.PYPROJECT_PATH.open("w", encoding="utf-8") as f:
            f.write(tomlkit.dumps(self.pyproject))

        typer.echo("✅ pyproject.toml updated with >= latest versions (upper bounds preserved) from uv.lock")


@app.command()
def bump() -> None:
    BumpPyprojectDepsFromLockFile().run()
