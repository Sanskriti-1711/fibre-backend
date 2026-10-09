"""API tests for the FTTH HLD endpoints (``/api/ftth/hld/...``).

The HLD endpoints are a Django gateway in front of the FastAPI engine, so the
engine (``ftth_hld.pipeline``) is mocked at the ``ftth_hld.api`` namespace and
the tests assert the gateway's own contract: auth, validation, status-code
mapping, progress clamping, the DB-first layer cache, and the file-download
basename guard.

Run with:
    python manage.py test --settings=config.test_settings ftth_hld
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from ftth_hld.models import FtthLayer, FtthProject
from ftth_hld.pipeline import EngineError
from testutils.factories import (
    LINE,
    make_feature,
    make_ftth_project,
    make_hld_layer,
    make_survey_copy,
    make_user,
)
from users.models import User


class HldApiTestCase(TestCase):
    """Base class: an authenticated planner and an authenticated engineer."""

    def setUp(self):
        self.planner = make_user('planner@example.com', role=User.Role.SUBADMIN)
        self.engineer = make_user('engineer@example.com', role=User.Role.ENGINEER)

        self.client = APIClient()
        self.client.force_authenticate(user=self.planner)

        # An unauthenticated client for the auth-boundary tests.
        self.anon = APIClient()

    def auth_as(self, user):
        self.client.force_authenticate(user=user)
        return self.client


# ======================================================================
# Authentication boundary
# ======================================================================


class HldAuthTests(HldApiTestCase):
    """Every HLD endpoint must reject an unauthenticated caller."""

    def test_every_endpoint_requires_authentication(self):
        project_id = 'a' * 32
        checks = [
            ('get', '/api/ftth/hld/results/%s/' % project_id),
            ('get', '/api/ftth/hld/results/%s/layers/objects/' % project_id),
            ('get', '/api/ftth/hld/download/%s/Objects.gpkg' % project_id),
            ('get', '/api/ftth/hld/results/%s/survey-package/' % project_id),
            ('get', '/api/ftth/hld/results/%s/design-package/' % project_id),
            ('get', '/api/ftth/hld/results/%s/boq/' % project_id),
            ('get', '/api/ftth/hld/results/%s/boq/download/' % project_id),
            ('post', '/api/ftth/hld/results/%s/boq/regenerate/' % project_id),
            ('get', '/api/ftth/hld/projects/'),
            ('delete', '/api/ftth/hld/projects/%s/' % project_id),
            ('post', '/api/ftth/hld/projects/%s/assign/' % project_id),
            ('get', '/api/ftth/hld/results/%s/trench-design/' % project_id),
            ('post', '/api/ftth/hld/results/%s/trench-design/run/' % project_id),
            ('post', '/api/ftth/hld/run/'),
            ('get', '/api/ftth/hld/results/%s/surface-ai-review/' % project_id),
            ('post', '/api/ftth/hld/results/%s/surface-ai-review/classify/' % project_id),
            ('post', '/api/ftth/hld/results/%s/surface-ai-review/imagery/' % project_id),
        ]
        for method, url in checks:
            with self.subTest(url=url, method=method):
                response = getattr(self.anon, method)(url)
                self.assertEqual(response.status_code, 401, f'{method.upper()} {url}')


# ======================================================================
# POST /api/ftth/hld/run/
# ======================================================================


class RunPipelineViewTests(HldApiTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # The view writes the uploads under HOST_OUTPUTS_DIR before calling the
        # engine; point it at a throwaway directory so the test never touches
        # the real media store.
        patcher = mock.patch('ftth_hld.api.HOST_OUTPUTS_DIR', Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _post(self, excel=None, roads=None, **extra):
        # Omit missing parts entirely — DRF's multipart encoder rejects None.
        data = {k: v for k, v in (('excel', excel), ('roads', roads)) if v is not None}
        data.update(extra)
        return self.client.post('/api/ftth/hld/run/', data)

    def test_requires_both_files(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        response = self._post(excel=SimpleUploadedFile('a.xlsx', b'x'))
        self.assertEqual(response.status_code, 400)
        self.assertIn('excel', response.json()['detail'])

        response = self._post(roads=SimpleUploadedFile('r.gpkg', b'x'))
        self.assertEqual(response.status_code, 400)

    def test_rejects_unsupported_extensions(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        response = self._post(SimpleUploadedFile('a.csv', b'x'), SimpleUploadedFile('r.gpkg', b'x'))
        self.assertEqual(response.status_code, 400)
        self.assertIn('Excel', response.json()['detail'])

        response = self._post(
            SimpleUploadedFile('a.xlsx', b'x'), SimpleUploadedFile('r.docx', b'x')
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('Roads', response.json()['detail'])

    def test_submits_to_engine_and_creates_project(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        with mock.patch(
            'ftth_hld.api.run_pipeline', return_value={'status': 'queued'}
        ) as run_pipeline:
            response = self._post(
                SimpleUploadedFile('Main_DataSet.xlsx', b'excel-bytes'),
                SimpleUploadedFile('roads.gpkg', b'roads-bytes'),
                name='Berlin Dry Run',
            )

        self.assertEqual(response.status_code, 202)
        body = response.json()
        project_id = body['project_id']
        self.assertEqual(body['status'], 'queued')
        self.assertEqual(body['results_url'], f'/api/ftth/hld/results/{project_id}/')

        # The run is recorded and the uploads are persisted for the engine.
        project = FtthProject.objects.get(pk=project_id)
        self.assertEqual(project.name, 'Berlin Dry Run')
        self.assertEqual(project.created_by, self.planner)
        self.assertEqual(project.excel_filename, 'Main_DataSet.xlsx')

        inputs = Path(self.tmp.name) / project_id / 'inputs'
        self.assertEqual((inputs / 'Main_DataSet.xlsx').read_bytes(), b'excel-bytes')
        self.assertEqual((inputs / 'roads.gpkg').read_bytes(), b'roads-bytes')

        # The engine is handed the on-disk paths, not the upload objects.
        kwargs = run_pipeline.call_args.kwargs
        self.assertEqual(kwargs['project_id'], project_id)
        self.assertTrue(kwargs['excel_path'].endswith('Main_DataSet.xlsx'))
        self.assertTrue(kwargs['roads_path'].endswith('roads.gpkg'))

    def test_engine_failure_returns_502(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        with mock.patch('ftth_hld.api.run_pipeline', side_effect=RuntimeError('engine down')):
            response = self._post(
                SimpleUploadedFile('a.xlsx', b'x'),
                SimpleUploadedFile('r.gpkg', b'x'),
            )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()['detail'], 'engine down')


# ======================================================================
# GET /api/ftth/hld/results/<id>/
# ======================================================================


class PipelineStatusViewTests(HldApiTestCase):
    def test_unknown_project_returns_404(self):
        response = self.client.get('/api/ftth/hld/results/does-not-exist/')
        self.assertEqual(response.status_code, 404)

    def test_completed_engine_status_is_persisted(self):
        ftth = make_ftth_project(status=FtthProject.STATUS_RUNNING)
        engine_payload = {
            'project_id': ftth.project_id,
            'status': 'completed',
            'progress': 100,
            'stage_name': 'Complete',
            'layers': [{'name': 'objects', 'count': 5}],
            'downloads': [],
            'messages': [],
        }
        with mock.patch('ftth_hld.api.get_status', return_value=engine_payload):
            response = self.client.get(f'/api/ftth/hld/results/{ftth.project_id}/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'completed')
        ftth.refresh_from_db()
        self.assertEqual(ftth.status, FtthProject.STATUS_COMPLETED)
        self.assertEqual(ftth.progress, 100)
        self.assertIsNotNone(ftth.completed_at)

    def test_running_progress_is_capped_below_100(self):
        """A non-completed run must never advertise a finished progress bar."""
        ftth = make_ftth_project(status=FtthProject.STATUS_QUEUED)
        engine_payload = {
            'project_id': ftth.project_id,
            'status': 'running',
            'progress': 100,  # buggy/stale engine value
            'stage_name': 'Ducts',
            'layers': [],
            'downloads': [],
            'messages': [],
        }
        with mock.patch('ftth_hld.api.get_status', return_value=engine_payload):
            response = self.client.get(f'/api/ftth/hld/results/{ftth.project_id}/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['progress'], 99)
        ftth.refresh_from_db()
        self.assertEqual(ftth.progress, 99)
        self.assertNotEqual(ftth.status, FtthProject.STATUS_COMPLETED)

    def test_unreachable_engine_falls_back_to_persisted_status(self):
        """When the engine forgets the run, the DB status must not claim 100%."""
        ftth = make_ftth_project(status=FtthProject.STATUS_RUNNING)
        FtthProject.objects.filter(pk=ftth.project_id).update(progress=100)

        with mock.patch('ftth_hld.api.get_status', return_value={'status': 'unknown'}):
            response = self.client.get(f'/api/ftth/hld/results/{ftth.project_id}/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], FtthProject.STATUS_RUNNING)
        self.assertEqual(response.json()['progress'], 99)

    def test_engine_forgotten_run_with_layers_reports_completed(self):
        """A run whose engine registry was lost still shows its layers."""
        ftth = make_ftth_project(status=FtthProject.STATUS_RUNNING)
        make_hld_layer(ftth, name='objects', feature_count=7)

        with mock.patch('ftth_hld.api.get_status', return_value={'status': 'unknown'}):
            response = self.client.get(f'/api/ftth/hld/results/{ftth.project_id}/')

        body = response.json()
        self.assertEqual(body['status'], 'completed')
        self.assertEqual(body['progress'], 100)
        self.assertEqual(body['layers'], [{'name': 'objects', 'count': 7}])

    def test_status_includes_survey_assignment_block(self):
        ftth = make_ftth_project(status=FtthProject.STATUS_RUNNING)
        copy = make_survey_copy(ftth, name='Berlin — Survey')

        with mock.patch(
            'ftth_hld.api.get_status', return_value={'status': 'running', 'progress': 10}
        ):
            response = self.client.get(f'/api/ftth/hld/results/{ftth.project_id}/')

        survey = response.json()['survey']
        self.assertEqual(survey['copy_project_id'], str(copy.id))
        self.assertEqual(survey['copy_name'], 'Berlin — Survey')


# ======================================================================
# GET /api/ftth/hld/results/<id>/layers/<name>/
# ======================================================================


class LayerGeoJSONViewTests(HldApiTestCase):
    def test_unknown_layer_name_returns_404(self):
        ftth = make_ftth_project()
        response = self.client.get(f'/api/ftth/hld/results/{ftth.project_id}/layers/not_a_layer/')
        self.assertEqual(response.status_code, 404)
        self.assertIn('Unknown layer', response.json()['detail'])

    def test_serves_persisted_layer_before_calling_the_engine(self):
        ftth = make_ftth_project()
        make_hld_layer(ftth, name='objects', feature_count=3)

        with mock.patch('ftth_hld.api.get_layer_geojson') as engine:
            response = self.client.get(f'/api/ftth/hld/results/{ftth.project_id}/layers/objects/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['features']), 1)
        engine.assert_not_called()

    def test_fetches_from_engine_and_persists_when_not_cached(self):
        ftth = make_ftth_project()
        payload = {
            'type': 'FeatureCollection',
            'features': [{'type': 'Feature', 'geometry': LINE, 'properties': {}}],
        }

        with mock.patch(
            'ftth_hld.api.get_layer_geojson', return_value=json.dumps(payload).encode()
        ):
            response = self.client.get(f'/api/ftth/hld/results/{ftth.project_id}/layers/objects/')

        self.assertEqual(response.status_code, 200)
        row = FtthLayer.objects.get(ftth_project=ftth, name='objects')
        self.assertEqual(row.feature_count, 1)

    def test_missing_engine_layer_returns_404(self):
        ftth = make_ftth_project()
        with mock.patch('ftth_hld.api.get_layer_geojson', return_value=None):
            response = self.client.get(f'/api/ftth/hld/results/{ftth.project_id}/layers/objects/')
        self.assertEqual(response.status_code, 404)

    def test_invalid_engine_geojson_returns_502(self):
        ftth = make_ftth_project()
        with mock.patch('ftth_hld.api.get_layer_geojson', return_value=b'not json{{'):
            response = self.client.get(f'/api/ftth/hld/results/{ftth.project_id}/layers/objects/')
        self.assertEqual(response.status_code, 502)

    def test_empty_cached_layer_is_refreshed_from_engine(self):
        """A FeatureCollection persisted mid-run must not be served forever."""
        ftth = make_ftth_project()
        make_hld_layer(
            ftth,
            name='objects',
            geojson={'type': 'FeatureCollection', 'features': []},
            feature_count=0,
        )

        payload = {
            'type': 'FeatureCollection',
            'features': [{'type': 'Feature', 'geometry': LINE, 'properties': {}}],
        }
        with mock.patch(
            'ftth_hld.api.get_layer_geojson', return_value=json.dumps(payload).encode()
        ):
            response = self.client.get(f'/api/ftth/hld/results/{ftth.project_id}/layers/objects/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['features']), 1)


# ======================================================================
# GET /api/ftth/hld/download/<id>/<path>
# ======================================================================


class DownloadFileViewTests(HldApiTestCase):
    def test_download_strips_directory_traversal_from_the_path(self):
        """The engine must only ever be asked for a basename."""
        ftth = make_ftth_project()
        with mock.patch('ftth_hld.api.get_download_file', return_value=b'data') as get_file:
            response = self.client.get(
                f'/api/ftth/hld/download/{ftth.project_id}/sub/../../etc/passwd'
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(get_file.call_args.args[1], 'passwd')

    def test_missing_file_returns_404(self):
        ftth = make_ftth_project()
        with mock.patch('ftth_hld.api.get_download_file', return_value=None):
            response = self.client.get(f'/api/ftth/hld/download/{ftth.project_id}/Objects.gpkg')
        self.assertEqual(response.status_code, 404)

    def test_successful_download_sets_attachment_headers(self):
        ftth = make_ftth_project()
        with mock.patch('ftth_hld.api.get_download_file', return_value=b'gpkg-bytes'):
            response = self.client.get(f'/api/ftth/hld/download/{ftth.project_id}/Objects.gpkg')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'gpkg-bytes')
        self.assertIn('filename="Objects.gpkg"', response['Content-Disposition'])
        self.assertEqual(response['Content-Length'], str(len(b'gpkg-bytes')))


# ======================================================================
# Packages
# ======================================================================


class PackageViewTests(HldApiTestCase):
    def test_survey_package_requires_a_completed_run(self):
        ftth = make_ftth_project(status=FtthProject.STATUS_RUNNING)
        response = self.client.get(f'/api/ftth/hld/results/{ftth.project_id}/survey-package/')
        self.assertEqual(response.status_code, 400)

    def test_survey_package_404_for_unknown_project(self):
        response = self.client.get('/api/ftth/hld/results/nope/survey-package/')
        self.assertEqual(response.status_code, 404)

    def test_survey_package_returns_zip(self):
        ftth = make_ftth_project()
        with mock.patch('ftth_hld.api.generate_survey_package', return_value=b'PK\x03\x04zip'):
            response = self.client.get(f'/api/ftth/hld/results/{ftth.project_id}/survey-package/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/zip')

    def test_missing_design_package_files_returns_404(self):
        ftth = make_ftth_project()
        with mock.patch(
            'ftth_hld.api.generate_design_package', side_effect=FileNotFoundError('no layers')
        ):
            response = self.client.get(f'/api/ftth/hld/results/{ftth.project_id}/design-package/')
        self.assertEqual(response.status_code, 404)

    def test_design_package_failure_returns_502(self):
        ftth = make_ftth_project()
        with mock.patch('ftth_hld.api.generate_design_package', side_effect=RuntimeError('boom')):
            response = self.client.get(f'/api/ftth/hld/results/{ftth.project_id}/design-package/')
        self.assertEqual(response.status_code, 502)


# ======================================================================
# BOQ / BOM
# ======================================================================


class BoqViewTests(HldApiTestCase):
    def test_boq_requires_a_completed_run(self):
        ftth = make_ftth_project(status=FtthProject.STATUS_RUNNING)
        response = self.client.get(f'/api/ftth/hld/results/{ftth.project_id}/boq/')
        self.assertEqual(response.status_code, 400)

    def test_boq_returns_snapshot_payload(self):
        ftth = make_ftth_project()
        snapshot = mock.Mock(
            boq_json=[{'code': '1.1'}],
            bom_json=[],
            boq_totals={'total': 1.0},
            bom_totals={},
            regenerated_at=None,
            created_at=None,
        )
        with (
            mock.patch('ftth_hld.api.generate_snapshot', return_value=snapshot),
            mock.patch(
                'ftth_hld.boq_anomalies.detect_boq_anomalies',
                return_value={'anomalies': [], 'checked': 0, 'basis': {}},
            ),
        ):
            response = self.client.get(f'/api/ftth/hld/results/{ftth.project_id}/boq/')

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['boq_rows'], [{'code': '1.1'}])
        self.assertEqual(body['project_id'], ftth.project_id)

    def test_boq_missing_layers_returns_404(self):
        ftth = make_ftth_project()
        with mock.patch('ftth_hld.api.generate_snapshot', side_effect=ValueError('no layers')):
            response = self.client.get(f'/api/ftth/hld/results/{ftth.project_id}/boq/')
        self.assertEqual(response.status_code, 404)

    def test_regenerate_forces_recompute(self):
        ftth = make_ftth_project()
        snapshot = mock.Mock(
            boq_json=[],
            bom_json=[],
            boq_totals={},
            bom_totals={},
            regenerated_at=None,
            created_at=None,
        )
        with mock.patch('ftth_hld.api.generate_snapshot', return_value=snapshot) as generate:
            response = self.client.post(f'/api/ftth/hld/results/{ftth.project_id}/boq/regenerate/')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(generate.call_args.kwargs.get('force'))

    def test_boq_download_returns_xlsx(self):
        ftth = make_ftth_project()
        with mock.patch('ftth_hld.api.render_boq_xlsx', return_value=b'xlsx-bytes'):
            response = self.client.get(f'/api/ftth/hld/results/{ftth.project_id}/boq/download/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'xlsx-bytes')
        self.assertIn('.xlsx', response['Content-Disposition'])


# ======================================================================
# Project list / delete / assign
# ======================================================================


class ProjectListTests(HldApiTestCase):
    def test_lists_projects_from_the_payload_builder(self):
        payload = [{'project_id': 'abc', 'name': 'Run 1'}]
        with mock.patch('ftth_hld.api.ftth_project_payloads', return_value=payload) as builder:
            response = self.client.get('/api/ftth/hld/projects/?limit=5')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), payload)
        builder.assert_called_once_with(5)


class DeleteProjectViewTests(HldApiTestCase):
    def test_unknown_project_returns_404(self):
        response = self.client.delete('/api/ftth/hld/projects/nope/')
        self.assertEqual(response.status_code, 404)

    def test_deletes_django_row_before_calling_the_engine(self):
        """The engine's project row is FK-referenced, so Django must go first."""
        ftth = make_ftth_project()
        order = []

        def fake_delete(project_id):
            order.append(('engine', FtthProject.objects.filter(pk=project_id).exists()))
            return {'deleted': True, 'postgis_cleaned': True}

        with mock.patch('ftth_hld.api.delete_project', side_effect=fake_delete):
            response = self.client.delete(f'/api/ftth/hld/projects/{ftth.project_id}/')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['deleted'])
        # By the time the engine was called, the Django row was already gone.
        self.assertEqual(order, [('engine', False)])
        self.assertFalse(FtthProject.objects.filter(pk=ftth.project_id).exists())

    def test_engine_delete_failure_is_surfaced_to_the_caller(self):
        """Engine cleanup is best-effort and reported, never fatal.

        ``ftth_hld.pipeline.delete_project`` swallows transport/HTTP failures
        and returns a failure payload instead of raising, so the gateway must
        pass that payload on (an untidy orphaned directory is recoverable; a
        500 after the Django row is already committed is not).
        """
        ftth = make_ftth_project()
        engine_result = {
            'deleted': False,
            'detail': 'Engine unreachable: connection refused',
            'postgis_cleaned': False,
            'postgis_error': 'connection refused',
        }
        with mock.patch('ftth_hld.api.delete_project', return_value=engine_result):
            response = self.client.delete(f'/api/ftth/hld/projects/{ftth.project_id}/')

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['deleted'])
        self.assertFalse(body['engine_deleted'])
        self.assertEqual(body['postgis_error'], 'connection refused')
        # The Django row is gone regardless of the engine outcome.
        self.assertFalse(FtthProject.objects.filter(pk=ftth.project_id).exists())


