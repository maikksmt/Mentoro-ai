# core/seo/utils.py
from html import unescape

from django.conf import settings
from django.templatetags.static import static
from django.urls import reverse
from django.utils.html import strip_tags
from django.utils.translation import override, get_language

from .types import AltHref


def absolute_url(path_or_url: str) -> str:
    if path_or_url.startswith("http"):
        return path_or_url
    base = getattr(settings, "SITE_URL", "").rstrip("/")
    return f"{base}/{path_or_url.lstrip('/')}"


def localized_alternates(
        request,
        url_name: str | None = None,
        kwargs: dict | None = None,
        obj: object | None = None,
) -> list[dict]:
    """
    Erzeugt hreflang-Alternates.

    - Wenn `obj` ein TranslatableModel mit `get_absolute_url(language=...)` ist,
      werden pro Sprache die sprachspezifischen URLs benutzt.
    - Fallback: `reverse(url_name, kwargs=...)` wie vorher.
    """
    alts: list[AltHref] = []

    for code, _ in settings.LANGUAGES:
        url: str | None = None

        if obj is not None and hasattr(obj, "get_absolute_url"):
            with override(code):
                try:
                    url = obj.get_absolute_url(language=code)
                except TypeError:
                    url = obj.get_absolute_url()  # type: ignore[call-arg]

        if url is None and url_name is not None:
            with override(code):
                url = reverse(url_name, kwargs=kwargs or {})

        if not url or url == "#":
            continue

        alts.append(AltHref(lang=code, url=absolute_url(url)))

    current_lang = getattr(request, "LANGUAGE_CODE", None) or get_language()
    current_alt = next((a for a in alts if a.lang == current_lang), None)

    if not current_alt:
        default_lang = getattr(settings, "LANGUAGE_CODE", None)
        current_alt = next((a for a in alts if a.lang == default_lang), None)

    if not current_alt and alts:
        current_alt = alts[0]

    if current_alt:
        alts.append(AltHref(lang="x-default", url=current_alt.url))
    else:
        alts.append(AltHref(lang="x-default", url=absolute_url(request.path)))

    return alts


def seo_text(value: str) -> str:
    """Konvertiert TinyMCE HTML in reinen SEO-kompatiblen Text."""
    if not value:
        return ""
    text = strip_tags(value)
    text = unescape(text)
    return " ".join(text.split())


def get_og_image(og_img: str | None = None) -> str:
    """
    Liefert eine absolute URL für og:image.
    - Wenn og_img übergeben wird: diese verwenden.
    - Sonst: globales Default-OG-Bild.
    """
    if og_img:
        return absolute_url(og_img)

    return absolute_url(static("img/og-default.png"))
