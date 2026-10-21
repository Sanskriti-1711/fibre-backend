"""The drawn section must survive the gateway.

The HLD endpoints are a Django gateway in front of the FastAPI engine, and the
section a planner draws on the map is only useful if it arrives at the engine:
the preview counts, the review layers and the run all have to describe the SAME
polygon, or the run designs an area whose numbers nobody saw.

What is pinned here is only the gateway's own contract — the polygon is carried
through as an object on JSON and as JSON text on the multipart path (where every
field is a string), and it reaches the engine call unchanged.  What a usable
polygon IS belongs to the engine's own tests.

Run with:
    python manage.py test --settings=config.test_settings ftth_hld.tests.test_area_section_proxy
"""

from __future__ import annotations

from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from testutils.factories import make_user
from users.models import User

SECTION = {
    'type': 'Polygon',
    'coordinates': [
        [[-1.9250, 52.4740], [-1.9210, 52.4740], [-1.9210, 52.4770],
         [-1.9250, 52.4770], [-1.9250, 52.4740]]
    ],
}


class AreaSectionProxyTests(TestCase):
    def setUp(self):
        self.user = make_user('planner@example.com', role=User.Role.SUBADMIN)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_the_preview_forwards_the_drawn_section(self):
        with mock.patch('ftth_hld.api.resolve_area', return_value={'bbox': []}) as engine:
            response = self.client.post(
                '/api/ftth/hld/resolve-area/',
                {'area': 'B16 8UU', 'polygon': SECTION},
                format='json',
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(engine.call_args.kwargs.get('polygon'), SECTION)

    def test_the_review_layers_forward_the_drawn_section(self):
        with mock.patch('ftth_hld.api.get_input_layer', return_value={'features': []}) as engine:
            response = self.client.post(
                '/api/ftth/hld/input-layers/',
                {'area': 'B16 8UU', 'layer': 'objects', 'polygon': SECTION},
                format='json',
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(engine.call_args.kwargs.get('polygon'), SECTION)

    def test_the_run_forwards_the_drawn_section(self):
        with mock.patch('ftth_hld.api.run_from_area',
                        return_value={'area': 'B16 8UU', 'status': 'queued'}) as engine:
            response = self.client.post(
                '/api/ftth/hld/run-from-area/',
                {'area': 'B16 8UU', 'polygon': SECTION},
                format='json',
            )

        self.assertIn(response.status_code, (200, 202))   # a run is accepted
        self.assertEqual(engine.call_args.kwargs.get('polygon'), SECTION)

    def test_the_section_survives_the_multipart_path_as_json_text(self):
        """A brownfield upload makes the whole body a form: fields become strings."""
        import json

        with mock.patch('ftth_hld.api.run_from_area',
                        return_value={'area': 'B16 8UU', 'status': 'queued'}) as engine:
            response = self.client.post(
                '/api/ftth/hld/run-from-area/',
                {
                    'area': 'B16 8UU',
                    'polygon': json.dumps(SECTION),
                    'brownfield': __import__('io').BytesIO(b'zip'),
                },
                format='multipart',
            )

        self.assertIn(response.status_code, (200, 202))
        self.assertEqual(engine.call_args.kwargs.get('polygon'), SECTION)

    def test_no_section_is_forwarded_as_none(self):
        with mock.patch('ftth_hld.api.resolve_area', return_value={'bbox': []}) as engine:
            self.client.post(
                '/api/ftth/hld/resolve-area/', {'area': 'B16 8UU'}, format='json'
            )

        self.assertIsNone(engine.call_args.kwargs.get('polygon'))
