"""Every teatree model reaches the Django admin, list-only unless a reviewed declaration says otherwise.

``core/admin.py`` ends by registering every core model without a hand-written admin with
``ReadOnlyAdmin``. The live lane re-reads the registry (the outcome, not the intent) for every
model of every ``teatree.*`` app, and judges an admin by CALLING its permission methods, so a
read-only subclass that turns a write back on is refused. The synthetic lanes plant each failure
on a fresh ``AdminSite`` with probe models from a standalone ``Apps()`` registry.
"""

import re
from collections.abc import Callable, Iterable
from types import SimpleNamespace

from django.apps import apps
from django.apps.registry import Apps
from django.contrib import admin
from django.core.exceptions import FieldDoesNotExist
from django.db import models
from django.db.models.constants import LOOKUP_SEP
from django_fsm import FSMField, FSMFieldMixin

from teatree.core.admin import ReadOnlyAdmin, register_read_only_defaults

_OPERATOR_EDITED = "operator-authored configuration; the admin is its editor"

WRITABLE_ADMINS: dict[str, str] = {
    "core.ConfigSetting": _OPERATOR_EDITED,
    "core.Loop": _OPERATOR_EDITED,
    "core.LoopState": "the dash Loops band links here to break glass on a loop hold",
    "core.Mode": _OPERATOR_EDITED,
    "core.ModeOverride": _OPERATOR_EDITED,
    "core.ModeSchedule": _OPERATOR_EDITED,
    "core.ModeScheduleSlot": _OPERATOR_EDITED,
    "core.Prompt": _OPERATOR_EDITED,
}

#: Django refuses to register a composite-primary-key model; nothing else may be listed here.
UNREGISTRABLE: dict[str, str] = {}

#: Credential, command, env, captured-error and rationale columns. Text/JSON/Binary are hidden by type.
MUST_STAY_HIDDEN = frozenset(
    {
        ("core.AnthropicActivePick", "pass_path"),
        ("core.AnthropicTokenUsage", "pass_path"),
        ("core.AnthropicTokenUsage", "token_fingerprint"),
        ("core.CriticFinding", "adversarial_question"),
        ("core.EvalScenarioResult", "judge_rationale"),
        ("core.IntentClassification", "rationale"),
        ("core.PendingPullRequest", "reason"),
        ("core.PendingReinstall", "last_error"),
        ("core.PullMainCloneMarker", "last_reason"),
        ("core.ReproEvidence", "command"),
        ("core.ReproEvidence", "command_fingerprint"),
        ("core.ReviewBackendCooldown", "signature"),
        ("core.SelfUpdateMarker", "last_reason"),
        ("core.SessionAuditRecord", "judge_rationale"),
        ("core.StandingGoal", "check_command"),
        ("core.SweepSkipStreak", "reason"),
        ("core.TrustedIdentity", "note"),
        ("core.WorktreeEnvOverride", "value"),
    }
)

_HIDDEN_TYPES = (models.TextField, models.JSONField, models.BinaryField)
_NAIVE_PLURAL = re.compile(r"(ch|sh|ss|x|z|[^aeiou]y)s$")
_FSM_FIELD_FLOOR = 6
_SUPERUSER = SimpleNamespace(
    user=SimpleNamespace(is_active=True, is_staff=True, is_superuser=True, has_perm=lambda *_args, **_kwargs: True)
)

type ModelClass = type[models.Model]
type HiddenColumns = Callable[[ModelClass], set[str]]


def teatree_models() -> list[ModelClass]:
    return [
        model
        for config in apps.get_app_configs()
        if config.name.startswith("teatree.")
        for model in config.get_models()
    ]


def is_list_only(model_admin: admin.ModelAdmin) -> bool:
    row = model_admin.model()
    return not any(
        (
            model_admin.has_add_permission(_SUPERUSER),
            model_admin.has_change_permission(_SUPERUSER),
            model_admin.has_change_permission(_SUPERUSER, row),
            model_admin.has_delete_permission(_SUPERUSER),
            model_admin.has_delete_permission(_SUPERUSER, row),
            model_admin.has_view_permission(_SUPERUSER, row),
        )
    )


