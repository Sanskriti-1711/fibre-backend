"""Area (polygon) assignments from an HLD run.

The planner picks the MFG service areas / polygons on the HLD output and the
engineer may then edit only what those areas cover. These pin the three things
that make that true in the database:

* one polygon-scope job per engineer per area, with the coverage snapshot the
  map and the edit guard both read,
* only covered features moving to ``assigned`` (the rest of the copy stays
  pending, so the engineer's map greys it out), and
* the API forwarding the selected areas instead of assigning the whole run.

Run with:
    python manage.py test --settings=config.test_settings ftth_hld.tests.test_assign_areas
"""

from __future__ import annotations

from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from assignments.models import AssignmentJob
from ftth_hld.assign import accept_survey_project, assign_hld_project, preview_area_coverage
from ftth_hld.models import FtthLayer
from projects.models import Feature
from testutils.factories import make_feature, make_ftth_project, make_survey_copy, make_user
from users.models import User

SQUARE = {
    'type': 'Polygon',
    'coordinates': [[[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, 0.0]]],
}
FAR_SQUARE = {
    'type': 'Polygon',
    'coordinates': [
        [[100.0, 100.0], [110.0, 100.0], [110.0, 110.0], [100.0, 110.0], [100.0, 100.0]]
    ],
}
INSIDE_LINE = {'type': 'LineString', 'coordinates': [[5.0, 5.0], [6.0, 6.0]]}
OUTSIDE_LINE = {'type': 'LineString', 'coordinates': [[50.0, 50.0], [51.0, 51.0]]}


