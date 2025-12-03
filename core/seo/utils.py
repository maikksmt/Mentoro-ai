# core/seo/utils.py
from html import unescape

from django.conf import settings
from django.templatetags.static import static
from django.urls import reverse
from django.utils.html import strip_tags
from django.utils.translation import override

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

    for code, lang_name in settings.LANGUAGES:
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

    default_lang = getattr(settings, "LANGUAGE_CODE", None)
    xdefault_alt = next((a for a in alts if a.lang == default_lang), None)
    if xdefault_alt:
        alts.append(AltHref(lang="x-default", url=xdefault_alt.url))
    else:
        # absolute Notfall-Variante
        alts.append(AltHref(lang="x-default", url=absolute_url(request.path)))

    return alts


def seo_text(value: str) -> str:
    """converts TinyMCE HTML to SEO-compatible raw text."""
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
