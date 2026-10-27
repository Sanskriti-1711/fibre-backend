"""The run form's existing-infrastructure toggle must reach the engine.

The engine feeds a project's own published network back into any re-run as
``BF_*`` and prices an existing edge at 0.1x a new one, so a re-run replays the
design it replaces. The toggle on the run form is the only way to ask for a
fresh design, so what is pinned here is the gateway's own contract: the flag
travels on both run routes, and an absent or unreadable flag keeps the previous
behaviour (reuse ON) rather than silently changing what a client gets.

Run with:
    python manage.py test --settings=config.test_settings ftth_hld.tests.test_brownfield_toggle
"""

from __future__ import annotations

from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from testutils.factories import make_user
from users.models import User


class BrownfieldToggleTests(TestCase):
    def setUp(self):
        self.user = make_user('planner@example.com', role=User.Role.SUBADMIN)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _run_from_area(self, **extra):
        payload = {'area': 'B16 8UU', 'use_brownfield': None}
        payload.update(extra)
        payload = {k: v for k, v in payload.items() if v is not None}
        with mock.patch(
            'ftth_hld.api.run_from_area',
            return_value={'project_id': 'p1', 'status': 'queued'},
        ) as engine:
            response = self.client.post(
                '/api/ftth/hld/run-from-area/', payload, format='json')
        return response, engine

    def test_the_toggle_off_is_forwarded(self):
        response, engine = self._run_from_area(use_brownfield=False)
        self.assertIn(response.status_code, (200, 202))
        self.assertIs(engine.call_args.kwargs.get('use_brownfield'), False)

    def test_the_toggle_on_is_forwarded(self):
        response, engine = self._run_from_area(use_brownfield=True)
        self.assertIn(response.status_code, (200, 202))
        self.assertIs(engine.call_args.kwargs.get('use_brownfield'), True)

    def test_an_absent_toggle_keeps_reuse_on(self):
        response, engine = self._run_from_area()
        self.assertIn(response.status_code, (200, 202))
        self.assertIs(engine.call_args.kwargs.get('use_brownfield'), True)

    def test_an_unreadable_toggle_keeps_reuse_on(self):
        response, engine = self._run_from_area(use_brownfield='maybe')
        self.assertIn(response.status_code, (200, 202))
        self.assertIs(engine.call_args.kwargs.get('use_brownfield'), True)

    def test_the_upload_route_forwards_the_toggle_too(self):
        excel = SimpleUploadedFile(
            'addresses.xlsx', b'x', content_type='application/vnd.ms-excel')
        roads = SimpleUploadedFile(
            'roads.geojson', b'{}', content_type='application/json')
        with mock.patch(
            'ftth_hld.api.run_pipeline',
            return_value={'project_id': 'p1', 'status': 'queued'},
        ) as engine:
            response = self.client.post(
                '/api/ftth/hld/run/',
                {'excel': excel, 'roads': roads, 'use_brownfield': 'false'},
                format='multipart',
            )

        self.assertIn(response.status_code, (200, 202))
        self.assertIs(engine.call_args.kwargs.get('use_brownfield'), False)
