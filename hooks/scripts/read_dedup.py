"""PostToolUse: warn when a file is re-read without having changed since the last read.

Extracted whole from ``hook_router`` so the shrink-only dispatcher nets smaller; the router re-exports
:func:`handle_read_dedup` into its ``PostToolUse`` chain unchanged. The per-session state helpers stay in
the router and are back-imported at call time, so a test patching ``router.STATE_DIR`` still steers this
handler.
"""

import sys
from pathlib import Path

from hooks.scripts.state_files import read_lines

sys.modules.setdefault("read_dedup", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.read_dedup", sys.modules[__name__])


def handle_read_dedup(data: dict) -> None:
    """Warn when a file is re-read without having changed since last read."""
    from hooks.scripts.hook_router import _ensure_state_dir, _state_file  # noqa: PLC0415 deferred back-import

    if data.get("tool_name") != "Read":
        return

    session_id = data.get("session_id", "")
    file_path = data.get("tool_input", {}).get("file_path", "")
    if not session_id or not file_path:
        return

    _ensure_state_dir()
    reads_file = _state_file(session_id, "reads")

    # Load existing reads: each line is "mtime\tpath"
    reads: dict[str, str] = {}
    for line in read_lines(reads_file):
        parts = line.split("\t", 1)
        if len(parts) == 2:  # noqa: PLR2004 — self-documenting literal in this context
            reads[parts[1]] = parts[0]

    # Get current mtime
    try:
        current_mtime = str(Path(file_path).stat().st_mtime)
    except OSError:
        return

    prev_mtime = reads.get(file_path)
    if prev_mtime == current_mtime:
        print(  # noqa: T201 — hook writes its protocol output to stdout
            f"TOKEN SAVINGS HINT: {file_path} was already read this session "
            "and hasn't changed. Use your cached knowledge of its contents "
            "instead of re-reading."
        )

    # Update the reads file (overwrite to keep latest mtime per path)
    reads[file_path] = current_mtime
    reads_file.write_text(
        "\n".join(f"{mtime}\t{path}" for path, mtime in reads.items()) + "\n",
        encoding="utf-8",
    )
