"""The permit reference layers must follow a project's own area.

The railway/waterway/protected-area/tree rules intersect a project's route
layers with ``gis.osm_*``. Those tables were global and filled by a management
command whose default bbox is Berlin — and that nothing in the pipeline ran —
so a permit package built from an area input elsewhere reported "no waterways,
no railways, no protected areas" because no data had ever been loaded for that
area, not because there was none. Two properties fix that and are pinned here:

* a project load writes ``project_id``-tagged rows and replaces **only** that
  project's previous rows, so two projects in different cities can be analysed
  from the same tables; and
* the spatial queries see the shared curated load *plus* the project's own, and
  stay valid against a table that predates the column.

Cheap ``SimpleTestCase``s: the scoping decision and the post-HLD step wiring are
plain Python, so they need no PostGIS.
"""

from __future__ import annotations

from unittest import mock

from django.test import SimpleTestCase

from permits.analysis import spatial_intersection as si
from permits.management.commands import load_osm_reference_layers as loader


class ReferenceScopeClauseTests(SimpleTestCase):
    """``reference_scope_clause`` decides what a spatial rule may see."""

    def test_scopes_to_shared_plus_own_project(self):
        with mock.patch.object(si, "_has_column", return_value=True):
            clause, extra = si.reference_scope_clause("osm_waterway")
        self.assertIn("ref.project_id IS NULL", clause)
        self.assertIn("ref.project_id = %s", clause)
        # Exactly one placeholder — the caller binds the project id once.
        self.assertEqual(clause.count("%s"), 1)
        self.assertEqual(extra, [])

    def test_alias_is_honoured(self):
        # The municipality join aliases the boundary table as ``a``.
        with mock.patch.object(si, "_has_column", return_value=True):
            clause, _ = si.reference_scope_clause("osm_admin_boundary", alias="a")
        self.assertIn("a.project_id", clause)
        self.assertNotIn("ref.project_id", clause)

    def test_absent_column_keeps_the_query_valid(self):
        # An install loaded before project scoping must not break: no clause,
        # no extra binding — the query is the one that always ran.
        with mock.patch.object(si, "_has_column", return_value=False):
            clause, extra = si.reference_scope_clause("osm_railway")
        self.assertEqual(clause, "")
        self.assertEqual(extra, [])


class _FakeCursor:
    """Records every statement, so a test can read the SQL decisions."""

    def __init__(self, log: list):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.log.append((" ".join(str(sql).split()), params))

    def executemany(self, sql, rows):
        self.log.append((" ".join(str(sql).split()), len(list(rows))))

    def fetchone(self):
        # ``_count`` asks for a row count; 0 means "empty, go ahead and load".
        return (0,)


class _FakeConnection:
    def __init__(self, log: list):
        self._log = log

    def cursor(self):
        return _FakeCursor(self._log)


class LoaderScopingTests(SimpleTestCase):
    """A project load must never delete another project's (or the shared) rows."""

    def _run(self, **kwargs):
        log: list = []
        # Overpass is not reachable from a test and not what is under test:
        # an empty element list still exercises every SQL decision.
        with mock.patch.object(loader, "_fetch_layer", return_value=[]):
            with mock.patch.object(loader, "connection", _FakeConnection(log)):
                counts = loader.load_categories((1.0, 1.0, 2.0, 2.0),
                                                ["osm_waterway"], **kwargs)
        return counts, " | ".join(sql for sql, _ in log)

    def test_project_load_deletes_only_its_own_rows(self):
        counts, sql = self._run(project_id="proj-1")
        self.assertEqual(counts, {"osm_waterway": 0})
        self.assertIn('DELETE FROM gis."osm_waterway" WHERE project_id = %s', sql)
        self.assertNotIn("TRUNCATE", sql)

    def test_shared_load_still_truncates(self):
        counts, sql = self._run()
        self.assertEqual(counts, {"osm_waterway": 0})
        self.assertIn("TRUNCATE", sql)
        self.assertNotIn("DELETE FROM", sql)

    def test_ddl_adds_the_project_column(self):
        _, sql = self._run(project_id="proj-1")
        self.assertIn("ADD COLUMN IF NOT EXISTS project_id TEXT", sql)

    def test_no_trenches_means_no_area_to_load(self):
        # A project with no trenches has no bbox; the step reports it instead of
        # querying Overpass for the Berlin default.
        with mock.patch.object(loader, "project_bbox", return_value=None):
            summary = loader.load_for_project("proj-1")
        self.assertEqual(summary["counts"], {})
        self.assertIn("no trenches", summary["skipped"])

    def test_project_bbox_drives_the_load(self):
        with mock.patch.object(loader, "project_bbox",
                               return_value=(1.0, 2.0, 3.0, 4.0)):
            with mock.patch.object(loader, "_fetch_layer", return_value=[]):
                with mock.patch.object(loader, "connection",
                                       _FakeConnection([])):
                    summary = loader.load_for_project("proj-1")
        self.assertEqual(summary["bbox"], [1.0, 2.0, 3.0, 4.0])
        self.assertIn("osm_railway", summary["counts"])
        self.assertIn("osm_waterway", summary["counts"])


