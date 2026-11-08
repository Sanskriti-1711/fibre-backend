"""Deleting an assignment job gives its features back.

``AssignmentJob.save()`` sets ``assigned`` on the rows a job covers; nothing
used to take that back, so deleting a job left a row claiming an engineer who
no longer had it — and, worse, looking unavailable to the next assignment.

These pin the release, and the two ways it must not over-reach: a wider job that
still covers the row, and work already past ``assigned``.

Run with:
    python manage.py test --settings=config.test_settings assignments
"""

from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from assignments.models import AssignmentJob
from projects.models import Feature
from supervision.models import FeaturePhoto
from testutils.factories import make_feature, make_ftth_project, make_survey_copy, make_user
from users.models import User


class AssignmentReleaseTests(TestCase):
    def setUp(self):
        self.engineer = make_user('engineer@example.com', role=User.Role.ENGINEER)
        self.other = make_user('other@example.com', role=User.Role.ENGINEER)

        self.project = make_survey_copy(make_ftth_project())
        self.span = make_feature(self.project, layer_id='spans', layer_name='spans')
        self.chamber = make_feature(self.project, layer_id='chambers', layer_name='chambers')

    def job(self, scope: str, *, assignee: User | None = None, **fields) -> AssignmentJob:
        return AssignmentJob.objects.create(
            project=self.project,
            scope=scope,
            assignee=assignee or self.engineer,
            **fields,
        )

    def status_of(self, feature: Feature) -> str:
        feature.refresh_from_db()
        return feature.status

    def test_deleting_a_feature_job_puts_the_row_back_to_pending(self):
        job = self.job(AssignmentJob.SCOPE_FEATURE, feature=self.span)
        self.assertEqual(self.status_of(self.span), Feature.STATUS_ASSIGNED)

        job.delete()

        self.assertEqual(self.status_of(self.span), Feature.STATUS_PENDING)

    def test_a_layer_job_that_still_covers_the_row_keeps_it_assigned(self):
        self.job(AssignmentJob.SCOPE_LAYER, layer_id='spans')
        feature_job = self.job(AssignmentJob.SCOPE_FEATURE, feature=self.span, assignee=self.other)

        feature_job.delete()

        self.assertEqual(self.status_of(self.span), Feature.STATUS_ASSIGNED)

    def test_a_project_job_that_still_covers_the_row_keeps_it_assigned(self):
        self.job(AssignmentJob.SCOPE_PROJECT)
        feature_job = self.job(AssignmentJob.SCOPE_FEATURE, feature=self.span, assignee=self.other)

        feature_job.delete()

        self.assertEqual(self.status_of(self.span), Feature.STATUS_ASSIGNED)

    def test_deleting_a_layer_job_releases_only_its_own_layer(self):
        self.job(AssignmentJob.SCOPE_LAYER, layer_id='spans')
        self.assertEqual(self.status_of(self.span), Feature.STATUS_ASSIGNED)
        self.assertEqual(self.status_of(self.chamber), Feature.STATUS_PENDING)

        AssignmentJob.objects.filter(scope=AssignmentJob.SCOPE_LAYER).delete()

        self.assertEqual(self.status_of(self.span), Feature.STATUS_PENDING)
        self.assertEqual(self.status_of(self.chamber), Feature.STATUS_PENDING)

    def test_deleting_a_project_job_releases_the_project_but_not_a_narrower_job(self):
        self.job(AssignmentJob.SCOPE_PROJECT)
        self.job(AssignmentJob.SCOPE_FEATURE, feature=self.chamber, assignee=self.other)

        AssignmentJob.objects.filter(scope=AssignmentJob.SCOPE_PROJECT).delete()

        # The project-wide job is gone; the chamber's own job still holds it.
        self.assertEqual(self.status_of(self.span), Feature.STATUS_PENDING)
        self.assertEqual(self.status_of(self.chamber), Feature.STATUS_ASSIGNED)

    def test_delete_endpoint_returns_the_feature_to_pending(self):
        """The request path, not just the model.

        The release hangs off ``post_delete``, so it only happens if the view
        really deletes the row. A view that soft-deleted, or marked the job
        gone with an ``update()``, would leave the feature assigned with no job
        behind it -- and every model-level test here would still pass.
        """
        planner = make_user('planner@example.com', role=User.Role.SUBADMIN)
        job = self.job(AssignmentJob.SCOPE_FEATURE, feature=self.span)
        self.assertEqual(self.status_of(self.span), Feature.STATUS_ASSIGNED)
        client = APIClient()
        client.force_authenticate(user=planner)

        response = client.delete(f'/api/assignments/{job.id}/')

        self.assertEqual(response.status_code, 204)
        self.assertFalse(AssignmentJob.objects.filter(pk=job.id).exists())
        self.assertEqual(self.status_of(self.span), Feature.STATUS_PENDING)

    def test_a_layer_job_does_not_downgrade_work_in_progress(self):
        """A layer job covers every stage; it must not reopen finished rows.

        The old update excluded only ``assigned``, so assigning a layer set
        approved rows back to assigned -- and deleting the job then released
        them to pending, losing the approval entirely.
        """
        Feature.objects.filter(pk=self.span.pk).update(status=Feature.STATUS_APPROVED)
        Feature.objects.filter(pk=self.chamber.pk).update(status=Feature.STATUS_UNDER_REVIEW)

        self.job(AssignmentJob.SCOPE_LAYER, layer_id='spans')
        self.job(AssignmentJob.SCOPE_LAYER, layer_id='chambers')

        self.assertEqual(self.status_of(self.span), Feature.STATUS_APPROVED)
        self.assertEqual(self.status_of(self.chamber), Feature.STATUS_UNDER_REVIEW)

    def test_a_feature_job_does_not_downgrade_approved_work(self):
        Feature.objects.filter(pk=self.span.pk).update(status=Feature.STATUS_APPROVED)

        self.job(AssignmentJob.SCOPE_FEATURE, feature=self.span, assignee=self.other)

        self.assertEqual(self.status_of(self.span), Feature.STATUS_APPROVED)

    def test_a_layer_accepts_several_engineers(self):
        """A second engineer joins the layer; they do not replace the first."""
        planner = make_user('planner@example.com', role=User.Role.SUBADMIN)
        client = APIClient()
        client.force_authenticate(user=planner)
        payload = {
            'project': str(self.project.id),
            'scope': AssignmentJob.SCOPE_LAYER,
            'layer_id': 'spans',
        }

        first = client.post(
            '/api/assignments/', {**payload, 'assignee': str(self.engineer.id)}, format='json'
        )
        second = client.post(
            '/api/assignments/', {**payload, 'assignee': str(self.other.id)}, format='json'
        )

        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 201, second.data)
        self.assertEqual(
            AssignmentJob.objects.filter(
                project=self.project, scope='layer', layer_id='spans'
            ).count(),
            2,
        )
        self.assertEqual(self.status_of(self.span), Feature.STATUS_ASSIGNED)

    def test_work_past_assignment_is_left_alone(self):
        """Approved work is the engineer's record, not the assignment's."""
        job = self.job(AssignmentJob.SCOPE_FEATURE, feature=self.span)
        Feature.objects.filter(pk=self.span.pk).update(status=Feature.STATUS_APPROVED)

        job.delete()

        self.assertEqual(self.status_of(self.span), Feature.STATUS_APPROVED)


