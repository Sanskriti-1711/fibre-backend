"""
Survey assignment service.

Turns a completed HLD pipeline run (FtthProject) into a **Survey copy**
(projects.Project row) by importing the generated survey package into the
standard project/feature tables, then links it to a field engineer through
an AssignmentJob (scope=project).

Flow
----
1. Admin picks an engineer on the projects page (HLD row → Assign).
2. ``assign_hld_project()``:
   - finds or creates the Survey copy (name = "<HLD name> - Survey",
     status = "assigned"),
   - generates + stores the survey-package ZIP (same bytes the
     ``survey-package`` download endpoint serves),
   - imports every WGS84 GeoJSON layer into the copy as Feature rows
     (reusing the standard zip import helpers),
   - upserts the project-scope AssignmentJob for the engineer,
   - records ``assigned_engineer`` / ``assigned_at`` on the FtthProject.
3. The engineer accepts in the mobile app → Survey copy status flips
   ``assigned`` → ``active``.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from assignments.models import AssignmentJob
from projects.models import Feature, ImportSession, Project
from users.models import User

from .models import FtthProject
from .pipeline import generate_survey_package

SURVEY_SUFFIX = ' - Survey'


def _package_hash(payload: bytes) -> str:
    """Identity of a survey package: its entries' names and bytes.

    Not the sha256 of the zip itself. Two builds of the same run carry the same
    entries -- measured 2026-10-13: 15 identical names, 15 identical entry
    hashes -- inside containers that do not match byte for byte, because the
    archive rewrite is not something a copy cares about. What the copy holds is
    the entries, so that is what is hashed, and the digest is also the name the
    package is stored under.
    """
    digest = hashlib.sha256()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for name in sorted(archive.namelist()):
            entry = archive.read(name)
            digest.update(f'{name}:{len(entry)}:'.encode())
            digest.update(hashlib.sha256(entry).digest())
    return digest.hexdigest()


def _survey_package(survey, ftth_project_id: str, session=None):
    """The run's current survey package, stored under its own content hash.

    Returns ``(import_session, path, content_hash)``.

    The package is built here, as it is on every download
    (``generate_survey_package``), because a run's layers can still be written
    after it is published -- one project's pole and aerial stages wrote two
    hours later, and a package built before that was missing them. A copy has
    to be imported from the package the platform is serving now.

    Storing it under its content hash (``imports/<sha256>.zip``) is what lets
    the next assignment tell whether the copy already holds these exact bytes,
    and therefore whether the import has to run at all.
    """
    if session is None:
        session = ImportSession.objects.filter(project=survey).first()
    stored_path = session.stored_file_path if session else None

    payload = generate_survey_package(ftth_project_id)
    digest = _package_hash(payload)
    stored = Path(settings.MEDIA_ROOT) / 'imports' / f'{digest}.zip'
    if str(stored) != stored_path:
        stored.parent.mkdir(parents=True, exist_ok=True)
        if not stored.exists():
            stored.write_bytes(payload)
        session, _ = ImportSession.objects.update_or_create(
            project=survey,
            defaults={
                'original_filename': f'{ftth_project_id}_survey_package.zip',
                'stored_file_path': str(stored),
                'status': 'imported',
                'validation_summary': None,
            },
        )
    return session, str(stored), digest


def _holds_this_import(survey, stored_path, package_hash: str) -> bool:
    """Does the copy already hold the import of exactly these package bytes?

    ``stored_path`` is the file the copy's import came from -- the path as it
    was recorded before this assignment touched it -- and it has to be named
    for its own content hash, which the assignment flow writes in the same
    transaction as the import. The name is then the proof that the rows and
    these bytes belong together: the bytes are current (``_survey_package``
    just built them) and the copy was imported from them and nothing has
    replaced them since. A copy whose package is missing, or was written by the
    older code under a random uuid name, answers False and is imported again --
    once; the name is canonical from then on.
    """
    if not stored_path or not package_hash:
        return False
    if Path(stored_path).stem != package_hash:
        return False
    return Feature.objects.filter(project=survey).exists()


def find_survey_copy(ftth_project_id: str):
    """Return the Survey copy Project for an HLD run, or None."""
    return Project.objects.filter(source_ftth_project_id=ftth_project_id).first()


def assign_hld_project(ftth_project_id: str, engineer_ids) -> dict:
    """Assign a completed HLD run to one or more field engineers.

    Creates (or reuses) the Survey copy, imports the survey package into it,
    and creates one project-scope AssignmentJob per engineer. Returns a
    summary dict.
    """
    if isinstance(engineer_ids, str):
        engineer_ids = [engineer_ids]
    engineer_ids = list(engineer_ids)
    if not engineer_ids:
        raise ValueError('At least one engineer_id is required')
    try:
        hld = FtthProject.objects.get(pk=ftth_project_id)
    except FtthProject.DoesNotExist:
        raise ValueError(f'HLD project {ftth_project_id} not found')

    if hld.status != FtthProject.STATUS_COMPLETED:
        raise ValueError(
            f'HLD project {ftth_project_id} is not completed '
            f'(status={hld.status}); only completed runs can be assigned'
        )

    engineers = []
    for eid in engineer_ids:
        try:
            eng = User.objects.get(pk=eid)
        except User.DoesNotExist:
            raise ValueError(f'Engineer {eid} not found')
        if eng.role != User.Role.ENGINEER:
            raise ValueError(f'User {eid} is not an engineer')
        engineers.append(eng)
    primary_engineer = engineers[0]

    # ── 1-4. Everything inside ONE transaction: a failure mid-way must
    #         not leave an 'assigned' copy with zero features/jobs. ──────
    with transaction.atomic():
        # ── 1. Survey copy (reuse if it already exists) ─────────────────
        survey = find_survey_copy(ftth_project_id)
        if survey is None:
            survey = Project.objects.create(
                name=f'{hld.name or ftth_project_id}{SURVEY_SUFFIX}',
                description=(
                    f"Survey copy of HLD run '{hld.name or ftth_project_id}'. "
                    'Field-survey package auto-imported for the assigned engineer.'
                ),
                region='Field Survey',
                status='assigned',
                source_ftth_project_id=ftth_project_id,
            )
        else:
            # Data-safety guard: once an engineer accepts the copy (active),
            # re-assigning would wipe their survey progress. Refuse instead.
            if survey.status in (
                'in_progress',
                'active',
                'submitted',
                'under_review',
                'reviewed',
                'accepted',
                'redo',
                'completed',
            ):
                raise ValueError(
                    f"Survey copy '{survey.name}' is already {survey.status}; "
                    're-assigning would erase survey data and approvals. '
                    'Re-assign only while the copy is pending.'
                )
            survey.name = f'{hld.name or ftth_project_id}{SURVEY_SUFFIX}'
            survey.status = 'assigned'
            survey.save(update_fields=['name', 'status', 'updated_at'])

        # ── 2. The survey package: built once, then reused ───────────────
        session = ImportSession.objects.filter(project=survey).first()
        imported_from = session.stored_file_path if session else None
        session, full_path, package_hash = _survey_package(survey, ftth_project_id, session)

        # ── 3. Import the WGS84 GeoJSON layers as Feature rows ───────────
        from projects.api.import_views import discover_zip_layers, import_zip_layers

        layers = discover_zip_layers(full_path)
        layer_names = [l['name'] for l in layers]

        if _holds_this_import(survey, imported_from, package_hash):
            # The copy was imported from these exact bytes, so re-importing it
            # produces the rows that are already there. Measured 2026-10-13:
            # dropping and re-inserting 2,164 features is 5.74s of Django
            # collector plus ~4.5s of INSERTs -- 10s of a request the host ends
            # somewhere around 20-45s, spent to arrive back where it started
            # (a browser saw this path die at 20.8s with no reply at all and
            # reported it as "could not reach the design service"). Only a copy
            # that does not already hold the current package pays for the
            # import.
            features_created = Feature.objects.filter(project=survey).count()
        else:
            # Drop any previous import so we never accumulate duplicates; the
            # enclosing transaction guarantees the old features survive a
            # failed re-import. Left to Django's collector on purpose: the
            # database will not cascade for us (Django writes every foreign key
            # as ON DELETE NO ACTION, and nine tables reference Feature), and a
            # bare DELETE would leave orphans for the deferred constraint to
            # reject at commit -- as a 502, after the import.
            Feature.objects.filter(project=survey).delete()
            features_created = import_zip_layers(survey, full_path, layer_names)

        # Features imported as pending; a project-scope assignment marks the
        # whole project as assigned (matches existing assignment semantics).
        if features_created:
            Feature.objects.filter(project=survey).exclude(status=Feature.STATUS_ASSIGNED).update(
                status=Feature.STATUS_ASSIGNED
            )

        # ── 4. Project-scope AssignmentJob per engineer (PK preserved) ───
        existing_jobs = list(
            AssignmentJob.objects.filter(project=survey, scope=AssignmentJob.SCOPE_PROJECT)
        )
        jobs = []
        for eng in engineers:
            job, _ = AssignmentJob.objects.update_or_create(
                project=survey,
                scope=AssignmentJob.SCOPE_PROJECT,
                assignee=eng,
                defaults={},
            )
            jobs.append(job)
        # Drop jobs for engineers no longer assigned
        keep_assignees = {eng.id for eng in engineers}
        for job in existing_jobs:
            if job.assignee_id not in keep_assignees:
                job.delete()

    # ── 5. Record assignment on the HLD run ──────────────────────────────
    hld.assigned_engineer = primary_engineer
    hld.assigned_at = timezone.now()
    hld.save(update_fields=['assigned_engineer', 'assigned_at', 'updated_at'])

    return {
        'hl_project_id': ftth_project_id,
        'hl_name': hld.name,
        'survey_project_id': str(survey.id),
        'survey_project_name': survey.name,
        'survey_status': survey.status,
        'engineers': [
            {
                'id': str(eng.id),
                'email': eng.email,
                'full_name': eng.full_name,
            }
            for eng in engineers
        ],
        'assignment_job_ids': [str(j.id) for j in jobs],
        'layers': layer_names,
        'features_created': features_created,
    }


def accept_survey_project(project_id: str, user) -> dict:
    """Engineer accepts an assigned Survey copy → status becomes active.

    Only the assigned engineer (project-scope AssignmentJob) or a SUBADMIN
    may accept. Returns the updated project summary.
    """
    try:
        survey = Project.objects.get(pk=project_id)
    except Project.DoesNotExist:
        raise ValueError(f'Survey project {project_id} not found')

    is_engineer = AssignmentJob.objects.filter(
        project=survey,
        scope=AssignmentJob.SCOPE_PROJECT,
        assignee=user,
    ).exists()
    is_admin = getattr(user, 'role', None) == User.Role.SUBADMIN
    if not (is_engineer or is_admin):
        raise PermissionError('Only the assigned engineer can accept this project')

    if survey.status == 'assigned':
        survey.status = 'active'
        survey.save(update_fields=['status', 'updated_at', 'last_activity_at'])
    elif survey.status == 'redo':
        # Engineer picks the project back up after an admin requested changes.
        survey.status = 'active'
        survey.save(update_fields=['status', 'updated_at', 'last_activity_at'])

    return {
        'project_id': str(survey.id),
        'name': survey.name,
        'status': survey.status,
        'source_ftth_project_id': survey.source_ftth_project_id,
    }


def submit_survey_project(project_id: str, user) -> dict:
    """Engineer submits their finished Survey copy → status becomes submitted.

    Only an engineer assigned to the project (project-scope AssignmentJob)
    or a SUBADMIN may submit. Status must currently be ``active``.
    """
    try:
        survey = Project.objects.get(pk=project_id)
    except Project.DoesNotExist:
        raise ValueError(f'Survey project {project_id} not found')

    is_engineer = AssignmentJob.objects.filter(
        project=survey,
        scope=AssignmentJob.SCOPE_PROJECT,
        assignee=user,
    ).exists()
    is_admin = getattr(user, 'role', None) == User.Role.SUBADMIN
    if not (is_engineer or is_admin):
        raise PermissionError('Only the assigned engineer can submit this project')

    if survey.status in ('active', 'redo'):
        survey.status = 'submitted'
        survey.save(update_fields=['status', 'updated_at', 'last_activity_at'])
    elif survey.status != 'submitted':
        raise ValueError(
            f"Cannot submit project in status '{survey.status}'; "
            'only active (or redo) projects can be submitted'
        )

    return {
        'project_id': str(survey.id),
        'name': survey.name,
        'status': survey.status,
        'source_ftth_project_id': survey.source_ftth_project_id,
    }


def review_survey_project(project_id: str, user, action: str) -> dict:
    """Admin review actions on a submitted Survey copy.

    Actions (SUBADMIN only):
      start_review  submitted     -> under_review   (admin starts reviewing)
      reviewed      under_review  -> reviewed       (admin finished reviewing)
      accept        reviewed      -> accepted       (final approval)
      redo          submitted | under_review | reviewed -> redo (send back)
      complete      accepted      -> completed      (close out)

    Returns the updated project summary.
    """
    try:
        survey = Project.objects.get(pk=project_id)
    except Project.DoesNotExist:
        raise ValueError(f'Survey project {project_id} not found')

    is_admin = getattr(user, 'role', None) == User.Role.SUBADMIN
    if not is_admin:
        raise PermissionError('Only a sub-admin can review projects')

    if not isinstance(action, str):
        raise ValueError("Review 'action' must be a string")
    action = action.strip().lower()
    transitions = {
        'start_review': (('submitted',), 'under_review'),
        'reviewed': (('under_review',), 'reviewed'),
        'accept': (('reviewed', 'under_review'), 'accepted'),
        'redo': (('submitted', 'under_review', 'reviewed'), 'redo'),
        'complete': (('accepted',), 'completed'),
    }
    if action not in transitions:
        raise ValueError(
            f"Unknown review action '{action}'; "
            'expected one of: ' + ', '.join(sorted(transitions))
        )

    allowed_from, target = transitions[action]
    if survey.status not in allowed_from:
        raise ValueError(
            f"Cannot {action} a project in status '{survey.status}'; "
            f"allowed from: {', '.join(allowed_from)}"
        )

    survey.status = target
    survey.save(update_fields=['status', 'updated_at', 'last_activity_at'])
    return {
        'project_id': str(survey.id),
        'name': survey.name,
        'status': survey.status,
        'action': action,
        'source_ftth_project_id': survey.source_ftth_project_id,
    }
