# Durable guard for the post-HLD chain, plus a content revision on derived layers.
#
# `ftth_hld.models.HldPostProcess` records what the post-completion chain (layer
# sync, road class, trench sections, permit matrix, permit package) ran against,
# so it is not re-run on every status poll. `FtthLayer.source_revision` records
# the content revision a derived layer was built from, so freshness is judged on
# SOURCE CONTENT rather than `updated_at` (which any re-publish bumps).

import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('ftth_hld', '0007_boqrate_boqsnapshot'),
    ]

    operations = [
        migrations.AddField(
            model_name='ftthlayer',
            name='source_revision',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.CreateModel(
            name='HldPostProcess',
            fields=[
                (
                    'id',
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ('project_id', models.CharField(db_index=True, max_length=64, unique=True)),
                (
                    'status',
                    models.CharField(
                        choices=[
                            ('pending', 'Pending'),
                            ('running', 'Running'),
                            ('done', 'Done'),
                            ('failed', 'Failed'),
                        ],
                        default='pending',
                        max_length=20,
                    ),
                ),
                ('trench_revision', models.CharField(blank=True, default='', max_length=64)),
                ('steps', models.JSONField(blank=True, default=dict)),
                ('error_message', models.TextField(blank=True, default='')),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('finished_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'db_table': 'ftth_hld_post_process',
            },
        ),
    ]
