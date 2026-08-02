# core/templatetags/i18n_next.py
from __future__ import annotations

from contextlib import suppress

from django import template
from django.core.exceptions import FieldError, ObjectDoesNotExist
from django.urls import NoReverseMatch, reverse
from django.utils.translation import override as lang_override
from parler.utils.context import switch_language

register = template.Library()


def _translated_slug_from_live(obj, lang: str) -> str | None:
    """
    Reads the slug from live_i18n[lang], if present.
    """
    live = getattr(obj, "live_i18n", None)
    if isinstance(live, dict):
        snap = live.get(lang) or {}
        return snap.get("public_slug") or snap.get("slug")


def _translated_slug_from_parler(obj, lang: str) -> str | None:
    """
    Fallback via Parler translations (safe_translation_getter).

    Beta 8.10: guarded by has_translation(lang) first - safe_translation_getter()
    under switch_language() silently returns the PARLER_LANGUAGES fallback
    translation's value (not None) when `lang` itself has no translation, so
    without this guard an EN-only object would produce a slug that belongs to
    a different language, combined with a lang_override(lang) URL prefix in
    _detail_url_for() - e.g. "/de/guides/<en-slug>/", a target that 404s
    under any strict language-aware detail view. Confirmed via reproduction
    for both Guide and Prompt.
    """
    has_translation = getattr(obj, "has_translation", None)
    if callable(has_translation) and not has_translation(lang):
        return None
    with switch_language(obj, lang):
        get = getattr(obj, "safe_translation_getter", None)
        if callable(get):
            meta = getattr(obj, "_parler_meta", None)
            has_public_slug = False
            has_slug = False
            if meta is not None:
                with suppress(FieldError):
                    meta.get_model_by_field("public_slug")
                    has_public_slug = True
                with suppress(FieldError):
                    meta.get_model_by_field("slug")
                    has_slug = True
            slug = None
            if has_public_slug:
                slug = get("public_slug")
            if not slug and has_slug:
                slug = get("slug")
            return slug


def _detail_url_for(obj, target_lang: str) -> str | None:
    """
    If possible, retrieve the detailed URL of the object in `target_lang`.
    - Glossary: via `translation_group` + language
    - Parler models (prompts/guides/usecases): `live_i18n` preferred, otherwise `parler`.
    """
    if obj.__class__.__name__ == "GlossaryTerm":
        try:
            Model = obj.__class__
            alt = (Model.objects
                   .filter(translation_group=obj.translation_group, language=target_lang)
                   .only("slug")
                   .first())
            with lang_override(target_lang):
                return reverse("glossary:detail", kwargs={"slug": alt.slug}) if alt else reverse("glossary:list")
        except Exception:  # noqa: BLE001 - a broken URL lookup must degrade to "no link", not crash the template
            return None

    slug = _translated_slug_from_live(obj, target_lang) or _translated_slug_from_parler(obj, target_lang)
    if slug:
        app = obj._meta.app_label  # "prompts", "guides", "usecases", ...
        with lang_override(target_lang):
            return reverse(f"{app}:detail", kwargs={"slug": slug})
    return None


@register.simple_tag(takes_context=True)
def i18n_next(context, language_code: str) -> str:
    """
    Provides the target URL for language switching.
    - Detail pages: Detail URL in target_lang (otherwise list fallback)
    - Lists/other pages: Change path prefix (/de/… <-> /en/…)
    """
    request = context.get("request")
    if not request:
        return "/"

    obj = context.get("object")
    if obj is None:
        for key in ("term",):
            if key in context:
                obj = context.get(key)
                break

    if obj is not None:
        url = _detail_url_for(obj, language_code)
        if url:
            return url
        if hasattr(obj, "get_absolute_url"):
            try:
                return obj.get_absolute_url(language=language_code)  # z.B. prompts/guides
            except TypeError:
                with lang_override(language_code):
                    return obj.get_absolute_url()
            except (ObjectDoesNotExist, NoReverseMatch):
                # No translation in language_code (e.g. Comparison/UseCase,
                # whose get_absolute_url() raises rather than returning "#"
                # like Guide/Prompt do) - fall through to the generic
                # prefix-swap below instead of crashing the whole page.
                pass

    path = request.get_full_path()
    parts = path.split("/", 2)  # ["", "de", "rest..."]
    if len(parts) > 1 and parts[1] in ("de", "en"):
        parts[1] = language_code
        return "/".join(parts)
    return path
