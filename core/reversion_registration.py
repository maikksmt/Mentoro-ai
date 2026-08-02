"""
The single source of truth for django-reversion registration of the editorial
content types (Guide, Prompt, Use Case, Comparison).

Why this module exists (Beta 11.11B1)
-------------------------------------
Until this slice the registration was decided by whichever code path happened
to run first during startup, and the result did not match what the project had
written down anywhere:

1. ``mentoroai.apps.MentoroAdminConfig`` is ``INSTALLED_APPS[0]``, so its
   ``ready()`` runs before every editorial app's ``ready()``.
2. ``django.contrib.admin.apps.AdminConfig.ready()`` calls
   ``admin.autodiscover()``, which imports every ``admin`` module and
   instantiates every ``ModelAdmin``.
3. ``reversion.admin.VersionAdmin.__init__()`` auto-registers its model when it
   is not registered yet, deriving ``follow`` from the admin's *inlines*.
4. Only afterwards did ``guides``/``prompts``/``usecases`` ``ready()`` run and
   call ``reversion.register(..., follow=("translations",))``.
5. That raised ``RegistrationError`` - which was swallowed by
   ``except RegistrationError: pass``.

So the intended ``follow=("translations",)`` was dead code in all three apps,
and the graph that actually shipped was an accident of the admin layout:
``Guide`` followed ``sections`` only, ``Prompt``/``UseCase`` followed nothing,
``Comparison`` followed ``tool_entries`` only, and
``GuideSectionTranslation``, ``GuideItem``, ``GuideItemTranslation``,
``ComparisonTranslation`` and ``ComparisonToolEntryTranslation`` were never
registered at all. A Comparison approval revision therefore contained no
editorial text whatsoever (Beta 11.11A audit, section 6).

The contract now
----------------
:func:`register_editorial_reversion_models` is called from
``MentoroAdminConfig.ready()`` *before* ``super().ready()``, i.e. before admin
autodiscovery. Every model in :data:`EDITORIAL_REVERSION_MANIFEST` is therefore
already registered by the time any ``VersionAdmin`` is instantiated, and
``VersionAdmin.__init__()``'s auto-registration branch (``if not
is_registered(self.model)``) never runs for these models - it neither registers
them nor touches their ``follow``.

The function fails fast (``ImproperlyConfigured``) if any of these models is
already registered when it runs. It never swallows a ``RegistrationError``, and
it never reaches into reversion's private registry: ``is_registered()`` and
``register()`` are the whole public surface it needs.

Serialization options are deliberately left at reversion's defaults
(``fields=None`` -> all local fields plus local m2m, ``format="json"``,
``for_concrete_model=True``, ``ignore_duplicates=False``,
``use_natural_foreign_keys=False``), which is exactly what the previous
auto-registration produced. This slice changes *when* and *what* is registered,
never *how* a row is serialized.

Known limits this slice does NOT close (see the Beta 11.11A audit)
------------------------------------------------------------------
* There is still no review/approval binding, no payload fingerprint and no
  invalidation when content changes during ``review``/``approved``.
* ``GuideSectionAdmin`` is not a ``VersionAdmin``, so saving a section through
  its own change form still produces no revision at all.
* Programmatic saves outside an explicit ``reversion.create_revision()`` still
  produce no revision.
* Deletions appear in a later revision only as absence - reversion writes no
  explicit delete marker.
* External ``catalog.Tool``/``catalog.Category`` rows are deliberately not
  frozen into a content revision; only the membership (FK/m2m ids) is.
* ``Prompt.tags`` (taggit) is deliberately still outside the graph, see
  :data:`DEFERRED_EDITORIAL_RELATIONS`.
"""
from django.core.exceptions import ImproperlyConfigured

