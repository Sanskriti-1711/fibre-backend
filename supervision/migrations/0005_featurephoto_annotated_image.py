from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('supervision', '0004_featurephoto_project_review_index'),
    ]

    operations = [
        migrations.AddField(
            model_name='featurephoto',
            name='annotated_image',
            field=models.CharField(blank=True, default='', max_length=500),
        ),
    ]
