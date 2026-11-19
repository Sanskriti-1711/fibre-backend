"""Scoped form fields reach the same schema the field clients already read.

The contract under test:

* an all-projects template adds its fields to a feature in every project;
* a one-project template adds them only inside that project;
* the layer list and the feature detail agree (both go through the resolver);
* a template never redefines a field the layer already carries;
* only a planner may write one.
"""

from __future__ import annotations

from django.test import TestCase
from rest_framework.test import APIClient

from projects.models import FieldTemplate
from testutils.factories import make_feature, make_ftth_project, make_survey_copy, make_user
from users.models import User


class FieldTemplateTests(TestCase):
    def setUp(self):
        self.planner = make_user('planner@example.com', role=User.Role.SUBADMIN)
        self.client = APIClient()
        self.client.force_authenticate(user=self.planner)

        self.project = make_survey_copy(make_ftth_project(), name='Project A')
        self.feature = make_feature(
            self.project,
            layer_id='Final_Trenches',
            layer_name='Final_Trenches',
            properties={'DEPTH_MM': 900, 'cable_id': 'C-1'},
        )

        self.other_project = make_survey_copy(make_ftth_project(name='Other HLD'), name='Project B')
        self.other_feature = make_feature(
            self.other_project,
            layer_id='Final_Trenches',
            layer_name='Final_Trenches',
            properties={'DEPTH_MM': 600},
        )

    def _put(self, *, scope, kind='trench', fields, project=None):
        body = {'scope': scope, 'layer_kind': kind, 'fields': fields}
        if project is not None:
            body['project'] = str(project.id)
        return self.client.put('/api/field-templates/', body, format='json')

    def _keys(self, response):
        self.assertEqual(response.status_code, 200, response.data)
        return [field['key'] for field in response.data['feature']['field_schema']]

    def test_global_template_adds_a_field_in_every_project(self):
        response = self._put(
            scope='global',
            fields=[{'key': 'custom_safety', 'label': 'Safety note', 'type': 'text'}],
        )
        self.assertEqual(response.status_code, 200)

        detail = self.client.get(f'/api/projects/{self.project.id}/features/{self.feature.id}/')
        other = self.client.get(
            f'/api/projects/{self.other_project.id}/features/{self.other_feature.id}/'
        )

        self.assertIn('custom_safety', self._keys(detail))
        self.assertIn('custom_safety', self._keys(other))

    def test_project_template_stays_inside_that_project(self):
        response = self._put(
            scope='project',
            project=self.project,
            fields=[{'key': 'custom_gate', 'label': 'Gate code', 'type': 'text'}],
        )
        self.assertEqual(response.status_code, 200)

        detail = self.client.get(f'/api/projects/{self.project.id}/features/{self.feature.id}/')
        other = self.client.get(
            f'/api/projects/{self.other_project.id}/features/{self.other_feature.id}/'
        )

        self.assertIn('custom_gate', self._keys(detail))
        self.assertNotIn('custom_gate', self._keys(other))

    def test_a_project_template_adds_to_the_global_one(self):
        self._put(
            scope='global',
            fields=[{'key': 'custom_safety', 'label': 'Safety note', 'type': 'text'}],
        )
        self._put(
            scope='project',
            project=self.project,
            fields=[{'key': 'custom_gate', 'label': 'Gate code', 'type': 'text'}],
        )

        keys = self._keys(
            self.client.get(f'/api/projects/{self.project.id}/features/{self.feature.id}/')
        )
        self.assertIn('custom_safety', keys)
        self.assertIn('custom_gate', keys)

        other_keys = self._keys(
            self.client.get(
                f'/api/projects/{self.other_project.id}/features/{self.other_feature.id}/'
            )
        )
        self.assertIn('custom_safety', other_keys)
        self.assertNotIn('custom_gate', other_keys)

    def test_layer_list_serves_the_same_fields_as_feature_detail(self):
        self._put(
            scope='project',
            project=self.project,
            fields=[{'key': 'custom_clearance', 'label': 'Clearance (mm)', 'type': 'number'}],
        )

        response = self.client.get(f'/api/projects/{self.project.id}/layers/Final_Trenches/')

        self.assertEqual(response.status_code, 200)
        keys = [field['key'] for field in response.data['features'][0]['field_schema']]
        self.assertIn('custom_clearance', keys)

    def test_a_template_does_not_redefine_an_existing_layer_field(self):
        self._put(
            scope='global',
            fields=[{'key': 'DEPTH_MM', 'label': 'Depth', 'type': 'text'}],
        )

        detail = self.client.get(f'/api/projects/{self.project.id}/features/{self.feature.id}/')
        depth = next(
            field
            for field in detail.data['feature']['field_schema']
            if field['key'] == 'DEPTH_MM'
        )

        # The imported attribute keeps its meaning: still a measured number.
        self.assertEqual(depth['role'], 'measurement')
        self.assertEqual(depth['widget'], 'number')

    def test_a_number_template_field_reaches_the_client_as_a_number(self):
        self._put(
            scope='global',
            fields=[{'key': 'custom_clearance', 'label': 'Clearance', 'type': 'number'}],
        )

        detail = self.client.get(f'/api/projects/{self.project.id}/features/{self.feature.id}/')
        field = next(
            item for item in detail.data['feature']['field_schema'] if item['key'] == 'custom_clearance'
        )

        self.assertEqual(field['role'], 'measurement')
        self.assertEqual(field['widget'], 'number')
        self.assertTrue(field['editable'])

    def test_preview_returns_the_layer_fields_and_the_added_ones(self):
        self._put(
            scope='global',
            fields=[{'key': 'custom_safety', 'label': 'Safety note', 'type': 'text'}],
        )

        response = self.client.get(
            f'/api/field-templates/preview/?layer_kind=trench&project={self.project.id}'
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['base'])
        self.assertIn('custom_safety', [field['key'] for field in response.data['schema']])
        self.assertEqual(response.data['layer_id'], 'Final_Trenches')

    def test_only_a_planner_may_write(self):
        engineer = make_user('engineer@example.com', role=User.Role.ENGINEER)
        self.client.force_authenticate(user=engineer)

        response = self._put(
            scope='global',
            fields=[{'key': 'custom_safety', 'label': 'Safety note', 'type': 'text'}],
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(FieldTemplate.objects.count(), 0)

    def test_unknown_layer_kind_is_rejected(self):
        response = self._put(scope='global', kind='spaceship', fields=[])

        self.assertEqual(response.status_code, 400)
