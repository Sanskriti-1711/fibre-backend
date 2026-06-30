from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0005_rename_feature_proj_layer_idx_features_project_24b9e2_idx_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='feature',
            name='field_schema',
            field=models.JSONField(blank=True, null=True),
        ),
    ]
