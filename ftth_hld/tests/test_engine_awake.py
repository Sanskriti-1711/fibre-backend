"""A sleeping engine is a boot to wait out, not a 502 to report.

The engine is a free Render instance: it stops after 15 minutes without traffic
and answers 502 from the platform's edge until it has booted.  That is what a
planner's first click after a quiet spell used to get — ``Engine unreachable``
for a service that was coming up and would have answered a minute later.

``ensure_engine_awake`` spends the boot before the call, on a /health probe, so
the call that follows is a real call.  These tests are pure: ``requests`` and
``time.sleep`` are mocked, so what is asserted is how many probes it takes, what
the planner reads when the budget runs out, and that no probe is made once the
engine is known to be up.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest import mock

import requests
from django.test import SimpleTestCase

from ftth_hld import pipeline


def _response(status_code, body=None):
    resp = mock.Mock(status_code=status_code)
    resp.json.return_value = {'status': 'ok'} if body is None else body
    return resp


class AwakeTestCase(SimpleTestCase):
    def setUp(self):
        # The trust window is module state shared by every request, so it has
        # to start clean for each test.
        pipeline._engine_ready_until = 0.0
        self.addCleanup(setattr, pipeline, '_engine_ready_until', 0.0)


class SleepingEngineTests(AwakeTestCase):
    def test_a_booting_engine_is_waited_out_and_the_call_then_runs(self):
        probes = [_response(502), _response(502), _response(200)]
        post = mock.Mock(return_value=_response(200, {'area': 'B16 8UU'}))
        with (
            mock.patch.object(pipeline.requests, 'get', side_effect=probes),
            mock.patch.object(pipeline.requests, 'post', post),
            mock.patch.object(pipeline.time, 'sleep') as sleep,
        ):
            payload = pipeline.resolve_area(area='B16 8UU')

        self.assertEqual(payload['area'], 'B16 8UU')
        self.assertEqual(post.call_count, 1)
        # The first probe failed, then one pause per failed probe: the call is
        # made once, after the engine answered.
        self.assertEqual(sleep.call_count, 2)

    def test_the_run_start_waits_too(self):
        probes = [_response(502), _response(200)]
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(pipeline, 'HOST_OUTPUTS_DIR', Path(tmp)),
            mock.patch.object(pipeline.requests, 'get', side_effect=probes),
            mock.patch.object(
                pipeline.requests, 'post', return_value=_response(202, {'status': 'queued'})
            ) as post,
            mock.patch.object(pipeline.time, 'sleep'),
        ):
            result = pipeline.run_from_area(area='B16 8UU', project_id='p-1')

        self.assertEqual(result['status'], 'queued')
        self.assertEqual(post.call_count, 1)

    def test_a_probe_that_cannot_connect_counts_as_a_boot(self):
        probes = [requests.ConnectionError('refused'), _response(200)]
        with (
            mock.patch.object(pipeline.requests, 'get', side_effect=probes),
            mock.patch.object(
                pipeline.requests, 'post', return_value=_response(200, {'features': []})
            ),
            mock.patch.object(pipeline.time, 'sleep') as sleep,
        ):
            pipeline.get_input_layer(area='B16 8UU', layer='roads')

        self.assertEqual(sleep.call_count, 1)

    def test_a_health_endpoint_that_answers_badly_is_not_awake(self):
        probes = [_response(500), _response(200)]
        with (
            mock.patch.object(pipeline.requests, 'get', side_effect=probes),
            mock.patch.object(pipeline.requests, 'post', return_value=_response(200)),
            mock.patch.object(pipeline.time, 'sleep') as sleep,
        ):
            pipeline.resolve_area(area='B16 8UU')

        self.assertEqual(sleep.call_count, 1)


class BudgetTests(AwakeTestCase):
    def test_a_budget_that_runs_out_says_the_engine_is_starting_not_unreachable(self):
        with (
            mock.patch.object(pipeline.requests, 'get', return_value=_response(502)),
            mock.patch.object(pipeline.requests, 'post') as post,
            mock.patch.object(pipeline.time, 'sleep'),
            self.assertRaises(pipeline.EngineError) as caught,
        ):
            pipeline.resolve_area(area='B16 8UU')

        self.assertEqual(caught.exception.status_code, 503)
        detail = str(caught.exception)
        self.assertIn('still starting up', detail)
        self.assertIn('try again', detail)
        self.assertNotIn('unreachable', detail)
        # Nothing was sent to an engine that was not up: the call is not made
        # and then reported as failed.
        post.assert_not_called()

    def test_the_budget_is_the_configured_one(self):
        with (
            mock.patch.dict(os.environ, {'FTTH_ENGINE_WAKE': '30'}),
            mock.patch.object(pipeline.requests, 'get', return_value=_response(502)),
            mock.patch.object(pipeline.time, 'sleep') as sleep,
            self.assertRaises(pipeline.EngineError) as caught,
        ):
            pipeline.resolve_area(area='B16 8UU')

        self.assertIn('30 seconds', str(caught.exception))
        self.assertLessEqual(sum(call.args[0] for call in sleep.call_args_list), 30)

    def test_a_zero_budget_skips_the_wait_entirely(self):
        with (
            mock.patch.dict(os.environ, {'FTTH_ENGINE_WAKE': '0'}),
            mock.patch.object(pipeline.requests, 'get') as get,
            mock.patch.object(
                pipeline.requests, 'post', return_value=_response(200, {'area': 'ok'})
            ) as post,
        ):
            pipeline.resolve_area(area='B16 8UU')

        get.assert_not_called()
        self.assertEqual(post.call_count, 1)


class TrustedWindowTests(AwakeTestCase):
    def test_one_probe_covers_a_page_that_makes_several_calls(self):
        with (
            mock.patch.object(pipeline.requests, 'get', return_value=_response(200)) as get,
            mock.patch.object(
                pipeline.requests, 'post', return_value=_response(200, {'features': []})
            ),
        ):
            pipeline.resolve_area(area='B16 8UU')
            pipeline.get_input_layer(area='B16 8UU', layer='roads')
            pipeline.get_input_layer(area='B16 8UU', layer='premises')

        self.assertEqual(get.call_count, 1)

    def test_the_window_expires(self):
        clock = {'now': 100.0}
        with (
            mock.patch.object(pipeline.requests, 'get', return_value=_response(200)) as get,
            mock.patch.object(pipeline.time, 'monotonic', side_effect=lambda: clock['now']),
            mock.patch.object(
                pipeline.requests, 'post', return_value=_response(200, {'area': 'ok'})
            ),
        ):
            pipeline.resolve_area(area='B16 8UU')
            clock['now'] += pipeline.ENGINE_READY_S + 1
            pipeline.resolve_area(area='B16 8UU')

        self.assertEqual(get.call_count, 2)
