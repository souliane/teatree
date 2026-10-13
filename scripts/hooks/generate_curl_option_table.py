"""Generate the curl option grammar from two pinned curl releases."""

import hashlib
import json
import re
import urllib.request
from pathlib import Path

SOURCES = {
    "curl-8_5_0": "2ecf41aa1b0b9f86ebd84ffd2623e7c6090d24290f50055a7117b132364a87d7",
    "curl-8_18_0": "dda1a00e15372da7acc86fbad6f856a891747cb21699604b88c656e0bf2dfd9f",
}
OUTPUT = Path(__file__).resolve().parents[2] / "src/teatree/hooks/curl_options.json"
OLD_ROW = re.compile(r'^\s*\{"([^\"]+)"\s*,\s*"([^\"]+)"\s*,\s*(ARG_[A-Z]+)', re.MULTILINE)
NEW_ROW = re.compile(r'^\s*\{"([^\"]+)"\s*,\s*(ARG_[A-Z]+)(?:\|[^,]+)?,\s*\'(.)\'', re.MULTILINE)
VALUE_KINDS = frozenset({"ARG_STRG", "ARG_STRING", "ARG_FILE", "ARG_FILENAME"})


def parse_aliases(source: str, tag: str) -> dict[str, tuple[str, str]]:
    if "aliases[]" in source:
        start = source.index("aliases[]")
        source = source[start : source.index("};", start)]
    table: dict[str, tuple[str, str]] = {}
    if tag == "curl-8_5_0":
        rows = ((name, kind, code if len(code) == 1 else "") for code, name, kind in OLD_ROW.findall(source))
    else:
        rows = ((name, kind, letter.strip()) for name, kind, letter in NEW_ROW.findall(source))
    for name, kind, letter in rows:
        option_kind = (
            "value"
            if kind in VALUE_KINDS
            else "boolean"
            if kind == "ARG_BOOL"
            else "switch"
            if kind == "ARG_NONE"
            else "unknown"
        )
        table[name] = (option_kind, letter)
    if not table:
        message = "unrecognised curl aliases table"
        raise ValueError(message)
    return table


def generate() -> str:
    sources: dict[str, str] = {}
    versions: list[dict[str, tuple[str, str]]] = []
    for tag, expected_sha in SOURCES.items():
        url = f"https://raw.githubusercontent.com/curl/curl/{tag}/src/tool_getparam.c"
        source = urllib.request.urlopen(url, timeout=30).read()
        source_sha = hashlib.sha256(source).hexdigest()
        if source_sha != expected_sha:
            message = f"curl source changed at {tag}"
            raise ValueError(message)
        sources[tag] = source_sha
        versions.append(parse_aliases(source.decode(), tag))
        if versions[-1].get("next") != ("switch", ":"):
            message = "curl next option missing"
            raise ValueError(message)
    names = sorted(set().union(*(version.keys() for version in versions)))
    options: list[tuple[str, str, str]] = []
    for name in names:
        definitions = [version[name] for version in versions if name in version]
        kinds = {kind for kind, _ in definitions}
        kind = kinds.pop() if len(kinds) == 1 else "unknown"
        letters = {letter for _, letter in definitions if letter}
        options.append((name, kind, letters.pop() if len(letters) == 1 else ""))
    lines = ["{", f'  "sources": {json.dumps(sources, sort_keys=True)},', '  "options": [']
    lines.extend(f"    {json.dumps(row)}{',' if i < len(options) - 1 else ''}" for i, row in enumerate(options))
    lines.extend(["  ]", "}", ""])
    return "\n".join(lines)


if __name__ == "__main__":
    OUTPUT.write_text(generate(), encoding="utf-8")
