"""Notion object IDs shared by the backend and cold hook paths."""

import re
from urllib.parse import parse_qs

_HEX32 = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])")
_DASHED_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def canonical_notion_id(reference: str) -> str | None:
    candidate = reference.strip().rstrip("/")
    path = candidate.partition("?")[0].partition("#")[0]
    query = candidate.partition("?")[2].partition("#")[0]
    for source in (*parse_qs(query).get("p", ()), path, candidate):
        if dashed := _DASHED_UUID.search(source):
            return dashed.group(0).lower()
        if matches := _HEX32.findall(source):
            raw = matches[-1].lower()
            return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"
    return None


def is_notion_object_id(reference: str) -> bool:
    candidate = reference.strip()
    return bool(_DASHED_UUID.fullmatch(candidate) or _HEX32.fullmatch(candidate))
