"""The slim Codex marketplace tree ``t3 setup`` registers instead of a checkout.

``codex plugin add`` copies the plugin source it is pointed at. Pointed at a
checkout it copied ``.git``, every virtualenv and the ``plugins/t3 -> ..`` loop,
so the tree handed to Codex holds only what the plugin manifest declares.
"""

import json
import os
import shutil
from pathlib import Path

from teatree.cli.setup.plugin_registrar import PLUGIN_NAME

PAYLOAD_MAX_BYTES = 50 * 1024 * 1024
_MARKETPLACE_MANIFEST = Path(".agents") / "plugins" / "marketplace.json"
_PLUGIN_MANIFEST_DIR = Path(".codex-plugin")


class CodexPayloadError(ValueError):
    pass


class CodexPluginPayload:
    def __init__(self, manifest_root: Path, target: Path) -> None:
        self.manifest_root = manifest_root.resolve()
        self.target = target

    def materialize(self) -> Path:
        sources = list(dict.fromkeys([_PLUGIN_MANIFEST_DIR, *self._declared_paths()]))
        total = sum(self._payload_bytes(self.manifest_root / source) for source in sources)
        if total > PAYLOAD_MAX_BYTES:
            msg = f"Codex plugin payload is {total} bytes, over the {PAYLOAD_MAX_BYTES}-byte cap"
            raise CodexPayloadError(msg)
        building = self.target.with_name(f".{self.target.name}.building-{os.getpid()}")
        shutil.rmtree(building, ignore_errors=True)
        try:
            self._copy(_MARKETPLACE_MANIFEST, building / _MARKETPLACE_MANIFEST)
            plugin_dir = building / "plugins" / PLUGIN_NAME
            for source in sources:
                self._copy(source, plugin_dir / source)
        except (OSError, shutil.Error) as exc:
            shutil.rmtree(building, ignore_errors=True)
            msg = f"Codex plugin payload could not be built: {exc}"
            raise CodexPayloadError(msg) from exc
        self._swap_in(building)
        return self.target

    def _declared_paths(self) -> list[Path]:
        manifest = self.manifest_root / _PLUGIN_MANIFEST_DIR / "plugin.json"
        try:
            fields = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError) as exc:
            msg = f"Codex plugin payload refused: {manifest} is unreadable ({exc})"
            raise CodexPayloadError(msg) from exc
        if not isinstance(fields, dict):
            msg = f"Codex plugin payload refused: {manifest} is not a JSON object"
            raise CodexPayloadError(msg)
        declared = [value for value in fields.values() if isinstance(value, str) and _is_path(value)]
        return [self._inside_root(value) for value in declared]

    def _inside_root(self, declared: str) -> Path:
        resolved = Path(os.path.normpath(self.manifest_root / declared))
        if resolved == self.manifest_root or not resolved.is_relative_to(self.manifest_root):
            msg = f"Codex plugin payload refused: declared path {declared!r} is not a path inside {self.manifest_root}"
            raise CodexPayloadError(msg)
        return resolved.relative_to(self.manifest_root)

    @staticmethod
    def _payload_bytes(path: Path) -> int:
        if path.is_symlink() and path.is_dir():
            msg = f"Codex plugin payload refused: {path} is a symlinked directory"
            raise CodexPayloadError(msg)
        try:
            if not path.is_dir():
                return path.stat().st_size
            total = 0
            for directory, subdirs, files in os.walk(path):
                for name in subdirs:
                    if (Path(directory) / name).is_symlink():
                        msg = f"Codex plugin payload refused: {Path(directory) / name} is a symlinked directory"
                        raise CodexPayloadError(msg)
                total += sum((Path(directory) / name).stat().st_size for name in files)
        except OSError as exc:
            msg = f"Codex plugin payload refused: {path} is unreadable ({exc})"
            raise CodexPayloadError(msg) from exc
        return total

    def _copy(self, source: Path, destination: Path) -> None:
        origin = self.manifest_root / source
        destination.parent.mkdir(parents=True, exist_ok=True)
        if origin.is_dir():
            shutil.copytree(origin, destination, symlinks=False)
        else:
            shutil.copy2(origin, destination)

    def _swap_in(self, building: Path) -> None:
        retired = self.target.with_name(f".{self.target.name}.retired-{os.getpid()}")
        shutil.rmtree(retired, ignore_errors=True)
        if self.target.exists():
            self.target.rename(retired)
        building.rename(self.target)
        shutil.rmtree(retired, ignore_errors=True)


def _is_path(value: str) -> bool:
    return value == "." or value.startswith(("./", "../", "/"))
