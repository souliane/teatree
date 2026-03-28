import json
import subprocess  # noqa: S404
import time
from pathlib import Path

_CACHE_FILE = Path.home() / ".local" / "share" / "teatree" / "update-check.json"
_CHECK_INTERVAL = 86400  # 24 hours


def check_for_updates(repo_dir: str = "") -> str | None:
    cached = _read_cache()
    if cached is not None:
        return cached

    try:
        if not repo_dir:
            repo_dir = str(Path(__file__).resolve().parents[3])

        current = subprocess.run(  # noqa: S603, S607
            ["git", "-C", repo_dir, "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True, timeout=5,
        ).stdout.strip()

        latest_tag = subprocess.run(  # noqa: S603, S607
            ["git", "-C", repo_dir, "describe", "--tags", "--abbrev=0"],
            capture_output=True, text=True, check=False, timeout=5,
        ).stdout.strip()

        if not latest_tag:
            _write_cache("")
            return None

        tag_sha = subprocess.run(  # noqa: S603, S607
            ["git", "-C", repo_dir, "rev-parse", latest_tag],
            capture_output=True, text=True, check=True, timeout=5,
        ).stdout.strip()

        if current != tag_sha:
            msg = f"Update available: {latest_tag} (you are on {current[:8]})"
            _write_cache(msg)
            return msg

        _write_cache("")
        return None

    except (subprocess.SubprocessError, OSError):
        return None


def _read_cache() -> str | None:
    try:
        if _CACHE_FILE.is_file():
            data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
            if time.time() - data.get("ts", 0) < _CHECK_INTERVAL:
                return data.get("msg") or None
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _write_cache(msg: str) -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(
            json.dumps({"ts": time.time(), "msg": msg}), encoding="utf-8"
        )
    except OSError:
        pass
