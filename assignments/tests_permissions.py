"""The assignment and review surface requires the right role.

Regression tests for the 2026-11-05 audit: the module had no
``permission_classes`` at all, so an engineer could approve their own submitted
work, delete another planner's assignment, and submit for somebody else by
naming them in the request body.
"""

from __future__ import annotations

from django.test import TestCase
from rest_framework.test import APIClient

from assignments.models import AssignmentJob
from projects.models import Feature
from testutils.factories import make_feature, make_ftth_project, make_survey_copy, make_user
from users.models import User


class ReviewPermissionTests(TestCase):
    def setUp(self):
        self.planner = make_user('planner-review@example.com', role=User.Role.SUBADMIN)
        self.engineer = make_user('engineer-review@example.com', role=User.Role.ENGINEER)
        self.other = make_user('other-review@example.com', role=User.Role.ENGINEER)

        self.project = make_survey_copy(make_ftth_project())
        self.feature = make_feature(self.project)
        self.job = AssignmentJob.objects.create(
            project=self.project,
            scope=AssignmentJob.SCOPE_FEATURE,
            feature=self.feature,
            assignee=self.engineer,
        )
        Feature.objects.filter(pk=self.feature.pk).update(status=Feature.STATUS_UNDER_REVIEW)

        self.client = APIClient()

    def test_an_engineer_cannot_approve(self):
        self.client.force_authenticate(user=self.engineer)

        response = self.client.post(
            '/api/features/approve/',
            {'feature_ids': [str(self.feature.id)]},
            format='json',
        )

        self.assertEqual(response.status_code, 403)
        self.feature.refresh_from_db()
        self.assertEqual(self.feature.status, Feature.STATUS_UNDER_REVIEW)

    def test_a_planner_can_approve(self):
        self.client.force_authenticate(user=self.planner)

        response = self.client.post(
            '/api/features/approve/',
            {'feature_ids': [str(self.feature.id)]},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.feature.refresh_from_db()
        self.assertEqual(self.feature.status, Feature.STATUS_APPROVED)

    def test_an_engineer_cannot_reject(self):
        self.client.force_authenticate(user=self.engineer)

        response = self.client.post(
            '/api/features/reject/',
            {'feature_ids': [str(self.feature.id)], 'rejection_reason': 'no'},
            format='json',
        )

        self.assertEqual(response.status_code, 403)

    def test_an_engineer_cannot_delete_a_job(self):
        self.client.force_authenticate(user=self.engineer)

        response = self.client.delete(f'/api/assignments/{self.job.id}/')

        self.assertEqual(response.status_code, 403)
        self.assertTrue(AssignmentJob.objects.filter(id=self.job.id).exists())

    def test_submit_uses_the_caller_not_the_body(self):
        # The feature belongs to `other`; naming them in the body must not
        # authorise `engineer` to submit it.
        foreign = make_feature(self.project, layer_id='spans', layer_name='spans')
        AssignmentJob.objects.create(
            project=self.project,
            scope=AssignmentJob.SCOPE_FEATURE,
            feature=foreign,
            assignee=self.other,
        )
        Feature.objects.filter(pk=foreign.pk).update(status=Feature.STATUS_ASSIGNED)

        self.client.force_authenticate(user=self.engineer)
        response = self.client.post(
            '/api/features/submit/',
            {'feature_ids': [str(foreign.id)], 'engineer': str(self.other.id)},
            format='json',
        )

        self.assertEqual(response.status_code, 403)
        foreign.refresh_from_db()
        self.assertEqual(foreign.status, Feature.STATUS_ASSIGNED)

    def test_submit_succeeds_for_a_feature_with_two_jobs(self):
        # Project scope plus feature scope: the old join counted this row twice
        # and refused a valid submission with an empty "missing" set.
        AssignmentJob.objects.create(
            project=self.project,
            scope=AssignmentJob.SCOPE_PROJECT,
            assignee=self.engineer,
        )
        Feature.objects.filter(pk=self.feature.pk).update(status=Feature.STATUS_ASSIGNED)

        self.client.force_authenticate(user=self.engineer)
        response = self.client.post(
            '/api/features/submit/',
            {'feature_ids': [str(self.feature.id)]},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.feature.refresh_from_db()
        self.assertEqual(self.feature.status, Feature.STATUS_UNDER_REVIEW)
