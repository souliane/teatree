import hashlib
import json
from dataclasses import dataclass, fields


@dataclass(frozen=True, slots=True)
class EscapeScope:
    gate: str
    reason: str
    actor: str
    subject: str
    head: str
    action_digest: str
    session_id: str
    call_id: str
    excerpt: str = ""

    def binding(self) -> str:
        parts = (self.gate, self.actor, self.subject, self.head, self.action_digest, self.session_id)
        return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


ESCAPE_SCOPE_FIELDS = frozenset(field.name for field in fields(EscapeScope))
