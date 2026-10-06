"""Tests for the post-HLD chain: off the status request, guarded durably.

The chain (road class -> street-attributed sections -> permit matrix ->
preliminary package) used to run **inside** ``GET /api/ftth/hld/results/<id>/``,
guarded by "any ``gis.trench_layer`` row with a NULL ``fclass``" and
``trenches.updated_at`` vs ``trench_sections.updated_at``. Both guards re-arm on
their own — the engine writes ``gis.trench_layer`` fresh every run and its
trench payload carries no ``fclass``; any re-publish bumps ``updated_at`` — so a
completed project re-ran the chain on every poll and one request cost 234.7 s
(the measured road attribution against a 405,599-road extract).

These tests pin the fix's contract:

  * the status GET runs **no** step inline and always answers with the chain's
    recorded state (``post_process``);
  * the guard is the trench CONTENT revision, so re-publishing unchanged
    trenches is a no-op and only a changed revision re-arms the chain;
  * scheduling claims the row once — a second poll while a worker runs does not
    start a second worker.

The test database has no ``gis`` schema, so ``trench_content_revision()`` is
``""`` here; that is also the "fresh project" path, which the tests exercise.

Run with:
    python manage.py test ftth_hld
"""

from __future__ import annotations

from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from ftth_hld import posthld
from ftth_hld.models import FtthLayer, FtthProject
from testutils.factories import make_ftth_project, make_hld_layer, make_user

STATUS_URL = "/api/ftth/hld/results/%s/"

# `_pending_steps` imports the freshness check lazily from its own module, so
# the patch target is there, not on `ftth_hld.posthld`.
_SECTIONS = "permits.analysis.trench_sections"

COMPLETED_PAYLOAD = {
    "project_id": "",
    "status": "completed",
    "progress": 100,
    "stage_name": "Complete",
    "layers": [{"name": "objects", "count": 5}],
    "downloads": [],
    "messages": [],
}


class TrenchRevisionTests(TestCase):
    """The guard must be content, not a timestamp or a NULL count."""

    def setUp(self):
        self.ftth = make_ftth_project()

    def test_revision_is_empty_without_trench_rows(self):
        # No `gis.trench_layer` in the test database (and none for a fresh
        # project): the revision is empty rather than an error.
        self.assertEqual(posthld.trench_content_revision(self.ftth.project_id), "")

    def test_no_trenches_means_only_the_layer_sync_can_be_pending(self):
        row = posthld.HldPostProcess.objects.create(project_id=self.ftth.project_id)
        with mock.patch.object(posthld, "_missing_layers", return_value=["objects"]):
            self.assertEqual(
                posthld._pending_steps(self.ftth.project_id, row, ""), ["layers"]
            )
        with mock.patch.object(posthld, "_missing_layers", return_value=[]):
            self.assertEqual(posthld._pending_steps(self.ftth.project_id, row, ""), [])

    def test_a_first_ever_row_defers_to_each_step_own_guard(self):
        """Deploying this must not re-run a matrix/package that already exists.

        The one exception is the reference-layer load: a project whose permit
        matrix predates that step has never had its railways, waterways and
        protected areas read at all, so it is scheduled once (the recorded
        outcome then makes it a no-op — see the test below).
        """
        with mock.patch.object(posthld, "_missing_layers", return_value=[]), \
             mock.patch(_SECTIONS + ".sections_are_fresh", return_value=True), \
             mock.patch.object(posthld, "road_class_gaps", return_value=0), \
             mock.patch.object(posthld, "_matrix_exists", return_value=True), \
             mock.patch.object(posthld, "_package_exists", return_value=True):
            self.assertEqual(
                posthld._pending_steps(self.ftth.project_id, None, "544:now"),
                ["reference_layers"],
            )

    def test_a_changed_trench_revision_re_arms_every_trench_step(self):
        row = posthld.HldPostProcess.objects.create(
            project_id=self.ftth.project_id, trench_revision="544:before",
        )
        with mock.patch.object(posthld, "_missing_layers", return_value=[]), \
             mock.patch(_SECTIONS + ".sections_are_fresh", return_value=True), \
             mock.patch.object(posthld, "road_class_gaps", return_value=0), \
             mock.patch.object(posthld, "_matrix_exists", return_value=True), \
             mock.patch.object(posthld, "_package_exists", return_value=True):
            pending = posthld._pending_steps(
                self.ftth.project_id, row, "544:after"
            )
        self.assertEqual(pending, [
            "road_class", "trench_sections", "reference_layers",
            "permit_matrix", "permit_package",
        ])

    def test_an_unchanged_revision_leaves_a_fresh_chain_alone(self):
        """The point of the content guard: re-publishing unchanged is a no-op."""
        row = posthld.HldPostProcess.objects.create(
            project_id=self.ftth.project_id, trench_revision="544:same",
            steps={"reference_layers": {"ok": True}},
        )
        with mock.patch.object(posthld, "_missing_layers", return_value=[]), \
             mock.patch(_SECTIONS + ".sections_are_fresh", return_value=True), \
             mock.patch.object(posthld, "road_class_gaps", return_value=0), \
             mock.patch.object(posthld, "_matrix_exists", return_value=True), \
             mock.patch.object(posthld, "_package_exists", return_value=True):
            self.assertEqual(
                posthld._pending_steps(self.ftth.project_id, row, "544:same"), []
            )