class JobListFieldProgressTests(TestCase):
    """The jobs list reports what has actually been recorded per row.

    A layer or project job has no single set of readings, so it reports how many
    of the features it covers carry evidence at all; a feature job keeps the
    readings and the count its field schema asks for. Every number comes from
    grouped queries, so the list stays a page of rows rather than a page of
    per-row requests.
    """

    def setUp(self):
        self.planner = make_user('progress-planner@example.com', role=User.Role.SUBADMIN)
        self.engineer = make_user('progress-engineer@example.com', role=User.Role.ENGINEER)
        self.project = make_survey_copy(make_ftth_project())
        self.read = make_feature(self.project, layer_id='spans', layer_name='spans')
        self.blank = make_feature(self.project, layer_id='spans', layer_name='spans')
        self.chamber = make_feature(self.project, layer_id='chambers', layer_name='chambers')

        Feature.objects.filter(pk=self.read.pk).update(
            field_schema=[{'key': 'length_m'}, {'key': 'depth_m'}],
            field_measurements={'length_m': {'value': 12.5, 'unit': 'm'}},
        )

        AssignmentJob.objects.create(
            project=self.project, scope=AssignmentJob.SCOPE_PROJECT, assignee=self.engineer
        )
        AssignmentJob.objects.create(
            project=self.project,
            scope=AssignmentJob.SCOPE_LAYER,
            layer_id='spans',
            assignee=self.engineer,
        )
        AssignmentJob.objects.create(
            project=self.project,
            scope=AssignmentJob.SCOPE_FEATURE,
            feature=self.read,
            assignee=self.engineer,
        )

    def fetch(self):
        client = APIClient()
        client.force_authenticate(user=self.planner)
        response = client.get('/api/assignments/jobs/')
        self.assertEqual(response.status_code, 200, response.data)
        return {row['scope']: row for row in response.data['results']}

    def test_a_feature_job_reports_its_readings_and_expected_count(self):
        progress = self.fetch()['feature']['field_progress']

        self.assertEqual(progress['readings'], 1)
        self.assertEqual(progress['expected_readings'], 2)
        self.assertEqual(progress['measured_features'], 1)
        self.assertEqual(progress['photos'], 0)

    def test_a_layer_job_counts_only_its_own_layer(self):
        progress = self.fetch()['layer']['field_progress']

        self.assertEqual(progress['features'], 2)
        self.assertEqual(progress['measured_features'], 1)
        self.assertIsNone(progress['expected_readings'])

    def test_a_project_job_covers_every_layer(self):
        progress = self.fetch()['project']['field_progress']

        self.assertEqual(progress['features'], 3)
        self.assertEqual(progress['measured_features'], 1)

    def test_photos_are_counted_at_every_scope(self):
        photo = FeaturePhoto.objects.create(
            feature=self.read,
            image=SimpleUploadedFile(
                'site.png', b'\x89PNG\r\n\x1a\n' + b'\0' * 16, content_type='image/png'
            ),
        )
        self.addCleanup(FeaturePhoto.objects.get(pk=photo.pk).image.delete, save=False)

        rows = self.fetch()

        self.assertEqual(rows['feature']['field_progress']['photos'], 1)
        self.assertEqual(rows['layer']['field_progress']['photos'], 1)
        self.assertEqual(rows['project']['field_progress']['photos'], 1)


