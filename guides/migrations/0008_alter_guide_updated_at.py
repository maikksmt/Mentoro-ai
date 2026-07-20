from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("guides", "0007_guide_starter_uniqueness"),
    ]

    operations = [
        migrations.AlterField(
            model_name="guide",
            name="updated_at",
            field=models.DateTimeField(auto_now=True),
        ),
    ]
