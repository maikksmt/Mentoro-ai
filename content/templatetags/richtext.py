import bleach
from bleach.css_sanitizer import CSSSanitizer
from django import template
from django.utils.safestring import mark_safe

register = template.Library()

ALLOWED_TAGS = [
    "p",
    "br",
    "hr",
    "blockquote",
    "pre",
    "code",
    "span",
    "h2",
    "h3",
    "h4",
    "h5",
    "ul",
    "ol",
    "li",
    "strong",
    "em",
    "u",
    "s",
    "sub",
    "sup",
    "b",
    "i",
    "a",
    "img",
    "figure",
    "figcaption",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "div",
    "section",
    "article",
    "header",
    "footer",
    "nav",
]
ALLOWED_ATTRIBUTES = {
    "*": ["class", "id", "style", "data-*", "aria-*"],
    "a": ["href", "title", "rel", "target"],
    "img": ["src", "alt", "title", "width", "height", "loading"],
    "table": ["border", "cellpadding", "cellspacing"],
    "th": ["colspan", "rowspan"],
    "td": ["colspan", "rowspan"],
}
ALLOWED_PROTOCOLS = ["http", "https", "mailto", "data"]

css_sanitizer = CSSSanitizer(
    allowed_css_properties=[
        # Textdarstellung
        "color",
        "background-color",
        "text-align",
        "font-weight",
        "font-style",
        "text-decoration",
        "line-height",
        "letter-spacing",

        # Abstände
        "margin",
        "margin-top",
        "margin-right",
        "margin-bottom",
        "margin-left",
        "padding",
        "padding-top",
        "padding-right",
        "padding-bottom",
        "padding-left",

        # Größen
        "width",
        "height",
        "max-width",

        # Rahmen
        "border",
        "border-width",
        "border-style",
        "border-color",
        "border-radius",

        # Bilder
        "object-fit",

        # Tabellen
        "border-collapse",
        "border-spacing",
        "vertical-align",

        # Sonstiges (sicher)
        "display",  # inline, block – unkritisch
        "opacity",  # unkritisch
    ],
)


@register.filter(name="richtext")
def richtext(html: str) -> str:
    if not html:
        return ""
    cleaned = bleach.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        protocols=ALLOWED_PROTOCOLS,
        css_sanitizer=css_sanitizer,
        strip=True,
    )
    return mark_safe(cleaned)
