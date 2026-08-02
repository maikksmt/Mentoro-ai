"""
Beta 11.11B1: what a real editorial admin request actually writes into a
django-reversion revision.

The Beta 11.11A audit measured this the hard way and found the graph badly
incomplete: Guide section and item text were never versioned at all, a
Comparison revision contained no editorial text whatsoever, and Prompt/Use Case
translations only appeared when the very same request happened to save them.
The registration fix in ``core.reversion_registration`` is only worth anything
if the revisions it produces really contain the graph, so these tests drive the
real ``VersionAdmin`` request paths - add form, change form with inline
formsets, and the changelist workflow actions - and then read the serialized
payload, not just the content types.

Two things these tests deliberately do NOT claim, because B1 does not deliver
them:

* that the revision is a sufficient stale-equality operator (deletions are only
  visible as absence, external Tool/Category rows are not frozen, and there is
  still no review binding or fingerprint);
* that every write path produces a revision. ``GuideSectionAdmin`` is not a
  ``VersionAdmin`` and programmatic saves need an explicit
  ``create_revision()``. Both gaps are asserted as known limitations at the end
  of this module so they stay visible until Beta 11.11F closes them.
"""
import json

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from reversion.models import Revision, Version

from catalog.models import Category, Tool
from compare.models import Comparison
from core.models.editorial import EditorialWorkflowMixin as Workflow
from guides.models import Guide, GuideItem, GuideSection
from prompts.models import Prompt
from usecases.models import UseCase

User = get_user_model()


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def refetch(obj):
    """``refresh_from_db()`` is unusable here: django-fsm's protected ``status``
    descriptor rejects the ``setattr`` it performs on a live instance."""
    return type(obj).objects.get(pk=obj.pk)


class RevisionCapture:
    """Records the revisions a single request produced."""

    def __init__(self):
        self.before = set(Revision.objects.values_list("id", flat=True))

    @property
    def revisions(self):
        return list(Revision.objects.exclude(id__in=self.before).order_by("id"))

    def single(self, test):
        revisions = self.revisions
        test.assertEqual(
            len(revisions),
            1,
            f"expected exactly one revision, got {[r.comment for r in revisions]}",
        )
        return revisions[0]


def labels_in(revision):
    return sorted(
        f"{v.content_type.app_label}.{v.content_type.model}"
        for v in revision.version_set.select_related("content_type")
    )


def payloads(revision, label):
    """Every serialized ``fields`` dict of one content type in ``revision``."""
    app_label, model_name = label.split(".")
    out = []
    for version in revision.version_set.select_related("content_type"):
        ct = version.content_type
        if ct.app_label == app_label and ct.model == model_name:
            out.append(json.loads(version.serialized_data)[0]["fields"])
    return out


def payload_for(revision, label, object_id):
    app_label, model_name = label.split(".")
    for version in revision.version_set.select_related("content_type"):
        ct = version.content_type
        if (
            ct.app_label == app_label
            and ct.model == model_name
            and version.object_id == str(object_id)
        ):
            return json.loads(version.serialized_data)[0]["fields"]
    return None


def object_ids(revision, label):
    app_label, model_name = label.split(".")
    return sorted(
        int(v.object_id)
        for v in revision.version_set.select_related("content_type")
        if v.content_type.app_label == app_label and v.content_type.model == model_name
    )


def translations_by_language(revision, label):
    return {p["language_code"]: p for p in payloads(revision, label)}


class EditorialGraphTestCase(TestCase):
    """Shared users and catalog fixtures.

    ``author`` is a separate user so the superuser can run ``approve``:
    ``core.authz`` grants it as ``is_editor & ~is_author``, i.e. an author may
    never approve their own content.
    """

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("b1-admin", "b1a@example.com", "pw")
        cls.author = User.objects.create_user(
            "b1-author", "b1b@example.com", "pw", is_staff=True
        )
        Group.objects.get_or_create(name="Author")[0].user_set.add(cls.author)

        cls.tool_a = Tool.objects.create(slug="b1-tool-a")
        cls.tool_a.create_translation("en", name="Tool A")
        cls.tool_b = Tool.objects.create(slug="b1-tool-b")
        cls.tool_b.create_translation("en", name="Tool B")
        cls.category = Category.objects.create()
        cls.category.create_translation("en", name="Cat", slug="b1-cat")

    def setUp(self):
        self.client.force_login(self.admin)

    def run_action(self, changelist_url, action_name, pk):
        return self.client.post(
            changelist_url,
            data={"action": action_name, "_selected_action": [str(pk)], "index": "0"},
            follow=True,
        )


# ======================================================================
# Guide
# ======================================================================


