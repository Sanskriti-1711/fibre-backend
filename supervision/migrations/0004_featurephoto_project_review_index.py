from django.db import migrations, models
import django.db.models.deletion


def backfill_project(apps, schema_editor):
    """Copy each photo's project from its feature.

    Photos predate the column, but their feature always knew the project, so
    the backfill is exact rather than a guess. Rows whose feature is gone are
    left null; the alter below only becomes non-null once every live row has
    an answer, which the cascade guarantees.
    """
    FeaturePhoto = apps.get_model('supervision', 'FeaturePhoto')
    rows = FeaturePhoto.objects.filter(project__isnull=True).values_list(
        'id', 'feature__project_id'
    )
    for photo_id, project_id in rows.iterator():
        if project_id:
            FeaturePhoto.objects.filter(pk=photo_id).update(project_id=project_id)


class Migration(migrations.Migration):
    dependencies = [
        ('projects', '0019_layerfieldconfig_tolerance'),
        ('supervision', '0003_featurephoto_sha256_featurephoto_size_bytes_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='featurephoto',
            name='project',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='feature_photos',
                to='projects.project',
            ),
        ),
        migrations.RunPython(backfill_project, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='featurephoto',
            name='project',
            field=models.ForeignKey(
                db_index=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='feature_photos',
                to='projects.project',
            ),
        ),
        migrations.AddIndex(
            model_name='featuredecisionrecord',
            index=models.Index(
                fields=['project', 'decision', 'created_at'],
                name='supervision_proj_dec_idx',
            ),
        ),
    ]