def list_only_admins(site: admin.AdminSite, candidates: Iterable[ModelClass]) -> list[admin.ModelAdmin]:
    return [
        site._registry[model]
        for model in candidates
        if site.is_registered(model) and is_list_only(site._registry[model])
    ]


def missing_admins(site: admin.AdminSite, candidates: Iterable[ModelClass], unregistrable: dict[str, str]) -> list[str]:
    return [
        f"{model._meta.label} has no admin: core/admin.py registers every core model read-only on import, so "
        "restore register_read_only_defaults(admin.site, ...) at its end; a model outside core registers "
        "itself with ReadOnlyAdmin in its app's admin.py"
        for model in candidates
        if not site.is_registered(model) and model._meta.label not in unregistrable
    ]


def unregistrable_mismatches(candidates: Iterable[ModelClass], unregistrable: dict[str, str]) -> list[str]:
    composite = {model._meta.label for model in candidates if model._meta.is_composite_pk}
    return [
        *(
            f"{label} is registrable: only a composite-primary-key model, which Django refuses to register, "
            "belongs in UNREGISTRABLE"
            for label in sorted(set(unregistrable) - composite)
        ),
        *(
            f"{label} has a composite primary key Django cannot register: list it in UNREGISTRABLE with the reason"
            for label in sorted(composite - set(unregistrable))
        ),
    ]


def writable_mismatches(site: admin.AdminSite, candidates: Iterable[ModelClass], declared: dict[str, str]) -> list[str]:
    writable = {
        model._meta.label
        for model in candidates
        if site.is_registered(model) and not is_list_only(site._registry[model])
    }
    return [
        *(
            f"{label} is writable or shows a per-row page: subclass ReadOnlyAdmin without re-enabling a "
            f"permission, or add '{label}' to WRITABLE_ADMINS here with the reason an operator must edit it"
            for label in sorted(writable - set(declared))
        ),
        *(
            f"WRITABLE_ADMINS lists {label}, which has no writable admin: drop the stale entry"
            for label in sorted(set(declared) - writable)
        ),
    ]


def fsm_fields(candidates: Iterable[ModelClass]) -> list[tuple[ModelClass, models.Field]]:
    return [
        (model, field)
        for model in candidates
        for field in model._meta.concrete_fields
        if isinstance(field, FSMFieldMixin)
    ]


def editable_fsm_fields(site: admin.AdminSite, candidates: Iterable[ModelClass]) -> list[str]:
    violations = []
    for model, field in fsm_fields(candidates):
        if not site.is_registered(model):
            continue
        model_admin, row = site._registry[model], model()
        add_edits = model_admin.has_add_permission(_SUPERUSER) and field.name not in model_admin.get_readonly_fields(
            _SUPERUSER
        )
        change_edits = model_admin.has_change_permission(
            _SUPERUSER, row
        ) and field.name not in model_admin.get_readonly_fields(_SUPERUSER, row)
        if add_edits or change_edits:
            violations.append(
                f"{model._meta.label}.{field.name} is editable in the admin, which skips the transition guards: "
                "register the model with ReadOnlyAdmin or list the field in readonly_fields"
            )
    return violations


def known_hidden_columns(model: ModelClass) -> set[str]:
    return {
        field.name
        for field in model._meta.concrete_fields
        if isinstance(field, _HIDDEN_TYPES) or (model._meta.label, field.name) in MUST_STAY_HIDDEN
    }


def _field_names(model: ModelClass, names: Iterable[str]) -> set[str]:
    resolved = set()
    for name in names:
        try:
            resolved.add(model._meta.get_field(name.split(LOOKUP_SEP, 1)[0]).name)
        except FieldDoesNotExist:
            continue
    return resolved


def _filter_names(model_admin: admin.ModelAdmin) -> list[str]:
    return [
        item if isinstance(item, str) else item[0]
        for item in model_admin.get_list_filter(_SUPERUSER)
        if isinstance(item, str | tuple | list)
    ]


def selected_columns(model_admin: admin.ModelAdmin) -> set[str]:
    names, defer = model_admin.get_queryset(_SUPERUSER).query.deferred_loading
    concrete = {field.name for field in model_admin.model._meta.concrete_fields}
    return concrete - _field_names(model_admin.model, names) if defer else _field_names(model_admin.model, names)


