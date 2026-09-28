"""Unit tests for surface-driven restoration quantities in the BOQ engine.

``_sum_open_cut_by_surface`` splits Open Cut trench metres by the SURFACE
attribute the QGIS trench designer now stamps from real routing evidence
(footway carrier vs carriageway carrier). Asphalt/carriageway spans price the
road-restoration item (2.11); footpath spans price the paving item (2.12);
missing surface falls back to 2.11 so pre-surface projects keep the old
behaviour. HDD drills and Garden drops must NOT leak into restoration.

Run with:
    python manage.py test --settings=config.test_settings ftth_hld
"""

from __future__ import annotations

from unittest import mock

from django.test import SimpleTestCase

from ftth_hld import boq


def _feat(trench_type, surface=None, length=10.0, infra="New"):
    props = {"trench_type": trench_type, "length_m": length,
             "INFRA_STATUS": infra}
    if surface is not None:
        props["SURFACE"] = surface
    return {"properties": props, "geometry": None}


def _sum(features):
    with mock.patch.object(boq, "_get_layer", return_value=object()), \
         mock.patch.object(boq, "_iter_features", return_value=features):
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
