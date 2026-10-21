"""Notification fan-out — the only module that writes ``Notification`` rows.

Call sites stay one line (``service.notify_platform(...)``) and never have to
know who the platform engineers are, whether a user is active, or what happens
when the database refuses the insert. Two rules govern everything here:

1. **Nobody is notified twice for the same event.** ``dedupe_key`` is honoured
   per recipient, so the mobile client's offline-sync retry or a double-clicked
   button cannot ring the same bell twice.
2. **A notification never breaks the thing that caused it.** Every helper
   swallows and logs; an inbox that is down must not fail an assignment, a
   review decision or an LLD run.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .models import Notification

logger = logging.getLogger(__name__)

User = get_user_model()

# Recipients for a role, cached per call chain only through the caller — a
# role change is rare and the query is two columns over an indexed table.
ROLE_PLATFORM = 'SUBADMIN'
ROLE_ENGINEER = 'ENGINEER'


def _coerce_users(recipients: Iterable) -> list:
    """Accept User objects, ids or emails; return distinct active Users."""
    resolved: list = []
    seen: set = set()
    ids_cast: list[str] = []
    emails: list[str] = []
    for item in recipients or []:
        if item is None:
            continue
        if isinstance(item, User):
            key = str(item.pk)
            if key in seen or not item.is_active:
                continue
            seen.add(key)
            resolved.append(item)
        elif hasattr(item, 'pk') and not isinstance(item, str):
            ids_cast.append(str(item.pk))
        else:
            value = str(item).strip()
            if not value:
                continue
            if '@' in value:
                emails.append(value)
            else:
                ids_cast.append(value)

    if ids_cast:
        for user in User.objects.filter(pk__in=ids_cast, is_active=True):
            key = str(user.pk)
            if key in seen:
                continue
            seen.add(key)
            resolved.append(user)
    if emails:
        for user in User.objects.filter(email__in=emails, is_active=True):
            key = str(user.pk)
            if key in seen:
                continue
            seen.add(key)
            resolved.append(user)
    return resolved


def role_users(role: str) -> list:
    """Active users holding ``role`` (``SUBADMIN`` = platform engineer)."""
    return list(User.objects.filter(role=role, is_active=True))


def notify(
    recipients: Iterable,
    *,
    kind: str,
    title: str,
    body: str = '',
    url: str = '',
    project_id: str = '',
    project_name: str = '',
    category: str = Notification.CATEGORY_SYSTEM,
    severity: str = Notification.SEVERITY_INFO,
    actor=None,
    entity_type: str = '',
    entity_id: str = '',
    dedupe_key: str = '',
    meta: dict | None = None,
) -> list[Notification]:
    """Create one row per recipient. Never raises; returns what was created."""
    users = _coerce_users(recipients)
    if not users:
        return []

    actor_obj = actor if isinstance(actor, User) else None
    if actor is not None and actor_obj is None and not isinstance(actor, str):
        actor_obj = getattr(actor, 'user', None) if hasattr(actor, 'user') else None

    if dedupe_key:
        already = set(
            Notification.objects.filter(recipient__in=users, dedupe_key=dedupe_key).values_list(
                'recipient_id', flat=True
            )
        )
        users = [user for user in users if user.pk not in already]
        if not users:
            return []

    rows = [
        Notification(
            recipient=user,
            actor=actor_obj,
            kind=kind,
            category=category,
            severity=severity,
            title=title[:200],
            body=body,
            project_id=str(project_id or '')[:64],
            project_name=(project_name or '')[:200],
            entity_type=(entity_type or '')[:40],
            entity_id=str(entity_id or '')[:64],
            url=(url or '')[:300],
            meta=meta or {},
            dedupe_key=(dedupe_key or '')[:200],
        )
        for user in users
    ]
    try:
        with transaction.atomic():
            return list(Notification.objects.bulk_create(rows))
    except Exception:
        logger.exception('Could not create notifications for %s', kind)
        return []


def notify_platform(*, actor=None, **kwargs) -> list[Notification]:
    """Notify every platform engineer (SUBADMIN)."""
    return notify(role_users(ROLE_PLATFORM), actor=actor, **kwargs)


def notify_engineers(recipients: Iterable, *, actor=None, **kwargs) -> list[Notification]:
    """Notify specific survey engineers (assignees), never the whole role."""
    return notify(recipients, actor=actor, **kwargs)


# ---------------------------------------------------------------------------
# Event helpers — one function per thing that actually happens
# ---------------------------------------------------------------------------


def survey_change_submitted(change, *, actor=None) -> list[Notification]:
    """A field edit entered the planner's approval queue."""
    project = getattr(change, 'project', None)
    project_id = str(getattr(project, 'id', '') or '')
    project_name = project.name if project is not None and project.name else project_id
    engineer = getattr(change, 'engineer', None)
    engineer_name = (
        (engineer.full_name or engineer.email) if engineer is not None else 'A survey engineer'
    )
    layer = getattr(change, 'layer_name', '') or 'feature'
    status = str(getattr(change, 'survey_status', '') or '')
    kind_label = 'edited' if status == 'modified' else 'submitted'
    return notify_platform(
        kind=Notification.KIND_SURVEY_CHANGE,
        category=Notification.CATEGORY_DESIGN,
        severity=Notification.SEVERITY_WARNING,
        title=f'Survey change {kind_label} — {layer}',
        body=f'{engineer_name} {kind_label} a {layer} feature on {project_name}.',
        url=(f'ftth-lld-feature-review.html?project_id={project_id}&change_id={change.pk}'),
        project_id=project_id,
        project_name=project_name,
        actor=actor or engineer,
        entity_type='survey_feature',
        entity_id=str(change.pk),
        dedupe_key=f'survey_change:{change.pk}:{getattr(change, "version_number", 1)}',
        meta={'layer': layer, 'change_type': status},
    )


