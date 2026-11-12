"""The by-design lookup joins a design feature id to its supervision row.

The LLD map and the design pages speak in engine (design) feature ids, while an
assignment keys on a ``projects.Feature`` row. That join is the whole point of
``GET /api/projects/<id>/features/by-design/<design_id>/``, so these tests pin
the mapping, the miss, the case folding and which job answers when more than one
covers the feature.

Run with:
    python manage.py test --settings=config.test_settings projects
"""

from __future__ import annotations

from django.test import TestCase
from rest_framework.test import APIClient

from assignments.models import AssignmentJob
from projects.models import Feature
from testutils.factories import make_feature, make_ftth_project, make_survey_copy, make_user
from users.models import User

DESIGN_ID = 'LLD-spans-abc123'


class DesignFeatureLookupTests(TestCase):
    def setUp(self):
        self.planner = make_user('planner@example.com', role=User.Role.SUBADMIN)
        self.engineer = make_user('engineer@example.com', role=User.Role.ENGINEER)
        self.other = make_user('other@example.com', role=User.Role.ENGINEER)

        self.project = make_survey_copy(make_ftth_project())
        self.feature = make_feature(
            self.project,
            layer_id='spans',
            layer_name='spans',
            properties={'feature_id': DESIGN_ID, 'cable_type': '48F'},
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.planner)

    def url(self, design_id: str) -> str:
        return f'/api/projects/{self.project.id}/features/by-design/{design_id}/'

    def assign(self, *, scope: str, assignee: User, layer_id: str | None = None) -> AssignmentJob:
        return AssignmentJob.objects.create(
            project=self.project,
            scope=scope,
            assignee=assignee,
            layer_id=layer_id,
            feature=self.feature if scope == AssignmentJob.SCOPE_FEATURE else None,
        )

    def test_resolves_the_row_and_the_job_covering_it(self):
        """The row the design id names, plus who holds it."""
        self.assign(scope=AssignmentJob.SCOPE_FEATURE, assignee=self.engineer)

        response = self.client.get(self.url(DESIGN_ID))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['design_id'], DESIGN_ID)
        self.assertEqual(response.data['feature']['id'], str(self.feature.id))
        self.assertEqual(response.data['feature']['layer_id'], 'spans')
        # Creating the job is what puts the row in front of an engineer.
        self.assertEqual(response.data['feature']['status'], Feature.STATUS_ASSIGNED)
        self.assertEqual(response.data['assignment']['scope'], AssignmentJob.SCOPE_FEATURE)
        self.assertEqual(response.data['assignment']['assignee']['email'], 'engineer@example.com')

    def test_unknown_design_id_is_not_found(self):
        """A design id with no supervision copy is a miss, not an error."""
        response = self.client.get(self.url('LLD-spans-does-not-exist'))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data['design_id'], 'LLD-spans-does-not-exist')

    def test_design_id_match_ignores_case(self):
        """Engine ids are case-sensitive in principle, but a link may not be."""
        response = self.client.get(self.url(DESIGN_ID.upper()))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['feature']['id'], str(self.feature.id))

    def test_feature_job_beats_the_layer_and_project_jobs(self):
        self.assign(scope=AssignmentJob.SCOPE_PROJECT, assignee=self.other)
        self.assign(scope=AssignmentJob.SCOPE_LAYER, assignee=self.other, layer_id='spans')
        self.assign(scope=AssignmentJob.SCOPE_FEATURE, assignee=self.engineer)

        response = self.client.get(self.url(DESIGN_ID))

        self.assertEqual(response.data['assignment']['scope'], AssignmentJob.SCOPE_FEATURE)
        self.assertEqual(response.data['assignment']['assignee']['email'], 'engineer@example.com')

    def test_layer_job_beats_the_project_job(self):
        self.assign(scope=AssignmentJob.SCOPE_PROJECT, assignee=self.other)
        self.assign(scope=AssignmentJob.SCOPE_LAYER, assignee=self.engineer, layer_id='spans')

        response = self.client.get(self.url(DESIGN_ID))

        self.assertEqual(response.data['assignment']['scope'], AssignmentJob.SCOPE_LAYER)
        # Which layer the job is on travels with it, so the map can name the job
        # instead of only saying "assigned".
        self.assertEqual(response.data['assignment']['layer_id'], 'spans')
        self.assertEqual(response.data['assignment']['assignee']['email'], 'engineer@example.com')

    def test_a_project_job_covers_every_feature(self):
        self.assign(scope=AssignmentJob.SCOPE_PROJECT, assignee=self.engineer)

        response = self.client.get(self.url(DESIGN_ID))

        self.assertEqual(response.data['assignment']['scope'], AssignmentJob.SCOPE_PROJECT)
        self.assertEqual(response.data['assignment']['assignee']['email'], 'engineer@example.com')

    def test_a_span_job_answers_for_its_components(self):
        """A span job names its components, so it covers this feature too."""
        AssignmentJob.objects.create(
            project=self.project,
            scope=AssignmentJob.SCOPE_SPAN,
            span_id='TRENCH-00012',
            covered_feature_ids=[str(self.feature.id)],
            assignee=self.engineer,
        )

        response = self.client.get(self.url(DESIGN_ID))

        self.assertEqual(response.data['assignment']['scope'], AssignmentJob.SCOPE_SPAN)
        self.assertEqual(response.data['assignment']['span_id'], 'TRENCH-00012')
        self.assertEqual(response.data['assignment']['assignee']['email'], 'engineer@example.com')

    def test_a_span_job_that_does_not_cover_the_row_is_ignored(self):
        # A span job now has to name a real, single-layer component set. Use
        # another feature in the same layer, so this job is valid but still does
        # not cover the by-design row under test.
        other_feature = make_feature(
            self.project,
            layer_id='spans',
            layer_name='spans',
            properties={'feature_id': 'LLD-spans-other'},
        )
        AssignmentJob.objects.create(
            project=self.project,
            scope=AssignmentJob.SCOPE_SPAN,
            span_id='TRENCH-OTHER',
            layer_id='spans',
            covered_feature_ids=[str(other_feature.id)],
            assignee=self.other,
        )

        response = self.client.get(self.url(DESIGN_ID))

        self.assertIsNone(response.data['assignment'])

    def test_feature_job_beats_the_span_job(self):
        AssignmentJob.objects.create(
            project=self.project,
            scope=AssignmentJob.SCOPE_SPAN,
            span_id='TRENCH-00012',
            covered_feature_ids=[str(self.feature.id)],
            assignee=self.other,
        )
        self.assign(scope=AssignmentJob.SCOPE_FEATURE, assignee=self.engineer)

        response = self.client.get(self.url(DESIGN_ID))

        self.assertEqual(response.data['assignment']['scope'], AssignmentJob.SCOPE_FEATURE)
        self.assertEqual(response.data['assignment']['assignee']['email'], 'engineer@example.com')

    def test_no_job_is_reported_as_unassigned(self):
        response = self.client.get(self.url(DESIGN_ID))

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data['assignment'])