def exposed_hidden_columns(site: admin.AdminSite, candidates: Iterable[ModelClass], hidden: HiddenColumns) -> list[str]:
    violations = []
    for model_admin in list_only_admins(site, candidates):
        model = model_admin.model
        surfaces = {
            "lists": _field_names(model, model_admin.get_list_display(_SUPERUSER)),
            "filters on": _field_names(model, _filter_names(model_admin)),
            "selects": selected_columns(model_admin),
        }
        violations.extend(
            f"{model._meta.label}'s admin {surface} hidden column(s) {sorted(exposed)}"
            for surface, shown in surfaces.items()
            if (exposed := shown & hidden(model))
        )
    return violations


def _search_target(model: ModelClass, term: str) -> tuple[ModelClass, str]:
    for part in term.lstrip("^=@").split(LOOKUP_SEP):
        try:
            field = model._meta.get_field(part)
        except FieldDoesNotExist:
            return model, ""
        if field.related_model is None:
            return model, field.name
        model = field.related_model
    return model, ""


def hidden_search_fields(site: admin.AdminSite, candidates: Iterable[ModelClass], hidden: HiddenColumns) -> list[str]:
    return [
        f"{model_admin.model._meta.label}'s search_fields entry {term!r} searches a hidden column, which the "
        "changelist and autocomplete routes would turn into an oracle"
        for model_admin in list_only_admins(site, candidates)
        for term in model_admin.get_search_fields(_SUPERUSER)
        if (target := _search_target(model_admin.model, term))[1] in hidden(target[0])
    ]


def _probe(registry: Apps, class_name: str, /, **fields: models.Field) -> ModelClass:
    meta = type("Meta", (), {"app_label": "probe", "apps": registry})
    return type(class_name, (models.Model,), {"__module__": __name__, "Meta": meta, **fields})


def _plain(registry: Apps, class_name: str = "Plain") -> ModelClass:
    return _probe(registry, class_name, title=models.CharField(max_length=10))


def _composite(registry: Apps) -> ModelClass:
    return _probe(
        registry,
        "Composite",
        pk=models.CompositePrimaryKey("left", "right"),
        left=models.IntegerField(),
        right=models.IntegerField(),
    )


def _never_seen(registry: Apps) -> ModelClass:
    owner = _plain(registry, "Owner")
    return _probe(
        registry,
        "NeverSeen",
        api_key=models.CharField(max_length=40),
        secret_token=models.CharField(max_length=40),
        db_password=models.CharField(max_length=40),
        webhook_url=models.URLField(),
        check_command=models.CharField(max_length=200),
        env_value=models.CharField(max_length=200),
        last_error=models.CharField(max_length=200),
        reason=models.CharField(max_length=200),
        contact_email=models.EmailField(),
        api_token=models.UUIDField(null=True),
        body=models.TextField(),
        payload=models.JSONField(default=dict),
        notes=models.TextField(),
        owner=models.ForeignKey(owner, on_delete=models.CASCADE),
        enabled=models.BooleanField(default=False),
        created_at=models.DateTimeField(null=True),
        input_tokens=models.IntegerField(default=0),
        status_reason=models.CharField(max_length=10, choices=[("stale", "Stale")]),
        name=models.CharField(max_length=40),
    )


_NEVER_SEEN_HIDDEN = {
    "api_key",
    "secret_token",
    "db_password",
    "webhook_url",
    "check_command",
    "env_value",
    "last_error",
    "reason",
    "contact_email",
    "api_token",
    "body",
    "payload",
    "notes",
}
_NEVER_SEEN_VISIBLE = {"id", "owner", "enabled", "created_at", "input_tokens", "status_reason", "name"}


def _site(*registrations: tuple[ModelClass, type[admin.ModelAdmin]]) -> admin.AdminSite:
    site = admin.AdminSite(name="probe")
    for model, model_admin in registrations:
        site.register(model, model_admin)
    return site


