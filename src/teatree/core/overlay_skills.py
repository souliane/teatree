"""Resolve an overlay's skills/tools root through the ``skill_root`` seam (#3355).

Three discovery sites once hard-coded ``<project>/skills``: the overlay-tool
registrar (:meth:`teatree.cli.overlay.OverlayAppBuilder._register_overlay_tools`),
the sub-agent skill-preamble builder
(:func:`teatree.cli.overlay._overlay_skills_dir`), and the doctor's skill-symlink
collector (:meth:`teatree.cli.doctor.service.DoctorService.collect_overlay_skills`).
An overlay whose skills live anywhere else matched nothing on all three — and the
tool registrar's failure was SILENT (the whole ``t3 <overlay> tool`` group never
registered, with no diagnostic).

This module is the single resolver those sites now share. An overlay declares its
skills root via ``SkillMetadata['skill_root']``
(:meth:`teatree.core.overlay_metadata.OverlayMetadata.get_skill_metadata`); the
resolver falls back to ``<project>/skills`` when it is unset, so every overlay
that works today keeps working unchanged.

``skill_root`` locates skills and nothing else — it is never a claim to expose
``t3 <overlay> tool`` commands. That claim is the separate, optional
``get_tool_commands()`` hook, which :func:`overlay_declares_tool_commands`
reads so the registrar's missing-manifest warning fires on a real mismatch
rather than on every invocation of every skill-shipping overlay (#3904, #3915).
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from django.core.exceptions import ImproperlyConfigured

if TYPE_CHECKING:
    from teatree.types import SkillMetadata

logger = logging.getLogger(__name__)


def overlay_skills_root(skill_metadata: "SkillMetadata", project_path: Path | None) -> Path | None:
    """The directory an overlay's skills / ``tool-commands.json`` are discovered under.

    Prefers the overlay-declared ``skill_root``; otherwise ``<project>/skills``
    (the layout ``overlay_init.generator`` scaffolds). Returns ``None`` only when
    neither is available. Existence is the caller's concern — the resolver names
    the intended root even when it is empty so the caller can warn on it.
    """
    root = str(skill_metadata.get("skill_root", "")).strip()
    if root:
        return Path(root).expanduser()
    if project_path is not None:
        return project_path / "skills"
    return None


def overlay_declares_tool_commands(overlay_name: str) -> bool:
    """Whether *overlay_name* claims a ``t3 <overlay> tool`` surface.

    ``skill_root`` says where an overlay's SKILLS live; exposing tool commands is
    the separate, optional ``get_tool_commands()`` extension point. The tool
    registrar keys its missing-manifest warning on THIS, so an overlay that ships
    skills and no tools — the common, correct case — stays silent (#3904, #3915).

    Guarded exactly like :func:`overlay_skill_metadata`: at CLI-BUILD time the
    overlay is unavailable, and a declaration that cannot be read is not a
    misconfiguration worth warning about on every invocation.
    """
    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415 — deferred: keeps CLI/build startup light

    try:
        return bool(get_overlay(overlay_name or None).metadata.get_tool_commands())
    except ImproperlyConfigured:
        logger.debug("tool-command declaration unavailable for overlay %r; assuming none", overlay_name)
        return False


def overlay_skill_metadata(overlay_name: str) -> "SkillMetadata":
    """Best-effort :class:`SkillMetadata` for *overlay_name*; ``{}`` when unavailable.

    Guarded because the overlay-tool registrar runs at CLI-BUILD time — before
    Django is configured — where :func:`teatree.core.overlay_loader.get_overlay`
    raises :class:`~django.core.exceptions.ImproperlyConfigured`. The resolver
    then falls back to ``<project>/skills`` exactly as before, so a build-time
    caller never regresses; a caller that runs with Django up (doctor,
    skill-preamble) gets the overlay's declared root.
    """
    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415 — deferred: keeps CLI/build startup light

    try:
        return get_overlay(overlay_name or None).metadata.get_skill_metadata()
    except ImproperlyConfigured:
        logger.debug("skill metadata unavailable for overlay %r; using the default root", overlay_name)
        return {}


__all__ = ["overlay_declares_tool_commands", "overlay_skill_metadata", "overlay_skills_root"]
