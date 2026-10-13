"""Assigning a run must not rebuild what the assignment already has.

A survey assignment built the field-survey package from the engine every time
it ran: one round trip per layer, measured at 7.8s against the live engine on
2026-10-13, and a failed assignment whenever the engine could not answer. Then
it dropped the copy's 2,164 features and re-inserted them (5.74s of Django
collector plus ~4.5s of INSERTs) to arrive at the rows that were already there.
The whole call was ~22s inside a request the host ends somewhere around 20-45s,
and a browser saw the re-assignment die at 20.8s with no reply at all --
reported to the planner as "could not reach the design service".

These tests pin the two decisions that took that work out: the package is built
once and then reused, and a copy that already holds that exact import is not
imported again. Both hinge on the stored file being named for its own content
hash, which is why that name -- and the fact that an older, uuid-named file is
canonicalised rather than trusted or rebuilt -- is asserted here.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings

from ftth_hld import assign

PACKAGE = b'survey-package-bytes'
DIGEST = hashlib.sha256(PACKAGE).hexdigest()
LEGACY_NAME = 'c349495181824d0f9b3929503d682673'  # what the old code wrote


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

    def test_a_stored_package_is_reused_instead_of_rebuilt(self):
        """The package on disk is the one the download endpoint serves."""
        stored = self._stored(DIGEST)
        session = SimpleNamespace(stored_file_path=str(stored))

        with mock.patch.object(assign, 'generate_survey_package') as build:
            _session, path, digest = assign._survey_package('project', 'p' * 32, session)

        build.assert_not_called()
        self.assertEqual(digest, DIGEST)
        self.assertEqual(Path(path), stored)
        # Canonical already: nothing about the copy is written.
        self.sessions.objects.update_or_create.assert_not_called()

    def test_a_legacy_name_is_canonicalised_and_the_bytes_are_untouched(self):
        """An older copy gets the hash name without its package changing."""
        stored = self._stored(LEGACY_NAME)
        session = SimpleNamespace(stored_file_path=str(stored))

        with mock.patch.object(assign, 'generate_survey_package') as build:
            _session, path, digest = assign._survey_package('project', 'p' * 32, session)

        build.assert_not_called()
        self.assertEqual(Path(path).read_bytes(), PACKAGE)
        self.assertEqual(Path(path).name, f'{DIGEST}.zip')
        self.sessions.objects.update_or_create.assert_called_once()
        self.assertEqual(
            self.sessions.objects.update_or_create.call_args.kwargs['defaults']['stored_file_path'],
            path,
        )

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
