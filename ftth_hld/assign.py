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

import os
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from assignments.models import AssignmentJob
from projects.models import Feature, ImportSession, Project
from users.models import User

from .models import FtthProject
from .pipeline import generate_survey_package

SURVEY_SUFFIX = " - Survey"


def find_survey_copy(ftth_project_id: str):
    """Return the Survey copy Project for an HLD run, or None."""
    return Project.objects.filter(
        source_ftth_project_id=ftth_project_id
    ).first()


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
        raise ValueError("At least one engineer_id is required")
    try:
        hld = FtthProject.objects.get(pk=ftth_project_id)
    except FtthProject.DoesNotExist:
        raise ValueError(f"HLD project {ftth_project_id} not found")

    if hld.status != FtthProject.STATUS_COMPLETED:
        raise ValueError(
            f"HLD project {ftth_project_id} is not completed "
            f"(status={hld.status}); only completed runs can be assigned"
        )

    engineers = []
    for eid in engineer_ids:
        try:
            eng = User.objects.get(pk=eid)
        except User.DoesNotExist:
            raise ValueError(f"Engineer {eid} not found")
        if eng.role != User.Role.ENGINEER:
            raise ValueError(f"User {eid} is not an engineer")
        engineers.append(eng)
    primary_engineer = engineers[0]

    # ── 1-4. Everything inside ONE transaction: a failure mid-way must
    #         not leave an 'assigned' copy with zero features/jobs. ──────
    with transaction.atomic():
        # ── 1. Survey copy (reuse if it already exists) ─────────────────
        survey = find_survey_copy(ftth_project_id)
        if survey is None:
            survey = Project.objects.create(
                name=f"{hld.name or ftth_project_id}{SURVEY_SUFFIX}",
                description=(
                    f"Survey copy of HLD run '{hld.name or ftth_project_id}'. "
                    "Field-survey package auto-imported for the assigned engineer."
                ),
                region="Field Survey",
                status="assigned",
                source_ftth_project_id=ftth_project_id,
            )
        else:
            # Data-safety guard: once an engineer accepts the copy (active),
            # re-assigning would wipe their survey progress. Refuse instead.
            if survey.status in ("active", "submitted", "completed"):
                raise ValueError(
                    f"Survey copy '{survey.name}' is already {survey.status}; "
                    "re-assigning would erase survey data and approvals. "
                    "Re-assign only while the copy is pending."
                )
            survey.name = f"{hld.name or ftth_project_id}{SURVEY_SUFFIX}"
            survey.status = "assigned"
            survey.save(update_fields=["name", "status", "updated_at"])

        # ── 2. Generate + store the survey package ZIP ───────────────────
        zip_bytes = generate_survey_package(ftth_project_id)
        filename = f"{uuid.uuid4().hex}.zip"
        rel_path = os.path.join("imports", filename)
        full_path = os.path.join(settings.MEDIA_ROOT, rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "wb") as f:
            f.write(zip_bytes)

        ImportSession.objects.update_or_create(
            project=survey,
            defaults={
                "original_filename": f"{ftth_project_id}_survey_package.zip",
                "stored_file_path": full_path,
                "status": "imported",
                "validation_summary": None,
            },
        )

        # ── 3. Import the WGS84 GeoJSON layers as Feature rows ───────────
        from projects.api.import_views import discover_zip_layers, import_zip_layers

        layers = discover_zip_layers(full_path)
        layer_names = [l["name"] for l in layers]

        # Re-assigning the same HLD run must be idempotent: drop any
        # previously imported features so we never accumulate duplicates.
        # The enclosing transaction guarantees old features survive a
        # failed re-import.
        Feature.objects.filter(project=survey).delete()
        features_created = import_zip_layers(survey, full_path, layer_names)

        # Features imported as pending; a project-scope assignment marks the
        # whole project as assigned (matches existing assignment semantics).
        if features_created:
            Feature.objects.filter(project=survey).exclude(
                status=Feature.STATUS_ASSIGNED
            ).update(status=Feature.STATUS_ASSIGNED)

        # ── 4. Project-scope AssignmentJob per engineer (PK preserved) ───
        existing_jobs = list(AssignmentJob.objects.filter(
            project=survey, scope=AssignmentJob.SCOPE_PROJECT
        ))
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
    hld.save(update_fields=["assigned_engineer", "assigned_at", "updated_at"])

    return {
        "hl_project_id": ftth_project_id,
        "hl_name": hld.name,
        "survey_project_id": str(survey.id),
        "survey_project_name": survey.name,
        "survey_status": survey.status,
        "engineers": [
            {
                "id": str(eng.id),
                "email": eng.email,
                "full_name": eng.full_name,
            }
            for eng in engineers
        ],
        "assignment_job_ids": [str(j.id) for j in jobs],
        "layers": layer_names,
        "features_created": features_created,
    }


def accept_survey_project(project_id: str, user) -> dict:
    """Engineer accepts an assigned Survey copy → status becomes active.

    Only the assigned engineer (project-scope AssignmentJob) or a SUBADMIN
    may accept. Returns the updated project summary.
    """
    try:
        survey = Project.objects.get(pk=project_id)
    except Project.DoesNotExist:
        raise ValueError(f"Survey project {project_id} not found")

    is_engineer = AssignmentJob.objects.filter(
        project=survey,
        scope=AssignmentJob.SCOPE_PROJECT,
        assignee=user,
    ).exists()
    is_admin = getattr(user, "role", None) == User.Role.SUBADMIN
    if not (is_engineer or is_admin):
        raise PermissionError("Only the assigned engineer can accept this project")

    if survey.status == "assigned":
        survey.status = "active"
        survey.save(update_fields=["status", "updated_at", "last_activity_at"])

    return {
        "project_id": str(survey.id),
        "name": survey.name,
        "status": survey.status,
        "source_ftth_project_id": survey.source_ftth_project_id,
    }


def submit_survey_project(project_id: str, user) -> dict:
    """Engineer submits their finished Survey copy → status becomes submitted.

    Only an engineer assigned to the project (project-scope AssignmentJob)
    or a SUBADMIN may submit. Status must currently be ``active``.
    """
    try:
        survey = Project.objects.get(pk=project_id)
    except Project.DoesNotExist:
        raise ValueError(f"Survey project {project_id} not found")

    is_engineer = AssignmentJob.objects.filter(
        project=survey,
        scope=AssignmentJob.SCOPE_PROJECT,
        assignee=user,
    ).exists()
    is_admin = getattr(user, "role", None) == User.Role.SUBADMIN
    if not (is_engineer or is_admin):
        raise PermissionError("Only the assigned engineer can submit this project")

    if survey.status == "active":
        survey.status = "submitted"
        survey.save(update_fields=["status", "updated_at", "last_activity_at"])
    elif survey.status != "submitted":
        raise ValueError(
            f"Cannot submit project in status '{survey.status}'; "
            "only active projects can be submitted"
        )

    return {
        "project_id": str(survey.id),
        "name": survey.name,
        "status": survey.status,
        "source_ftth_project_id": survey.source_ftth_project_id,
    }
