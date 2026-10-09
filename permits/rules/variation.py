"""Variation-permit workflow — rides the Survey Change review flow.

When a survey change is **approved** on a route section that carries an
APPROVED (or CLOSED) permit, that permit's approval is superseded: the row
gets a new revision (``revision`` + 1) and is re-opened as IDENTIFIED, so it
has to pass evidence → ready → submitted → approved again. The previous
approval stays reproducible through the ``PermitEvent`` trail
(``VARIATION_REQUIRED`` with from/to revision) plus the frozen Approved
Survey / LLD versions the package was built from.

Implements DESIGN.md §6 (Construction — lock): *"A route change requires a
Variation Permit — this rides the existing Survey Change / LLD Change
revision workflow"* and §7.3 (Variation approved → new revision of affected
permits).

The approval endpoints call this fire-and-forget, exactly like the survey
evidence hook — it never raises, so permit bookkeeping can never fail a
survey approval.
"""

from __future__ import annotations

import logging

from permits.models import PermitEvent, PermitMatrix

logger = logging.getLogger(__name__)

# Statuses whose approval is superseded by a route change. Submitted /
# under-review rows are not locked to anything yet — the survey evidence
# hook refreshes their evidence in place instead.
VARIABLE_STATUSES = frozenset(
    {
        PermitMatrix.STATUS_APPROVED,
        PermitMatrix.STATUS_CLOSED,
    }
)


def _route_section_candidates(sf) -> list[str]:
    """Route-section keys a permit row for this survey feature may use.

    Mirrors the survey evidence hook's mapping: a changed HLD feature keys
    on ``original_hld_feature_id``, a brand-new field feature keys on the
    SurveyFeature UUID itself. Both candidates are matched so the variation
    always finds the rows the evidence hook would enrich.
    """
    candidates = []
    for value in (getattr(sf, 'original_hld_feature_id', None), getattr(sf, 'id', None)):
        if value:
            candidates.append(str(value))
    # De-duplicate while preserving order.
    return list(dict.fromkeys(candidates))


def create_variations(ftth_project_id: str, sf, user=None) -> dict:
    """Re-open APPROVED/CLOSED permit rows touched by an approved survey change.

    For every affected row: bump ``revision`` (+1), reset status to
    IDENTIFIED, clear the per-cycle submission/approval/expiry dates (the
    previous approval is superseded), append a variation note to ``comments``
    and record a ``VARIATION_REQUIRED`` event. Returns a summary dict and
    never raises — the caller must not fail because of permit bookkeeping.
    """
    summary = {
        'project_id': ftth_project_id,
        'route_sections': [],
        'variations_created': 0,
        'notes': [],
    }
    try:
        route_sections = _route_section_candidates(sf)
        if not route_sections:
            return summary
        summary['route_sections'] = route_sections

        rows = list(
            PermitMatrix.objects.filter(
                project_id=ftth_project_id,
                route_section__in=route_sections,
                status__in=VARIABLE_STATUSES,
            )
        )
        if not rows:
            return summary

        change_id = str(sf.id)
        for pm in rows:
            old_status = pm.status
            old_revision = pm.revision
            # Phase 3: detach the row from its submission — the superseded
            # application stays as history on the submission record, but the
            # new revision must travel in its own submission when ready.
            old_submission = pm.submission_id
            pm.submission = None
            pm.revision += 1
            pm.status = PermitMatrix.STATUS_IDENTIFIED
            pm.submission_date = None
            pm.approval_date = None
            pm.expiry_date = None
            note = (
                f'VARIATION: {old_status} superseded by survey change '
                f'{change_id} — now revision {pm.revision} (was r{old_revision}). '
                'Must pass evidence → ready → submitted → approved again.'
            )
            pm.comments = f'{note}\n{pm.comments}'.strip() if pm.comments else note
            pm.save(
                update_fields=[
                    'revision',
                    'status',
                    'submission',
                    'submission_date',
                    'approval_date',
                    'expiry_date',
                    'comments',
                    'updated_at',
                ]
            )
            PermitEvent.objects.create(
                permit=pm,
                event='VARIATION_REQUIRED',
                detail={
                    'change_id': change_id,
                    'route_section': pm.route_section,
                    'from_revision': old_revision,
                    'to_revision': pm.revision,
                    'from_status': old_status,
                    'to_status': pm.status,
                    'from_submission': str(old_submission) if old_submission else None,
                    'by': getattr(user, 'email', None) if user else None,
                },
            )
            summary['variations_created'] += 1

        logger.info(
            'Variations created for %s from change %s: %s row(s)',
            ftth_project_id,
            change_id,
            summary['variations_created'],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning('Variation creation failed for %s: %s', ftth_project_id, exc)
        summary['notes'].append(str(exc))
    return summary