class TestLiveTree:
    def test_every_teatree_model_has_an_admin(self) -> None:
        missing = missing_admins(admin.site, teatree_models(), UNREGISTRABLE)
        assert not missing, "\n".join(missing)

    def test_unregistrable_is_exactly_the_composite_key_models(self) -> None:
        mismatches = unregistrable_mismatches(teatree_models(), UNREGISTRABLE)
        assert not mismatches, "\n".join(mismatches)

    def test_only_declared_admins_are_writable(self) -> None:
        mismatches = writable_mismatches(admin.site, teatree_models(), WRITABLE_ADMINS)
        assert not mismatches, "\n".join(mismatches)

    def test_every_fsm_field_has_no_edit_path(self) -> None:
        assert len(fsm_fields(teatree_models())) >= _FSM_FIELD_FLOOR
        violations = editable_fsm_fields(admin.site, teatree_models())
        assert not violations, "\n".join(violations)

    def test_no_read_only_admin_lists_a_text_json_or_known_sensitive_field(self) -> None:
        live = {(model._meta.label, field.name) for model in teatree_models() for field in model._meta.concrete_fields}
        assert sorted(MUST_STAY_HIDDEN - live) == [], "MUST_STAY_HIDDEN names a column that no longer exists"
        violations = exposed_hidden_columns(admin.site, teatree_models(), known_hidden_columns)
        assert not violations, "\n".join(violations)

    def test_every_model_reads_as_a_plural_in_the_admin_index(self) -> None:
        naive = [
            f"{model._meta.label}: {model._meta.verbose_name_plural!s}"
            for model in teatree_models()
            if _NAIVE_PLURAL.search(str(model._meta.verbose_name_plural))
        ]
        assert not naive, "Django appended a bare 's'; set verbose_name_plural in Meta:\n" + "\n".join(naive)

    def test_a_search_field_never_names_a_hidden_column(self) -> None:
        searchable = [ma for ma in list_only_admins(admin.site, teatree_models()) if ma.get_search_fields(_SUPERUSER)]
        assert searchable, "no read-only admin searches anything, so this lane would judge nothing"
        violations = hidden_search_fields(admin.site, teatree_models(), known_hidden_columns)
        assert not violations, "\n".join(violations)


class TestLoop:
    def test_registers_every_candidate_read_only(self) -> None:
        registry = Apps()
        candidates = [_plain(registry, "First"), _plain(registry, "Second")]
        site = _site()

        register_read_only_defaults(site, candidates)

        assert [type(site._registry[model]) for model in candidates] == [ReadOnlyAdmin, ReadOnlyAdmin]

    def test_keeps_a_hand_written_admin(self) -> None:
        registry = Apps()
        model = _plain(registry)
        site = _site((model, admin.ModelAdmin))

        register_read_only_defaults(site, [model])

        assert type(site._registry[model]) is admin.ModelAdmin

    def test_skips_a_composite_key_model(self) -> None:
        registry = Apps()
        composite, plain = _composite(registry), _plain(registry)
        site = _site()

        register_read_only_defaults(site, [composite, plain])

        assert (site.is_registered(composite), site.is_registered(plain)) == (False, True)

    def test_is_idempotent(self) -> None:
        registry = Apps()
        candidates = [_plain(registry, "First"), _plain(registry, "Second")]
        site = _site()
        register_read_only_defaults(site, candidates)
        first = dict(site._registry)

        register_read_only_defaults(site, candidates)

        assert site._registry == first


