"""One span is one job.

The assignment page resolves a span to the components that share its id and
used to POST one feature job per component, so a 78-component span became 78
rows on the engineer's list and deleting one left the span only partly
assigned. A span job covers the whole span in a single row.

Run with:
    python manage.py test --settings=config.test_settings assignments
"""

from __future__ import annotations

from django.test import TestCase
from rest_framework.test import APIClient

from assignments.models import AssignmentJob
from projects.models import Feature
from testutils.factories import make_feature, make_ftth_project, make_survey_copy, make_user
from users.models import User


class SpanScopeAssignmentTests(TestCase):
    def setUp(self):
        self.planner = make_user('planner@example.com', role=User.Role.SUBADMIN)
        self.engineer = make_user('engineer@example.com', role=User.Role.ENGINEER)
        self.project = make_survey_copy(make_ftth_project())
        self.trench = make_feature(
            self.project, layer_id='final_trenches', layer_name='final_trenches'
        )
        self.duct = make_feature(
            self.project, layer_id='distribution_ducts', layer_name='distribution_ducts'
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.planner)

    def create(self, **overrides):
        payload = {
            'project': str(self.project.id),
            'scope': 'span',
            'span_id': 'TRENCH-00012',
            'feature_ids': [str(self.trench.id), str(self.duct.id)],
            'assignee': str(self.engineer.id),
        }
        payload.update(overrides)
        return self.client.post('/api/assignments/', payload, format='json')

    def test_one_span_is_one_job_over_all_its_components(self):
        response = self.create()

        self.assertEqual(response.status_code, 201, response.data)
        job = AssignmentJob.objects.get(pk=response.data['id'])
        self.assertEqual(job.scope, AssignmentJob.SCOPE_SPAN)
        self.assertEqual(job.span_id, 'TRENCH-00012')
        self.assertEqual(
            set(job.covered_feature_ids), {str(self.trench.id), str(self.duct.id)}
        )
        self.trench.refresh_from_db()
        self.duct.refresh_from_db()
        self.assertEqual(self.trench.status, Feature.STATUS_ASSIGNED)
        self.assertEqual(self.duct.status, Feature.STATUS_ASSIGNED)
        self.assertEqual(AssignmentJob.objects.count(), 1)

    def test_reassigning_the_span_replaces_the_same_engineers_claim(self):
        self.create()
        self.create()

        self.assertEqual(AssignmentJob.objects.filter(scope='span').count(), 1)

    def test_a_component_from_another_project_is_rejected(self):
        foreign = make_survey_copy(make_ftth_project())
        foreign_feature = make_feature(foreign, layer_id='final_trenches')

        response = self.create(feature_ids=[str(self.trench.id), str(foreign_feature.id)])

        self.assertEqual(response.status_code, 400)
        self.assertFalse(AssignmentJob.objects.exists())

    def test_deleting_the_span_job_releases_its_components(self):
        response = self.create()

        self.client.delete(f'/api/assignments/{response.data["id"]}/')

        self.trench.refresh_from_db()
        self.duct.refresh_from_db()
        self.assertEqual(self.trench.status, Feature.STATUS_PENDING)
        self.assertEqual(self.duct.status, Feature.STATUS_PENDING)