class AssignPolygonAreaTests(TestCase):
    def setUp(self):
        self.engineer = make_user('area-engineer@example.com', role=User.Role.ENGINEER)
        self.other = make_user('area-other@example.com', role=User.Role.ENGINEER)
        self.hld = make_ftth_project(name='Area run')
        self.survey = make_survey_copy(self.hld, name='Area run - Survey', status='assigned')
        self.inside = make_feature(self.survey, geometry=INSIDE_LINE, properties={'fid': 1})
        self.outside = make_feature(self.survey, geometry=OUTSIDE_LINE, properties={'fid': 2})

    def assign(self, areas, engineers=None):
        """Run the service without touching the real package files.

        ``keeps_rows`` is what a copy imported from the current package answers,
        so the import is skipped and the test describes only the assignment.
        """
        with (
            mock.patch(
                'ftth_hld.assign._survey_package',
                return_value=(None, 'imports/package.zip', 'package-hash'),
            ),
            mock.patch('ftth_hld.assign._holds_this_import', return_value=True),
            mock.patch('projects.api.import_views.discover_zip_layers', return_value=[]),
        ):
            return assign_hld_project(
                self.hld.project_id,
                [eng.id for eng in (engineers or [self.engineer])],
                areas=areas,
            )

    def test_one_area_assigns_only_the_features_it_covers(self):
        result = self.assign([{'polygon_id': 'POLY-1', 'name': 'North', 'geometry': SQUARE}])

        job = AssignmentJob.objects.get(scope=AssignmentJob.SCOPE_POLYGON)
        self.assertEqual(job.polygon_id, 'POLY-1')
        self.assertEqual(job.polygon_name, 'North')
        self.assertEqual(job.covered_feature_ids, [str(self.inside.id)])
        # The id is stamped, not looked up through a relationship: the snapshot
        # is what the map and the edit guard read.
        self.assertEqual(Feature.objects.get(pk=self.inside.pk).status, Feature.STATUS_ASSIGNED)
        self.assertEqual(Feature.objects.get(pk=self.outside.pk).status, Feature.STATUS_PENDING)
        # An area selection is not a project-wide claim.
        self.assertFalse(
            AssignmentJob.objects.filter(scope=AssignmentJob.SCOPE_PROJECT).exists()
        )
        self.assertEqual(result['scope'], 'polygon')
        self.assertEqual(result['areas'][0]['feature_count'], 1)

    def test_several_engineers_share_one_area_without_recomputing_it(self):
        result = self.assign(
            [{'polygon_id': 'POLY-1', 'geometry': SQUARE}],
            engineers=[self.engineer, self.other],
        )

        jobs = AssignmentJob.objects.filter(scope=AssignmentJob.SCOPE_POLYGON)
        self.assertEqual(jobs.count(), 2)
        self.assertEqual({job.assignee_id for job in jobs}, {self.engineer.id, self.other.id})
        self.assertTrue(all(job.covered_feature_ids == [str(self.inside.id)] for job in jobs))
        self.assertEqual(len(result['assignment_job_ids']), 2)

    def test_multiple_areas_become_multiple_jobs(self):
        result = self.assign(
            [
                {'polygon_id': 'POLY-1', 'geometry': SQUARE},
                {'polygon_id': 'POLY-2', 'geometry': FAR_SQUARE},
            ]
        )

        self.assertEqual(
            set(
                AssignmentJob.objects.filter(scope=AssignmentJob.SCOPE_POLYGON).values_list(
                    'polygon_id', flat=True
                )
            ),
            {'POLY-1', 'POLY-2'},
        )
        self.assertEqual(
            [area['feature_count'] for area in result['areas']], [1, 0]
        )

    def test_an_area_named_only_by_its_polygon_row_is_resolved_from_the_copy(self):
        polygon = make_feature(
            self.survey,
            geometry=SQUARE,
            properties={'POLYGON_ID': 'POLY-1', 'name': 'North'},
            layer_id='polygons',
            layer_name='polygons',
        )

        self.assign([{'feature_id': str(polygon.id)}])

        job = AssignmentJob.objects.get(scope=AssignmentJob.SCOPE_POLYGON)
        self.assertEqual(job.polygon_id, str(polygon.id))
        self.assertEqual(job.polygon_name, 'North')
        self.assertIn(str(self.inside.id), job.covered_feature_ids)

    def test_an_area_with_no_geometry_and_no_matching_polygon_is_rejected(self):
        with self.assertRaisesMessage(ValueError, 'no geometry'):
            self.assign([{'polygon_id': 'MISSING'}])

    def test_an_empty_area_selection_is_rejected_rather_than_assigning_everything(self):
        """There is no project-wide fallback: survey work is handed over by area."""
        with self.assertRaisesMessage(ValueError, 'At least one area'):
            self.assign([])

    def test_an_assignment_with_no_areas_at_all_is_rejected(self):
        with self.assertRaisesMessage(ValueError, 'At least one area'):
            self.assign(None)

    def test_area_assignment_retires_an_old_project_scope_job(self):
        """No project job may survive: it would grant every feature."""
        AssignmentJob.objects.create(
            project=self.survey, scope=AssignmentJob.SCOPE_PROJECT, assignee=self.other
        )
        Feature.objects.filter(project=self.survey).update(status=Feature.STATUS_ASSIGNED)

        self.assign([{'polygon_id': 'POLY-1', 'geometry': SQUARE}])

        self.assertFalse(
            AssignmentJob.objects.filter(
                project=self.survey, scope=AssignmentJob.SCOPE_PROJECT
            ).exists()
        )
        # The old job's uncovered rows are not silently left assigned.
        self.assertEqual(Feature.objects.get(pk=self.outside.pk).status, Feature.STATUS_PENDING)

    def test_an_area_assigned_engineer_can_accept_and_submit_the_copy(self):
        """Area jobs count for the copy's lifecycle; there is no project job."""
        self.assign([{'polygon_id': 'POLY-1', 'geometry': SQUARE}])
        self.assertFalse(
            AssignmentJob.objects.filter(scope=AssignmentJob.SCOPE_PROJECT).exists()
        )

        accepted = accept_survey_project(self.survey.id, self.engineer)

        self.assertEqual(accepted['status'], 'active')

    def test_adding_an_area_after_field_work_only_touches_the_new_area(self):
        """A second area is added to a copy the engineer is already working on.

        The package is unchanged, so the rows are kept (`keeps_rows`) and the
        data-safety guard does not fire — only the new area's coverage moves.
        """
        self.assign([{'polygon_id': 'POLY-1', 'geometry': SQUARE}])
        Feature.objects.filter(pk=self.inside.pk).update(status=Feature.STATUS_UNDER_REVIEW)

        with mock.patch('ftth_hld.assign._holds_field_data', return_value=True):
            self.assign(
                [
                    {'polygon_id': 'POLY-1', 'geometry': SQUARE},
                    {'polygon_id': 'POLY-2', 'geometry': FAR_SQUARE},
                ]
            )

        self.assertEqual(
            AssignmentJob.objects.filter(scope=AssignmentJob.SCOPE_POLYGON).count(), 2
        )
        # The engineer's submitted work keeps its status.
        self.assertEqual(
            Feature.objects.get(pk=self.inside.pk).status, Feature.STATUS_UNDER_REVIEW
        )