class SectionFreshnessTests(TestCase):
    """``sections_are_fresh`` compares the revision stamp, not ``updated_at``."""

    def setUp(self):
        self.ftth = make_ftth_project()

    def test_sections_built_from_the_current_trenches_are_fresh(self):
        from permits.analysis.trench_sections import sections_are_fresh

        make_hld_layer(self.ftth, name="trench_sections", feature_count=12)
        FtthLayer.objects.filter(
            ftth_project=self.ftth, name="trench_sections"
        ).update(source_revision="544:abc")
        with mock.patch(
            "ftth_hld.posthld.trench_content_revision", return_value="544:abc"
        ):
            self.assertTrue(sections_are_fresh(self.ftth.project_id))

    def test_sections_built_from_older_trenches_are_stale(self):
        from permits.analysis.trench_sections import sections_are_fresh

        make_hld_layer(self.ftth, name="trench_sections", feature_count=12)
        FtthLayer.objects.filter(
            ftth_project=self.ftth, name="trench_sections"
        ).update(source_revision="544:abc")
        with mock.patch(
            "ftth_hld.posthld.trench_content_revision", return_value="544:moved"
        ):
            self.assertFalse(sections_are_fresh(self.ftth.project_id))

    def test_re_publishing_unchanged_trenches_does_not_re_arm(self):
        """The old guard compared timestamps — re-publishing bumped one side."""
        from permits.analysis.trench_sections import sections_are_fresh

        make_hld_layer(self.ftth, name="trenches", feature_count=544)
        make_hld_layer(self.ftth, name="trench_sections", feature_count=12)
        FtthLayer.objects.filter(
            ftth_project=self.ftth, name="trench_sections"
        ).update(source_revision="544:abc")
        with mock.patch(
            "ftth_hld.posthld.trench_content_revision", return_value="544:abc"
        ):
            before = sections_are_fresh(self.ftth.project_id)
            # Re-publish the trench layer: same content, newer timestamp.
            layer = FtthLayer.objects.get(ftth_project=self.ftth, name="trenches")
            layer.save(update_fields=["geojson"])
            after = sections_are_fresh(self.ftth.project_id)
        self.assertTrue(before)
        self.assertTrue(after)


class SchedulingTests(TestCase):
    """The claim is single-flight and durable."""

    def setUp(self):
        self.ftth = make_ftth_project()

    def test_the_test_runner_never_spawns_a_worker(self):
        self.assertTrue(posthld._spawn_disabled())
        self.assertIsNone(posthld.schedule_post_hld(self.ftth.project_id))

    def test_a_second_poll_does_not_start_a_second_worker(self):
        with mock.patch.object(posthld, "_spawn_disabled", return_value=False), \
             mock.patch.object(posthld, "_missing_layers", return_value=["objects"]), \
             mock.patch.object(posthld, "threading") as fake_threading:
            first = posthld.schedule_post_hld(self.ftth.project_id)
            second = posthld.schedule_post_hld(self.ftth.project_id)

        self.assertEqual(fake_threading.Thread.call_count, 1)
        self.assertEqual(first.status, posthld.HldPostProcess.STATUS_RUNNING)
        self.assertEqual(second.status, posthld.HldPostProcess.STATUS_RUNNING)

    def test_a_finished_chain_with_nothing_pending_is_not_rescheduled(self):
        row = posthld.HldPostProcess.objects.create(
            project_id=self.ftth.project_id,
            status=posthld.HldPostProcess.STATUS_PENDING,
            trench_revision="",
        )
        with mock.patch.object(posthld, "_spawn_disabled", return_value=False), \
             mock.patch.object(posthld, "_missing_layers", return_value=[]), \
             mock.patch.object(posthld, "threading") as fake_threading:
            posthld.schedule_post_hld(self.ftth.project_id)

        fake_threading.Thread.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(row.status, posthld.HldPostProcess.STATUS_DONE)

    def test_state_reports_the_steps_left(self):
        posthld.HldPostProcess.objects.create(
            project_id=self.ftth.project_id,
            status=posthld.HldPostProcess.STATUS_RUNNING,
            steps={"layers": {"ok": True, "seconds": 0.4, "detail": "synced 1"}},
        )
        state = posthld.post_hld_state(self.ftth.project_id)
        self.assertEqual(state["status"], "running")
        self.assertNotIn("layers", state["remaining"])
        self.assertIn("permit_matrix", state["remaining"])

    def test_state_is_empty_for_an_unknown_project(self):
        self.assertEqual(posthld.post_hld_state("nope")["status"], "none")


class StatusViewOffRequestTests(TestCase):
    """The regression: the GET must not run the chain."""

    def setUp(self):
        self.user = make_user("planner@example.com")
        self.ftth = make_ftth_project(status=FtthProject.STATUS_RUNNING)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _get(self, payload):
        with mock.patch("ftth_hld.api.get_status", return_value=payload):
            return self.client.get(STATUS_URL % self.ftth.project_id)

    def test_status_poll_runs_no_post_hld_step_inline(self):
        payload = dict(COMPLETED_PAYLOAD, project_id=self.ftth.project_id)
        with mock.patch("ftth_hld.api.get_status", return_value=payload), \
             mock.patch(
                 "permits.analysis.road_class.attribute_road_class"
             ) as road_class, \
             mock.patch(
                 "permits.analysis.trench_sections.build_trench_sections"
             ) as sections, \
             mock.patch("permits.rules.engine.run_analysis") as matrix, \
             mock.patch(
                 "permits.generators.hld_package.generate_hld_package"
             ) as package:
            response = self.client.get(STATUS_URL % self.ftth.project_id)

        self.assertEqual(response.status_code, 200)
        road_class.assert_not_called()
        sections.assert_not_called()
        matrix.assert_not_called()
        package.assert_not_called()

    def test_status_poll_reports_the_chain_state(self):
        payload = dict(COMPLETED_PAYLOAD, project_id=self.ftth.project_id)
        response = self._get(payload)

        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertIn("post_process", body)
        self.assertIn(body["post_process"]["status"], ("none", "pending", "done"))