class GuideRevisionGraphTests(EditorialGraphTestCase):
    """
    Guide is the deepest graph: Guide -> sections -> items, each with its own
    Parler translations.

    ``GuideItem`` rows are created through the ORM rather than a form because
    ``GuideAdmin`` has no item inline - items are edited through
    ``GuideSectionAdmin``, and B1 must not restructure the admin. The point
    under test is unaffected: what matters is that a *root* Guide admin request
    pulls the whole existing graph into its revision, whoever created the rows.
    """

    def _add_payload(self, **overrides):
        data = {
            "author": str(self.author.pk),
            "review_note": "",
            "published_at_0": "",
            "published_at_1": "",
            "categories": [str(self.category.pk)],
            "tools": [str(self.tool_a.pk)],
            "slug": "b1-guide-en",
            "title": "Guide EN",
            "intro": "intro en",
            "body": "body en",
            "sections-TOTAL_FORMS": "1",
            "sections-INITIAL_FORMS": "0",
            "sections-MIN_NUM_FORMS": "0",
            "sections-MAX_NUM_FORMS": "1000",
            "sections-0-id": "",
            "sections-0-guide": "",
            "sections-0-order": "0",
            "sections-0-title": "Section EN",
            "sections-0-body": "section body en",
            "_continue": "Save",
        }
        data.update(overrides)
        return data

    def _change_payload(self, guide, sections, **overrides):
        data = {
            "author": str(self.author.pk),
            "review_note": "",
            "published_at_0": "",
            "published_at_1": "",
            "categories": [str(self.category.pk)],
            "tools": [str(self.tool_a.pk)],
            "slug": guide.safe_translation_getter("slug", language_code="en") or "",
            "title": guide.safe_translation_getter("title", language_code="en") or "",
            "intro": guide.safe_translation_getter("intro", language_code="en") or "",
            "body": guide.safe_translation_getter("body", language_code="en") or "",
            "sections-TOTAL_FORMS": str(len(sections)),
            "sections-INITIAL_FORMS": str(len([s for s in sections if s.get("id")])),
            "sections-MIN_NUM_FORMS": "0",
            "sections-MAX_NUM_FORMS": "1000",
            "_continue": "Save",
        }
        for index, section in enumerate(sections):
            for key, value in section.items():
                data[f"sections-{index}-{key}"] = value
        data.update(overrides)
        return data

    def setUp(self):
        super().setUp()
        self.add_url = reverse("admin:guides_guide_add")
        self.changelist = reverse("admin:guides_guide_changelist")

        # EN parent + EN section through the real root add form.
        self.client.post(self.add_url, data=self._add_payload())
        self.guide = Guide.objects.order_by("-pk").first()
        self.section = self.guide.sections.get()
        self.change_url = reverse("admin:guides_guide_change", args=[self.guide.pk])

        # DE parent + DE section through the real root change form.
        self.client.post(
            self.change_url + "?language=de",
            data=self._change_payload(
                self.guide,
                [
                    {
                        "id": str(self.section.pk),
                        "guide": str(self.guide.pk),
                        "order": "0",
                        "title": "Section DE",
                        "body": "section body de",
                    }
                ],
                slug="b1-guide-de",
                title="Guide DE",
                intro="intro de",
                body="body de",
            ),
        )

        # Items: no root-admin path exists, see the class docstring.
        self.item = GuideItem.objects.create(
            section=self.section, kind="guide", order=0, url="https://example.com/a"
        )
        self.item.create_translation("en", title="Item EN", teaser="teaser en")
        self.item.create_translation("de", title="Item DE", teaser="teaser de")

    def _en_sections(self, **overrides):
        section = {
            "id": str(self.section.pk),
            "guide": str(self.guide.pk),
            "order": "0",
            "title": "Section EN",
            "body": "section body en",
        }
        section.update(overrides)
        return [section]

    # -- Phase 8: the full graph ---------------------------------------

    def test_root_change_request_writes_the_whole_guide_graph(self):
        capture = RevisionCapture()
        response = self.client.post(
            self.change_url,
            data=self._change_payload(
                self.guide, self._en_sections(), title="Guide EN changed"
            ),
        )
        self.assertEqual(response.status_code, 302)
        revision = capture.single(self)

        self.assertEqual(
            labels_in(revision),
            [
                "guides.guide",
                "guides.guideitem",
                "guides.guideitemtranslation",
                "guides.guideitemtranslation",
                "guides.guidesection",
                "guides.guidesectiontranslation",
                "guides.guidesectiontranslation",
                "guides.guidetranslation",
                "guides.guidetranslation",
            ],
        )

    def test_submit_for_review_revision_contains_every_translation(self):
        capture = RevisionCapture()
        self.run_action(self.changelist, "action_submit_for_review", self.guide.pk)
        revision = capture.single(self)

        self.assertEqual(refetch(self.guide).status, Workflow.STATUS_REVIEW)

        guide_translations = translations_by_language(revision, "guides.guidetranslation")
        self.assertEqual(sorted(guide_translations), ["de", "en"])
        self.assertEqual(guide_translations["en"]["title"], "Guide EN")
        self.assertEqual(guide_translations["de"]["title"], "Guide DE")
        self.assertEqual(guide_translations["en"]["slug"], "b1-guide-en")

        section_translations = translations_by_language(
            revision, "guides.guidesectiontranslation"
        )
        self.assertEqual(sorted(section_translations), ["de", "en"])
        self.assertEqual(section_translations["en"]["title"], "Section EN")
        self.assertEqual(section_translations["de"]["body"], "section body de")

        item_translations = translations_by_language(
            revision, "guides.guideitemtranslation"
        )
        self.assertEqual(sorted(item_translations), ["de", "en"])
        self.assertEqual(item_translations["en"]["title"], "Item EN")
        self.assertEqual(item_translations["de"]["teaser"], "teaser de")

    def test_submit_revision_carries_structure_and_plain_m2m_membership(self):
        capture = RevisionCapture()
        self.run_action(self.changelist, "action_submit_for_review", self.guide.pk)
        revision = capture.single(self)

        guide = payload_for(revision, "guides.guide", self.guide.pk)
        self.assertEqual(guide["categories"], [self.category.pk])
        self.assertEqual(guide["tools"], [self.tool_a.pk])

        section = payload_for(revision, "guides.guidesection", self.section.pk)
        self.assertEqual(section["guide"], self.guide.pk)
        self.assertEqual(section["order"], 0)

        item = payload_for(revision, "guides.guideitem", self.item.pk)
        self.assertEqual(item["section"], self.section.pk)
        self.assertEqual(item["order"], 0)
        self.assertEqual(item["url"], "https://example.com/a")

    # -- Phase 9: graph changes ----------------------------------------

    def test_changing_section_text_through_the_root_inline_lands_in_the_revision(self):
        capture = RevisionCapture()
        self.client.post(
            self.change_url,
            data=self._change_payload(
                self.guide, self._en_sections(title="Section EN edited", body="edited")
            ),
        )
        revision = capture.single(self)

        section_en = translations_by_language(
            revision, "guides.guidesectiontranslation"
        )["en"]
        self.assertEqual(section_en["title"], "Section EN edited")
        self.assertEqual(section_en["body"], "edited")

    def test_changing_item_text_shows_up_in_the_next_root_revision(self):
        self.item.set_current_language("en")
        self.item.title = "Item EN edited"
        self.item.save()

        capture = RevisionCapture()
        self.run_action(self.changelist, "action_submit_for_review", self.guide.pk)
        revision = capture.single(self)

        self.assertEqual(
            translations_by_language(revision, "guides.guideitemtranslation")["en"][
                "title"
            ],
            "Item EN edited",
        )

    def test_reordering_sections_and_items_lands_in_the_revision(self):
        self.item.order = 7
        self.item.save()

        capture = RevisionCapture()
        self.client.post(
            self.change_url,
            data=self._change_payload(self.guide, self._en_sections(order="3")),
        )
        revision = capture.single(self)

        self.assertEqual(
            payload_for(revision, "guides.guidesection", self.section.pk)["order"], 3
        )
        self.assertEqual(
            payload_for(revision, "guides.guideitem", self.item.pk)["order"], 7
        )

    def test_added_section_and_item_appear_in_the_next_revision(self):
        extra_item = GuideItem.objects.create(
            section=self.section, kind="guide", order=1, url="https://example.com/b"
        )
        extra_item.create_translation("en", title="Item EN 2", teaser="")

        capture = RevisionCapture()
        self.client.post(
            self.change_url,
            data=self._change_payload(
                self.guide,
                self._en_sections()
                + [
                    {
                        "id": "",
                        "guide": str(self.guide.pk),
                        "order": "1",
                        "title": "Section EN 2",
                        "body": "second",
                    }
                ],
            ),
        )
        revision = capture.single(self)

        new_section = self.guide.sections.order_by("order").last()
        self.assertEqual(
            object_ids(revision, "guides.guidesection"),
            sorted([self.section.pk, new_section.pk]),
        )
        self.assertIn(extra_item.pk, object_ids(revision, "guides.guideitem"))
        self.assertEqual(
            sorted(
                p["title"]
                for p in payloads(revision, "guides.guidesectiontranslation")
            ),
            ["Section DE", "Section EN", "Section EN 2"],
        )

    def test_deleted_item_and_section_are_absent_from_the_next_revision(self):
        """Reversion writes no delete marker; a removal is representable only as
        absence in the following revision. B1 documents that limit rather than
        working around it."""
        deleted_item_pk = self.item.pk
        self.item.delete()

        capture = RevisionCapture()
        self.client.post(
            self.change_url,
            data=self._change_payload(
                self.guide, self._en_sections(DELETE="on")
            ),
        )
        revision = capture.single(self)

        self.assertEqual(self.guide.sections.count(), 0)
        self.assertEqual(object_ids(revision, "guides.guidesection"), [])
        self.assertEqual(object_ids(revision, "guides.guideitem"), [])
        self.assertEqual(payloads(revision, "guides.guidesectiontranslation"), [])
        self.assertNotIn(
            deleted_item_pk, object_ids(revision, "guides.guideitem")
        )


