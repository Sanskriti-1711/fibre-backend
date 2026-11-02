from django.db import migrations, models


class Migration(migrations.Migration):
    """Stage 4 of the Field Supervision integration (INTEGRATION_PLAN.md).

    ``ImportSession`` now records which source produced a project's features:
    the GeoPackage upload (existing rows, hence ``default='gpkg'``) or an LLD
    run handoff. ``source_ref`` keeps the run marker for audit. Both additive.
    """

    dependencies = [
        ('projects', '0017_project_source_lld_run_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='importsession',
            name='source_kind',
            field=models.CharField(
                choices=[('gpkg', 'GeoPackage / zip upload'), ('lld_run', 'LLD run handoff')],
                default='gpkg',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='importsession',
            name='source_ref',
            field=models.CharField(blank=True, default='', max_length=128),
        ),
    ]
