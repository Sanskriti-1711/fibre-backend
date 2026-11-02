from django.db import migrations, models


class Migration(migrations.Migration):
    """Stage 3 of the Field Supervision integration (INTEGRATION_PLAN.md).

    Additive on both sides: `source_lld_run_id` is nullable so every existing
    project (survey copies included) keeps NULL, and the `stage` choice list
    only gains a value — no stored row changes meaning.
    """

    dependencies = [
        ('projects', '0016_feature_approval_notes'),
    ]

    operations = [
        migrations.AddField(
            model_name='project',
            name='source_lld_run_id',
            field=models.CharField(blank=True, db_index=True, max_length=128, null=True),
        ),
        migrations.AlterField(
            model_name='stageevent',
            name='stage',
            field=models.CharField(
                choices=[
                    ('hld', 'HLD'),
                    ('survey', 'Survey'),
                    ('lld', 'LLD'),
                    ('supervision', 'Supervision'),
                ],
                max_length=12,
            ),
        ),
    ]
