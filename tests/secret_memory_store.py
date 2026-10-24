class MemoryStore:
    def __init__(self) -> None:
        self.entries: dict[str, str] = {}

    def read(self, key: str) -> str | None:
        return self.entries.get(key)

    def write(self, key: str, value: str) -> bool:
        self.entries[key] = value
        return True

    def remove(self, key: str) -> bool:
        return self.entries.pop(key, None) is not None

    def names(self, prefix: str) -> frozenset[str]:
        root = prefix.rstrip("/") + "/"
        return frozenset(
            name[len(root) :] for name in self.entries if name.startswith(root) and "/" not in name[len(root) :]
        )
