# core/context_processors.py
from django.conf import settings

NAV_SECTION_BY_APP = {
    "catalog": "catalog",
    "guides": "guides",
    "prompts": "prompts",
    "usecases": "usecases",
    "compare": "compare",
    "glossary": "glossary",
}


def account_signup_settings(request):
    return {
        "ACCOUNT_SIGNUP_ENABLED": getattr(settings, "ACCOUNT_SIGNUP_ENABLED", True),
    }


def nav_active_section(request):
    """Identifies which primary nav section (if any) the current request
    belongs to, based on the resolved URL rather than fragile path strings."""
    resolver_match = getattr(request, "resolver_match", None)
    if resolver_match is None:
        return {"nav_active_section": None}

    if resolver_match.view_name == "content:home":
        return {"nav_active_section": "home"}

    top_level_app = resolver_match.app_names[0] if resolver_match.app_names else None
    return {"nav_active_section": NAV_SECTION_BY_APP.get(top_level_app)}
