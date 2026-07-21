"""
Turning stored rich text into short, plain card summaries.

Editorial cards across guides, prompts, use cases and comparisons all show a
teaser of a rich-text field. This module is the single place that decides how
such a field becomes a short line of readable text, so the rule lives in one
testable function instead of in every calling template.
"""
import html as html_entities
import re

import bleach

#: The uniform intro length for editorial cards.
#:
#: This is not a newly invented number. Before this change every call site used
#: ``truncatewords_html:30``; measured against the existing guide intros that
#: produced a median of 199 and a mean of 180 visible characters. 200 keeps the
#: length editors already write for, while replacing a word count - whose
#: rendered width swings with word length - by a limit that actually bounds the
#: line.
EDITORIAL_INTRO_MAX_CHARS = 200

#: Three periods, deliberately not the typographic ellipsis, so the marker is
#: identical everywhere and greppable in tests.
ELLIPSIS = "..."

# ``<br>`` is a void element: the sanitiser removes it without leaving any
# whitespace, so "Line one<br>Line two" would collapse into "Line oneLine two".
# Block elements do not have that problem - the sanitiser already emits a
# newline between them. This normalises one known void element; it is not an
# attempt to parse HTML with a regular expression.
#
# The attribute part is not decoration: the stored content really does carry
# tags like `<br data-start="749" data-end="752">` from the editor it was
# pasted out of, and matching only the bare tag glued "assistant." to "Learn".
_LINE_BREAK = re.compile(r"<br\b[^>]*>", re.IGNORECASE)

# Trailing punctuation left dangling by the cut would read as ".." or ",..."
_DANGLING = " \t\n\r,;:.!?-–—…"


def visible_text(value) -> str:
    """
    Returns only the human-visible text of a stored rich-text value.

    Tags are removed with bleach - the sanitiser the ``richtext`` filter
    already relies on - rather than with a regular expression. Entities are
    then decoded, so that Django's autoescaping escapes them exactly once on
    output instead of rendering ``&amp;`` as ``&amp;amp;``.

    The result is always a plain ``str``: never a ``SafeString``, so a caller
    cannot accidentally inject markup by piping this through ``|safe``.
    """
    if not value:
        return ""
    with_breaks = _LINE_BREAK.sub(" ", str(value))
    stripped = bleach.clean(with_breaks, tags=[], attributes={}, strip=True)
    # split()/join() collapses the newlines the sanitiser leaves at block
    # boundaries, along with any authored indentation, into single spaces.
    return " ".join(html_entities.unescape(stripped).split())


def truncate_at_word_boundary(
    text: str, max_chars: int = EDITORIAL_INTRO_MAX_CHARS
) -> str:
    """
    Shortens *text* to at most *max_chars* characters without splitting a word.

    Appends exactly ``...`` when something was dropped, and nothing at all when
    the text already fits.
    """
    if len(text) <= max_chars:
        return text

    window = text[:max_chars]
    boundary = window.rfind(" ")
    if boundary > 0:
        kept = window[:boundary]
    else:
        # The first word is itself longer than the limit, so there is no
        # boundary to retreat to. Splitting it is the one thing this function
        # must never do, so the limit yields and the word is kept whole; the
        # card lets such a token wrap rather than overflow.
        first_space = text.find(" ")
        if first_space == -1:
            return text
        kept = text[:first_space]
    return kept.rstrip(_DANGLING) + ELLIPSIS


def summarize_html(value, max_chars: int = EDITORIAL_INTRO_MAX_CHARS) -> str:
    """Rich text in, short plain-text summary out."""
    return truncate_at_word_boundary(visible_text(value), max_chars)
