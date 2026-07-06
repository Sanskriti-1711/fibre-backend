import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0006_feature_field_schema'),
    ]

    operations = [
        migrations.CreateModel(
            name='LayerFieldConfig',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('layer_id', models.CharField(max_length=255)),
                ('layer_name', models.CharField(blank=True, max_length=255)),
                ('schema', models.JSONField(default=list)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='projects.project')),
            ],
            options={
                'db_table': 'layer_field_configs',
                'unique_together': {('project', 'layer_id')},
            },
        ),
    ]
