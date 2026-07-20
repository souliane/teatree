"""Claude Code ``PermissionMode`` values teatree pins, and who pins which.

Three lanes read the operator's ``permissions.defaultMode`` from
``~/.claude/settings.json`` unless they pin a mode of their own, so the
constants live here rather than beside any one consumer — a lane that forgets
to pin silently inherits whatever the operator set, which is the drift these
names exist to prevent.

:data:`UNATTENDED` is pinned by the headless dispatch options and by the
``t3 loop start`` argv. Both run with no human able to answer, so a classifier
denial has nobody to override it.

:data:`READER_DEFAULT_DENY` is pinned only by the #116 quarantined reader:
``dontAsk`` denies whatever no allow rule permits, and the reader defines none,
so its effective tool set is empty by default rather than by enumeration.

``auto`` is deliberately absent. It is the posture ``t3 doctor check`` advises
for a session the operator is sitting in front of, and teatree never pins it —
that session is the one lane whose mode is the operator's to choose.
"""

UNATTENDED = "bypassPermissions"
READER_DEFAULT_DENY = "dontAsk"
