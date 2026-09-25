"""Factories for the models the HLD/LLD API tests need.

Deliberately plain functions (no factory_boy dependency): each one creates the
minimum valid object for the field it fills, so a test reads as the domain
story it is checking — an HLD run, its survey copy, the engineer's edits, an
Approved Survey Version, an LLD run.
"""

from __future__ import annotations

import uuid

from ftth_hld.models import FtthProject, FtthLayer
from ftth_lld.models import ApprovedSurveyVersion, LldLayer, LldRun
from projects.models import Feature, Project, ProjectMember
from survey.models import SurveyFeature
from users.models import User

# A tiny valid WGS84 line — the code paths under test only pass geometry
# through, they never measure it.
LINE = {
    "type": "LineString",
    "coordinates": [[13.373030306946523, 52.44855475307314],
                    [13.373090289114723, 52.44858710395022]],
}
OTHER_LINE = {
    "type": "LineString",
    "coordinates": [[13.373030306946523, 52.44855475307314],
                    [13.374000000000000, 52.44870000000000]],
}


def make_user(email: str, role: str = User.Role.SUBADMIN, password: str = "pw-test-123456"):
    return User.objects.create_user(email=email, password=password, role=role)


def make_ftth_project(project_id: str | None = None, *, name: str = "Test HLD Run",
                      status: str = FtthProject.STATUS_COMPLETED,
                      created_by: User | None = None) -> FtthProject:
    return FtthProject.objects.create(
        project_id=project_id or uuid.uuid4().hex,
        name=name,
        status=status,
        created_by=created_by,
        progress=100 if status == FtthProject.STATUS_COMPLETED else 0,
    )


def make_survey_copy(ftth: FtthProject, *, name: str = "Survey copy", status: str = "active") -> Project:
    """The survey copy Project linked back to an HLD run."""
    return Project.objects.create(
        name=name,
        source_ftth_project_id=ftth.project_id,
        status=status,
    )


def make_feature(project: Project, *, geometry: dict | None = None,
                 properties: dict | None = None, layer_id: str = "trench_layer",
                 layer_name: str = "Feeder_Trench") -> Feature:
    return Feature.objects.create(
        project=project,
        layer_id=layer_id,
        layer_name=layer_name,
        geometry=geometry if geometry is not None else LINE,
        properties=properties if properties is not None else {"fid": 1},
    )


def make_survey_feature(project: Project, engineer: User, *, status: str = SurveyFeature.SurveyStatus.PENDING_REVIEW,
                        original_hld_feature: Feature | None = None,
                        survey_geometry: dict | None = None,
                        original_geometry: dict | None = None,
                        survey_attributes: dict | None = None,
                        original_attributes: dict | None = None,
                        change_reason: str = "field correction",
                        is_removal: bool = False,
                        gps_quality: str = "") -> SurveyFeature:
    return SurveyFeature.objects.create(
        project=project,
        engineer=engineer,
        original_hld_feature=original_hld_feature,
        layer_id="trench_layer",
        layer_name="Feeder_Trench",
        survey_geometry=survey_geometry if survey_geometry is not None else LINE,
        original_geometry=original_geometry if original_geometry is not None else LINE,
        survey_attributes=survey_attributes if survey_attributes is not None else {"depth_mm": 900},
        original_attributes=original_attributes if original_attributes is not None else {"depth_mm": 600},
        survey_status=status,
        change_reason=change_reason,
        is_removal=is_removal,
        gps_quality=gps_quality,
    )


def make_project_member(project: Project, user: User, *, role: str = ProjectMember.Role.REVIEWER,
                        added_by: User | None = None) -> ProjectMember:
    return ProjectMember.objects.create(project=project, user=user, role=role, added_by=added_by)


def make_asv(ftth: FtthProject, *, version: str = "AS-V01",
             dataset: dict | None = None, created_by: User | None = None) -> ApprovedSurveyVersion:
    return ApprovedSurveyVersion.objects.create(
        ftth_project=ftth,
        version=version,
        hld_version="HLD-V1",
        dataset=dataset if dataset is not None else {"type": "FeatureCollection", "features": []},
        summary={"features": 0},
        created_by=created_by,
    )


def make_lld_run(ftth: FtthProject, *, version: str = "LLD-V01", asv: ApprovedSurveyVersion | None = None,
                 status: str = LldRun.STATUS_RUNNING, mode: str = LldRun.MODE_VERIFY,
                 progress: int = 0, validation: dict | None = None,
                 run_by: User | None = None) -> LldRun:
    return LldRun.objects.create(
        ftth_project=ftth,
        lld_version=version,
        hld_version="HLD-V1",
        approved_survey_version=asv,
        mode=mode,
        status=status,
        progress=progress,
        validation=validation if validation is not None else {},
        run_by=run_by,
    )


def make_lld_layer(run: LldRun, *, name: str = "final_trenches",
                   geojson: dict | None = None, feature_count: int = 1) -> LldLayer:
    return LldLayer.objects.create(
        lld_run=run,
        name=name,
        geojson=geojson if geojson is not None else {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "geometry": LINE,
                          "properties": {"layer": name, "feature_id": "1"}}],
        },
        feature_count=feature_count,
    )


def make_hld_layer(ftth: FtthProject, *, name: str = "objects",
                   geojson: dict | None = None, feature_count: int = 1) -> FtthLayer:
    return FtthLayer.objects.create(
        ftth_project=ftth,
        name=name,
        geojson=geojson if geojson is not None else {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "geometry": LINE, "properties": {"fid": 1}}],
        },
        feature_count=feature_count,
    )
