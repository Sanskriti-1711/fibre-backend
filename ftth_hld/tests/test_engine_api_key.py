"""Django must send the engine's key on every mutating engine call.

The engine refuses writes without ``X-FTTH-Engine-Key`` (and refuses them with
503 when it has no key configured). If one call site forgets the header the
failure is a 401 at the far end of a proxy, which reads like "the engine is
down" — so the header is pinned here at the helper and at a real call.
"""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from ftth_hld import config, pipeline


class EngineApiKeyTests(unittest.TestCase):
    def test_no_key_means_no_header(self):
        with patch.object(config, 'FTTH_ENGINE_API_KEY', ''):
            self.assertEqual(config.engine_write_headers(), {})

    def test_a_key_becomes_the_engine_header(self):
        with patch.object(config, 'FTTH_ENGINE_API_KEY', 'shared-secret'):
            self.assertEqual(
                config.engine_write_headers(), {'X-FTTH-Engine-Key': 'shared-secret'}
            )

    def test_a_delete_carries_the_header(self):
        response = Mock(status_code=200)
        response.json.return_value = {'deleted': True}
        with patch.object(config, 'FTTH_ENGINE_API_KEY', 'shared-secret'):
            with patch.object(pipeline.requests, 'delete', return_value=response) as delete:
                pipeline.delete_project('p-1')
        self.assertEqual(
            delete.call_args.kwargs['headers'], {'X-FTTH-Engine-Key': 'shared-secret'}
        )

    def test_a_write_without_a_configured_key_sends_no_header(self):
        response = Mock(status_code=200)
        response.json.return_value = {'deleted': True}
        with patch.object(config, 'FTTH_ENGINE_API_KEY', ''):
            with patch.object(pipeline.requests, 'delete', return_value=response) as delete:
                pipeline.delete_project('p-1')
        self.assertEqual(delete.call_args.kwargs['headers'], {})
