"""Split legacy span jobs into one job per component layer.

Migration 0010 made span jobs layer-aware for new writes but deliberately left
existing rows NULL. Those rows can contain trenches, ducts and cables that
happen to share a span key, so the mobile screen had to guess a layer.

This migration does the data part only. Each legacy row is rebuilt from the
layers of its actual components: the original row keeps its id for the first
layer, additional rows are created for the other layers, and duplicate rows are
merged. A row with no components and no layer cannot satisfy the new identity
and is removed.

The database constraint lives in 0012. Keeping it out of this migration avoids
PostgreSQL's "cannot ALTER TABLE ... pending trigger events" error when the
same transaction updates rows and then changes the table's constraints.
"""

from collections import defaultdict

from django.db import migrations


def _groups_for_job(Feature, job, using):
    """Map each component layer to the ids of that layer's components."""
    ids = [str(feature_id) for feature_id in (job.covered_feature_ids or [])]
    if not ids:
        return {}
    layers = {
        str(feature_id): str(layer_id or '').strip()
        for feature_id, layer_id in Feature.objects.using(using)
        .filter(id__in=ids)
        .values_list('id', 'layer_id')
    }
    groups: dict[str, list[str]] = defaultdict(list)
    for feature_id in ids:
        layer_id = layers.get(feature_id, '')
        if layer_id:
            groups[layer_id].append(feature_id)
    return dict(groups)


def _find_existing(AssignmentJob, job, layer_id, using):
    return (
        AssignmentJob.objects.using(using)
        .filter(
            project_id=job.project_id,
            scope='span',
            span_id=job.span_id,
            layer_id=layer_id,
            assignee_id=job.assignee_id,
        )
        .exclude(pk=job.pk)
        .first()
    )


def split_legacy_spans(apps, schema_editor):
    AssignmentJob = apps.get_model('assignments', 'AssignmentJob')
    Feature = apps.get_model('projects', 'Feature')
    using = schema_editor.connection.alias

    # Materialise the list: the loop creates, updates and deletes rows in the
    # same table, and iterating the live queryset makes that unsafe.
    jobs = list(
        AssignmentJob.objects.using(using)
        .filter(scope='span')
        .order_by('created_at', 'id')
    )

    for job in jobs:
        groups = _groups_for_job(Feature, job, using)
        existing_layer = str(job.layer_id or '').strip()

        if not groups:
            if existing_layer:
                # A layer-only span with no surviving components is still a
                # valid (empty) assignment; keep it and drop stale ids.
                job.covered_feature_ids = []
                job.layer_id = existing_layer
                job.save(using=using)
            else:
                # No layer can be inferred from a row that no longer has any
                # components. It cannot satisfy the new constraint.
                job.delete(using=using)
            continue

        preferred = existing_layer if existing_layer in groups else None
        primary_layer = preferred or sorted(groups)[0]
        primary_target = _find_existing(AssignmentJob, job, primary_layer, using)
        if primary_target is None:
            primary_target = job
            primary_target.covered_feature_ids = []
            primary_target.layer_id = primary_layer

        for layer_id, feature_ids in groups.items():
            if layer_id == primary_layer:
                target = primary_target
            else:
                target = _find_existing(AssignmentJob, job, layer_id, using)
                if target is None:
                    target = AssignmentJob.objects.using(using).create(
                        project_id=job.project_id,
                        layer_id=layer_id,
                        span_id=job.span_id,
                        covered_feature_ids=[],
                        assignee_id=job.assignee_id,
                        scope='span',
                    )
                    # Keep the planner's original ordering rather than moving
                    # a split row to the end of the queue.
                    AssignmentJob.objects.using(using).filter(pk=target.pk).update(
                        created_at=job.created_at
                    )

            merged = list(target.covered_feature_ids or [])
            for feature_id in feature_ids:
                if feature_id not in merged:
                    merged.append(feature_id)
            target.covered_feature_ids = merged
            target.layer_id = layer_id
            target.save(using=using)

        # If a duplicate row already held the primary layer, the original row
        # was merged into it and must not survive as a second claim.
        if primary_target is not job:
            job.delete(using=using)


class Migration(migrations.Migration):
    dependencies = [
        ('assignments', '0010_assignmentjob_span_layer'),
    ]

    operations = [
        migrations.RunPython(split_legacy_spans, migrations.RunPython.noop),
    ]
