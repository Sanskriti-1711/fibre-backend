"""Tests for survey attribute-anomaly detection (Tier-1 A16).

The flagship rule is "trench surface ≠ road class"; the rest check that one
record does not contradict itself (construction method vs surface, surface vs
reinstatement) or the surveyed majority on its street.

Run with:
    python manage.py test --settings=config.test_settings survey.tests.test_anomaly
"""

from __future__ import annotations

from django.test import TestCase

from survey.anomaly import (
    detect_anomalies,
    freeze_blockers,
    surface_value,
)
from testutils.factories import (
    make_ftth_project,
    make_survey_copy,
    make_survey_feature,
    make_user,
)
from users.models import User


class AnomalyRuleTestCase(TestCase):
    """One engineer and one survey copy; each test makes its own features."""

    def setUp(self):
        self.engineer = make_user('eng@example.com', role=User.Role.ENGINEER)
        self.copy = make_survey_copy(make_ftth_project(name='Anomalies'))

    def make(self, **attrs):
        return make_survey_feature(self.copy, self.engineer, **attrs)

    def flags(self, sf):
        return detect_anomalies([sf])[str(sf.id)]

    def rules(self, sf):
        return [a['rule'] for a in self.flags(sf)]

    def rules_in(self, sfs, sf):
        """Flags for ``sf`` scored together with its siblings ``sfs``."""
        by_id = detect_anomalies(sfs)
        return [a['rule'] for a in by_id[str(sf.id)]]


# ======================================================================
# Flagship: trench surface vs road class
# ======================================================================


class SurfaceVsRoadClassTests(AnomalyRuleTestCase):
    def test_asphalt_surface_on_a_footway_is_flagged(self):
        sf = self.make(survey_attributes={'SURFACE': 'Asphalt', 'fclass': 'footway'})
        flags = self.flags(sf)
        self.assertEqual([a['rule'] for a in flags], ['surface_vs_road_class'])
        self.assertEqual(flags[0]['severity'], 'warn')
        self.assertEqual(flags[0]['confidence'], 1.0)
        self.assertIn('pedestrian way', flags[0]['message'])

    def test_footway_surface_on_a_carriageway_is_the_normal_kerb_case(self):
        # A trench on the kerb band of a residential street legitimately
        # reports a footway surface — that must NOT be an error.
        sf = self.make(survey_attributes={'SURFACE': 'Footway', 'fclass': 'residential'})
        self.assertEqual(self.flags(sf), [])

    def test_footway_surface_on_a_major_road_is_a_low_confidence_check(self):
        sf = self.make(survey_attributes={'SURFACE': 'Footway', 'fclass': 'primary'})
        flags = self.flags(sf)
        self.assertEqual([a['rule'] for a in flags], ['surface_vs_road_class'])
        self.assertEqual(flags[0]['confidence'], 0.5)
        self.assertEqual(flags[0]['severity'], 'warn')

    def test_garden_surface_on_a_mapped_street_is_flagged(self):
        sf = self.make(survey_attributes={'SURFACE': 'Garden', 'fclass': 'residential'})
        flags = self.flags(sf)
        self.assertEqual([a['rule'] for a in flags], ['surface_vs_road_class'])
        self.assertEqual(flags[0]['confidence'], 0.5)

    def test_matching_surface_and_class_is_silent(self):
        sf = self.make(survey_attributes={'SURFACE': 'Asphalt', 'fclass': 'residential'})
        self.assertEqual(self.flags(sf), [])

    def test_an_unmodelled_surface_vocabulary_is_never_a_false_flag(self):
        sf = self.make(survey_attributes={'SURFACE': 'Concrete', 'fclass': 'footway'})
        self.assertEqual(self.flags(sf), [])


# ======================================================================
# Internal contradictions (these block the ASV freeze)
# ======================================================================


class ContradictionTests(AnomalyRuleTestCase):
    def test_hdd_cannot_be_a_footway_surface(self):
        sf = self.make(survey_attributes={'SURFACE': 'Footway', 'trench_type': 'HDD'})
        flags = self.flags(sf)
        self.assertEqual([a['rule'] for a in flags], ['surface_vs_construction'])
        self.assertEqual(flags[0]['severity'], 'error')
        self.assertEqual(flags[0]['expected'], 'Asphalt')

    def test_hdd_under_asphalt_is_correct_and_silent(self):
        sf = self.make(survey_attributes={'SURFACE': 'Asphalt', 'trench_type': 'HDD'})
        self.assertEqual(self.flags(sf), [])

    def test_garden_drop_cannot_be_a_road_surface(self):
        sf = self.make(survey_attributes={'SURFACE': 'Asphalt', 'trench_type': 'Garden'})
        flags = self.flags(sf)
        self.assertEqual([a['rule'] for a in flags], ['surface_vs_construction'])
        self.assertEqual(flags[0]['expected'], 'Garden')

    def test_aerial_has_no_surface_to_contradict(self):
        sf = self.make(survey_attributes={'SURFACE': 'Footway', 'trench_type': 'Aerial'})
        self.assertEqual(self.flags(sf), [])

    def test_surface_and_reinstate_must_agree(self):
        sf = self.make(
            survey_attributes={
                'SURFACE': 'Asphalt',
                'REINSTATE': 'Sidewalk',
            }
        )
        flags = self.flags(sf)
        self.assertEqual([a['rule'] for a in flags], ['surface_vs_reinstate'])
        self.assertEqual(flags[0]['severity'], 'error')

    def test_a_compatible_pair_is_silent(self):
        sf = self.make(survey_attributes={'SURFACE': 'Asphalt', 'REINSTATE': 'Road'})
        self.assertEqual(self.flags(sf), [])

    def test_garden_surface_reinstates_as_seed(self):
        sf = self.make(survey_attributes={'SURFACE': 'Garden', 'REINSTATE': 'Seed'})
        self.assertEqual(self.flags(sf), [])


