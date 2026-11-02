"""Feature Decision Records for supervision (INTEGRATION_PLAN.md §3 Stage 6).

Approving work that does not match the design is a legitimate outcome — a
chamber landed two metres off because a gas main was in the way, and the
reviewer accepted it. That decision used to exist only as free text in
``Feature.approval_notes``: visible on the row, but not attributable to a stage,
not queryable per project, and easy to overwrite on the next approval.

This module writes the record, the stage event and the notification that make
the decision durable and visible. It writes nothing to the design stages: a
deviation is a *proposal for a new design version*, never an edit to the
existing one (§5.2, §5.3).
"""

from __future__ import annotations

import logging

from projects.models import LayerFieldConfig, StageEvent
from projects.services import feature_deviation

from .models import FeatureDecisionRecord

logger = logging.getLogger(__name__)


def layer_tolerances(features):
    """``{(project_id, layer_id): tolerance}`` for the features' own layers.

    One query for the batch: a bulk approval is often across several layers, and
    a per-feature lookup would make the approval cost grow with the selection.
    """
    project_ids = {feature.project_id for feature in features}
    layer_ids = {feature.layer_id for feature in features}
    if not project_ids or not layer_ids:
        return {}

    rows = LayerFieldConfig.objects.filter(
        project_id__in=project_ids, layer_id__in=layer_ids
    ).values('project_id', 'layer_id', 'tolerance')
    return {(str(row['project_id']), row['layer_id']): row['tolerance'] for row in rows}


def record_approval_deviations(features, *, actor=None, notes=''):
    """Record a decision for every approved feature outside its tolerance.

    Returns one summary per record so the approval response can tell the
    reviewer what was written down. Features within tolerance produce no record:
    "approved as designed" is already the row's own status, and writing a
    decision record for every approval would turn an exception log into noise.
    """
    features = list(features)
    if not features:
        return []

    tolerances = layer_tolerances(features)
    recorded = []

    for feature in features:
        deviation = feature_deviation(
            feature, tolerances.get((str(feature.project_id), feature.layer_id), 0)
        )
        if not deviation['has_deviation']:
            continue

        record = FeatureDecisionRecord.objects.create(
            feature=feature,
            project_id=feature.project_id,
            decision=FeatureDecisionRecord.DECISION_APPROVED_WITH_DEVIATION,
            reason=notes or feature.approval_notes or '',
            deviation=deviation,
            reviewer=actor if getattr(actor, 'is_authenticated', False) else None,
        )
        recorded.append((record, deviation))

    if recorded:
        _publish(recorded, actor=actor, notes=notes)

    return [
        {
            'record_id': str(record.id),
            'feature_id': str(record.feature_id),
            'layer_id': record.feature.layer_id,
            'outside_tolerance': deviation['outside_tolerance'],
            'fields': deviation['fields'],
        }
        for record, deviation in recorded
    ]


def _publish(recorded, *, actor=None, notes=''):
    """Write the timeline event and the notification, per project.

    Both are secondary: the record itself is the evidence, so a failure here is
    logged rather than raised — a reviewer must not lose an approval because a
    notification insert failed (the same rule the stage timeline follows).
    """
    by_project: dict = {}
    for record, _deviation in recorded:
        by_project.setdefault(str(record.project_id), []).append(record)

    for project_id, records in by_project.items():
        feature_ids = [str(record.feature_id) for record in records]

        try:
            StageEvent.objects.create(
                project_id=project_id,
                stage=StageEvent.Stage.SUPERVISION,
                event='deviation_raised',
                actor=actor if getattr(actor, 'is_authenticated', False) else None,
                entity_id=feature_ids[0],
                metadata={
                    'feature_ids': feature_ids,
                    'count': len(feature_ids),
                    'notes': notes or '',
                },
            )
        except Exception:
            logger.exception('Could not write deviation_raised for project %s', project_id)

        try:
            from notifications import service as notifications

            notifications.supervision_deviation(
                records[0].feature,
                feature_ids=feature_ids,
                actor=actor,
                notes=notes,
            )
        except Exception:
            logger.exception('Could not notify about a deviation on project %s', project_id)
