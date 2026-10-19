"""The project layer API applies the field-survey layer rule.

The survey app builds its entire layer list — the layers panel, the map sources
and the per-layer feature fetches — from these three answers, so this is where
an already-installed app obeys the rule without being rebuilt:

* ``GET /api/projects/<id>/layers/`` (the list the app fetches first)
* ``GET /api/projects/<id>/layers/<layer_id>/`` (the rows behind one layer)
* ``GET /api/projects/<id>/map-data/`` (the web map's single payload)

A Survey copy can still hold the rows an older package carried: project
``24cd3573…`` has 23 layers including ``Pseudo_HH`` (224) beside ``objects``
(285). ``Pseudo_HH`` is the footway end of a garden leg — a design artefact of
the trench network with no address and no household — while ``objects`` is the
premises layer, so the first is never served and the second always is.
"""

from __future__ import annotations

from django.test import TestCase
from rest_framework.test import APIClient

from projects.layer_rules import is_field_survey_layer, normalize_layer_id
from testutils.factories import make_feature, make_ftth_project, make_survey_copy, make_user
from users.models import User


class FieldLayerRuleTests(TestCase):
    def setUp(self):
        self.user = make_user('planner@example.com', role=User.Role.SUBADMIN)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.copy = make_survey_copy(make_ftth_project())
        make_feature(self.copy, layer_id='objects', layer_name='Objects')
        make_feature(self.copy, layer_id='Pseudo_HH', layer_name='Pseudo_HH')
        make_feature(self.copy, layer_id='final_trenches', layer_name='Final_Trenches')
        # The designer's per-tier mirrors of the same trench geometry. A copy
        # built from an older package holds them beside `final_trenches`
        # (project `24cd3573…`: 265 + 224 + 58 rows against 387).
        make_feature(self.copy, layer_id='Distribution_Trench', layer_name='Distribution_Trench')
        make_feature(self.copy, layer_id='Garden_Trench', layer_name='Garden_Trench')
        make_feature(self.copy, layer_id='Feeder_Trench', layer_name='Feeder_Trench')
        make_feature(self.copy, layer_id='Trench_Nodes', layer_name='Trench_Nodes')

    def test_the_layer_list_serves_the_object_layer_and_not_the_artefact(self):
        response = self.client.get(f'/api/projects/{self.copy.id}/layers/')

        self.assertEqual(response.status_code, 200)
        ids = [layer['layer_id'] for layer in response.data['layers']]
        self.assertIn('objects', ids)
        self.assertIn('final_trenches', ids)
        self.assertNotIn('Pseudo_HH', ids)
        # One trench layer in the survey: `final_trenches`. The per-tier mirrors
        # repeat the same paths and would show the crew the same trench three
        # more times.
        for mirror in ('Distribution_Trench', 'Garden_Trench', 'Feeder_Trench'):
            self.assertNotIn(mirror, ids, mirror)
        self.assertNotIn('Trench_Nodes', ids)

    def test_the_artefact_layer_has_no_detail(self):
        detail = self.client.get(f'/api/projects/{self.copy.id}/layers/Pseudo_HH/')
        objects = self.client.get(f'/api/projects/{self.copy.id}/layers/objects/')

        self.assertEqual(detail.status_code, 404)
        self.assertEqual(objects.status_code, 200)

    def test_map_data_carries_no_artefact_layer(self):
        response = self.client.get(f'/api/projects/{self.copy.id}/map-data/')

        self.assertEqual(response.status_code, 200)
        # map-data keys its payloads on the layer NAME (the display spelling).
        self.assertIn('Objects', response.data['geojson'])
        self.assertNotIn('Pseudo_HH', response.data['geojson'])
        described = [layer['name'] for layer in response.data['layers']]
        self.assertIn('Objects', described)
        self.assertNotIn('Pseudo_HH', described)

    def test_completion_does_not_count_the_artefact_layer(self):
        """A row the engineer is never shown must not cap completion.

        Standard completion is approved/total over every Feature, so leaving
        the hidden layers in would make 100% unreachable on a copy that carries
        them (224 `Pseudo_HH` and 547 per-tier trench rows of 3,578 on project
        `24cd3573…`). The spellings are covered too: the aggregate cannot use
        the Python check, so it is the query that has to match them.
        """
        for layer_id in ('pseudo hh', 'imp-pseudo_hh', 'PSEUDO-HH'):
            make_feature(self.copy, layer_id=layer_id, layer_name=layer_id)
        for layer_id in ('distribution trench', 'imp-Garden_Trench', 'FEEDER-TRENCH'):
            make_feature(self.copy, layer_id=layer_id, layer_name=layer_id)
        make_feature(self.copy, layer_id='trench nodes', layer_name='trench nodes')

        response = self.client.get(f'/api/projects/{self.copy.id}/completion/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['total_features'], 2)
        ids = [layer['layer_id'] for layer in response.data['layers']]
        self.assertIn('objects', ids)
        self.assertNotIn('Pseudo_HH', ids)

    def test_the_rule_matches_every_spelling(self):
        for layer_id in ('Pseudo_HH', 'pseudo_hh', 'pseudo hh', 'imp-Pseudo_HH'):
            self.assertFalse(is_field_survey_layer(layer_id), layer_id)
        for layer_id in (
            'Distribution_Trench',
            'distribution_trench',
            'distribution trench',
            'imp-Distribution_Trench',
            'Garden_Trench',
            'garden_trench',
            'Feeder_Trench',
            'feeder_trench',
            # Trench joints, not trenches.
            'Trench_Nodes',
            'trench_nodes',
            'imp-trench_nodes',
        ):
            self.assertFalse(is_field_survey_layer(layer_id), layer_id)
        for layer_id in (
            'objects',
            'Objects',
            'imp-objects',
            # The single published trench layer stays -- including the snake
            # case the copy spells it in.
            'final_trenches',
            'Final_Trenches',
            'imp-final_trenches',
        ):
            self.assertTrue(is_field_survey_layer(layer_id), layer_id)
        # Not a substring match, and two real layers must not merge.
        self.assertTrue(is_field_survey_layer('pseudo_hh_extra'))
        self.assertTrue(is_field_survey_layer('distribution_trench_nodes'))
        self.assertNotEqual(normalize_layer_id('pseudo_hh'), normalize_layer_id('pseudo_hh_x'))