# ======================================================================
# The legacy "sidewalk" channel and the value readers
# ======================================================================


class SurfaceValueReaderTests(AnomalyRuleTestCase):
    def test_a_boolean_sidewalk_flag_is_not_a_surface(self):
        # This misreading stamped every HDD road crossing "Footpath" once.
        sf = self.make(survey_attributes={'sidewalk': 'false', 'trench_type': 'HDD'})
        self.assertEqual(surface_value(sf), (None, None))
        self.assertEqual(self.flags(sf), [])

    def test_the_legacy_channel_carries_the_surface_name(self):
        # The designer wrote the surface NAME into "sidewalk" — read a name as
        # a name, so an HDD span stamped "Asphalt" there is correct.
        sf = self.make(survey_attributes={'sidewalk': 'Asphalt', 'trench_type': 'HDD'})
        self.assertEqual(surface_value(sf), ('road', 'Asphalt'))
        self.assertEqual(self.flags(sf), [])

    def test_the_surveyed_edit_wins_over_the_frozen_hld_value(self):
        sf = self.make(
            survey_attributes={'SURFACE': 'Asphalt'},
            original_attributes={'SURFACE': 'Footway', 'fclass': 'footway'},
        )
        self.assertEqual(surface_value(sf), ('road', 'Asphalt'))
        # ...and the road class still comes from the frozen record.
        self.assertIn('surface_vs_road_class', self.rules(sf))


# ======================================================================
# Inconsistent with neighbours
# ======================================================================


class NeighbourConsistencyTests(AnomalyRuleTestCase):
    def make_street(self, street, surfaces):
        return [
            self.make(
                survey_attributes={
                    'SURFACE': surf,
                    'route_section': street,
                }
            )
            for surf in surfaces
        ]

    def test_the_odd_one_out_is_flagged(self):
        feats = self.make_street('Hagley Road', ['Footway'] * 3 + ['Asphalt'])
        rules = [self.rules_in(feats, f) for f in feats]
        self.assertIn('surface_vs_neighbours', rules[3])
        # The three that agree are not flagged for agreeing with the majority.
        self.assertEqual(rules[0], [])
        self.assertEqual(rules[1], [])
        self.assertEqual(rules[2], [])

    def test_the_rule_needs_a_minimum_number_of_siblings(self):
        feats = self.make_street('Hagley Road', ['Footway', 'Asphalt'])
        self.assertEqual(self.rules_in(feats, feats[0]), [])
        self.assertEqual(self.rules_in(feats, feats[1]), [])

    def test_a_different_street_is_not_a_neighbour(self):
        feats = self.make_street('Hagley Road', ['Footway'] * 3)
        other = self.make(
            survey_attributes={
                'SURFACE': 'Asphalt',
                'route_section': 'Moseley Road',
            }
        )
        feats = feats + [other]
        self.assertEqual(self.rules_in(feats, other), [])


# ======================================================================
# Freeze gate
# ======================================================================


class FreezeBlockerTests(AnomalyRuleTestCase):
    def test_only_contradictions_block_a_freeze(self):
        contradiction = self.make(
            survey_attributes={
                'SURFACE': 'Footway',
                'trench_type': 'HDD',
            }
        )
        suspicion = self.make(
            survey_attributes={
                'SURFACE': 'Asphalt',
                'fclass': 'footway',
                'route_section': 'Hagley Road',
            }
        )
        blockers = freeze_blockers([contradiction, suspicion])
        self.assertEqual([sid for sid, _msg in blockers], [str(contradiction.id)])

    def test_a_clean_dataset_has_no_blockers(self):
        sf = self.make(
            survey_attributes={
                'SURFACE': 'Asphalt',
                'REINSTATE': 'Road',
                'trench_type': 'HDD',
                'fclass': 'residential',
            }
        )
        self.assertEqual(freeze_blockers([sf]), [])
