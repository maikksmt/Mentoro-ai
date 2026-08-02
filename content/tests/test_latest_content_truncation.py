"""
Beta 10.9 correction: "Aktuelle Inhalte" showed intros cut mid-word with no
"..." - e.g. "persoenlicher Assis", "bis zur individuel", "Assistent
beruecksichtig" - and adjacent rich-text blocks glued together
("moechten.Erfahre"). Root cause: core.services.teaser_for_guide() and its
prompt/usecase/comparison siblings pre-cut the text themselves with
strip_tags(src)[:160], a blind slice with no word-boundary check and no
marker. That text was already short enough that the card's own `summarize`
filter (fixed earlier in this same slice for guides/list.html and friends)
never touched it again - the bug lived entirely upstream of the card,
which is exactly why the fix that landed for the eleven direct call sites
never reached this path. This module renders the real "Aktuelle Inhalte"
homepage section end-to-end (real service, real template, real card
partial) rather than the isolated card partial, so a regression here can
only mean the teaser functions started pre-cutting again.
"""
import re

from django.test import TestCase
from django.utils import timezone, translation

from compare.models import Comparison
from core.models.editorial import EditorialWorkflowMixin
from guides.models import Guide
from prompts.models import Prompt
from usecases.models import UseCase

CARD_RE = re.compile(r'<article class="editorial-card[^"]*">.*?</article>', re.DOTALL)
INTRO_RE = re.compile(r'<div class="editorial-card-intro[^"]*"[^>]*>(.*?)</div>', re.DOTALL)

# The exact block-boundary shapes named in the correction: a plain paragraph
# break, and a <br> carrying the editor-emitted attributes real guide intros
# were confirmed to contain.
LONG_GUIDE_INTRO = (
    "<p>Ein praxisnaher Leitfaden für alle, die ihren Büroalltag smarter "
    "gestalten wollen – mit Künstlicher Intelligenz als persönlicher "
    "Assistentin.</p>"
    '<br data-start="749" data-end="752">'
    "<p>Lerne Schritt für Schritt, wie du mit KI Reisen individuell planst, "
    "Aufgaben automatisierst und dabei stets alle Wünsche berücksichtigst, "
    "auch bei komplizierten Anfragen und kurzfristigen Änderungen, die sonst "
    "viel zusätzliche Zeit kosten würden.</p>"
)
LONG_PROMPT_INTRO = (
    "<p>Möchten Sie Ihre Lernunterlagen automatisch zusammenfassen lassen?</p>"
    "<p>Erfahre mehr darüber, wie ein individuell zugeschnittener Prompt dir "
    "dabei hilft, Prüfungsstoff strukturiert aufzubereiten, ohne stundenlang "
    "selbst Notizen zu sortieren oder wichtige Zusammenhänge zu übersehen.</p>"
)
LONG_USECASE_INTRO = (
    "<p>Sandra, 35, Büroangestellte, schreibt jede Woche denselben "
    "Bericht.</p><p>Früher brauchte sie dafür zwei Stunden, heute ist er in "
    "zehn Minuten fertig, weil die KI Tabellen liest, Trends erkennt und "
    "einen klar formulierten Berichtsentwurf mit Handlungsempfehlungen "
    "erstellt.</p>"
)
LONG_COMPARISON_INTRO = (
    "<p>Zwei beliebte KI-Assistenten im direkten Vergleich für den "
    "Büroalltag.</p><p>Wir schauen uns Preis, Funktionsumfang, "
    "Datenschutzeinstellungen und Integration in bestehende Werkzeuge genau "
    "an, damit die Entscheidung leichter fällt, welches Werkzeug tatsächlich "
    "zu den eigenen Anforderungen passt.</p>"
)
SHORT_GUIDE_INTRO = "<p>Kurzer Einstieg.</p>"


def make_guide(slug, intro):
    g = Guide.objects.create(
        status=EditorialWorkflowMixin.STATUS_PUBLISHED, published_at=timezone.now()
    )
    g.create_translation("de", title=f"Titel {slug}", intro=intro, body="b", slug=slug)
    return g


def make_prompt(slug, intro):
    p = Prompt.objects.create(
        status=EditorialWorkflowMixin.STATUS_PUBLISHED, published_at=timezone.now()
    )
    p.create_translation("de", title=f"Titel {slug}", intro=intro, body="b", slug=slug)
    return p


def make_usecase(slug, intro):
    u = UseCase.objects.create(
        status=EditorialWorkflowMixin.STATUS_PUBLISHED, published_at=timezone.now()
    )
    u.create_translation(
        "de", title=f"Titel {slug}", intro=intro, body="b", outro="o", slug=slug, persona=""
    )
    return u


def make_comparison(slug, intro):
    c = Comparison.objects.create(
        status=EditorialWorkflowMixin.STATUS_PUBLISHED, published_at=timezone.now()
    )
    c.create_translation("de", title=f"Titel {slug}", intro=intro, body="b", slug=slug)
    return c


