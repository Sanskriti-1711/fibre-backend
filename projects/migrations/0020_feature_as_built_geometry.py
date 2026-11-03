from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('projects', '0019_layerfieldconfig_tolerance'),
    ]

    operations = [
        migrations.AddField(
            model_name='feature',
            name='as_built_geometry',
            field=models.JSONField(blank=True, null=True),
        ),
    ]
