# core/views_i18n.py
from urllib.parse import urlparse

from django.conf import settings
from django.http import HttpResponseRedirect
from django.urls import Resolver404, resolve, reverse
from django.utils import translation
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import check_for_language
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_POST


def _safe_redirect_url(request, url: str) -> str | None:
    """
    Wraps Django's own django.views.i18n.set_language security contract:
    a candidate redirect target is only safe if it resolves to the current
    host and (when the request itself is secure) to https. Returns None for
    anything else - external hosts, protocol-relative URLs, userinfo tricks,
    backslash variants, control characters, etc. are all rejected by
    url_has_allowed_host_and_scheme() itself.
    """
    if url and url_has_allowed_host_and_scheme(
        url=url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return url
    return None


def _resolve_safe_next_url(request) -> str:
    """
    Determines a *safe* redirect target, mirroring django.views.i18n.set_language:
    1. POST 'next', if it passes the host/scheme check
    2. HTTP_REFERER, if it passes the same check
    3. '/' as the final, always-safe fallback

    This runs unconditionally, before any language validation - an invalid
    or unsupported `language` value must never bypass this and reach
    HttpResponseRedirect with unvalidated input.
    """
    safe = _safe_redirect_url(request, request.POST.get("next") or "")
    if safe:
        return safe
    safe_referer = _safe_redirect_url(request, request.META.get("HTTP_REFERER") or "")
    if safe_referer:
        return safe_referer
    return "/"


def _source_language_from_path(path: str) -> str | None:
    """
    Extracts the leading i18n_patterns() language prefix from `path`, if any
    of the project's configured LANGUAGES matches - e.g. "de" for
    "/de/glossary/foo/". Returns None for unprefixed paths (routes outside
    i18n_patterns), in which case resolve() needs no language override.
    """
    segment = path.strip("/").split("/", 1)[0]
    supported = {code for code, _ in settings.LANGUAGES}
    return segment if segment in supported else None


def _persist_language(request, lang_code: str):
    """
    Persistiert die Sprache wie die offizielle Django-View:
    - aktiviert die Sprache für die aktuelle Response
    - speichert sie in der Session (Schlüssel = LANGUAGE_COOKIE_NAME)
      oder alternativ als Cookie auf der Response
    """
    translation.activate(lang_code)
    # Session: gleiche Key-Namensgebung wie Cookie (Django macht das so)
    if hasattr(request, "session"):
        request.session[settings.LANGUAGE_COOKIE_NAME] = lang_code


def _attach_language_cookie(response, lang_code: str):
    """
    Hängt das Sprach-Cookie an die Response (Fallback/zusätzlich zur Session).
    Respektiert alle relevanten Settings.
    """
    response.set_cookie(
        settings.LANGUAGE_COOKIE_NAME,
        lang_code,
        max_age=getattr(settings, "LANGUAGE_COOKIE_AGE", None),
        path=getattr(settings, "LANGUAGE_COOKIE_PATH", "/"),
        domain=getattr(settings, "LANGUAGE_COOKIE_DOMAIN", None),
        secure=getattr(settings, "LANGUAGE_COOKIE_SECURE", False),
        httponly=getattr(settings, "LANGUAGE_COOKIE_HTTPONLY", False),
        samesite=getattr(settings, "LANGUAGE_COOKIE_SAMESITE", "Lax"),
    )


@require_POST
@cache_control(max_age=86400)
def set_language_smart(request):
    """
    Sprachwechsel mit intelligentem Redirect:
      - next/Referer werden wie in django.views.i18n.set_language ausschließlich
        gegen den eigenen Host/Scheme validiert (url_has_allowed_host_and_scheme);
        ein unsicheres Ziel fällt auf einen sicheren Referer oder zuletzt "/"
        zurück - unabhängig davon, ob die angeforderte Sprache gültig ist.
      - Der validierte Ausgangspfad wird unter der Sprache aufgelöst, die sein
        eigener i18n_patterns()-Prefix trägt (nicht die neue Zielsprache) -
        andernfalls würde Djangos LocalePrefixPattern (die nur die aktuell
        *aktive* Sprache matcht) einen "/en/..."-Pfad nach Aktivierung von "de"
        nicht mehr auflösen können.
      - Sprache setzen (Session/Cookie) - Wenn 'next' Glossary-Detail ist:
          (1) gleicher slug in Ziel-Sprache
          (2) sonst via translation_group passende Übersetzung
          (3) sonst Fallback: Glossary-Liste
      - Alle anderen Pfade: normaler Redirect auf 'next'
    """
    next_url = _resolve_safe_next_url(request)
    lang_code = (request.POST.get("language") or "").split("-", 1)[0]

    if not (lang_code and check_for_language(lang_code)):
        return HttpResponseRedirect(next_url)

    path = urlparse(next_url).path
    source_lang = _source_language_from_path(path)
    try:
        if source_lang:
            with translation.override(source_lang):
                match = resolve(path)
        else:
            match = resolve(path)
    except Resolver404:
        _persist_language(request, lang_code)
        resp = HttpResponseRedirect(next_url)
        _attach_language_cookie(resp, lang_code)
        return resp

    if match.app_name == "glossary" and match.url_name == "detail":
        slug = match.kwargs.get("slug")
        from glossary.models import GlossaryTerm
        target = GlossaryTerm.objects.filter(language=lang_code, slug=slug).first()

        if not target:
            current = (
                GlossaryTerm.objects
                .filter(slug=slug)
                .only("translation_group")
                .first()
            )
            if current and current.translation_group:
                target = GlossaryTerm.objects.filter(
                    translation_group=current.translation_group,
                    language=lang_code,
                ).first()

        _persist_language(request, lang_code)

        if target:
            with translation.override(lang_code):
                resp = HttpResponseRedirect(target.get_absolute_url())
                _attach_language_cookie(resp, lang_code)
                return resp

        with translation.override(lang_code):
            resp = HttpResponseRedirect(reverse("glossary:list"))
            _attach_language_cookie(resp, lang_code)
            return resp

    _persist_language(request, lang_code)
    resp = HttpResponseRedirect(next_url)
    _attach_language_cookie(resp, lang_code)
    return resp
