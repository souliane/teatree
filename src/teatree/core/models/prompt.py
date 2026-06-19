"""First-class reusable prompt with templated params + version history (#2513).

A :class:`Prompt` row is a named, reusable instruction — the durable home for
prose that used to live in skill markdown. A prompt is triggerable on its own
(the ``/prompts`` skill), and a :class:`teatree.core.models.loop.Loop` may point
at one via a nullable FK: a loop runs EITHER an on-disk ``script`` OR a
``Prompt`` (the loop XOR). ``name`` is the durable, unique identity used by the
seed and the ``/prompts`` trigger; ``body`` is the instruction text; ``overlay``
names the backend the prompt runs against generically (mirrors ``Loop.overlay``,
empty = overlay-agnostic).

**Templated params (D2).** ``params`` declares the named arguments the ``body``
templates over (``{who}`` placeholders). :meth:`Prompt.render` substitutes the
declared params and ONLY those — an undeclared ``{...}`` in the body is left
literal, so a prompt can carry JSON/braces without them being mistaken for a
format field. A declared param missing at render time, or an undeclared kwarg
passed in, is a loud error rather than a silent wrong-render.

**Version history (D2).** Every content change is recorded: :meth:`Prompt.revise`
snapshots the SUPERSEDED body+params as a :class:`PromptVersion` row (keyed on
``(prompt, version)``) before writing the new content, so the full edit history
is durable and auditable. An identical revise is a no-op — no version churn.
"""

from typing import ClassVar

from django.db import models, transaction


class MissingPromptParamError(KeyError):
    """A declared param was not supplied at :meth:`Prompt.render` time."""


class UnknownPromptParamError(KeyError):
    """A kwarg passed to :meth:`Prompt.render` is not a declared param."""


class PromptManager(models.Manager["Prompt"]):
    """Read surface for prompts — the trigger and seed query through here."""

    def by_name(self, name: str) -> "Prompt | None":
        """The prompt named *name*, or ``None`` — the ``/prompts`` trigger lookup."""
        return self.filter(name=name).first()


class Prompt(models.Model):
    """One row per named, reusable, triggerable prompt."""

    name = models.CharField(max_length=64, unique=True)
    body = models.TextField()
    params = models.JSONField(default=list, blank=True)
    description = models.TextField(blank=True, default="")
    overlay = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects: ClassVar[PromptManager] = PromptManager()

    class Meta:
        db_table = "teatree_prompt"
        ordering: ClassVar = ["name"]

    def __str__(self) -> str:
        return f"prompt<{self.name}>"

    @property
    def declared_params(self) -> frozenset[str]:
        """The declared param names as a set (``params`` stores them as a list)."""
        return frozenset(self.params or [])

    @property
    def current_version(self) -> int:
        """How many revisions this prompt has — the highest snapshotted version (0 = none)."""
        latest = self.versions.aggregate(models.Max("version"))["version__max"]  # ty: ignore[unresolved-attribute]
        return latest or 0

    def render(self, **args: str) -> str:
        """Substitute the declared params into ``body``; other braces stay literal.

        Raises :class:`MissingPromptParamError` when a declared param is absent
        and :class:`UnknownPromptParamError` when a kwarg is not declared — a
        silent wrong-render is worse than a loud one.
        """
        declared = self.declared_params
        supplied = frozenset(args)
        missing = declared - supplied
        if missing:
            msg = f"prompt {self.name!r} missing params: {sorted(missing)}"
            raise MissingPromptParamError(msg)
        unknown = supplied - declared
        if unknown:
            msg = f"prompt {self.name!r} given undeclared params: {sorted(unknown)}"
            raise UnknownPromptParamError(msg)
        # Substitute ONLY the declared ``{name}`` tokens; every other ``{...}``
        # (a JSON snippet, an example) is left literal — a body is safe to carry
        # braces without them being mistaken for a format field.
        rendered = self.body
        for name in declared:
            rendered = rendered.replace("{" + name + "}", str(args[name]))
        return rendered

    def revise(self, *, body: str, params: list[str] | None = None) -> "PromptVersion | None":
        """Update the content, snapshotting the superseded body+params as a version.

        An identical revise (same body AND same params) is a no-op — it neither
        snapshots nor bumps ``updated_at``. Otherwise the OLD content is captured
        as the next :class:`PromptVersion` row, THEN the live row is rewritten —
        all in one transaction so a snapshot can never be orphaned from its edit.
        """
        new_params = list(params if params is not None else self.params or [])
        if body == self.body and new_params == list(self.params or []):
            return None
        with transaction.atomic():
            version = self.versions.create(  # ty: ignore[unresolved-attribute]
                version=self.current_version + 1,
                body=self.body,
                params=list(self.params or []),
            )
            self.body = body
            self.params = new_params
            self.save(update_fields=["body", "params", "updated_at"])
        return version


class PromptVersionManager(models.Manager["PromptVersion"]):
    """Read surface for prompt version history."""


class PromptVersion(models.Model):
    """A superseded snapshot of one :class:`Prompt`'s body+params (#2513, D2)."""

    prompt = models.ForeignKey(Prompt, on_delete=models.CASCADE, related_name="versions")
    version = models.PositiveIntegerField()
    body = models.TextField()
    params = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects: ClassVar[PromptVersionManager] = PromptVersionManager()

    class Meta:
        db_table = "teatree_prompt_version"
        ordering: ClassVar = ["prompt", "version"]
        constraints: ClassVar = [
            models.UniqueConstraint(fields=["prompt", "version"], name="prompt_version_unique"),
        ]

    def __str__(self) -> str:
        return f"prompt-version<{self.prompt.name} v{self.version}>"
