"""The design-change loop (INTEGRATION_PLAN.md §3 Stage 6.3).

Supervision can already send work back to the engineer (``redo``). That is the
wrong answer when the *design* is what is wrong: a chamber that has to move
because a gas main is in the way is not the crew's mistake. Until now the only
escape hatch was for someone to notice the deviation and remember to open the
LLD page by hand.

This module closes that loop in the one direction the platform allows: it
records a **request** that the next design version be raised, and points the
planner at the LLD review flow. It deliberately does not create a version, edit
a run or touch ``LldRun``/``LldLayer`` — the revision itself happens by running
the design again, which is the LLD stage's job (§5.3). Supervision proposes; the
design stage decides.

Two properties are worth stating because callers rely on them:

* A request needs a deviation behind it. With no approved-with-deviation record
  there is nothing to revise, so the caller gets ``None`` and the view answers
  ``409`` rather than writing a meaningless event.
* Asking twice about the *same* record is one request. The second call returns
  the first one's timestamp and ``duplicate: True`` instead of adding a second
  stage event or ringing the planners' bell again.
"""

from __future__ import annotations

import logging

from django.utils import timezone

from projects.models import StageEvent

from .models import FeatureDecisionRecord

logger = logging.getLogger(__name__)

# The stage whose timeline the request lands on: the *revision* is LLD work,
# even though the request came from supervision. Keeping it on the LLD stage is
# what makes "which designs were re-opened because of as-built findings?" a
# single query.
REQUEST_EVENT = 'design_revision_requested'

# Where the request sends the planner. Relative, like every other notification
# link (see notifications/service.py) — the frontend resolves its own origin.


def design_review_url(project_id):
    return f'ftth-lld-review.html?project_id={project_id}'


def latest_deviation(feature):
    """The newest accepted deviation for ``feature``, or ``None``.

    Only ``approved_with_deviation`` counts: a redo record means the engineer
    still has to act, and an approved-as-designed feature has nothing the design
    stage could act on.
    """
    return (
        FeatureDecisionRecord.objects.filter(
            feature=feature,
            decision=FeatureDecisionRecord.DECISION_APPROVED_WITH_DEVIATION,
        )
        .order_by('-created_at')
        .first()
    )


def _existing_request(feature, record_id):
    """The stage event already written for this record, if there is one."""
    for event in StageEvent.objects.filter(
        project_id=feature.project_id,
        stage=StageEvent.Stage.LLD,
        event=REQUEST_EVENT,
        entity_id=str(feature.id),
    ):
        if str((event.metadata or {}).get('record_id', '')) == str(record_id):
            return event
    return None


def _payload(feature, record, requested_at, *, duplicate, notified=0):
    project = feature.project
    return {
        'feature_id': str(feature.id),
        'layer_id': feature.layer_id,
        'project_id': str(project.id),
        'record_id': str(record.id),
        'requested_at': requested_at.isoformat() if requested_at else None,
        'design_version': project.source_lld_run_id or '',
        'review_url': design_review_url(project.id),
        'notified': notified,
        'duplicate': duplicate,
    }


def request_design_revision(feature, *, actor=None, note=''):
    """Record "raise the next design version for this feature".

    Returns the payload the view serves, or ``None`` when there is no accepted
    deviation to revise (the view turns that into ``409 Conflict``).
    """
    record = latest_deviation(feature)
    if record is None:
        return None

    existing = _existing_request(feature, record.id)
    if existing is not None:
        return _payload(feature, record, existing.created_at, duplicate=True)

    payload = _payload(feature, record, timezone.now(), duplicate=False)
    deviation = record.deviation if isinstance(record.deviation, dict) else {}

    # The event is the durable part of the request. If it cannot be written the
    # request did not happen, so the error is raised rather than swallowed —
    # unlike the notification below, a silent request would be a lie.
    event = StageEvent.objects.create(
        project_id=feature.project_id,
        stage=StageEvent.Stage.LLD,
        event=REQUEST_EVENT,
        actor=actor if getattr(actor, 'is_authenticated', False) else None,
        entity_id=str(feature.id),
        metadata={
            'record_id': str(record.id),
            'feature_ids': [str(feature.id)],
            'layer_id': feature.layer_id,
            'design_version': payload['design_version'],
            'outside_tolerance': deviation.get('outside_tolerance', 0),
            'fields': [
                row.get('key')
                for row in deviation.get('fields', [])
                if isinstance(row, dict) and not row.get('within_tolerance')
            ],
            'note': note or '',
        },
    )
    payload['requested_at'] = event.created_at.isoformat() or payload['requested_at']

    # A notification is secondary: the request is already recorded, and a failed
    # insert must not fail the click (the rule the deviation fan-out follows).
    try:
        from notifications import service as notifications

        payload['notified'] = len(
            notifications.design_revision_requested(
                feature,
                record_id=record.id,
                note=note,
                actor=actor,
            )
        )
    except Exception:
        logger.exception('Could not notify about a design revision for feature %s', feature.id)

    return payload


def requests_for(feature):
    """Every design-revision request recorded for ``feature``, oldest last.

    Read-only view of the requests, so the audit page can show that the design
    stage was asked to act without the frontend having to interpret stage
    events itself.
    """
    rows = []
    for event in StageEvent.objects.filter(
        project_id=feature.project_id,
        stage=StageEvent.Stage.LLD,
        event=REQUEST_EVENT,
        entity_id=str(feature.id),
    ).order_by('-created_at'):
        metadata = event.metadata or {}
        rows.append(
            {
                'record_id': str(metadata.get('record_id', '')),
                'requested_at': event.created_at.isoformat(),
                'requested_by': (
                    (event.actor.full_name or event.actor.email) if event.actor_id else None
                ),
                'design_version': metadata.get('design_version', ''),
                'note': metadata.get('note', ''),
                'fields': metadata.get('fields', []),
            }
        )
    return rows