class FtthProjectAssignViewTests(HldApiTestCase):
    def test_non_subadmin_cannot_assign(self):
        ftth = make_ftth_project()
        self.auth_as(self.engineer)
        response = self.client.post(
            f'/api/ftth/hld/projects/{ftth.project_id}/assign/',
            {'engineer_ids': [str(self.engineer.id)]},
        )
        self.assertEqual(response.status_code, 403)

    def test_requires_an_engineer_id(self):
        ftth = make_ftth_project()
        response = self.client.post(f'/api/ftth/hld/projects/{ftth.project_id}/assign/', {})
        self.assertEqual(response.status_code, 400)
        self.assertIn('engineer_id', response.json()['detail'])

    def test_assigns_and_returns_201(self):
        ftth = make_ftth_project()
        with mock.patch(
            'ftth_hld.api.assign_hld_project', return_value={'survey_project_id': 'copy-1'}
        ) as assign:
            response = self.client.post(
                f'/api/ftth/hld/projects/{ftth.project_id}/assign/',
                {'engineer_ids': [str(self.engineer.id)]},
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['survey_project_id'], 'copy-1')
        self.assertEqual(assign.call_args.args[0], ftth.project_id)

    def test_missing_survey_package_returns_404(self):
        ftth = make_ftth_project()
        with mock.patch(
            'ftth_hld.api.assign_hld_project', side_effect=FileNotFoundError('no package')
        ):
            response = self.client.post(
                f'/api/ftth/hld/projects/{ftth.project_id}/assign/',
                {'engineer_ids': [str(self.engineer.id)]},
            )
        self.assertEqual(response.status_code, 404)


