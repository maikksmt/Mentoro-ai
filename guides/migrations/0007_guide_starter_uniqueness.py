from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("guides", "0006_mark_historical_starter_guide"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="guide",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_starter", True), ("status", "published")),
                fields=("is_starter",),
                name="guide_single_published_starter",
                violation_error_message="Another guide is already published as the starter guide.",
            ),
        ),
    ]