class LatestContentTruncationTests(TestCase):
    """Renders /de/ for real - the actual view, the actual template, the
    actual teaser pipeline - rather than the isolated card partial."""

    @classmethod
    def setUpTestData(cls):
        cls.guide = make_guide("lc-trunc-guide", LONG_GUIDE_INTRO)
        cls.prompt = make_prompt("lc-trunc-prompt", LONG_PROMPT_INTRO)
        cls.usecase = make_usecase("lc-trunc-usecase", LONG_USECASE_INTRO)
        cls.comparison = make_comparison("lc-trunc-comparison", LONG_COMPARISON_INTRO)
        cls.short_guide = make_guide("lc-trunc-short-guide", SHORT_GUIDE_INTRO)

    def setUp(self):
        self.addCleanup(translation.deactivate_all)

    def cards(self):
        translation.activate("de")
        html = self.client.get("/de/").content.decode()
        return html, CARD_RE.findall(html)

    def intro_of(self, cards, title):
        card = next(c for c in cards if title in c)
        match = INTRO_RE.search(card)
        self.assertIsNotNone(match, f"no .editorial-card-intro found for {title!r}")
        return card, match.group(1).strip()

    def test_long_guide_intro_ends_with_the_marker_on_a_whole_word(self):
        _, intros = self.cards()
        _, text = self.intro_of(intros, "Titel lc-trunc-guide")
        self.assertTrue(text.endswith("..."), text)
        self.assertFalse(text.endswith("...."), text)
        last_word = text[:-3].rstrip().rsplit(" ", 1)[-1]
        self.assertIn(last_word, LONG_GUIDE_INTRO)

    def test_long_prompt_intro_ends_with_the_marker_on_a_whole_word(self):
        _, intros = self.cards()
        _, text = self.intro_of(intros, "Titel lc-trunc-prompt")
        self.assertTrue(text.endswith("..."), text)
        last_word = text[:-3].rstrip().rsplit(" ", 1)[-1]
        self.assertIn(last_word, LONG_PROMPT_INTRO)

    def test_long_usecase_intro_ends_with_the_marker_on_a_whole_word(self):
        _, intros = self.cards()
        _, text = self.intro_of(intros, "Titel lc-trunc-usecase")
        self.assertTrue(text.endswith("..."), text)
        last_word = text[:-3].rstrip().rsplit(" ", 1)[-1]
        self.assertIn(last_word, LONG_USECASE_INTRO)

    def test_long_comparison_intro_ends_with_the_marker_on_a_whole_word(self):
        _, intros = self.cards()
        _, text = self.intro_of(intros, "Titel lc-trunc-comparison")
        self.assertTrue(text.endswith("..."), text)
        last_word = text[:-3].rstrip().rsplit(" ", 1)[-1]
        self.assertIn(last_word, LONG_COMPARISON_INTRO)

    def test_no_typographic_ellipsis_on_editorial_cards(self):
        _, intros = self.cards()
        for title in (
            "lc-trunc-guide", "lc-trunc-prompt", "lc-trunc-usecase", "lc-trunc-comparison",
        ):
            with self.subTest(title=title):
                _, text = self.intro_of(intros, f"Titel {title}")
                self.assertNotIn("…", text)

    def test_the_previously_reported_cut_words_do_not_reappear(self):
        # The exact defective endings observed in the browser before this
        # fix - a regression here means teaser_for_*() started pre-cutting
        # the text again. Matched as a whole word (\b...\b): "Assis" as a
        # *word-boundary* match would mean it stands alone, cut off from
        # "Assistentin" - the full word containing that substring must not
        # trip this, only a truncated remnant may.
        _, intros = self.cards()
        for title, bad_word in (
            ("lc-trunc-guide", "Assis"),
            ("lc-trunc-guide", "individuel"),
            ("lc-trunc-guide", "berücksichtig"),
        ):
            _, text = self.intro_of(intros, f"Titel {title}")
            with self.subTest(title=title, bad_word=bad_word):
                self.assertNotRegex(text, rf"\b{bad_word}\b")

    def test_the_previously_reported_glue_does_not_reappear(self):
        _, intros = self.cards()
        _, text = self.intro_of(intros, "Titel lc-trunc-prompt")
        self.assertNotIn("möchten.Erfahre", text)

    def test_adjacent_blocks_are_not_glued_together(self):
        _, intros = self.cards()
        for title in (
            "lc-trunc-guide", "lc-trunc-prompt", "lc-trunc-usecase", "lc-trunc-comparison",
        ):
            with self.subTest(title=title):
                _, text = self.intro_of(intros, f"Titel {title}")
                self.assertNotRegex(text, r"[a-zäöüß]\.[A-ZÄÖÜ]")

    def test_short_intro_gets_no_marker(self):
        _, intros = self.cards()
        _, text = self.intro_of(intros, "Titel lc-trunc-short-guide")
        self.assertEqual(text, "Kurzer Einstieg.")
        self.assertNotIn("...", text)

    def test_intro_element_carries_no_clamp_or_truncation_class(self):
        _, intros = self.cards()
        card, _ = self.intro_of(intros, "Titel lc-trunc-guide")
        intro_tag = re.search(r'<div class="([^"]*editorial-card-intro[^"]*)"', card)
        self.assertIsNotNone(intro_tag)
        classes = intro_tag.group(1).split()
        for forbidden in ("line-clamp-1", "line-clamp-2", "line-clamp-3", "line-clamp-4",
                          "line-clamp-5", "line-clamp-6", "truncate"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, classes)

    def test_intro_carries_no_raw_html(self):
        _, intros = self.cards()
        for title in ("lc-trunc-guide", "lc-trunc-prompt", "lc-trunc-usecase", "lc-trunc-comparison"):
            with self.subTest(title=title):
                _, text = self.intro_of(intros, f"Titel {title}")
                self.assertNotIn("<p>", text)
                self.assertNotIn("<br", text)
                self.assertNotIn("&amp;", text)

    def test_footer_link_title_badge_and_date_survive(self):
        _html, intros = self.cards()
        card, _ = self.intro_of(intros, "Titel lc-trunc-guide")
        self.assertIn("Weiterlesen", card)
        self.assertIn("badge-guide", card)
        self.assertIn(self.guide.get_absolute_url(), card)
        self.assertIn('datetime=', card)
