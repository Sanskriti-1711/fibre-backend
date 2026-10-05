"""Unit tests for cable capacity BOQ rows and surface-driven restoration."""

from __future__ import annotations

from unittest import mock

from django.test import SimpleTestCase

from ftth_hld import boq


def _cable_feature(cable_type, fiber_count, length=10.0, **extra):
    props = {"CABLE_TYPE": cable_type, "FIBER_COUNT": fiber_count,
             "length_m": length, "HH_COUNT": 1}
    props.update(extra)
    return {"type": "Feature", "geometry": None, "properties": props}


class CableLadderBoqTests(SimpleTestCase):

    def _quantities(self, cables):
        class Layer:
            def __init__(self, features):
                self.geojson = {"type": "FeatureCollection", "features": features}

        layers = {"objects": Layer([]), "cables": Layer(cables)}
        with (
            mock.patch.object(
                boq, "_get_layer", side_effect=lambda _project, name: layers.get(name)
            ),
            mock.patch.object(boq, "_count_otb_tiers", return_value={}),
            mock.patch.object(boq, "_rate_map", return_value={}),
        ):
            return boq.compute_quantities("test-project")

    def test_drop_ladder_uses_distinct_boq_material_rows(self):
        cables = [
            _cable_feature("Drop", 12, HH_COUNT=10),
            _cable_feature("Drop", 24, HH_COUNT=18),
            _cable_feature("Drop", 48, HH_COUNT=40),
            _cable_feature("Drop", 72, HH_COUNT=60),
            _cable_feature("Drop", 96, HH_COUNT=80),
            _cable_feature("Drop", 144, HH_COUNT=120),
            _cable_feature("Drop", 288, HH_COUNT=286),
        ]
        quantities = self._quantities(cables)
        self.assertEqual([quantities[f"4.{i}"] for i in range(11, 18)], [10.0] * 7)

    def test_over_capacity_drops_are_separately_counted_for_review(self):
        cables = [_cable_feature(
            "Drop", 288, length=45.0, HH_COUNT=300,
            CAPACITY_STATUS="OVER_CAPACITY", CAPACITY_WARNING="requires review", REVIEW=1,
        )]
        quantities = self._quantities(cables)
        self.assertEqual(quantities["4.17"], 45.0)
        self.assertEqual(quantities["4.10"], 1.0)
        with (
            mock.patch.object(boq, "_rate_map", return_value={}),
            mock.patch.object(boq.BoqRate.objects, "filter", return_value=[]),
        ):
            rows = boq.build_boq_rows(quantities)
        row = next(row for row in rows if row["item_code"] == "4.10")
        self.assertEqual(row["item_name"], "Over-capacity drops requiring engineering review")
        self.assertIn("CAPACITY_WARNING", row["notes"])

    def test_distribution_ladder_sizes_have_distinct_boq_rows(self):
        cables = [
            _cable_feature("Distribution", 48, HH_COUNT=40),
            _cable_feature("Distribution", 72, HH_COUNT=60),
            _cable_feature("Distribution", 96, HH_COUNT=80),
            _cable_feature("Distribution", 144, HH_COUNT=120),
            _cable_feature("Distribution", 288, HH_COUNT=200),
        ]
        quantities = self._quantities(cables)
        self.assertEqual([quantities[f"4.{i}"] for i in range(18, 23)], [10.0] * 5)

    def test_unpriced_drop_material_has_a_distinct_boq_row(self):
        with (
            mock.patch.object(boq, "_rate_map", return_value={}),
            mock.patch.object(boq.BoqRate.objects, "filter", return_value=[]),
        ):
            rows = boq.build_boq_rows({"4.17": 90.0})
        row = next(row for row in rows if row["item_code"] == "4.17")
        self.assertEqual(row["item_name"], "Physical-location drop cable 288 FO")
        self.assertEqual(row["unit"], "m")
        self.assertEqual(row["notes"], "No rate card entry; add project pricing")


def _feat(trench_type, surface=None, length=10.0, infra="New"):
    props = {"trench_type": trench_type, "length_m": length,
             "INFRA_STATUS": infra}
    if surface is not None:
        props["SURFACE"] = surface
    return {"properties": props, "geometry": None}


def _sum(features):
    with (
        mock.patch.object(boq, "_get_layer", return_value=object()),
        mock.patch.object(boq, "_iter_features", return_value=features),
    ):
        return boq._sum_open_cut_by_surface("proj-1")


class SumOpenCutBySurfaceTests(SimpleTestCase):

    def test_asphalt_prices_road_restoration(self):
        got = _sum([_feat("Open Cut", "Asphalt", 100.0)])
        self.assertEqual(got, {"2.11": 100.0})

    def test_footpath_prices_paving_restoration(self):
        got = _sum([_feat("Open Cut", "Footpath", 40.0)])
        self.assertEqual(got, {"2.12": 40.0})

    def test_surface_split_on_one_project(self):
        got = _sum([_feat("Open Cut", "Asphalt", 100.0),
                    _feat("Open Cut", "Footpath", 40.0),
                    _feat("Open Cut", "Footway", 20.0)])
        self.assertEqual(got, {"2.11": 100.0, "2.12": 60.0})

    def test_hdd_and_garden_do_not_count_as_restoration(self):
        got = _sum([_feat("HDD", "Asphalt", 30.0),
                    _feat("Garden", "Garden", 25.0)])
        self.assertEqual(got, {})

    def test_missing_surface_falls_back_to_asphalt(self):
        got = _sum([_feat("Open Cut", None, 50.0)])
        self.assertEqual(got, {"2.11": 50.0})

    def test_reused_features_are_excluded(self):
        got = _sum([_feat("Open Cut", "Asphalt", 100.0, infra="Existing")])
        self.assertEqual(got, {})
