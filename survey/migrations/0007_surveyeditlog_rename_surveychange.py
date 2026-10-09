# Manual migration: rename survey.SurveyChange -> SurveyEditLog.
#
# Renaming (rather than delete+create) keeps the existing audit-trail rows.
# The related_name on the two FKs also changes survey_changes -> survey_edit_logs
# (a state-only change; no SQL is emitted for related_name).

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('projects', '0014_remove_surveychange_created_by_and_more'),
        ('survey', '0006_surveyfeature_is_removal'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RenameModel(
            old_name='SurveyChange',
            new_name='SurveyEditLog',
        ),
        migrations.AlterModelOptions(
            name='surveyeditlog',
            options={
                'ordering': ['-created_at'],
                'verbose_name': 'Survey Edit Log',
                'verbose_name_plural': 'Survey Edit Logs',
            },
        ),
        migrations.AlterField(
            model_name='surveyeditlog',
            name='engineer',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='survey_edit_logs',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name='surveyeditlog',
            name='feature',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='survey_edit_logs',
                to='projects.feature',
            ),
        ),
    ]