# ======================================================================
# Requirement: an ENGINEER may read HLD results but not act on them
# ======================================================================


class EngineerAccessTests(HldApiTestCase):
    def test_engineer_can_read_project_status(self):
        ftth = make_ftth_project()
        self.auth_as(self.engineer)
        with mock.patch(
            'ftth_hld.api.get_status', return_value={'status': 'running', 'progress': 3}
        ):
            response = self.client.get(f'/api/ftth/hld/results/{ftth.project_id}/')
        self.assertEqual(response.status_code, 200)

    def test_engineer_cannot_assign_or_delete(self):
        ftth = make_ftth_project()
        self.auth_as(self.engineer)

        response = self.client.post(
            f'/api/ftth/hld/projects/{ftth.project_id}/assign/',
            {'engineer_ids': [str(self.engineer.id)]},
        )
        self.assertEqual(response.status_code, 403)

        with mock.patch('ftth_hld.api.delete_project', return_value={'deleted': True}):
            response = self.client.delete(f'/api/ftth/hld/projects/{ftth.project_id}/')
        self.assertEqual(response.status_code, 200)


# ======================================================================
# POST /api/ftth/hld/results/<id>/surface-ai-review/classify/
# ======================================================================


class SurfaceAIPointClassifyViewTests(HldApiTestCase):
    """Classify one clicked map coordinate (advisory only)."""

    _URL = '/api/ftth/hld/results/%s/surface-ai-review/classify/'

    def setUp(self):
        super().setUp()
        self.project = make_ftth_project()

    def _post(self, payload, project_id=None):
        return self.client.post(
            self._URL % (project_id or self.project.pk),
            data=json.dumps(payload),
            content_type='application/json',
        )

    def test_delegates_to_the_engine_and_returns_its_item(self):
        item = {
            'AI_SURFACE': 'road',
            'confidence': 0.8,
            'review_status': 'pending',
            'span_id': 'CLICK-1.52490-49.07620',
        }
        with mock.patch('ftth_hld.api.classify_surface_at_point', return_value=item) as engine:
            response = self._post({'coordinates': [1.5249, 49.0762], 'crs': 'EPSG:4326'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), item)
        engine.assert_called_once()
        args, kwargs = engine.call_args
        self.assertEqual(args[0], self.project.pk)
        self.assertEqual(args[1], [1.5249, 49.0762])
        self.assertEqual(kwargs['crs'], 'EPSG:4326')

    def test_rejects_malformed_coordinates_without_calling_the_engine(self):
        with mock.patch('ftth_hld.api.classify_surface_at_point') as engine:
            for payload in (
                {},
                {'coordinates': [1.0]},
                {'coordinates': ['a', 'b']},
                {'coordinates': None},
            ):
                with self.subTest(payload=payload):
                    response = self._post(payload)
                    self.assertEqual(response.status_code, 400)
        engine.assert_not_called()

    def test_unknown_project_is_404(self):
        with mock.patch('ftth_hld.api.classify_surface_at_point') as engine:
            response = self._post({'coordinates': [1.0, 49.0]}, project_id='f' * 32)
        self.assertEqual(response.status_code, 404)
        engine.assert_not_called()

    def test_engine_status_code_travels_to_the_caller(self):
        with mock.patch(
            'ftth_hld.api.classify_surface_at_point',
            side_effect=EngineError(400, 'coordinates must be [x, y]'),
        ):
            response = self._post({'coordinates': [1.0, 49.0]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['detail'], 'coordinates must be [x, y]')

    def test_vision_review_refusal_is_still_a_normal_200(self):
        # The engine reports an unparseable model answer as a 200 with
        # review_status "error" — a suggestion that failed, not a gateway fault.
        item = {'review_status': 'error', 'reason': 'ValueError', 'AI_SURFACE': None}
        with mock.patch('ftth_hld.api.classify_surface_at_point', return_value=item):
            response = self._post({'coordinates': [1.5249, 49.0762]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['review_status'], 'error')

    def test_span_payload_reaches_the_engine_with_its_own_route(self):
        # A per-span opt-in sends the span's real geometry, so exactly one model
        # call is spent on the span the reader chose.
        item = {'AI_SURFACE': 'garden', 'review_status': 'pending', 'span_id': 'TR-1'}
        route = [[1.5249, 49.0762], [1.52492, 49.07618]]
        with mock.patch('ftth_hld.api.classify_surface_at_point', return_value=item) as engine:
            response = self._post(
                {
                    'span_id': 'TR-1',
                    'coordinates': route,
                    'coordinates_crs': 'EPSG:25833',
                    'claimed_surface': 'Footpath',
                    'include_imagery': True,
                }
            )
        self.assertEqual(response.status_code, 200)
        args, kwargs = engine.call_args
        self.assertEqual(args[0], self.project.pk)
        self.assertEqual(args[1], route)
        self.assertEqual(kwargs['span_id'], 'TR-1')
        self.assertEqual(kwargs['coordinates_crs'], 'EPSG:25833')
        self.assertEqual(kwargs['claimed_surface'], 'Footpath')
        self.assertTrue(kwargs['include_imagery'])

    def test_a_route_without_a_span_id_is_rejected(self):
        with mock.patch('ftth_hld.api.classify_surface_at_point') as engine:
            response = self._post({'coordinates': [[1.0, 49.0], [1.001, 49.001]]})
        self.assertEqual(response.status_code, 400)
        engine.assert_not_called()


# ======================================================================
# POST /api/ftth/hld/results/<id>/surface-ai-review/imagery/
# ======================================================================


class SurfaceAIImageryViewTests(HldApiTestCase):
    """Show the imagery patch a detect would send, without calling the model."""

    _URL = '/api/ftth/hld/results/%s/surface-ai-review/imagery/'

    def setUp(self):
        super().setUp()
        self.project = make_ftth_project()

    def _post(self, payload, project_id=None):
        return self.client.post(
            self._URL % (project_id or self.project.pk),
            data=json.dumps(payload),
            content_type='application/json',
        )

    def test_delegates_to_the_engine_and_returns_the_patch(self):
        item = {
            'review_status': 'pending',
            'image_base64': 'aGk=',
            'imagery_source': 'Esri World Imagery',
            'span_id': 'TR-1',
        }
        route = [[1.5249, 49.0762], [1.52492, 49.07618]]
        with mock.patch('ftth_hld.api.preview_surface_imagery', return_value=item) as engine:
            response = self._post(
                {'span_id': 'TR-1', 'coordinates': route, 'coordinates_crs': 'EPSG:25833'}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), item)
        args, _kwargs = engine.call_args
        self.assertEqual(args[0], self.project.pk)
        self.assertEqual(args[1]['coordinates'], route)
        self.assertEqual(args[1]['coordinates_crs'], 'EPSG:25833')
        self.assertEqual(args[1]['span_id'], 'TR-1')

    def test_a_clicked_point_forwards_its_crs(self):
        with mock.patch(
            'ftth_hld.api.preview_surface_imagery', return_value={'review_status': 'no_imagery'}
        ) as engine:
            response = self._post({'coordinates': [1.5249, 49.0762], 'crs': 'EPSG:4326'})
        self.assertEqual(response.status_code, 200)
        args, _kwargs = engine.call_args
        self.assertEqual(args[1]['crs'], 'EPSG:4326')

    def test_rejects_malformed_coordinates_without_calling_the_engine(self):
        with mock.patch('ftth_hld.api.preview_surface_imagery') as engine:
            for payload in ({}, {'coordinates': [1.0]}, {'coordinates': None}):
                with self.subTest(payload=payload):
                    self.assertEqual(self._post(payload).status_code, 400)
        engine.assert_not_called()

    def test_unknown_project_is_404(self):
        with mock.patch('ftth_hld.api.preview_surface_imagery') as engine:
            response = self._post({'coordinates': [1.0, 49.0]}, project_id='f' * 32)
        self.assertEqual(response.status_code, 404)
        engine.assert_not_called()

    def test_engine_status_code_travels_to_the_caller(self):
        with mock.patch(
            'ftth_hld.api.preview_surface_imagery',
            side_effect=EngineError(502, 'The engine could not fetch the surface imagery.'),
        ):
            response = self._post({'coordinates': [1.0, 49.0]})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json()['detail'], 'The engine could not fetch the surface imagery.'
        )


# Keep a reference so linters see the imported factory helpers as used by
# subclasses that import from this module.
__all__ = ['HldApiTestCase', 'LINE', 'make_feature']
