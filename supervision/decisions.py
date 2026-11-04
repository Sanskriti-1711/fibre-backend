"""Feature Decision Records for supervision (INTEGRATION_PLAN.md §3 Stage 6).

Every feature that is reviewed gets a decision record. That is the project's
review ledger: approved as designed, approved with a deviation, or sent back for
rework — with the planned-vs-as-built comparison as it stood at that moment.

A record used to be written only when the work fell outside the layer's
tolerance, which left the ordinary case invisible: a project could show 2,000
approved features and no evidence of who approved them or what they measured.
The ledger is append-only, so re-approving after a redo adds the next record
rather than replacing the first.

This module writes the record, the stage event and the notification that make a
deviation durable and visible. It writes nothing to the design stages: a
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


def _reviewer(actor):
    return actor if getattr(actor, 'is_authenticated', False) else None


def _snapshot(feature, tolerances):
    return feature_deviation(
        feature, tolerances.get((str(feature.project_id), feature.layer_id), 0)
    )


def _create(feature, decision, *, deviation, reason, actor):
    return FeatureDecisionRecord.objects.create(
        feature=feature,
        project_id=feature.project_id,
        decision=decision,
        reason=reason or '',
        deviation=deviation,
        reviewer=_reviewer(actor),
    )


def _summary(record, deviation):
    return {
        'record_id': str(record.id),
        'feature_id': str(record.feature_id),
        'layer_id': record.feature.layer_id,
        'decision': record.decision,
        'outside_tolerance': deviation.get('outside_tolerance', 0),
        'fields': deviation.get('fields', []),
    }


def record_approval_decisions(features, *, actor=None, notes=''):
    """Write one decision record for every approved feature.

    ``deviation`` is snapshotted for both outcomes, so an approved-as-designed
    record can still answer "what did the reviewer see?" later. Deviations are
    published to the project timeline; ordinary approvals already write their
    own ``feature_approved`` stage event.

    Returns ``{'records': [...], 'deviations': [...]}`` so the caller can report
    the ledger count and keep the existing deviation summary shape.
    """
    features = list(features)
    if not features:
        return {'records': [], 'deviations': []}

    tolerances = layer_tolerances(features)
    records = []
    deviations = []
    deviation_pairs = []

    for feature in features:
        deviation = _snapshot(feature, tolerances)
        outside = bool(deviation['has_deviation'])
        record = _create(
            feature,
            FeatureDecisionRecord.DECISION_APPROVED_WITH_DEVIATION
            if outside
            else FeatureDecisionRecord.DECISION_APPROVED,
            deviation=deviation,
            reason=notes or feature.approval_notes or '',
            actor=actor,
        )
        records.append(record)
        if outside:
            deviations.append(_summary(record, deviation))
            deviation_pairs.append((record, deviation))

    if deviation_pairs:
        _publish(deviation_pairs, actor=actor, notes=notes)

    return {
        'records': [_summary(record, record.deviation or {}) for record in records],
        'deviations': deviations,
    }


def record_redo_decisions(features, *, actor=None, reason=''):
    """Write a redo record for every feature sent back to the engineer.

    The comparison is snapshotted exactly as it was when the reviewer asked for
    the rework, so the reason can be read later even after the engineer
    re-submits and the live readings change.
    """
    features = list(features)
    if not features:
        return []

    tolerances = layer_tolerances(features)
    records = []
    for feature in features:
        deviation = _snapshot(feature, tolerances)
        records.append(
            _create(
                feature,
                FeatureDecisionRecord.DECISION_REDO,
                deviation=deviation,
                reason=reason,
                actor=actor,
            )
        )

    return [_summary(record, record.deviation or {}) for record in records]


def record_approval_deviations(features, *, actor=None, notes=''):
    """Backward-compatible alias returning only the outside-tolerance records.

    Kept for callers that predate the full ledger; all approvals now write a
    record, so new code should call :func:`record_approval_decisions` directly.
    """
    return record_approval_decisions(features, actor=actor, notes=notes)['deviations']


def _publish(recorded, *, actor=None, notes=''):
    """Write the deviation timeline event and notification, per project.

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
                actor=_reviewer(actor),
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
