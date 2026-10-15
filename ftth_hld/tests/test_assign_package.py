"""Assigning a run must not rebuild what the assignment already has.

A survey assignment built the field-survey package from the engine every time
it ran: one round trip per layer, measured at 7.8s against the live engine on
2026-10-13, and a failed assignment whenever the engine could not answer. Then
it dropped the copy's 2,164 features and re-inserted them (5.74s of Django
collector plus ~4.5s of INSERTs) to arrive at the rows that were already there.
The whole call was ~22s inside a request the host ends somewhere around 20-45s,
and a browser saw the re-assignment die at 20.8s with no reply at all --
reported to the planner as "could not reach the design service".

What is left of the cost is the engine round trips a package build needs (one
per layer, and a directory listing), which cannot be cached without the copy
falling behind the package the platform serves. So the tests pin the decision
that took the rest out: a copy that already holds the current package --
identified by the stored file being named for its own content hash -- is not
imported again, while an older, uuid-named file is not trusted as the import
and the copy is re-imported from the current build.
"""

from __future__ import annotations

import io
import shutil
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings

from ftth_hld import assign


def _package(entries: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


LAYERS = {'objects.geojson': b'{"objects": 1}', 'pdps.geojson': b'{"pdps": 1}'}
PACKAGE = _package(LAYERS)
DIGEST = assign._package_hash(PACKAGE)
LEGACY_NAME = 'c349495181824d0f9b3929503d682673'  # what the old code wrote


class PackageIdentityTests(SimpleTestCase):
    """Two builds of the same run do not produce the same zip bytes.

    Measured 2026-10-13 against the live engine: 15 identical entry names and
    15 identical entry hashes, in containers that differ. A copy holds the
    entries, so the archive's own bytes must not be the identity -- otherwise
    every assignment rebuilds, compares two hashes that can never match, and
    re-imports 2,164 rows to arrive where it started.
    """

    def _built(self, payload: bytes) -> str:
        return assign._package_hash(payload)

    def test_the_archive_bytes_are_not_the_identity(self):
        reordered = _package(
            {'pdps.geojson': LAYERS['pdps.geojson'], 'objects.geojson': LAYERS['objects.geojson']}
        )

        self.assertNotEqual(PACKAGE, reordered)
        self.assertEqual(self._built(PACKAGE), self._built(reordered))

    def test_a_changed_layer_changes_the_identity(self):
        changed = _package({**LAYERS, 'objects.geojson': b'{"objects": 2}'})

        self.assertNotEqual(self._built(PACKAGE), self._built(changed))


class SurveyPackageTests(SimpleTestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        settings_override = override_settings(MEDIA_ROOT=self.media)
        settings_override.enable()
        self.addCleanup(settings_override.disable)
        # SimpleTestCase forbids the database, so the import session is faked.
        self.sessions = mock.patch.object(assign, 'ImportSession').start()
        self.sessions.objects.update_or_create.return_value = (
            SimpleNamespace(stored_file_path=None),
            True,
        )
        self.addCleanup(mock.patch.stopall)

    def _stored(self, name: str, payload: bytes = PACKAGE) -> Path:
        path = Path(self.media) / 'imports' / f'{name}.zip'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def test_the_package_is_the_current_build_and_is_named_for_its_bytes(self):
        """The copy is imported from what the engine serves now."""
        session = SimpleNamespace(stored_file_path=None)

        with mock.patch.object(assign, 'generate_survey_package', return_value=PACKAGE) as build:
            _session, path, digest = assign._survey_package('project', 'p' * 32, session)

        build.assert_called_once_with('p' * 32)
        self.assertEqual(digest, DIGEST)
        self.assertEqual(Path(path).name, f'{DIGEST}.zip')
        self.assertEqual(Path(path).read_bytes(), PACKAGE)

    def test_an_older_name_is_replaced_by_the_current_build(self):
        """A stored package under another name is not trusted as the import."""
        stored = self._stored(LEGACY_NAME, b'older-package')
        session = SimpleNamespace(stored_file_path=str(stored))

        with mock.patch.object(assign, 'generate_survey_package', return_value=PACKAGE) as build:
            _session, path, digest = assign._survey_package('project', 'p' * 32, session)

        build.assert_called_once()
        self.assertEqual(Path(path).read_bytes(), PACKAGE)
        self.assertEqual(Path(path).name, f'{DIGEST}.zip')
        self.sessions.objects.update_or_create.assert_called_once()
        self.assertEqual(
            self.sessions.objects.update_or_create.call_args.kwargs['defaults']['stored_file_path'],
            path,
        )

    def test_a_package_with_the_same_entries_keeps_the_record(self):
        """Same bytes, same name: the record already points at it."""
        stored = self._stored(DIGEST)
        session = SimpleNamespace(stored_file_path=str(stored))

        with mock.patch.object(assign, 'generate_survey_package', return_value=PACKAGE):
            _session, path, digest = assign._survey_package('project', 'p' * 32, session)

        self.assertEqual(Path(path), stored)
        self.sessions.objects.update_or_create.assert_not_called()

    def test_a_deleted_package_is_written_back_even_when_the_name_matches(self):
        """The host's disk does not survive a deploy.

        The record can point at the right name while the file is gone, and the
        import reads the file: the live service answered 404 "Survey package not
        available" for a package whose name and content had not changed, because
        the container holding it had been replaced between the two calls.
        """
        stored = Path(self.media) / 'imports' / f'{DIGEST}.zip'
        session = SimpleNamespace(stored_file_path=str(stored))
        self.assertFalse(stored.exists())

        with mock.patch.object(assign, 'generate_survey_package', return_value=PACKAGE):
            _session, path, digest = assign._survey_package('project', 'p' * 32, session)

        self.assertEqual(digest, DIGEST)
        self.assertTrue(Path(path).exists())
        self.assertEqual(Path(path).read_bytes(), PACKAGE)
        self.sessions.objects.update_or_create.assert_not_called()

    def test_a_missing_package_is_built(self):
        session = SimpleNamespace(stored_file_path=str(Path(self.media) / 'imports' / 'gone.zip'))

        with mock.patch.object(assign, 'generate_survey_package', return_value=PACKAGE) as build:
            _session, path, digest = assign._survey_package('project', 'p' * 32, session)

        build.assert_called_once_with('p' * 32)
        self.assertEqual(digest, DIGEST)
        self.assertEqual(Path(path).read_bytes(), PACKAGE)

    def test_no_import_session_at_all_is_built(self):
        with mock.patch.object(assign, 'generate_survey_package', return_value=PACKAGE) as build:
            _session, path, digest = assign._survey_package('project', 'p' * 32, None)

        build.assert_called_once()
        self.assertEqual(digest, DIGEST)
        self.assertEqual(Path(path).read_bytes(), PACKAGE)


class ReassignGuardTests(SimpleTestCase):
    """A copy may be replaced until someone has recorded something in it.

    The guard used to refuse by ``status``, which made an accepted-but-untouched
    copy impossible to re-assign from the results page: an engineer who taps
    "accept" and does nothing else could not be replaced or corrected. The
    decision now reads the data, so the tests pin which records count and that
    any single one of them is enough.
    """

    @staticmethod
    def _probes(*recorded: bool):
        probes: list = []
        for flag in recorded:
            model = mock.Mock()
            model.objects.filter.return_value.exists.return_value = flag
            probes.append((model, {}))
        return probes

    def test_every_record_of_field_work_is_probed(self):
        probes = assign._field_data_queries(None)
        models = [model.__name__ for model, _lookup in probes]

        self.assertEqual(len(models), len(set(models)))
        for name in (
            'SurveyFeature',
            'ApprovalRecord',
            'GPSTrace',
            'AssignmentJob',
            'TrenchSurvey',
            'ExistingAsset',
            'RiskAssessment',
            'Hazard',
            'FieldEvidence',
            'SurveyEditLog',
            'SurveyStatus',
        ):
            self.assertIn(name, models)
        # The typed rows hang off the copy's features, which a re-import replaces.
        typed = {model.__name__: lookup for model, lookup in probes}
        self.assertIn('feature__in', typed['TrenchSurvey'])

    def test_a_copy_with_nothing_recorded_can_be_replaced(self):
        with mock.patch.object(
            assign, '_field_data_queries', return_value=self._probes(False, False, False)
        ):
            self.assertFalse(assign._holds_field_data(None))

    def test_any_single_record_makes_the_copy_irreplaceable(self):
        with mock.patch.object(
            assign, '_field_data_queries', return_value=self._probes(False, True, False)
        ):
            self.assertTrue(assign._holds_field_data(None))


class HoldsThisImportTests(SimpleTestCase):
    """When may an assignment leave the copy's features alone?"""

    def setUp(self):
        self.features = mock.patch.object(assign, 'Feature').start()
        self.features.objects.filter.return_value.exists.return_value = True
        self.addCleanup(mock.patch.stopall)

    def test_a_package_named_for_its_bytes_over_existing_rows_is_the_import(self):
        self.assertTrue(assign._holds_this_import('survey', f'/media/imports/{DIGEST}.zip', DIGEST))

    def test_a_legacy_uuid_name_is_not_the_import(self):
        """Existing copies keep the behaviour they had: re-import, once."""
        self.assertFalse(
            assign._holds_this_import('survey', f'/media/imports/{LEGACY_NAME}.zip', DIGEST)
        )

    def test_another_package_is_not_the_import(self):
        self.assertFalse(assign._holds_this_import('survey', '/media/imports/other.zip', DIGEST))

    def test_a_copy_with_no_package_and_a_copy_with_no_rows_are_not_imports(self):
        self.assertFalse(assign._holds_this_import('survey', None, DIGEST))
        self.features.objects.filter.return_value.exists.return_value = False
        self.assertFalse(
            assign._holds_this_import('survey', f'/media/imports/{DIGEST}.zip', DIGEST)
        )
