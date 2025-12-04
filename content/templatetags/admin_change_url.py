# content/templatetags/admin_urls.py
from django import template
from django.urls import reverse

register = template.Library()


@register.simple_tag
def admin_change_url(obj):
    opts = obj._meta
    return reverse(f"admin:{opts.app_label}_{opts.model_name}_change", args=[obj.pk])