def survey_change_decided(
    change, *, decision: str, actor=None, comment: str = ''
) -> list[Notification]:
    """Tell the engineer who made the edit what the planner decided."""
    engineer = getattr(change, 'engineer', None)
    if engineer is None:
        return []
    project = getattr(change, 'project', None)
    project_id = str(getattr(project, 'id', '') or '')
    project_name = project.name if project is not None and project.name else project_id
    layer = getattr(change, 'layer_name', '') or 'feature'
    words = {
        'approved': ('Change approved', Notification.SEVERITY_SUCCESS),
        'rejected': ('Change rejected', Notification.SEVERITY_CRITICAL),
        'needs_correction': ('Correction requested', Notification.SEVERITY_WARNING),
    }
    title, severity = words.get(decision, ('Change reviewed', Notification.SEVERITY_INFO))
    body = f'Your {layer} change on {project_name} was {decision.replace("_", " ")}.'
    if comment:
        body += f' Note: {comment}'
    return notify_engineers(
        [engineer],
        kind=Notification.KIND_SURVEY_DECISION,
        category=Notification.CATEGORY_SURVEY,
        severity=severity,
        title=f'{title} — {layer}',
        body=body,
        url=(
            f'engineer/engineer-feature-detail.html?project_id={project_id}'
            f'&change_id={change.pk}'
        ),
        project_id=project_id,
        project_name=project_name,
        actor=actor,
        entity_type='survey_feature',
        entity_id=str(change.pk),
        dedupe_key=f'survey_decision:{change.pk}:{decision}',
        meta={'decision': decision, 'comment': comment},
    )


def survey_assigned(
    engineers: Iterable, *, project_id: str, project_name: str = '', feature_count=None, actor=None
) -> list[Notification]:
    """A planner handed a project to one or more field engineers."""
    count = feature_count if feature_count is not None else None
    detail = f'{count} feature(s) to survey' if count else 'Open it to see the features to survey'
    return notify_engineers(
        engineers,
        kind=Notification.KIND_SURVEY_ASSIGNED,
        category=Notification.CATEGORY_SURVEY,
        severity=Notification.SEVERITY_INFO,
        title=f'New survey assignment — {project_name or project_id}',
        body=f'You were assigned a field survey. {detail}.',
        url=f'engineer/engineer-project-details.html?project_id={project_id}',
        project_id=str(project_id or ''),
        project_name=project_name or '',
        actor=actor,
        entity_type='project',
        entity_id=str(project_id or ''),
        dedupe_key=f'survey_assigned:{project_id}',
        meta={'feature_count': count},
    )


