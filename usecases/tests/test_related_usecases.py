"""
Beta 8.9a: related_usecases() and the "Related Use Cases" widget on
UseCase detail pages must be strictly language-isolated (visible_in_language())
and must not crash with a FieldError when persona is set.

Confirmed root cause (empirical + code analysis): `persona` is a field on
the UseCaseTranslation model (declared inside UseCase.translations =
TranslatedFields(...)), not on UseCase itself. The pre-existing filter
`Q(persona__iexact=persona)` in core.services.related_usecases() therefore
raised:

    django.core.exceptions.FieldError: Cannot resolve keyword 'persona'
    into field. Choices are: author, author_id, created_at, id,
    is_published, last_published_revision_id, live_i18n, published_at,
    review_note, reviewed_at, reviewed_by, reviewed_by_id, status,
    submitted_for_review_at, tools, translations, updated_at

for any published use case with persona set, because get_context_data()
always calls related_usecases() to build the "similar" widget. Confirmed
via a controlled shell reproduction before the fix (see Beta 8.9a report).
The correct path is `translations__persona__iexact` (the reverse accessor
Parler generates for TranslatedFields is `translations`).
"""
from django.test import TestCase
from django.utils import timezone, translation

from core.models.editorial import EditorialWorkflowMixin
from core.services import related_usecases
from usecases.models import UseCase


def make_usecase(*, slug, status=EditorialWorkflowMixin.STATUS_PUBLISHED,
                  published_at=None, languages=("en", "de"), persona="", **extra):
    if published_at is None and status == EditorialWorkflowMixin.STATUS_PUBLISHED:
        published_at = timezone.now()
    u = UseCase.objects.create(status=status, published_at=published_at, **extra)
    for lang in languages:
        u.create_translation(
            lang, title=f"Title {slug} {lang}", intro="i", body="b", outro="o",
            slug=f"{slug}-{lang}", persona=persona,
        )
    return u


class PersonaFieldErrorReproductionTests(TestCase):
    """Section A: the fix must turn this from a crash into HTTP 200."""

    def test_detail_page_with_persona_no_longer_raises_field_error(self):
        make_usecase(slug="persona-crash-check", languages=("en",), persona="Founder")
        # Before the fix this raised FieldError inside related_usecases();
        # after the fix it must render cleanly.
        resp = self.client.get("/en/usecases/persona-crash-check-en/")
        self.assertEqual(resp.status_code, 200)

    def test_related_usecases_service_call_with_persona_does_not_raise(self):
        current = make_usecase(slug="persona-service-check", languages=("en",), persona="Marketer")
        try:
            result = related_usecases(current, limit=3, language_code="en")
        except Exception as exc:  # noqa: BLE001 - explicit assertion, not prod code
            self.fail(f"related_usecases() raised {exc!r} for a use case with persona set")
        self.assertIsInstance(result, list)


class RelatedUseCasePersonaMatchTests(TestCase):
    def test_persona_match_in_active_language_is_considered(self):
        current = make_usecase(slug="current-persona-en", languages=("en",), persona="Founder")
        match = make_usecase(slug="match-persona-en", languages=("en",), persona="Founder")
        other = make_usecase(slug="other-persona-en", languages=("en",), persona="Teacher")
        result = related_usecases(current, limit=6, language_code="en")
        result_pks = [u.pk for u in result]
        self.assertIn(match.pk, result_pks)
        self.assertIn(other.pk, result_pks)
        # Persona match must rank above the non-matching persona.
        self.assertLess(result_pks.index(match.pk), result_pks.index(other.pk))

    def test_wrong_language_persona_is_not_considered_for_match(self):
        # A DE-only use case whose DE persona text happens to equal the
        # current (EN) use case's persona must not be treated as a match
        # via its DE translation row while resolving in EN context - and
        # since it also has no active EN translation at all, it must be
        # fully absent from the EN-language recommendation set.
        current = make_usecase(slug="current-wronglang", languages=("en",), persona="Founder")
        de_only_same_text = make_usecase(slug="de-only-same-text", languages=("de",), persona="Founder")
        result = related_usecases(current, limit=6, language_code="en")
        self.assertNotIn(de_only_same_text.pk, [u.pk for u in result])


