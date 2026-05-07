import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class StatuslineZones:
    anchors: list[str] = field(default_factory=list)
    action_needed: list[str] = field(default_factory=list)
    in_flight: list[str] = field(default_factory=list)


_ZONE_HEADERS: dict[str, str] = {
    "anchors": "",
    "action_needed": "Action needed:",
    "in_flight": "In flight:",
}


def default_path() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "teatree" / "statusline.txt"


def render(zones: StatuslineZones, *, target: Path | None = None) -> Path:
    target = target or default_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    sections: list[str] = []
    for name in ("anchors", "action_needed", "in_flight"):
        items = getattr(zones, name)
        if not items:
            continue
        header = _ZONE_HEADERS[name]
        block = "\n".join(items)
        sections.append(f"{header}\n{block}".lstrip("\n") if header else block)

    body = "\n\n".join(sections) + "\n"

    fd, tmp_str = tempfile.mkstemp(prefix=".statusline-", suffix=".tmp", dir=str(target.parent))
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        Path(tmp_path).replace(target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    return target


__all__ = ["StatuslineZones", "default_path", "render"]
