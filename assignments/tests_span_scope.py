"""One span is one layerwise job.

The assignment page resolves a span to the components of one layer that share
its id, so a trench span never claims ducts or cables too. A 78-component
mixed-layer target used to become 78 feature rows; now it becomes one row per
layer, each with its own components and engineer claim.

Run with:
    python manage.py test --settings=config.test_settings assignments
"""

from __future__ import annotations

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
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
        self.trench_a = make_feature(
            self.project, layer_id='final_trenches', layer_name='final_trenches'
        )
        self.trench_b = make_feature(
            self.project, layer_id='final_trenches', layer_name='final_trenches'
        )
        self.duct_a = make_feature(
            self.project, layer_id='distribution_ducts', layer_name='distribution_ducts'
        )
        self.duct_b = make_feature(
            self.project, layer_id='distribution_ducts', layer_name='distribution_ducts'
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.planner)

    def create(self, **overrides):
        payload = {
            'project': str(self.project.id),
            'scope': 'span',
            'span_id': 'TRENCH-00012',
            'feature_ids': [str(self.trench_a.id), str(self.trench_b.id)],
            'assignee': str(self.engineer.id),
        }
        payload.update(overrides)
        return self.client.post('/api/assignments/', payload, format='json')

    def test_one_span_is_one_job_over_all_its_layer_components(self):
        response = self.create()

        self.assertEqual(response.status_code, 201, response.data)
        job = AssignmentJob.objects.get(pk=response.data['id'])
        self.assertEqual(job.scope, AssignmentJob.SCOPE_SPAN)
        self.assertEqual(job.span_id, 'TRENCH-00012')
        self.assertEqual(job.layer_id, 'final_trenches')
        detail = self.client.get(f'/api/assignments/{response.data["id"]}/')
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(detail.data['span']['layer_id'], 'final_trenches')
        self.assertEqual(detail.data['layer']['id'], 'final_trenches')
        self.assertEqual(
            set(job.covered_feature_ids), {str(self.trench_a.id), str(self.trench_b.id)}
        )
        self.trench_a.refresh_from_db()
        self.trench_b.refresh_from_db()
        self.assertEqual(self.trench_a.status, Feature.STATUS_ASSIGNED)
        self.assertEqual(self.trench_b.status, Feature.STATUS_ASSIGNED)
        self.assertEqual(AssignmentJob.objects.count(), 1)

    def test_a_span_cannot_mix_layers(self):
        response = self.create(feature_ids=[str(self.trench_a.id), str(self.duct_a.id)])

        self.assertEqual(response.status_code, 400)
        self.assertIn('exactly one layer', str(response.data))
        self.assertFalse(AssignmentJob.objects.exists())

    def test_the_same_span_key_can_have_one_job_per_layer(self):
        trench_response = self.create()
        duct_response = self.create(
            feature_ids=[str(self.duct_a.id), str(self.duct_b.id)],
            layer_id='distribution_ducts',
        )

        self.assertEqual(trench_response.status_code, 201, trench_response.data)
        self.assertEqual(duct_response.status_code, 201, duct_response.data)
        jobs = AssignmentJob.objects.filter(scope='span').order_by('layer_id')
        self.assertEqual(
            [(job.layer_id, set(job.covered_feature_ids)) for job in jobs],
            [
                (
                    'distribution_ducts',
                    {str(self.duct_a.id), str(self.duct_b.id)},
                ),
                ('final_trenches', {str(self.trench_a.id), str(self.trench_b.id)}),
            ],
        )

    def test_reassigning_the_span_replaces_the_same_engineers_claim(self):
        self.create()
        self.create()

        self.assertEqual(AssignmentJob.objects.filter(scope='span').count(), 1)

    def test_a_component_from_another_project_is_rejected(self):
        foreign = make_survey_copy(make_ftth_project())
        foreign_feature = make_feature(foreign, layer_id='final_trenches')

        response = self.create(feature_ids=[str(self.trench_a.id), str(foreign_feature.id)])

        self.assertEqual(response.status_code, 400)
        self.assertFalse(AssignmentJob.objects.exists())

    def test_deleting_the_span_job_releases_its_components(self):
        response = self.create()

        self.client.delete(f'/api/assignments/{response.data["id"]}/')

        self.trench_a.refresh_from_db()
        self.trench_b.refresh_from_db()
        self.assertEqual(self.trench_a.status, Feature.STATUS_PENDING)
        self.assertEqual(self.trench_b.status, Feature.STATUS_PENDING)


class SpanLayerDataMigrationTests(TransactionTestCase):
    """The legacy 78-component span becomes one job per actual layer.

    This seeds the pre-fix shape through the historical model at 0010, then runs
    the data and constraint migrations together. It is the regression for the
    real ``TR-000010`` row: a job with no layer covering ducts, trenches and
    feeder ducts. The migration must preserve one row per layer, never leave a
    NULL layer behind, and keep every covered component.
    """

    migrate_from = [('assignments', '0010_assignmentjob_span_layer')]
    migrate_to = [('assignments', '0012_require_span_layer')]

    def test_a_mixed_span_is_split_by_layer(self):
        project = make_survey_copy(make_ftth_project())
        engineer = make_user('migration@example.com', role=User.Role.ENGINEER)
        ducts = [
            make_feature(project, layer_id='distribution_ducts'),
            make_feature(project, layer_id='distribution_ducts'),
        ]
        feeder = make_feature(project, layer_id='feeder_ducts')
        trench = make_feature(project, layer_id='final_trenches')
        covered_ids = [str(feature.id) for feature in [*ducts, feeder, trench]]

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        try:
            old_apps = executor.loader.project_state(self.migrate_from).apps
            old_job = old_apps.get_model('assignments', 'AssignmentJob')
            old_job.objects.create(
                project_id=project.id,
                scope='span',
                span_id='TR-MIGRATION',
                layer_id=None,
                covered_feature_ids=covered_ids,
                assignee_id=engineer.id,
            )

            executor = MigrationExecutor(connection)
            executor.migrate(self.migrate_to)
        finally:
            # A failed assertion must not leave the test database stuck at 0010
            # for every test that runs after this one.
            MigrationExecutor(connection).migrate(self.migrate_to)

        jobs = list(
            AssignmentJob.objects.filter(
                project=project, scope=AssignmentJob.SCOPE_SPAN
            ).order_by('layer_id')
        )

        self.assertEqual(len(jobs), 3)
        self.assertFalse(any(not job.layer_id for job in jobs))
        self.assertEqual(
            [(job.layer_id, set(job.covered_feature_ids)) for job in jobs],
            [
                ('distribution_ducts', {str(feature.id) for feature in ducts}),
                ('feeder_ducts', {str(feeder.id)}),
                ('final_trenches', {str(trench.id)}),
            ],
        )