class AssignmentSummaryFeaturesTests(TestCase):
    """The summary carries the features behind each supervision.

    Counts alone read the same for a whole project and for one span, which is
    what made the summary say nothing about the work a supervision holds.
    """

    def setUp(self):
        self.engineer = make_user('summary-engineer@example.com', role=User.Role.ENGINEER)
        self.project = make_survey_copy(make_ftth_project())
        self.span = make_feature(self.project, layer_id='spans', layer_name='spans')
        self.chamber = make_feature(self.project, layer_id='chambers', layer_name='chambers')

    def summary(self):
        client = APIClient()
        client.force_authenticate(user=self.engineer)
        response = client.get(f'/api/assignments/summary/?project={self.project.id}')
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def test_a_layer_job_lists_only_its_layer(self):
        AssignmentJob.objects.create(
            project=self.project,
            scope=AssignmentJob.SCOPE_LAYER,
            layer_id='spans',
            assignee=self.engineer,
        )

        record = self.summary()['assignments']['layer'][0]

        self.assertEqual(record['feature_count'], 1)
        self.assertEqual([f['id'] for f in record['features']], [str(self.span.id)])
        self.assertEqual(record['feature_status_counts'], {Feature.STATUS_ASSIGNED: 1})

    def test_a_project_job_covers_every_feature_and_the_totals_agree(self):
        AssignmentJob.objects.create(
            project=self.project,
            scope=AssignmentJob.SCOPE_PROJECT,
            assignee=self.engineer,
        )

        data = self.summary()
        record = data['assignments']['project'][0]

        self.assertEqual(record['feature_count'], 2)
        self.assertEqual(data['feature_total'], 2)
        self.assertEqual(data['stats']['total'], 2)
