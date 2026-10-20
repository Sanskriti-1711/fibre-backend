"""What the gateway says when the engine's download outlives its patience.

Why this exists: the area preview waited 40 minutes, and when that expired the
planner got ``Engine unreachable: HTTPSConnectionPool(...): Read timed out.
(read timeout=2400)`` — which names neither the cause nor the next step, and
held a gunicorn thread for three quarters of an hour while the engine's own
download kept running.  The wait is now 20 minutes and the reply says the
download continues and that a reload will be served from the extract cache.

These tests are pure: ``requests`` is mocked, so what is asserted is the status
code and the words, not a network.
"""

from __future__ import annotations

import os
from unittest import mock

import requests
from django.test import SimpleTestCase

from ftth_hld import pipeline


def _no_timeout_env():
    """The defaults only mean anything with the override unset."""
    env = {k: v for k, v in os.environ.items() if not k.startswith('FTTH_')}
    return mock.patch.dict(os.environ, env, clear=True)


class StillDownloadingReplyTests(SimpleTestCase):
    def test_a_preview_that_times_out_says_the_download_continues(self):
        with (
            _no_timeout_env(),
            mock.patch.object(
                pipeline.requests, 'post', side_effect=requests.Timeout('Read timed out.')
            ),
            self.assertRaises(pipeline.EngineError) as caught,
        ):
            pipeline.resolve_area(area='B16 9BH, Birmingham, United Kingdom')

        self.assertEqual(caught.exception.status_code, 504)
        detail = str(caught.exception)
        self.assertIn('still downloading', detail)
        self.assertIn('reload', detail)
        self.assertIn('cache', detail)
        # The raw transport message must not be what the planner reads.
        self.assertNotIn('Read timed out', detail)
        self.assertNotIn('Engine unreachable', detail)

    def test_an_input_layer_that_times_out_says_the_same_thing(self):
        with (
            _no_timeout_env(),
            mock.patch.object(pipeline.requests, 'post', side_effect=requests.Timeout('boom')),
            self.assertRaises(pipeline.EngineError) as caught,
        ):
            pipeline.get_input_layer(area='Berlin', layer='roads')

        self.assertEqual(caught.exception.status_code, 504)
        self.assertIn('roads layer', str(caught.exception))
        self.assertIn('still downloading', str(caught.exception))

    def test_a_transport_failure_that_is_not_a_timeout_is_still_unreachable(self):
        with (
            _no_timeout_env(),
            mock.patch.object(
                pipeline.requests, 'post', side_effect=requests.ConnectionError('refused')
            ),
            self.assertRaises(pipeline.EngineError) as caught,
        ):
            pipeline.resolve_area(area='Berlin')

        self.assertEqual(caught.exception.status_code, 502)
        self.assertIn('Engine unreachable', str(caught.exception))


class WaitBudgetTests(SimpleTestCase):
    """40 minutes held a worker for three quarters of an hour on a dead download."""

    def _timeout_seen(self, call):
        with (
            _no_timeout_env(),
            mock.patch.object(
                pipeline.requests,
                'post',
                return_value=mock.Mock(status_code=200, json=lambda: {'area': 'ok'}),
            ) as post,
        ):
            call()
        return post.call_args.kwargs['timeout']

    def test_the_preview_waits_twenty_minutes_by_default(self):
        self.assertEqual(self._timeout_seen(lambda: pipeline.resolve_area(area='B16 9BH')), 1200)

    def test_the_input_layer_waits_the_same(self):
        self.assertEqual(
            self._timeout_seen(lambda: pipeline.get_input_layer(area='B16 9BH', layer='roads')),
            1200,
        )

    def test_the_budget_can_still_be_raised_by_environment(self):
        with (
            mock.patch.dict(os.environ, {'FTTH_RESOLVE_TIMEOUT': '1800'}),
            mock.patch.object(
                pipeline.requests,
                'post',
                return_value=mock.Mock(status_code=200, json=lambda: {'area': 'ok'}),
            ) as post,
        ):
            pipeline.resolve_area(area='B16 9BH')
        self.assertEqual(post.call_args.kwargs['timeout'], 1800)
