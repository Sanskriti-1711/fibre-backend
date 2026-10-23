"""A sleeping engine is a boot to wait out, not a run failed at 0 %.

The engine is a free Render instance: it stops after 15 minutes without
traffic.  On 2026-10-23 its edge answered the LLD re-plan's POST with 429 while
the service was booting, the request never reached uvicorn, and the run was
settled as ``failed`` at 0 % half a second after it started — while the engine
finished booting two minutes later on its own.

``ftth_lld.engine`` now probes /health first (the probe is what starts a
sleeping service) and retries a submit the edge refuses.  These tests are pure:
``requests`` and the clock are mocked, so what is asserted is how many probes
and submits it takes, which pauses it honours, and that an ambiguous failure is
never retried into a duplicate task.
"""

from __future__ import annotations

import os
from unittest import mock

import requests
from django.test import SimpleTestCase

from ftth_hld import pipeline
from ftth_lld import engine

DATASET = {'type': 'FeatureCollection', 'features': [{'type': 'Feature', 'properties': {}}]}


def _response(status_code, body=None, headers=None):
    resp = mock.Mock(status_code=status_code)
    resp.json.return_value = {'detail': 'Too Many Requests'} if body is None else body
    resp.text = '' if body is not None else 'Too Many Requests'
    resp.headers = headers or {}
    return resp


class FakeClock:
    """A monotonic clock that moves only when something sleeps."""

    def __init__(self, start=1000.0):
        self.now = start
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class EngineWakeTestCase(SimpleTestCase):
    def setUp(self):
        # Module state shared by every request: the probe's trust window.
        pipeline._engine_ready_until = 0.0
        self.addCleanup(setattr, pipeline, '_engine_ready_until', 0.0)
        self.clock = FakeClock()
        for patcher in (
            mock.patch('time.monotonic', self.clock.monotonic),
            mock.patch('time.sleep', self.clock.sleep),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)


class SleepingEngineTests(EngineWakeTestCase):
    def test_a_booting_engine_is_waited_out_before_the_submit(self):
        probes = [_response(502), _response(502), _response(200)]
        with (
            mock.patch.object(pipeline.requests, 'get', side_effect=probes) as get,
            mock.patch.object(
                engine.requests, 'post', return_value=_response(202, {'status': 'queued'})
            ) as post,
        ):
            task = engine.lld_replan('p-1', 'LLD-V01', DATASET)

        self.assertEqual(task['status'], 'queued')
        self.assertEqual(get.call_count, 3)
        # One submit, after the engine answered: the boot is not a failed POST.
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs['json']['lld_version'], 'LLD-V01')

    def test_the_edge_refusing_the_submit_is_retried_after_its_retry_after(self):
        with (
            mock.patch.object(engine, 'ensure_engine_awake'),
            mock.patch.object(
                engine.requests,
                'post',
                side_effect=[
                    _response(429, headers={'Retry-After': '7'}),
                    _response(202, {'status': 'queued'}),
                ],
            ) as post,
        ):
            engine.lld_run('p-1', 'LLD-V01', DATASET)

        self.assertEqual(post.call_count, 2)
        # The wait the edge named is honoured, not the 5 s backoff.
        self.assertIn(7.0, self.clock.sleeps)

    def test_a_health_probe_alone_does_not_skip_the_wake(self):
        """The probe is the wake: a sleeping service answers only once it boots."""
        with (
            mock.patch.object(pipeline.requests, 'get', return_value=_response(200)) as get,
            mock.patch.object(engine.requests, 'post', return_value=_response(202, {})),
        ):
            engine.lld_replan('p-1', 'LLD-V01', DATASET)

        self.assertEqual(get.call_count, 1)


class BudgetTests(EngineWakeTestCase):
    def test_a_persistent_edge_refusal_names_the_startup_and_loses_nothing(self):
        with (
            mock.patch.dict(os.environ, {'FTTH_LLD_SUBMIT_BUDGET': '20'}),
            mock.patch.object(engine, 'ensure_engine_awake'),
            mock.patch.object(engine.requests, 'post', return_value=_response(429)),
            self.assertRaises(RuntimeError) as caught,
        ):
            engine.lld_replan('p-1', 'LLD-V01', DATASET)

        detail = str(caught.exception)
        self.assertIn('did not accept it within 20 s', detail)
        self.assertIn('Nothing was submitted', detail)
        self.assertNotIn('Unknown error', detail)
        # Bounded by the budget, not an unbroken hammer.
        self.assertLessEqual(sum(self.clock.sleeps), 20)

    def test_a_client_error_is_not_retried(self):
        with (
            mock.patch.object(engine, 'ensure_engine_awake'),
            mock.patch.object(
                engine.requests, 'post', return_value=_response(400, {'detail': 'dataset.features'})
            ) as post,
            self.assertRaises(RuntimeError) as caught,
        ):
            engine.lld_replan('p-1', 'LLD-V01', DATASET)

        self.assertEqual(post.call_count, 1)
        self.assertIn('dataset.features', str(caught.exception))

    def test_a_timeout_is_not_retried_into_a_duplicate_task(self):
        """The engine keys a task by version: a re-post queues the design twice."""
        with (
            mock.patch.object(engine, 'ensure_engine_awake'),
            mock.patch.object(
                engine.requests, 'post', side_effect=requests.Timeout('read timed out')
            ) as post,
            self.assertRaises(RuntimeError) as caught,
        ):
            engine.lld_replan('p-1', 'LLD-V01', DATASET)

        self.assertEqual(post.call_count, 1)
        self.assertIn('could not reach the engine', str(caught.exception))
