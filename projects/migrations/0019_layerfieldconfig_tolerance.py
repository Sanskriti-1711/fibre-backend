from django.db import migrations, models


class Migration(migrations.Migration):
    """Stage 5 of the Field Supervision integration (INTEGRATION_PLAN.md).

    Adds the per-layer tolerance the planned-vs-as-built comparison needs.
    Additive: every existing row keeps a 0 tolerance, which means "any
    difference is a deviation" — the strictest reading, so no previously
    accepted reading silently becomes a pass.
    """

    dependencies = [
        ('projects', '0018_importsession_source'),
    ]

    operations = [
        migrations.AddField(
            model_name='layerfieldconfig',
            name='tolerance',
            field=models.DecimalField(decimal_places=3, default=0, max_digits=10),
        ),
    ]