# ======================================================================
# Prompt
# ======================================================================


class PromptRevisionGraphTests(EditorialGraphTestCase):
    def _payload(self, prompt=None, **overrides):
        data = {
            "author": str(self.author.pk),
            "review_note": "",
            "published_at_0": "",
            "published_at_1": "",
            "tools": [str(self.tool_a.pk)],
            "slug": "b1-prompt-en",
            "title": "Prompt EN",
            "intro": "i",
            "body": "b",
            "outro": "o",
            "_continue": "Save",
        }
        if prompt is not None:
            data.update(
                {
                    "slug": prompt.safe_translation_getter("slug", language_code="en"),
                    "title": prompt.safe_translation_getter("title", language_code="en"),
                    "intro": prompt.safe_translation_getter("intro", language_code="en"),
                    "body": prompt.safe_translation_getter("body", language_code="en"),
                    "outro": prompt.safe_translation_getter("outro", language_code="en"),
                }
            )
        data.update(overrides)
        return data

    def setUp(self):
        super().setUp()
        self.changelist = reverse("admin:prompts_prompt_changelist")
        self.client.post(reverse("admin:prompts_prompt_add"), data=self._payload())
        self.prompt = Prompt.objects.order_by("-pk").first()
        self.change_url = reverse("admin:prompts_prompt_change", args=[self.prompt.pk])
        self.client.post(
            self.change_url + "?language=de",
            data=self._payload(
                self.prompt,
                slug="b1-prompt-de",
                title="Prompt DE",
                intro="id",
                body="bd",
                outro="od",
            ),
        )
        self.prompt.tags.add("alpha")

    def test_submit_revision_contains_parent_and_every_translation(self):
        capture = RevisionCapture()
        self.run_action(self.changelist, "action_submit_for_review", self.prompt.pk)
        revision = capture.single(self)

        self.assertEqual(
            labels_in(revision),
            ["prompts.prompt", "prompts.prompttranslation", "prompts.prompttranslation"],
        )
        translations = translations_by_language(revision, "prompts.prompttranslation")
        self.assertEqual(sorted(translations), ["de", "en"])
        self.assertEqual(translations["en"]["title"], "Prompt EN")
        self.assertEqual(translations["de"]["outro"], "od")

    def test_submit_revision_carries_tool_membership_but_not_tags(self):
        capture = RevisionCapture()
        self.run_action(self.changelist, "action_submit_for_review", self.prompt.pk)
        revision = capture.single(self)

        parent = payload_for(revision, "prompts.prompt", self.prompt.pk)
        self.assertEqual(parent["tools"], [self.tool_a.pk])
        # Deliberately still outside the graph in B1 - see
        # core.reversion_registration.DEFERRED_EDITORIAL_RELATIONS.
        self.assertNotIn("tags", parent)
        self.assertEqual(list(refetch(self.prompt).tags.names()), ["alpha"])

    def test_tool_membership_change_lands_in_the_parent_version(self):
        capture = RevisionCapture()
        self.client.post(
            self.change_url,
            data=self._payload(self.prompt, tools=[str(self.tool_b.pk)]),
        )
        revision = capture.single(self)
        self.assertEqual(
            payload_for(revision, "prompts.prompt", self.prompt.pk)["tools"],
            [self.tool_b.pk],
        )


