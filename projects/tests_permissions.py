"""Project detail is scoped, and deleting or importing is a planner action.

Regression tests for the 2026-11-05 audit: ``ProjectDetailAPIView`` read and
deleted any project by uuid while only the *list* endpoint scoped engineers, and
the GeoPackage import never looked at ``request.user`` at all.
"""

from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from assignments.models import AssignmentJob
from projects.models import Project
from testutils.factories import make_feature, make_ftth_project, make_survey_copy, make_user
from users.models import User


class ProjectDetailPermissionTests(TestCase):
    def setUp(self):
        self.planner = make_user('planner-audit@example.com', role=User.Role.SUBADMIN)
        self.engineer = make_user('engineer-audit@example.com', role=User.Role.ENGINEER)
        self.project = make_survey_copy(make_ftth_project())
        make_feature(self.project)
        self.client = APIClient()

    def assign(self):
        return AssignmentJob.objects.create(
            project=self.project,
            scope=AssignmentJob.SCOPE_PROJECT,
            assignee=self.engineer,
        )

    def test_an_engineer_cannot_read_a_project_they_are_not_assigned_to(self):
        self.client.force_authenticate(user=self.engineer)

        response = self.client.get(f'/api/projects/{self.project.id}/')

        self.assertEqual(response.status_code, 403)

    def test_an_assigned_engineer_can_read_the_project(self):
        self.assign()
        self.client.force_authenticate(user=self.engineer)

        response = self.client.get(f'/api/projects/{self.project.id}/')

        self.assertEqual(response.status_code, 200)

    def test_an_engineer_cannot_delete_any_project(self):
        # Assigned on purpose: deletion is a planner action even for their own
        # project, because it cascades to every feature and assignment.
        self.assign()
        self.client.force_authenticate(user=self.engineer)

        response = self.client.delete(f'/api/projects/{self.project.id}/')

        self.assertEqual(response.status_code, 403)
        self.assertTrue(Project.objects.filter(id=self.project.id).exists())

    def test_a_planner_can_delete_a_project(self):
        self.client.force_authenticate(user=self.planner)

        response = self.client.delete(f'/api/projects/{self.project.id}/')

        self.assertEqual(response.status_code, 204)
        self.assertFalse(Project.objects.filter(id=self.project.id).exists())

    def test_an_engineer_cannot_import_a_geopackage(self):
        self.client.force_authenticate(user=self.engineer)

        response = self.client.post(
            f'/api/projects/{self.project.id}/import/upload/',
            {'file': SimpleUploadedFile('data.gpkg', b'not-really-a-gpkg')},
            format='multipart',
        )

        self.assertEqual(response.status_code, 403)
