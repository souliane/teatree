"""The brief a fix dispatch carries: anchored to the heads it was written against, and overrulable by the code."""

from dataclasses import dataclass

from teatree.hooks.brief_anchor_scanner import TRUST_THE_CODE_CLAUSE


@dataclass(frozen=True, slots=True)
class FixerBrief:
    pr_url: str
    head_sha: str
    base_sha: str
    agent_type: str
    escape: str
    checkout_kind: str
    findings: tuple[str, ...] = ()

    def render(self) -> str:
        lines = [
            f"Fix {self.pr_url} as the {self.agent_type} agent, in the {self.checkout_kind}.",
            *self._anchors(),
            f"Recorded escape: {self.escape or 'none'}.",
            *(f"- {finding}" for finding in self.findings),
            TRUST_THE_CODE_CLAUSE,
        ]
        return "\n".join(lines)

    def _anchors(self) -> list[str]:
        return [_anchor("PR head", self.head_sha), _anchor("PR base", self.base_sha)]


def _anchor(label: str, sha: str) -> str:
    return f"{label}: verified at {sha}." if sha else f"{label}: not read; read it first."