# ======================================================================
# Use Case
# ======================================================================


class UseCaseRevisionGraphTests(EditorialGraphTestCase):
    def _payload(self, usecase=None, **overrides):
        data = {
            "author": str(self.author.pk),
            "review_note": "",
            "published_at_0": "",
            "published_at_1": "",
            "tools": [str(self.tool_a.pk)],
            "slug": "b1-uc-en",
            "title": "UC EN",
            "intro": "i",
            "body": "b",
            "outro": "o",
            "_continue": "Save",
        }
        if usecase is not None:
            for name in ("slug", "title", "intro", "body", "outro"):
                data[name] = usecase.safe_translation_getter(name, language_code="en")
        data.update(overrides)
        return data

    def setUp(self):
        super().setUp()
        self.changelist = reverse("admin:usecases_usecase_changelist")
        self.client.post(reverse("admin:usecases_usecase_add"), data=self._payload())
        self.usecase = UseCase.objects.order_by("-pk").first()
        self.change_url = reverse(
            "admin:usecases_usecase_change", args=[self.usecase.pk]
        )
        self.client.post(
            self.change_url + "?language=de",
            data=self._payload(
                self.usecase,
                slug="b1-uc-de",
                title="UC DE",
                intro="id",
                body="bd",
                outro="od",
            ),
        )
        # persona is not on UseCaseAdmin's fieldsets (a separate, known defect
        # from the Beta 11.11A audit, explicitly out of scope for B1), so it is
        # written programmatically here. The registration must still capture it.
        for language, value in (("en", "Persona EN"), ("de", "Persona DE")):
            translation = self.usecase.translations.get(language_code=language)
            translation.persona = value
            translation.save()

    def test_submit_revision_contains_parent_and_every_translation(self):
        capture = RevisionCapture()
        self.run_action(self.changelist, "action_submit_for_review", self.usecase.pk)
        revision = capture.single(self)

        self.assertEqual(
            labels_in(revision),
            [
                "usecases.usecase",
                "usecases.usecasetranslation",
                "usecases.usecasetranslation",
            ],
        )
        translations = translations_by_language(revision, "usecases.usecasetranslation")
        self.assertEqual(sorted(translations), ["de", "en"])
        self.assertEqual(translations["en"]["title"], "UC EN")
        self.assertEqual(translations["de"]["slug"], "b1-uc-de")

    def test_submit_revision_contains_persona_per_language(self):
        capture = RevisionCapture()
        self.run_action(self.changelist, "action_submit_for_review", self.usecase.pk)
        revision = capture.single(self)

        translations = translations_by_language(revision, "usecases.usecasetranslation")
        self.assertEqual(translations["en"]["persona"], "Persona EN")
        self.assertEqual(translations["de"]["persona"], "Persona DE")

    def test_submit_revision_carries_tool_membership(self):
        capture = RevisionCapture()
        self.run_action(self.changelist, "action_submit_for_review", self.usecase.pk)
        revision = capture.single(self)
        self.assertEqual(
            payload_for(revision, "usecases.usecase", self.usecase.pk)["tools"],
            [self.tool_a.pk],
        )