class RelatedUseCaseLanguageIsolationTests(TestCase):
    def test_en_only_related_absent_under_de(self):
        current = make_usecase(slug="current-de", languages=("de",))
        en_only = make_usecase(slug="en-only-related", languages=("en",))
        result = related_usecases(current, limit=6, language_code="de")
        self.assertNotIn(en_only.pk, [u.pk for u in result])

    def test_de_only_related_absent_under_en(self):
        current = make_usecase(slug="current-en-2", languages=("en",))
        de_only = make_usecase(slug="de-only-related", languages=("de",))
        result = related_usecases(current, limit=6, language_code="en")
        self.assertNotIn(de_only.pk, [u.pk for u in result])

    def test_bilingual_related_appears_in_both_languages(self):
        current_en = make_usecase(slug="current-en-3", languages=("en",))
        current_de = make_usecase(slug="current-de-3", languages=("de",))
        both = make_usecase(slug="both-related", languages=("en", "de"))
        self.assertIn(both.pk, [u.pk for u in related_usecases(current_en, limit=6, language_code="en")])
        self.assertIn(both.pk, [u.pk for u in related_usecases(current_de, limit=6, language_code="de")])


class RelatedUseCaseExclusionAndStatusTests(TestCase):
    def test_current_usecase_excluded_from_its_own_recommendations(self):
        current = make_usecase(slug="self-exclude", languages=("en",), persona="Founder")
        make_usecase(slug="filler-1", languages=("en",), persona="Founder")
        result = related_usecases(current, limit=6, language_code="en")
        self.assertNotIn(current.pk, [u.pk for u in result])

    def test_draft_is_never_recommended(self):
        current = make_usecase(slug="current-draft-check", languages=("en",))
        make_usecase(slug="draft-related", status=EditorialWorkflowMixin.STATUS_DRAFT,
                     published_at=None, languages=("en",))
        result = related_usecases(current, limit=6, language_code="en")
        self.assertEqual(len(result), 0)


class RelatedUseCaseRankingLimitTests(TestCase):
    def test_existing_limit_is_preserved(self):
        current = make_usecase(slug="current-limit", languages=("en",))
        for i in range(10):
            make_usecase(slug=f"limit-filler-{i}", languages=("en",))
        result = related_usecases(current, limit=3, language_code="en")
        self.assertLessEqual(len(result), 3)

    def test_no_duplicates(self):
        from catalog.models import Tool
        from parler.utils.context import switch_language

        tool = Tool.objects.create(slug="dup-check-tool", website="https://example.com/dup")
        with switch_language(tool, "en"):
            tool.name = "Dup Tool"
            tool.short_description = "s"
            tool.long_description = "l"
            tool.save()

        current = make_usecase(slug="current-nodup", languages=("en",), persona="Founder")
        current.tools.add(tool)
        related = make_usecase(slug="nodup-related", languages=("en",), persona="Founder")
        related.tools.add(tool)

        result = related_usecases(current, limit=6, language_code="en")
        pks = [u.pk for u in result]
        self.assertEqual(len(pks), len(set(pks)))

    def test_empty_persona_does_not_raise(self):
        current = make_usecase(slug="empty-persona-current", languages=("en",), persona="")
        make_usecase(slug="empty-persona-other", languages=("en",), persona="")
        try:
            result = related_usecases(current, limit=6, language_code="en")
        except Exception as exc:  # noqa: BLE001
            self.fail(f"related_usecases() raised {exc!r} for an empty persona")
        self.assertIsInstance(result, list)

    def test_no_related_content_yields_clean_empty_state(self):
        current = make_usecase(slug="lonely-usecase", languages=("en",), persona="")
        result = related_usecases(current, limit=6, language_code="en")
        self.assertEqual(result, [])

    def test_all_related_links_return_200_in_active_language(self):
        current = make_usecase(slug="current-links-check", languages=("en",), persona="Founder")
        for i in range(5):
            make_usecase(slug=f"link-check-{i}", languages=("en",), persona="Founder")
        result = related_usecases(current, limit=6, language_code="en")
        with translation.override("en"):
            for u in result:
                resp = self.client.get(u.get_absolute_url(language="en"))
                self.assertEqual(resp.status_code, 200)