#: The canonical registration contract, in registration order.
#:
#: ``(app_label, model_name, follow)`` - ``follow`` holds accessor names
#: verified against the real models, not assumed ones:
#:
#: * every Parler translation set is reachable as ``translations``
#:   (``Model._parler_meta.root_rel_name``);
#: * ``Guide.sections`` / ``GuideSection.items`` / ``Comparison.tool_entries``
#:   are the ``related_name``s declared on the child FKs.
#:
#: A parent follows its own translations plus its direct editorial children; a
#: child follows its own translations plus its own deeper children. Nothing
#: follows ``catalog.Tool`` or ``catalog.Category``: those have no editorial
#: workflow of their own and must not be frozen into a content revision - only
#: the *membership* travels along, as plain-m2m id lists on the parent version
#: (``Guide.categories``, ``Guide.tools``, ``Prompt.tools``, ``UseCase.tools``).
#:
#: ``Comparison.tools`` is an explicit-through m2m and is therefore *not*
#: serialized onto the parent version by Django's serializer. It does not need
#: to be: ``ComparisonToolEntry`` carries ``tool_id`` and ``position``, and the
#: full entry graph - rows plus translations - is now part of the revision, so
#: membership and order are represented there.
EDITORIAL_REVERSION_MANIFEST = (
    # --- Guide -----------------------------------------------------------
    ("guides", "Guide", ("translations", "sections")),
    ("guides", "GuideTranslation", ()),
    ("guides", "GuideSection", ("translations", "items")),
    ("guides", "GuideSectionTranslation", ()),
    ("guides", "GuideItem", ("translations",)),
    ("guides", "GuideItemTranslation", ()),
    # --- Prompt ----------------------------------------------------------
    ("prompts", "Prompt", ("translations",)),
    ("prompts", "PromptTranslation", ()),
    # --- Use Case --------------------------------------------------------
    ("usecases", "UseCase", ("translations",)),
    ("usecases", "UseCaseTranslation", ()),
    # --- Comparison ------------------------------------------------------
    ("compare", "Comparison", ("translations", "tool_entries")),
    ("compare", "ComparisonTranslation", ()),
    ("compare", "ComparisonToolEntry", ("translations",)),
    ("compare", "ComparisonToolEntryTranslation", ()),
)

#: Editorial data that is knowingly still outside the revision graph after this
#: slice, with the reason. Kept as data so the test suite can assert the gap
#: instead of letting it drift into an unnoticed assumption.
#:
#: ``prompts.Prompt.tags`` is a taggit ``TaggableManager``. It appears in the
#: registered ``fields`` tuple (reversion derives that from
#: ``local_fields + local_many_to_many``), but Django's serializer skips any m2m
#: whose through model is not ``auto_created``, so ``tags`` is never written
#: into ``serialized_data`` - verified empirically in Beta 11.11A.
#:
#: Closing it here is not possible within this slice's boundaries:
#: ``getattr(prompt, "tags")`` yields ``Tag`` objects, so ``follow=("tags",)``
#: would freeze the *external* tag records (explicitly out of contract) while
#: still not recording membership, which lives in ``taggit.TaggedItem``. Reaching
#: those rows would need a ``GenericRelation`` on ``Prompt`` - a model change,
#: which Beta 11.11B1 must not make. Deferred to the Prompt-specific slice.
DEFERRED_EDITORIAL_RELATIONS = {
    ("prompts", "Prompt", "tags"): (
        "taggit TaggableManager; explicit-through m2m is not serialized and "
        "membership lives in taggit.TaggedItem, which is unreachable without a "
        "GenericRelation on Prompt (model change, out of scope for 11.11B1)."
    ),
}


def register_editorial_reversion_models():
    """
    Register every editorial model in :data:`EDITORIAL_REVERSION_MANIFEST`.

    Must run before ``django.contrib.admin.autodiscover()``; see the module
    docstring for why. Imports live inside the function so this module stays
    importable from ``mentoroai/apps.py`` without pulling model or admin modules
    into the app-loading phase.

    Raises ``ImproperlyConfigured`` if any manifest model is already registered.
    That is deliberate: a pre-registration means some other code path won the
    race again, and the resulting graph would silently differ from this
    manifest. Failing loudly at startup is the whole point of this slice - the
    swallowed ``RegistrationError`` it replaces is exactly how the previous
    divergence stayed invisible.
    """
    import reversion
    from django.apps import apps

    models = [
        (apps.get_model(app_label, model_name), follow)
        for app_label, model_name, follow in EDITORIAL_REVERSION_MANIFEST
    ]

    preregistered = [
        model._meta.label for model, _follow in models if reversion.is_registered(model)
    ]
    if preregistered:
        raise ImproperlyConfigured(
            "django-reversion registration for editorial models must come from "
            "core.reversion_registration.register_editorial_reversion_models() "
            "alone, but the following models were already registered when it "
            f"ran: {', '.join(preregistered)}. Something registered them "
            "earlier - most likely a VersionAdmin was instantiated before "
            "MentoroAdminConfig.ready() called this function, or a per-app "
            "reversion.register() call was reintroduced. Remove the competing "
            "registration instead of relaxing this check."
        )

    for model, follow in models:
        reversion.register(model, follow=follow)
