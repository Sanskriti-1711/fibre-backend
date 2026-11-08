"""Span-scope assignments.

A span is the chamber-to-chamber unit of supervision work, so a span claim is
one job over every component that shares ``SPAN_ID`` (``TRENCH_ID`` /
``PARENT_TRENCH`` on older civil layers). Before this the assignment page
resolved a span to its components and created one feature job per component,
turning a 78-component span into 78 rows on the engineer's list.

The components are snapshotted into the existing ``covered_feature_ids``
column; ``span_id`` stores the key the client grouped them by.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('assignments', '0008_assignmentjob_polygon_fields'),
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
                    ('span', 'Span'),
                ],
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='assignmentjob',
            name='span_id',
            field=models.CharField(blank=True, max_length=255, null=True),
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
                        scope='span',
                        layer_id__isnull=True,
                        feature__isnull=True,
                        polygon__isnull=True,
                        span_id__isnull=False,
                    )
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
                condition=models.Q(('scope', 'span')),
                fields=('project', 'span_id', 'assignee'),
                name='unique_span_assignment',
            ),
        ),
    ]
