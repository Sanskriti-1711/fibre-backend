"""The polygon scope's storage, which the model declared but no migration added.

``AssignmentJob`` grew ``polygon``/``polygon_id``/``polygon_name``/
``covered_feature_ids`` for area assignments, but the last migration only
touched the layer uniqueness rule -- so on any migrated database the columns
did not exist and an area assignment would fail at INSERT time. This adds the
columns, the per-entity uniqueness of a polygon claim, and the scope check.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('assignments', '0007_remove_assignmentjob_unique_layer_assignment_and_more'),
        ('projects', '0020_feature_as_built_geometry'),
    ]

    operations = [
        migrations.AlterField(
            model_name='assignmentjob',
            name='scope',
            field=models.CharField(
                choices=[
                    ('project', 'Project'),
                    ('layer', 'Layer'),
                    ('feature', 'Feature'),
                    ('polygon', 'Polygon'),
                ],
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='assignmentjob',
            name='polygon',
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='assignmentjob',
            name='polygon_id',
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name='assignmentjob',
            name='polygon_name',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='assignmentjob',
            name='covered_feature_ids',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.RemoveConstraint(
            model_name='assignmentjob',
            name='valid_scope_constraints',
        ),
        migrations.AddConstraint(
            model_name='assignmentjob',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(scope='project', layer_id__isnull=True, feature__isnull=True)
                    | models.Q(scope='layer', layer_id__isnull=False, feature__isnull=True)
                    | models.Q(scope='feature', feature__isnull=False)
                    | models.Q(
                        scope='polygon',
                        layer_id__isnull=True,
                        feature__isnull=True,
                        polygon__isnull=False,
                    )
                ),
                name='valid_scope_constraints',
            ),
        ),
        migrations.AddConstraint(
            model_name='assignmentjob',
            constraint=models.UniqueConstraint(
                condition=models.Q(('scope', 'polygon')),
                fields=('project', 'polygon_id', 'assignee'),
                name='unique_polygon_assignment',
            ),
        ),
    ]