# ======================================================================
# Comparison
# ======================================================================


class ComparisonRevisionGraphTests(EditorialGraphTestCase):
    """
    The type the audit found worst off: before B1 a Comparison revision held
    nothing but workflow fields and bare entry rows.
    """

    def _entry_forms(self, existing, new=()):
        rows = list(existing) + list(new)
        data = {
            "tool_entries-TOTAL_FORMS": str(len(rows)),
            "tool_entries-INITIAL_FORMS": str(len(existing)),
            "tool_entries-MIN_NUM_FORMS": "0",
            "tool_entries-MAX_NUM_FORMS": "1000",
        }
        for index, row in enumerate(rows):
            for key, value in row.items():
                data[f"tool_entries-{index}-{key}"] = value
        return data

    def _payload(self, comparison=None, entries=None, new_entries=(), **overrides):
        data = {
            "author": str(self.author.pk),
            "reviewed_by": "",
            "reviewed_at_0": "",
            "reviewed_at_1": "",
            "review_note": "",
            "published_at_0": "",
            "published_at_1": "",
            "slug": "b1-cmp-en",
            "title": "CMP EN",
            "intro": "i",
            "body": "b",
            "_continue": "Save",
        }
        if comparison is not None:
            for name in ("slug", "title", "intro", "body"):
                data[name] = comparison.safe_translation_getter(
                    name, language_code="en"
                )
        data.update(self._entry_forms(entries or [], new_entries))
        data.update(overrides)
        return data

    def _en_entry(self, **overrides):
        row = {
            "id": str(self.entry.pk),
            "comparison": str(self.comparison.pk),
            "tool": str(self.tool_a.pk),
            "position": "0",
            "label": "Entry EN",
            "summary": "summary en",
            "pros": "pros en",
            "cons": "cons en",
            "special": "special en",
        }
        row.update(overrides)
        return row

    def setUp(self):
        super().setUp()
        self.changelist = reverse("admin:compare_comparison_changelist")
        self.client.post(
            reverse("admin:compare_comparison_add"),
            data=self._payload(
                new_entries=[
                    {
                        "id": "",
                        "comparison": "",
                        "tool": str(self.tool_a.pk),
                        "position": "0",
                        "label": "Entry EN",
                        "summary": "summary en",
                        "pros": "pros en",
                        "cons": "cons en",
                        "special": "special en",
                    }
                ]
            ),
        )
        self.comparison = Comparison.objects.order_by("-pk").first()
        self.entry = self.comparison.tool_entries.get()
        self.change_url = reverse(
            "admin:compare_comparison_change", args=[self.comparison.pk]
        )
        self.client.post(
            self.change_url + "?language=de",
            data=self._payload(
                self.comparison,
                entries=[
                    {
                        "id": str(self.entry.pk),
                        "comparison": str(self.comparison.pk),
                        "tool": str(self.tool_a.pk),
                        "position": "0",
                        "label": "Entry DE",
                        "summary": "summary de",
                        "pros": "pros de",
                        "cons": "cons de",
                        "special": "special de",
                    }
                ],
                slug="b1-cmp-de",
                title="CMP DE",
                intro="id",
                body="bd",
            ),
        )

    # -- Phase 8 --------------------------------------------------------

    def test_submit_revision_contains_the_full_comparison_graph(self):
        capture = RevisionCapture()
        self.run_action(self.changelist, "action_submit_for_review", self.comparison.pk)
        revision = capture.single(self)

        self.assertEqual(
            labels_in(revision),
            [
                "compare.comparison",
                "compare.comparisontoolentry",
                "compare.comparisontoolentrytranslation",
                "compare.comparisontoolentrytranslation",
                "compare.comparisontranslation",
                "compare.comparisontranslation",
            ],
        )

        parent_translations = translations_by_language(
            revision, "compare.comparisontranslation"
        )
        self.assertEqual(sorted(parent_translations), ["de", "en"])
        self.assertEqual(parent_translations["en"]["title"], "CMP EN")
        self.assertEqual(parent_translations["de"]["intro"], "id")

        entry_translations = translations_by_language(
            revision, "compare.comparisontoolentrytranslation"
        )
        self.assertEqual(sorted(entry_translations), ["de", "en"])
        self.assertEqual(entry_translations["en"]["summary"], "summary en")
        self.assertEqual(entry_translations["de"]["pros"], "pros de")

    def test_membership_and_order_come_from_the_entry_row_not_a_parent_m2m(self):
        """``Comparison.tools`` is an explicit-through m2m, so Django's
        serializer never writes it onto the parent version. The entry graph
        carries the same information and is what the contract relies on."""
        capture = RevisionCapture()
        self.run_action(self.changelist, "action_submit_for_review", self.comparison.pk)
        revision = capture.single(self)

        parent = payload_for(revision, "compare.comparison", self.comparison.pk)
        self.assertNotIn("tools", parent)

        entry = payload_for(revision, "compare.comparisontoolentry", self.entry.pk)
        self.assertEqual(entry["tool"], self.tool_a.pk)
        self.assertEqual(entry["position"], 0)
        self.assertEqual(entry["comparison"], self.comparison.pk)

    # -- Phase 9 --------------------------------------------------------

    def test_entry_text_change_lands_in_the_revision(self):
        capture = RevisionCapture()
        self.client.post(
            self.change_url,
            data=self._payload(
                self.comparison, entries=[self._en_entry(summary="summary en edited")]
            ),
        )
        revision = capture.single(self)
        self.assertEqual(
            translations_by_language(revision, "compare.comparisontoolentrytranslation")[
                "en"
            ]["summary"],
            "summary en edited",
        )

    def test_entry_tool_swap_and_reposition_land_in_the_revision(self):
        capture = RevisionCapture()
        self.client.post(
            self.change_url,
            data=self._payload(
                self.comparison,
                entries=[self._en_entry(tool=str(self.tool_b.pk), position="5")],
            ),
        )
        revision = capture.single(self)
        entry = payload_for(revision, "compare.comparisontoolentry", self.entry.pk)
        self.assertEqual(entry["tool"], self.tool_b.pk)
        self.assertEqual(entry["position"], 5)

    def test_added_entry_appears_with_its_translation(self):
        capture = RevisionCapture()
        self.client.post(
            self.change_url,
            data=self._payload(
                self.comparison,
                entries=[self._en_entry()],
                new_entries=[
                    {
                        "id": "",
                        "comparison": str(self.comparison.pk),
                        "tool": str(self.tool_b.pk),
                        "position": "1",
                        "label": "Entry B EN",
                        "summary": "summary b",
                        "pros": "",
                        "cons": "",
                        "special": "",
                    }
                ],
            ),
        )
        revision = capture.single(self)

        self.assertEqual(self.comparison.tool_entries.count(), 2)
        self.assertEqual(
            object_ids(revision, "compare.comparisontoolentry"),
            sorted(self.comparison.tool_entries.values_list("pk", flat=True)),
        )
        summaries = sorted(
            p["summary"]
            for p in payloads(revision, "compare.comparisontoolentrytranslation")
        )
        self.assertEqual(summaries, ["summary b", "summary de", "summary en"])

    def test_deleted_entry_is_absent_from_the_next_revision(self):
        capture = RevisionCapture()
        self.client.post(
            self.change_url,
            data=self._payload(
                self.comparison, entries=[self._en_entry(DELETE="on")]
            ),
        )
        revision = capture.single(self)

        self.assertEqual(self.comparison.tool_entries.count(), 0)
        self.assertEqual(object_ids(revision, "compare.comparisontoolentry"), [])
        self.assertEqual(
            payloads(revision, "compare.comparisontoolentrytranslation"), []
        )


