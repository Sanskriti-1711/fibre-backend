"""The two health probes are only useful if they disagree correctly.

``/healthz`` must never fail because a dependency is down — the platform
restarts the container on a non-200, so a liveness probe that checked the
engine would turn an engine outage into a restart loop.

``/healthz/engine`` must fail loudly whenever the engine is unreachable or
unhealthy, because that is the only signal available for a service we do not
otherwise monitor.

Both are pinned here, including the detail that the probe must not leak the
engine's filesystem paths.
"""

from __future__ import annotations

import json
from unittest import mock

from django.test import SimpleTestCase

from config import health


def _engine_response(body, status_code=200):
    """Build a stand-in for the engine's ``requests`` response."""
    response = mock.Mock()
    response.status_code = status_code
    response.json.return_value = body
    return response


def _engine_response_bad_json(status_code=200):
    response = mock.Mock()
    response.status_code = status_code
    response.json.side_effect = ValueError('not json')
    return response


HEALTHY_ENGINE = {
    'status': 'ok',
    'service': 'ftth-engine-api',
    'uptime_seconds': 1234,
    'qgis_process': '/usr/bin/qgis_process',
    'postgis': {'available': True},
    'endpoints': ['POST /ftth/hld/run'],
}


class LivenessProbeTests(SimpleTestCase):
    def test_healthz_is_ok_without_any_dependency(self):
        response = self.client.get('/healthz')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'ok'})

    def test_healthz_never_calls_the_engine(self):
        """A dead engine must not make the platform restart a healthy app."""
        with mock.patch('config.health.requests.get') as get:
            response = self.client.get('/healthz')
        self.assertEqual(response.status_code, 200)
        get.assert_not_called()


class EngineProbeTests(SimpleTestCase):
    def setUp(self):
        patcher = mock.patch.object(health, 'FTTH_ENGINE_URL', 'https://engine.example.test')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_healthy_engine_reports_ok(self):
        with mock.patch(
            'config.health.requests.get', return_value=_engine_response(HEALTHY_ENGINE)
        ) as get:
            response = self.client.get('/healthz/engine')

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['engine_status'], 'ok')
        self.assertEqual(body['service'], 'ftth-engine-api')
        self.assertEqual(body['uptime_seconds'], 1234)
        self.assertTrue(body['qgis_available'])
        self.assertIn('latency_ms', body)
        get.assert_called_once_with(
            'https://engine.example.test/health',
            timeout=health.ENGINE_PROBE_TIMEOUT,
        )

    def test_timeout_is_generous_enough_for_the_real_engine(self):
        """The live engine's /health was measured at ~5.3s. A probe timeout at
        or below that would flap, so guard against it being tuned back down
        without evidence."""
        self.assertGreaterEqual(health.ENGINE_PROBE_TIMEOUT, 10)

    def test_unreachable_engine_is_503(self):
        import requests

        with mock.patch(
            'config.health.requests.get',
            side_effect=requests.ConnectionError('connection refused'),
        ):
            response = self.client.get('/healthz/engine')

        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertFalse(body['ok'])
        self.assertIn('ConnectionError', body['error'])

    def test_timeout_is_503(self):
        import requests

        with mock.patch(
            'config.health.requests.get',
            side_effect=requests.Timeout('timed out'),
        ):
            response = self.client.get('/healthz/engine')

        self.assertEqual(response.status_code, 503)
        self.assertIn('Timeout', response.json()['error'])

    def test_engine_http_error_is_503(self):
        with mock.patch(
            'config.health.requests.get',
            return_value=_engine_response({}, status_code=502),
        ):
            response = self.client.get('/healthz/engine')

        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body['http_status'], 502)
        self.assertIn('502', body['error'])

    def test_non_json_body_is_503(self):
        with mock.patch(
            'config.health.requests.get',
            return_value=_engine_response_bad_json(),
        ):
            response = self.client.get('/healthz/engine')

        self.assertEqual(response.status_code, 503)
        self.assertIn('non-JSON', response.json()['error'])

    def test_engine_reporting_unhealthy_is_503(self):
        body = dict(HEALTHY_ENGINE, status='degraded')
        with mock.patch('config.health.requests.get', return_value=_engine_response(body)):
            response = self.client.get('/healthz/engine')

        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()['ok'])
        self.assertIn('degraded', response.json()['error'])

    def test_engine_up_but_qgis_missing_still_answers_ok(self):
        """Being reachable and being able to run a pipeline are different
        failure modes; qgis_available is surfaced so a monitor can alert on it
        without the probe conflating the two."""
        body = dict(HEALTHY_ENGINE, qgis_process=None)
        with mock.patch('config.health.requests.get', return_value=_engine_response(body)):
            response = self.client.get('/healthz/engine')

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['qgis_available'])

    def test_probe_does_not_leak_engine_filesystem_paths(self):
        """The endpoint is unauthenticated, so the engine's own qgis_process
        path and database details must not be echoed back."""
        with mock.patch(
            'config.health.requests.get', return_value=_engine_response(HEALTHY_ENGINE)
        ):
            response = self.client.get('/healthz/engine')

        raw = json.dumps(response.json())
        self.assertNotIn('qgis_process', raw)
        self.assertNotIn('/usr/bin', raw)
        self.assertNotIn('postgis', raw)

    def test_probe_uses_the_configured_engine_url(self):
        with (
            mock.patch.object(health, 'FTTH_ENGINE_URL', 'https://moved-engine.example.test'),
            mock.patch(
                'config.health.requests.get', return_value=_engine_response(HEALTHY_ENGINE)
            ) as get,
        ):
            self.client.get('/healthz/engine')

        get.assert_called_once_with(
            'https://moved-engine.example.test/health',
            timeout=health.ENGINE_PROBE_TIMEOUT,
        )
