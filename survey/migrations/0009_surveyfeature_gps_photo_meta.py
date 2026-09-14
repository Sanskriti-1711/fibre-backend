"""Tier-1 A1/A2 fields on SurveyFeature: GPS capture quality + photo tags."""
import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('survey', '0008_approvalrecord'),
    ]

    operations = [
        migrations.AddField(
            model_name='surveyfeature',
            name='gps_accuracy_m',
            field=models.FloatField(
                blank=True,
                help_text='Device-reported horizontal accuracy (metres) at capture time',
                null=True,
            ),
        ),
        migrations.AddField(
            model_name='surveyfeature',
            name='gps_quality',
            field=models.CharField(
                blank=True,
                choices=[
                    ('ok', 'OK (within requirement)'),
                    ('warn', 'Warn (degraded, allowed)'),
                    ('reject', 'Reject-grade (engineer override)'),
                    ('unknown', 'Unknown accuracy'),
                ],
                default='',
                help_text='Grade of the GPS fix against the layer requirement',
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name='surveyfeature',
            name='photo_tags',
            field=models.JSONField(
                blank=True,
                default=list,
                help_text='Classified tags for the attached photo',
            ),
        ),
    ]