# ======================================================================
# Phase 10: the workflow must behave exactly as before
# ======================================================================


class WorkflowNonRegressionTests(EditorialGraphTestCase):
    """B1 widens the revision graph and nothing else."""

    def setUp(self):
        super().setUp()
        self.changelist = reverse("admin:usecases_usecase_changelist")
        self.client.post(
            reverse("admin:usecases_usecase_add"),
            data={
                "author": str(self.author.pk),
                "review_note": "",
                "published_at_0": "",
                "published_at_1": "",
                "tools": [str(self.tool_a.pk)],
                "slug": "b1-workflow-uc",
                "title": "Workflow UC",
                "intro": "i",
                "body": "b",
                "outro": "o",
                "_continue": "Save",
            },
        )
        self.usecase = UseCase.objects.order_by("-pk").first()

    def test_each_workflow_action_still_produces_exactly_one_revision(self):
        for action, expected_status in (
            ("action_submit_for_review", Workflow.STATUS_REVIEW),
            ("action_approve", Workflow.STATUS_APPROVED),
            ("action_publish", Workflow.STATUS_PUBLISHED),
        ):
            with self.subTest(action=action):
                capture = RevisionCapture()
                self.run_action(self.changelist, action, self.usecase.pk)
                capture.single(self)
                self.assertEqual(refetch(self.usecase).status, expected_status)

    def test_rework_still_works_and_produces_one_revision(self):
        self.run_action(self.changelist, "action_submit_for_review", self.usecase.pk)
        capture = RevisionCapture()
        self.run_action(self.changelist, "action_request_rework", self.usecase.pk)
        capture.single(self)
        self.assertEqual(refetch(self.usecase).status, Workflow.STATUS_REWORK)

    def test_publish_still_writes_the_live_snapshot(self):
        for action in ("action_submit_for_review", "action_approve", "action_publish"):
            self.run_action(self.changelist, action, self.usecase.pk)
        published = refetch(self.usecase)
        self.assertEqual(published.status, Workflow.STATUS_PUBLISHED)
        self.assertEqual(sorted(published.live_i18n), ["en"])
        self.assertEqual(published.live_i18n["en"]["title"], "Workflow UC")
        self.assertTrue(published.is_published)
        self.assertIsNotNone(published.published_at)

    def test_last_published_revision_id_still_holds_a_version_id(self):
        """Unchanged legacy semantics: despite its name the marker stores a
        ``Version.id``. B1 must not reinterpret or rename it - that is a later
        slice - so this pins the current contract."""
        for action in ("action_submit_for_review", "action_approve", "action_publish"):
            self.run_action(self.changelist, action, self.usecase.pk)

        marker = refetch(self.usecase).last_published_revision_id
        self.assertIsNotNone(marker)
        version = Version.objects.get(id=marker)
        self.assertEqual(version.content_type.model, "usecase")
        self.assertEqual(version.object_id, str(self.usecase.pk))

    def test_editing_during_review_still_does_not_invalidate_the_review(self):
        """KNOWN GAP, intentionally still open after B1.

        Beta 11.11A proved a reviewer can approve state X while state Y goes
        live. Closing it needs the review binding, fingerprint and invalidation
        of Beta 11.11B2+. This test records the unsafe status quo so that slice
        has to change it deliberately rather than by accident."""
        self.run_action(self.changelist, "action_submit_for_review", self.usecase.pk)
        self.assertEqual(refetch(self.usecase).status, Workflow.STATUS_REVIEW)

        self.client.post(
            reverse("admin:usecases_usecase_change", args=[self.usecase.pk]),
            data={
                "author": str(self.author.pk),
                "review_note": "",
                "published_at_0": "",
                "published_at_1": "",
                "tools": [str(self.tool_a.pk)],
                "slug": "b1-workflow-uc",
                "title": "Edited after submit",
                "intro": "i",
                "body": "b",
                "outro": "o",
                "_continue": "Save",
            },
        )
        self.assertEqual(refetch(self.usecase).status, Workflow.STATUS_REVIEW)

        self.run_action(self.changelist, "action_approve", self.usecase.pk)
        self.assertEqual(refetch(self.usecase).status, Workflow.STATUS_APPROVED)


