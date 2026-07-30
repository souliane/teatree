"""Declared-dependency provisioning: enumerate the mandate, probe it, install it.

The three halves of epic #3445's acceptance principle — a dependency the
configuration declares REQUIRED but nothing provisioned is a loud FAIL, and the
enumeration comes from the declaration surfaces so a new mandate is covered with
no code change.
"""

from teatree.provisioning.declared import (
    DeclarationUnreadableError,
    DeclaredDependency,
    DependencyKind,
    declared_dependencies,
)
from teatree.provisioning.probes import unprovisioned
from teatree.provisioning.skill_source import InstallOutcome, MandatedSkillInstaller

__all__ = [
    "DeclarationUnreadableError",
    "DeclaredDependency",
    "DependencyKind",
    "InstallOutcome",
    "MandatedSkillInstaller",
    "declared_dependencies",
    "unprovisioned",
]