class TestSyntheticLanes:
    def test_a_planted_unregistered_model_is_named_with_its_fix(self) -> None:
        model = _plain(Apps())

        missing = missing_admins(_site(), [model], {})

        assert len(missing) == 1
        assert "probe.Plain has no admin" in missing[0]
        assert "register_read_only_defaults" in missing[0]

    def test_a_listed_composite_key_model_is_neither_missing_nor_mismatched(self) -> None:
        composite = _composite(Apps())
        declared = {"probe.Composite": "Django refuses a composite primary key"}

        assert (missing_admins(_site(), [composite], declared), unregistrable_mismatches([composite], declared)) == (
            [],
            [],
        )

    def test_an_unregistrable_entry_that_is_registrable_is_refused(self) -> None:
        model = _plain(Apps())

        mismatches = unregistrable_mismatches([model], {"probe.Plain": "prefers not to be registered"})

        assert len(mismatches) == 1
        assert "probe.Plain is registrable" in mismatches[0]

    def test_a_planted_writable_admin_is_refused(self) -> None:
        model = _plain(Apps())

        mismatches = writable_mismatches(_site((model, admin.ModelAdmin)), [model], {})

        assert len(mismatches) == 1
        assert "probe.Plain is writable" in mismatches[0]
        assert "WRITABLE_ADMINS" in mismatches[0]

    def test_a_read_only_subclass_that_re_enables_change_is_refused(self) -> None:
        class ChangeableAdmin(ReadOnlyAdmin):
            def has_change_permission(self, request, obj=None) -> bool:
                return True

        model = _plain(Apps())
        site = _site((model, ChangeableAdmin))

        assert isinstance(site._registry[model], ReadOnlyAdmin)
        assert len(writable_mismatches(site, [model], {})) == 1

    def test_a_read_only_subclass_that_re_enables_the_row_page_is_refused(self) -> None:
        class RowPageAdmin(ReadOnlyAdmin):
            def has_view_permission(self, request, obj=None) -> bool:
                return True

        model = _plain(Apps())

        assert len(writable_mismatches(_site((model, RowPageAdmin)), [model], {})) == 1

    def test_a_stale_writable_declaration_is_refused(self) -> None:
        model = _plain(Apps())
        declared = {"probe.Plain": "was editable once", "probe.Gone": "renamed away"}

        mismatches = writable_mismatches(_site((model, ReadOnlyAdmin)), [model], declared)

        assert mismatches == [
            "WRITABLE_ADMINS lists probe.Gone, which has no writable admin: drop the stale entry",
            "WRITABLE_ADMINS lists probe.Plain, which has no writable admin: drop the stale entry",
        ]

    def test_an_editable_fsm_field_is_refused(self) -> None:
        registry = Apps()
        model = _probe(registry, "Machine", state=FSMField(default="new"))

        assert len(editable_fsm_fields(_site((model, admin.ModelAdmin)), [model])) == 1
        assert editable_fsm_fields(_site((model, ReadOnlyAdmin)), [model]) == []

    def test_a_listed_text_column_is_refused(self) -> None:
        class TextListingAdmin(ReadOnlyAdmin):
            def get_list_display(self, request) -> list[str]:
                return ["id", "body"]

        model = _probe(Apps(), "Note", body=models.TextField())

        violations = exposed_hidden_columns(_site((model, TextListingAdmin)), [model], known_hidden_columns)

        assert violations == ["probe.Note's admin lists hidden column(s) ['body']"]

    def test_a_search_on_a_hidden_column_is_refused(self) -> None:
        class SearchingAdmin(ReadOnlyAdmin):
            search_fields = ("^owner__title", "notes")

        registry = Apps()
        owner = _plain(registry, "Owner")
        model = _probe(
            registry, "Searched", notes=models.TextField(), owner=models.ForeignKey(owner, on_delete=models.CASCADE)
        )

        violations = hidden_search_fields(_site((model, SearchingAdmin)), [model], known_hidden_columns)

        assert len(violations) == 1
        assert "'notes'" in violations[0]

    def test_a_never_seen_model_hides_what_looks_sensitive_and_keeps_the_rest(self) -> None:
        model = _never_seen(Apps())
        model_admin = ReadOnlyAdmin(model, _site())
        names, defer = model_admin.get_queryset(_SUPERUSER).query.deferred_loading
        surfaces = {
            "list_display": _field_names(model, model_admin.get_list_display(_SUPERUSER)),
            "only()": _field_names(model, names),
        }

        assert defer is False
        for surface, shown in surfaces.items():
            assert shown & _NEVER_SEEN_HIDDEN == set(), surface
            assert shown >= _NEVER_SEEN_VISIBLE, surface
        assert set(_filter_names(model_admin)) == {"enabled", "status_reason"}

    def test_a_lookup_on_a_hidden_column_is_refused(self) -> None:
        model_admin = ReadOnlyAdmin(_never_seen(Apps()), _site())

        verdicts = {
            lookup: model_admin.lookup_allowed(lookup, "x", _SUPERUSER)
            for lookup in (
                "api_key__startswith",
                "payload__a",
                "api_token__exact",
                "id__exact",
                "name__startswith",
                "status_reason__exact",
            )
        }

        assert verdicts == {
            "api_key__startswith": False,
            "payload__a": False,
            "api_token__exact": False,
            "id__exact": True,
            "name__startswith": True,
            "status_reason__exact": True,
        }
