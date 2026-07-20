from django.db import migrations

# The pre-Beta-8.6 runtime identified the starter guide by this slug prefix
# (see the removed content.views.home.resolve_starter_guide_url). Any
# translation whose slug starts with this belongs to the historical starter.
HISTORICAL_STARTER_SLUG_PREFIX = "start-guide"


def mark_historical_starter(apps, schema_editor):
    Guide = apps.get_model("guides", "Guide")
    GuideTranslation = apps.get_model("guides", "GuideTranslation")

    translations = GuideTranslation.objects.filter(
        slug__startswith=HISTORICAL_STARTER_SLUG_PREFIX
    )
    guide_ids = set(translations.values_list("master_id", flat=True))

    if not guide_ids:
        return

    if len(guide_ids) > 1:
        matches = list(translations.values_list("master_id", "slug").order_by("master_id"))
        raise RuntimeError(
            "Beta 8.6 data migration guides.0006_mark_historical_starter_guide "
            f"found {len(guide_ids)} different Guide objects matching the historical "
            f"starter slug prefix {HISTORICAL_STARTER_SLUG_PREFIX!r}: {matches}. "
            "Refusing to guess which one is the real starter guide. Resolve this "
            "manually (set is_starter on the correct Guide, or remove the stale "
            "start-guide-* slug from the others) and re-run migrate."
        )

    guide_id = guide_ids.pop()
    Guide.objects.filter(pk=guide_id).update(is_starter=True)


def unmark_historical_starter(apps, schema_editor):
    Guide = apps.get_model("guides", "Guide")
    GuideTranslation = apps.get_model("guides", "GuideTranslation")

    guide_ids = set(
        GuideTranslation.objects.filter(
            slug__startswith=HISTORICAL_STARTER_SLUG_PREFIX
        ).values_list("master_id", flat=True)
    )
    if guide_ids:
        Guide.objects.filter(pk__in=guide_ids, is_starter=True).update(is_starter=False)


class Migration(migrations.Migration):

    dependencies = [
        ("guides", "0005_guide_is_starter"),
    ]

    operations = [
        migrations.RunPython(mark_historical_starter, unmark_historical_starter),
    ]