def survey_package_decided(
    engineers: Iterable,
    *,
    project_id: str,
    project_name: str = '',
    action: str,
    actor=None,
) -> list[Notification]:
    """Tell the assigned engineers what the planner did with their package.

    ``redo`` is the one that actually asks for work, so it carries the louder
    severity; ``accept``/``complete`` close the loop so the engineer stops
    wondering whether their submission landed.
    """
    words = {
        'redo': (
            'Survey sent back for rework',
            'A planner sent your survey package back. Reopen it to see what changed.',
            Notification.SEVERITY_WARNING,
        ),
        'accept': (
            'Survey accepted',
            'A planner accepted your survey package. The design team can run LLD from it.',
            Notification.SEVERITY_SUCCESS,
        ),
        'complete': (
            'Survey project completed',
            'The survey project you worked on has been closed out.',
            Notification.SEVERITY_SUCCESS,
        ),
    }
    if action not in words:
        return []
    label, body, severity = words[action]
    return notify_engineers(
        engineers,
        kind=Notification.KIND_SURVEY_DECISION,
        category=Notification.CATEGORY_SURVEY,
        severity=severity,
        title=f'{label} — {project_name or project_id}',
        body=body,
        url=f'engineer/engineer-project-details.html?project_id={project_id}',
        project_id=str(project_id or ''),
        project_name=project_name or '',
        actor=actor,
        entity_type='project',
        entity_id=str(project_id or ''),
        # No dedupe: the same package can legitimately be sent back, resubmitted
        # and accepted again.  A double-click cannot fire twice because the
        # status transition rejects the replay.
        meta={'action': action},
    )


def approved_survey_ready(
    *, project_id: str, project_name: str = '', version: str, actor=None
) -> list[Notification]:
    """A frozen ASV exists — the planner can now run LLD."""
    return notify_platform(
        kind=Notification.KIND_LLD_READY,
        category=Notification.CATEGORY_DESIGN,
        severity=Notification.SEVERITY_SUCCESS,
        title=f'{version} created — ready to run LLD',
        body=(
            f'Approved Survey Version {version} for {project_name or project_id} is frozen. '
            'Run LLD (or a full re-plan for structural changes) when you are ready.'
        ),
        url=f'ftth-lld-review.html?project_id={project_id}',
        project_id=project_id,
        project_name=project_name or '',
        actor=actor,
        entity_type='approved_survey_version',
        entity_id=version,
        dedupe_key=f'lld_ready:{project_id}:{version}',
        meta={'version': version},
    )


def lld_run_finished(run, *, ok: bool, error: str = '') -> list[Notification]:
    """A background LLD run completed or failed."""
    project = getattr(run, 'ftth_project', None)
    project_id = str(getattr(run, 'ftth_project_id', '') or '')
    project_name = project.name if project is not None and project.name else project_id
    version = getattr(run, 'lld_version', 'LLD')
    if ok:
        title = f'{version} completed — {project_name}'
        body = 'The LLD output is ready to review and download.'
        severity = Notification.SEVERITY_SUCCESS
        url = f'ftth-lld-results.html?project_id={project_id}&version={version}'
    else:
        title = f'{version} failed — {project_name}'
        body = error or 'The LLD run failed. Open the run to see the engine message.'
        severity = Notification.SEVERITY_CRITICAL
        url = f'ftth-lld-versions.html?project_id={project_id}'
    return notify_platform(
        kind=Notification.KIND_LLD_RUN,
        category=Notification.CATEGORY_DESIGN,
        severity=severity,
        title=title,
        body=body,
        url=url,
        project_id=project_id,
        project_name=project_name or '',
        entity_type='lld_run',
        entity_id=str(getattr(run, 'pk', '')),
        dedupe_key=f'lld_run:{getattr(run, "pk", "")}:{"ok" if ok else "fail"}',
        meta={'version': version, 'status': 'completed' if ok else 'failed'},
    )


def survey_package_submitted(
    project_id: str, *, project_name: str = '', actor=None
) -> list[Notification]:
    """An engineer submitted their whole survey package for review."""
    return notify_platform(
        kind=Notification.KIND_SURVEY_CHANGE,
        category=Notification.CATEGORY_DESIGN,
        severity=Notification.SEVERITY_WARNING,
        title=f'Survey submitted — {project_name or project_id}',
        body='A field engineer submitted the survey package. Changes are waiting in the approval queue.',
        url=f'ftth-survey-changes.html?project_id={project_id}',
        project_id=str(project_id or ''),
        project_name=project_name or '',
        actor=actor,
        entity_type='project',
        entity_id=str(project_id or ''),
        meta={},
    )


def unread_summary(user) -> dict:
    """Counts for the bell badge, cheap enough to poll."""
    rows = Notification.objects.filter(recipient=user, read_at__isnull=True)
    by_category: dict[str, int] = {}
    for row in rows.only('category'):
        by_category[row.category] = by_category.get(row.category, 0) + 1
    latest = (
        Notification.objects.filter(recipient=user)
        .order_by('-created_at')
        .values_list('created_at', flat=True)
        .first()
    )
    return {
        'unread': sum(by_category.values()),
        'by_category': by_category,
        'latest_at': latest.isoformat() if latest else None,
        'now': timezone.now().isoformat(),
    }
