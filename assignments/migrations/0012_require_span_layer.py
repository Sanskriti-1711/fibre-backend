"""Require a layer for every span assignment.

Kept separate from the data migration so PostgreSQL commits the split before
it alters the table's constraints. The model's ``save()`` guard also infers the
layer from a single-layer component set and rejects a mixed set before this
constraint is reached.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('assignments', '0011_split_legacy_span_layers'),
    ]

    operations = [
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
                    | (
                        models.Q(
                            scope='span',
                            feature__isnull=True,
                            polygon__isnull=True,
                            span_id__isnull=False,
                            layer_id__isnull=False,
                        )
                        & ~models.Q(layer_id='')
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
    ]
