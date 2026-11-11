"""Make span assignments layerwise.

A span is now one layer's unit of work (a trench span, a duct span or a cable
span), not every civil and network layer that happens to share ``SPAN_ID``.
``layer_id`` is therefore part of the span's identity and of the unique claim:
the same span key can hold one job per layer and per engineer.

Existing span jobs keep their NULL layer and remain readable; the client
selects one layer from their covered components when opening them.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('assignments', '0009_assignmentjob_span_fields'),
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
                    | models.Q(
                        scope='span',
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
        migrations.RemoveConstraint(
            model_name='assignmentjob',
            name='unique_span_assignment',
        ),
        migrations.AddConstraint(
            model_name='assignmentjob',
            constraint=models.UniqueConstraint(
                condition=models.Q(scope='span'),
                fields=('project', 'span_id', 'layer_id', 'assignee'),
                name='unique_span_assignment',
            ),
        ),
    ]
