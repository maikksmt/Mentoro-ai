"""
Shared fixtures for the editorial search adapter tests.

Content is published through the real FSM so ``live_i18n`` is written exactly
as production writes it, and drafts are created by editing the translation
without republishing - the state that separates the published revision from
the current one.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field

from django.contrib.auth import get_user_model
from parler.utils.context import switch_language

from compare.models import Comparison
from core.models.editorial import EditorialWorkflowMixin
from guides.models import Guide
from prompts.models import Prompt
from search.adapters.comparisons import ComparisonSearchAdapter
from search.adapters.guides import GuideSearchAdapter
from search.adapters.prompts import PromptSearchAdapter
from search.adapters.usecases import UseCaseSearchAdapter
from search.result_types import SearchResultKind
from usecases.models import UseCase

User = get_user_model()


@dataclass(frozen=True)
class AdapterSpec:
    """Everything a shared test needs to exercise one adapter."""

    name: str
    adapter_class: type
    model: type
    kind: SearchResultKind
    url_prefix: str
    #: Translated text fields the adapter indexes, excluding the title.
    text_fields: tuple[str, ...]
    #: True when the model's visible_in_language() builds on
    #: visible_on_site(), so a review with a live revision stays public.
    review_with_live_revision_is_public: bool
    #: Extra fields every translation of this model needs.
    required_extra: dict = dataclass_field(default_factory=dict)

    def build_adapter(self):
        return self.adapter_class()


ADAPTER_SPECS: tuple[AdapterSpec, ...] = (
    AdapterSpec(
        name="guide",
        adapter_class=GuideSearchAdapter,
        model=Guide,
        kind=SearchResultKind.GUIDE,
        url_prefix="guides",
        text_fields=("intro", "body"),
        review_with_live_revision_is_public=True,
    ),
    AdapterSpec(
        name="prompt",
        adapter_class=PromptSearchAdapter,
        model=Prompt,
        kind=SearchResultKind.PROMPT,
        url_prefix="prompts",
        text_fields=("intro", "body", "outro"),
        review_with_live_revision_is_public=True,
    ),
    AdapterSpec(
        name="usecase",
        adapter_class=UseCaseSearchAdapter,
        model=UseCase,
        kind=SearchResultKind.USE_CASE,
        url_prefix="usecases",
        text_fields=("intro", "body", "outro"),
        # Beta 11.7 moved UseCaseQuerySet.visible_in_language() from
        # published() to visible_on_site(), matching guide and prompt.
        review_with_live_revision_is_public=True,
        required_extra={"persona": ""},
    ),
    AdapterSpec(
        name="comparison",
        adapter_class=ComparisonSearchAdapter,
        model=Comparison,
        kind=SearchResultKind.COMPARISON,
        url_prefix="compare",
        text_fields=("intro", "body"),
        review_with_live_revision_is_public=False,
    ),
)

#: The three adapters this slice introduces.
NEW_ADAPTER_SPECS = tuple(spec for spec in ADAPTER_SPECS if spec.name != "guide")


def make_author(username="editorial-adapter-editor"):
    return User.objects.create_user(
        username=username, email=f"{username}@example.com", password="testpass123"
    )


def _translation_values(spec: AdapterSpec, values: dict) -> dict:
    payload = dict(spec.required_extra)
    payload["title"] = values.get("title", "Untitled")
    for name in spec.text_fields:
        payload[name] = values.get(name, "")
    payload["slug"] = values["slug"]
    return payload


def publish(spec: AdapterSpec, *, author, translations: dict):
    """Creates and publishes an object through the real publish transition."""
    obj = spec.model.objects.create(
        status=EditorialWorkflowMixin.STATUS_APPROVED, author=author
    )
    for language_code, values in translations.items():
        obj.create_translation(language_code, **_translation_values(spec, values))
    obj.publish(by=author)
    obj.save()
    return obj


def make_legacy(spec: AdapterSpec, *, translations: dict):
    """A published record with no snapshot at all, as produced before the
    live-revision mechanism existed."""
    obj = spec.model.objects.create(status=EditorialWorkflowMixin.STATUS_PUBLISHED)
    for language_code, values in translations.items():
        obj.create_translation(language_code, **_translation_values(spec, values))
    return obj


def edit_without_publishing(obj, *, language_code, **fields):
    """
    Changes the current translation and leaves the published snapshot alone.

    Deliberately does not move the object into review: for use cases and
    comparisons that would drop them out of their own public queryset
    entirely, and a draft-leak test that passes because the object vanished
    proves nothing.
    """
    with switch_language(obj, language_code):
        for name, value in fields.items():
            setattr(obj, name, value)
        obj.save()
    return obj


def begin_unpublished_revision(obj, *, author, language_code, **fields):
    """Edits the translation and moves the object into review with a live
    revision - the state guides and prompts keep public."""
    edit_without_publishing(obj, language_code=language_code, **fields)
    obj.move_to_review(by=author)
    obj.last_published_revision_id = 1
    obj.save()
    return obj


def republish(obj, *, author):
    """Approves and republishes an object sitting in review."""
    fresh = obj.__class__.objects.get(pk=obj.pk)
    fresh.approve(by=author)
    fresh.save()
    fresh.publish(by=author)
    fresh.save()
    return fresh
