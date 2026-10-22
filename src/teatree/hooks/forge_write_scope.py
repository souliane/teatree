"""Whether a Bash publish command writes to a forge, as opposed to Slack or another surface."""

from pathlib import Path

from teatree.hooks._forge_write_detection import command_has_forge_free_text_publish
from teatree.hooks._publish_detection import (
    command_has_interpreter_forge_transport,
    segment_is_api_write,
    segment_word_lists,
)
from teatree.hooks._python_rest_detection import command_has_python_rest_publish_surface
from teatree.hooks.publish_destination import resolve_publish_destination


def is_forge_write(command: str, cwd_repo: Path | None) -> bool:
    return (
        command_has_forge_free_text_publish(command)
        or command_has_python_rest_publish_surface(command)
        or command_has_interpreter_forge_transport(command)
        or any(segment_is_api_write(words) for words in segment_word_lists(command))
        or resolve_publish_destination(command, cwd_repo) is not None
    )