class PreviewAreaCoverageTests(TestCase):
    """The count the Assign jobs summary shows before anything is written."""

    def setUp(self):
        self.hld = make_ftth_project(name='Preview run')
        self.copy = make_survey_copy(self.hld, name='Preview run - Survey')
        make_feature(self.copy, geometry=INSIDE_LINE, properties={'fid': 1})
        make_feature(self.copy, geometry=OUTSIDE_LINE, properties={'fid': 2})

    def test_counts_the_rows_in_the_survey_copy(self):
        result = preview_area_coverage(
            self.hld.project_id,
            [{'polygon_id': 'POLY-1', 'name': 'North', 'geometry': SQUARE}],
        )

        self.assertEqual(result['source'], 'survey')
        self.assertEqual(result['feature_count'], 1)
        self.assertEqual(result['areas'][0]['feature_count'], 1)
        self.assertEqual(result['survey_project_id'], str(self.copy.id))

    def test_counts_the_run_layers_when_no_copy_exists(self):
        Project = self.copy.__class__
        Project.objects.filter(pk=self.copy.pk).delete()
        FtthLayer.objects.create(
            ftth_project=self.hld,
            name='final_trenches',
            feature_count=2,
            geojson={
                'type': 'FeatureCollection',
                'features': [
                    {'type': 'Feature', 'properties': {'fid': 1}, 'geometry': INSIDE_LINE},
                    {'type': 'Feature', 'properties': {'fid': 2}, 'geometry': OUTSIDE_LINE},
                ],
            },
        )

        result = preview_area_coverage(
            self.hld.project_id,
            [{'polygon_id': 'POLY-1', 'geometry': SQUARE}],
        )

        self.assertEqual(result['source'], 'run')
        self.assertEqual(result['feature_count'], 1)

    def test_an_area_without_geometry_is_refused_before_a_copy_exists(self):
        self.copy.__class__.objects.filter(pk=self.copy.pk).delete()

        with self.assertRaisesMessage(ValueError, 'has no geometry'):
            preview_area_coverage(self.hld.project_id, [{'polygon_id': 'POLY-1'}])

    def test_the_view_returns_the_preview_for_a_planner(self):
        planner = make_user('preview-planner@example.com', role=User.Role.SUBADMIN)
        client = APIClient()
        client.force_authenticate(user=planner)

        response = client.post(
            f'/api/ftth/hld/projects/{self.hld.project_id}/areas/coverage/',
            {'areas': [{'polygon_id': 'POLY-1', 'name': 'North', 'geometry': SQUARE}]},
            format='json',
        )

        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body['feature_count'], 1)
        self.assertEqual(body['areas'][0]['polygon_id'], 'POLY-1')

    def test_an_engineer_cannot_preview_coverage(self):
        engineer = make_user('preview-engineer@example.com', role=User.Role.ENGINEER)
        client = APIClient()
        client.force_authenticate(user=engineer)

        response = client.post(
            f'/api/ftth/hld/projects/{self.hld.project_id}/areas/coverage/',
            {'areas': []},
            format='json',
        )

        self.assertEqual(response.status_code, 403)


class AssignViewAreaTests(TestCase):
    def setUp(self):
        self.planner = make_user('area-planner@example.com', role=User.Role.SUBADMIN)
        self.engineer = make_user('area-view-engineer@example.com', role=User.Role.ENGINEER)
        self.hld = make_ftth_project()
        self.client = APIClient()
        self.client.force_authenticate(user=self.planner)

    def test_the_view_forwards_the_selected_areas(self):
        payload = {
            'engineer_ids': [str(self.engineer.id)],
            'areas': [{'polygon_id': 'POLY-1', 'name': 'North', 'geometry': SQUARE}],
        }
        with mock.patch(
            'ftth_hld.api.assign_hld_project',
            return_value={'survey_project_id': 'copy-1', 'scope': 'polygon'},
        ) as assign:
            response = self.client.post(
                f'/api/ftth/hld/projects/{self.hld.project_id}/assign/',
                payload,
                format='json',
            )

        self.assertEqual(response.status_code, 201, response.content)
        areas = assign.call_args.kwargs['areas']
        self.assertEqual(areas[0]['polygon_id'], 'POLY-1')
        self.assertEqual(areas[0]['geometry'], SQUARE)

    def test_polygon_ids_are_expanded_into_areas(self):
        with mock.patch(
            'ftth_hld.api.assign_hld_project',
            return_value={'survey_project_id': 'copy-1'},
        ) as assign:
            response = self.client.post(
                f'/api/ftth/hld/projects/{self.hld.project_id}/assign/',
                {'engineer_ids': [str(self.engineer.id)], 'polygon_ids': 'A-1,A-2'},
                format='json',
            )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(
            [area['polygon_id'] for area in assign.call_args.kwargs['areas']],
            ['A-1', 'A-2'],
        )

    def test_a_malformed_area_payload_is_a_400(self):
        with mock.patch('ftth_hld.api.assign_hld_project') as assign:
            response = self.client.post(
                f'/api/ftth/hld/projects/{self.hld.project_id}/assign/',
                {'engineer_ids': [str(self.engineer.id)], 'areas': 'not-json'},
                format='json',
            )

        self.assertEqual(response.status_code, 400)
        assign.assert_not_called()