class KnownWritePathLimitationsTests(EditorialGraphTestCase):
    """Write paths that still produce no revision after B1."""

    def setUp(self):
        super().setUp()
        self.guide = Guide.objects.create(author=self.author)
        self.guide.create_translation("en", title="T", intro="i", body="b", slug="b1-lim")
        self.section = GuideSection.objects.create(guide=self.guide, order=0)
        self.section.create_translation("en", title="Sec", body="Body")

    def test_guidesection_own_admin_still_saves_without_a_revision(self):
        """``GuideSectionAdmin`` is not a ``VersionAdmin``. B1 deliberately does
        not change that - it is Beta 11.11F's job - so the gap is asserted here
        instead of being assumed closed by the wider follow graph."""
        capture = RevisionCapture()
        response = self.client.post(
            reverse("admin:guides_guidesection_change", args=[self.section.pk]),
            data={
                "guide": str(self.guide.pk),
                "order": "0",
                "title": "Edited via child admin",
                "body": "Body",
                "items-TOTAL_FORMS": "0",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            GuideSection.objects.get(pk=self.section.pk).safe_translation_getter(
                "title", language_code="en"
            ),
            "Edited via child admin",
        )
        self.assertEqual(capture.revisions, [])

    def test_programmatic_save_still_needs_an_explicit_revision_context(self):
        capture = RevisionCapture()
        guide = refetch(self.guide)
        guide.is_starter = False
        guide.save()
        self.assertEqual(capture.revisions, [])


class EditorialAdminSmokeTests(EditorialGraphTestCase):
    """
    The four editorial admins still work end to end after the registration
    moved. Pre-registering the models means ``VersionAdmin.__init__()`` no
    longer registers anything, so this checks that everything it *does* provide
    - history, recover list, the change form with its inlines - is still wired
    up, and that Beta 11.10's draft-preview button is untouched.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.guide = Guide.objects.create(author=cls.author)
        cls.guide.create_translation(
            "en", title="Smoke Guide", intro="i", body="b", slug="b1-smoke-guide"
        )
        cls.prompt = Prompt.objects.create(author=cls.author)
        cls.prompt.create_translation(
            "en", title="Smoke Prompt", intro="i", body="b", outro="o",
            slug="b1-smoke-prompt",
        )
        cls.usecase = UseCase.objects.create(author=cls.author)
        cls.usecase.create_translation(
            "en", title="Smoke UC", intro="i", body="b", outro="o",
            persona="P", slug="b1-smoke-uc",
        )
        cls.comparison = Comparison.objects.create(author=cls.author)
        cls.comparison.create_translation(
            "en", title="Smoke CMP", intro="i", body="b", slug="b1-smoke-cmp"
        )

    def _objects(self):
        return (
            ("guides", "guide", self.guide),
            ("prompts", "prompt", self.prompt),
            ("usecases", "usecase", self.usecase),
            ("compare", "comparison", self.comparison),
        )

    def test_changelists_render(self):
        for app_label, model_name, _obj in self._objects():
            with self.subTest(model=f"{app_label}.{model_name}"):
                url = reverse(f"admin:{app_label}_{model_name}_changelist")
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_change_forms_render(self):
        for app_label, model_name, obj in self._objects():
            with self.subTest(model=f"{app_label}.{model_name}"):
                url = reverse(f"admin:{app_label}_{model_name}_change", args=[obj.pk])
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_version_history_and_recover_list_are_reachable(self):
        for app_label, model_name, obj in self._objects():
            with self.subTest(model=f"{app_label}.{model_name}"):
                history = reverse(
                    f"admin:{app_label}_{model_name}_history", args=[obj.pk]
                )
                self.assertEqual(self.client.get(history).status_code, 200)
                recover = reverse(f"admin:{app_label}_{model_name}_recoverlist")
                self.assertEqual(self.client.get(recover).status_code, 200)

    def test_draft_preview_button_context_is_unchanged(self):
        """Beta 11.10 contract, re-asserted because B1 touched app startup."""
        for app_label, model_name, obj in self._objects():
            with self.subTest(model=f"{app_label}.{model_name}"):
                url = reverse(f"admin:{app_label}_{model_name}_change", args=[obj.pk])
                context = self.client.get(url).context
                self.assertTrue(context["show_draft_preview"])
                self.assertEqual(context["draft_preview_language"], "en")

    def test_a_root_admin_change_still_produces_a_revision_with_inlines(self):
        capture = RevisionCapture()
        response = self.client.post(
            reverse("admin:compare_comparison_change", args=[self.comparison.pk]),
            data={
                "author": str(self.author.pk),
                "reviewed_by": "",
                "reviewed_at_0": "",
                "reviewed_at_1": "",
                "review_note": "",
                "published_at_0": "",
                "published_at_1": "",
                "slug": "b1-smoke-cmp",
                "title": "Smoke CMP edited",
                "intro": "i",
                "body": "b",
                "tool_entries-TOTAL_FORMS": "1",
                "tool_entries-INITIAL_FORMS": "0",
                "tool_entries-MIN_NUM_FORMS": "0",
                "tool_entries-MAX_NUM_FORMS": "1000",
                "tool_entries-0-id": "",
                "tool_entries-0-comparison": str(self.comparison.pk),
                "tool_entries-0-tool": str(self.tool_a.pk),
                "tool_entries-0-position": "0",
                "tool_entries-0-label": "Inline entry",
                "tool_entries-0-summary": "inline summary",
                "tool_entries-0-pros": "",
                "tool_entries-0-cons": "",
                "tool_entries-0-special": "",
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 302)
        revision = capture.single(self)
        self.assertEqual(self.comparison.tool_entries.count(), 1)
        self.assertIn("compare.comparisontoolentry", labels_in(revision))
        self.assertEqual(
            translations_by_language(
                revision, "compare.comparisontoolentrytranslation"
            )["en"]["summary"],
            "inline summary",
        )