class TrafficRuleUsesHldSectionsTests(SimpleTestCase):
    """Traffic management is decided from the HLD design, not reported as a gap.

    ``TRAFFIC_001`` used to read only the LLD ``final_trenches`` layer, so an
    HLD project recorded "no final_trenches LLD output" and the permit package
    went out with no traffic rows at all — for a route that is dug in surfaced
    ground. The civil sections carry the same SURFACE/CONSTRUCT the rule keys
    on, so the rule now falls back to them.
    """

    def _run(self, lld_rows, sections):
        from permits.rules import engine
        from permits.rules.registry import TRAFFIC_RULE

        calls: list = []
        rule = mock.Mock(required_level="POTENTIAL", blocks_construction=False,
                         version=1)
        with mock.patch.object(engine, "RULE_CATALOGUE", [TRAFFIC_RULE]), \
                mock.patch.object(engine, "attribute_municipality",
                                  return_value={}), \
                mock.patch.object(engine, "assign_groups",
                                  return_value={"trench_rows": 0,
                                                "lld_rows": 0,
                                                "no_roads": False}), \
                mock.patch.object(engine, "_ensure_rule",
                                  return_value=(rule, 1)), \
                mock.patch.object(engine, "_latest_lld_layer_rows",
                                  return_value=lld_rows), \
                mock.patch.object(engine, "hld_layer_sections",
                                  return_value=sections), \
                mock.patch.object(engine, "_refresh_readiness"), \
                mock.patch.object(engine, "_upsert_permit",
                                  side_effect=lambda *a, **kw: calls.append((a, kw))):
            summary = engine.run_analysis("proj-1")
        return calls, summary

    def test_hld_only_project_still_gets_traffic_rows(self):
        calls, summary = self._run([], [{
            "SURFACE": "Asphalt", "CONSTRUCT": "Open Cut",
            "PARENT_FEATURE_ID": "12", "SECTION_ID": "Open-Cut-0001",
            "street_name": "High Street",
        }])
        self.assertEqual(len(calls), 1)
        args, kwargs = calls[0]
        self.assertEqual(kwargs["layer"], "trench_layer")
        # Same section identity the road-authority rows use.
        self.assertEqual(args[2], "12#Open-Cut-0001")
        self.assertEqual(summary["rows_created"], 1)
        self.assertFalse([g for g in summary["gaps"] if "TRAFFIC" in g])

    def test_unsurfaced_sections_raise_no_traffic_permit(self):
        calls, summary = self._run([], [{
            "SURFACE": "Grass", "CONSTRUCT": "Garden",
            "PARENT_FEATURE_ID": "12", "SECTION_ID": "Garden-0001",
        }])
        self.assertEqual(calls, [])
        self.assertEqual(summary["rows_created"], 0)

    def test_lld_rows_still_win_when_present(self):
        calls, _ = self._run([{"SURFACE": "Footpath", "feature_id": "f1"}],
                             [{"SURFACE": "Asphalt",
                               "PARENT_FEATURE_ID": "12",
                               "SECTION_ID": "Open-Cut-0001"}])
        self.assertEqual(len(calls), 1)
        args, kwargs = calls[0]
        self.assertEqual(kwargs["layer"], "final_trenches")
        self.assertEqual(args[2], "f1")


class PostHldReferenceStepTests(SimpleTestCase):
    """The reference load runs before the matrix, and only while it is missing."""

    def test_step_runs_before_the_matrix(self):
        from ftth_hld import posthld

        order = list(posthld.STEP_ORDER)
        self.assertIn("reference_layers", order)
        self.assertLess(order.index("reference_layers"),
                        order.index("permit_matrix"))

    def test_every_step_has_a_label_and_a_function(self):
        from ftth_hld import posthld

        for name in posthld.STEP_ORDER:
            self.assertIn(name, posthld.STEP_LABELS)
            self.assertIn(name, posthld.STEP_FUNCS)

    def test_done_only_after_a_successful_load(self):
        from ftth_hld import posthld

        class Row:
            def __init__(self, steps):
                self.steps = steps

        self.assertFalse(posthld._reference_layers_done(Row(None)))
        self.assertFalse(posthld._reference_layers_done(Row({})))
        self.assertFalse(posthld._reference_layers_done(
            Row({"reference_layers": {"ok": False, "error": "Overpass down"}})))
        self.assertTrue(posthld._reference_layers_done(
            Row({"reference_layers": {"ok": True}})))
