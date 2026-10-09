"""The HTTP portal adapter is a client, not a stub — pin that down.

``HttpPortalAdapter`` was documented as a "placeholder ... returns None until a
base URL + token are configured", which read as "unimplemented". It is in fact
a complete client: it queries ``{PERMITS_PORTAL_URL}/status?reference=...``,
authenticates, maps the remote status onto our state machine and refuses any
transition the state machine does not allow. What it lacks is a configured
portal, not code.

These tests drive it against a mocked portal so the behaviour is proven without
a real authority endpoint, and they pin the two properties that matter
operationally: an unknown/illegal answer is ignored rather than applied, and a
portal failure is swallowed (the poll loop must survive portal downtime).
"""

from __future__ import annotations

import json
import os
from unittest import mock

from django.test import SimpleTestCase

from permits.models import PermitSubmission
from permits.submissions import ALLOWED_TRANSITIONS
from permits.sync import (
    ADAPTERS,
    HttpPortalAdapter,
    enabled_adapters,
)

PORTAL_ENV = {'PERMITS_PORTAL_URL': 'https://portal.example.test/api'}


class _Resp:
    def __init__(self, status_code=200, payload=None, text=''):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = {'content-type': 'application/json'}

    def json(self):
        if self._payload is None:
            raise ValueError('no json')
        return self._payload


def _submission(status, reference='REF-1'):
    """An unsaved PermitSubmission: the adapter only reads status/reference."""
    return PermitSubmission(status=status, reference=reference)


class StatusMapTests(SimpleTestCase):
    def test_every_mapped_status_is_a_real_submission_status(self):
        valid = {
            PermitSubmission.STATUS_SUBMITTED,
            PermitSubmission.STATUS_UNDER_REVIEW,
            PermitSubmission.STATUS_APPROVED,
            PermitSubmission.STATUS_REJECTED,
            PermitSubmission.STATUS_CLOSED,
        }
        for remote, mapped in HttpPortalAdapter.STATUS_MAP.items():
            with self.subTest(remote=remote):
                self.assertIn(mapped, valid)

    def test_the_synonyms_the_portals_actually_use_are_mapped(self):
        for remote, expected in (
            ('acknowledged', PermitSubmission.STATUS_UNDER_REVIEW),
            ('in_progress', PermitSubmission.STATUS_UNDER_REVIEW),
            ('granted', PermitSubmission.STATUS_APPROVED),
            ('denied', PermitSubmission.STATUS_REJECTED),
            ('completed', PermitSubmission.STATUS_CLOSED),
        ):
            with self.subTest(remote=remote):
                self.assertEqual(HttpPortalAdapter.STATUS_MAP.get(remote), expected)

    def test_the_adapter_is_registered(self):
        self.assertIsInstance(ADAPTERS['http_portal'], HttpPortalAdapter)


class EnablementTests(SimpleTestCase):
    def test_disabled_without_a_url(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(HttpPortalAdapter().is_enabled())
            self.assertNotIn('http_portal', [a.name for a in enabled_adapters()])

    def test_enabled_once_a_url_is_set(self):
        with mock.patch.dict(os.environ, PORTAL_ENV, clear=True):
            self.assertTrue(HttpPortalAdapter().is_enabled())
            self.assertIn('http_portal', [a.name for a in enabled_adapters()])

    def test_poll_returns_none_without_a_url(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(
                HttpPortalAdapter().poll(_submission(PermitSubmission.STATUS_UNDER_REVIEW))
            )

    def test_poll_returns_none_without_a_reference(self):
        with mock.patch.dict(os.environ, PORTAL_ENV, clear=True):
            self.assertIsNone(
                HttpPortalAdapter().poll(
                    _submission(PermitSubmission.STATUS_UNDER_REVIEW, reference='')
                )
            )


class PollTests(SimpleTestCase):
    def _poll(self, status, payload, response=None, env=None):
        env = dict(PORTAL_ENV if env is None else env)
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch('requests.get', return_value=response) as get:
                result = HttpPortalAdapter().poll(_submission(status))
        return result, get

    def test_a_legal_remote_status_becomes_a_sync_result(self):
        response = _Resp(
            payload={
                'status': 'approved',
                'reference': 'AUTH-9',
                'notes': 'granted with conditions',
                'conditions': 'reinstate footway',
                'expiry_date': '2027-01-01',
            }
        )
        result, get = self._poll(PermitSubmission.STATUS_UNDER_REVIEW, None, response=response)

        self.assertIsNotNone(result)
        self.assertEqual(result.to_status, PermitSubmission.STATUS_APPROVED)
        self.assertEqual(result.reference, 'AUTH-9')
        self.assertEqual(result.conditions, 'reinstate footway')
        self.assertEqual(result.expiry_date, '2027-01-01')
        self.assertEqual(result.detail['remote_status'], 'approved')

    def test_the_status_is_read_from_either_key(self):
        for payload_key in ('status', 'state'):
            with self.subTest(key=payload_key):
                result, _ = self._poll(
                    PermitSubmission.STATUS_UNDER_REVIEW,
                    None,
                    response=_Resp(payload={payload_key: 'rejected'}),
                )
                self.assertEqual(result.to_status, PermitSubmission.STATUS_REJECTED)

    def test_query_and_auth_headers(self):
        result, get = self._poll(
            PermitSubmission.STATUS_UNDER_REVIEW,
            None,
            response=_Resp(payload={'status': 'approved'}),
            env={
                **PORTAL_ENV,
                'PERMITS_PORTAL_TOKEN': 'bearer-1',
                'PERMITS_PORTAL_API_KEY': 'key-1',
            },
        )
        self.assertIsNotNone(result)
        args, kwargs = get.call_args
        self.assertEqual(args[0], 'https://portal.example.test/api/status')
        self.assertEqual(kwargs['params'], {'reference': 'REF-1'})
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer bearer-1')
        self.assertEqual(kwargs['headers']['X-API-Key'], 'key-1')

    def test_a_trailing_slash_on_the_base_is_not_doubled(self):
        _, get = self._poll(
            PermitSubmission.STATUS_UNDER_REVIEW,
            None,
            response=_Resp(payload={'status': 'approved'}),
            env={'PERMITS_PORTAL_URL': 'https://portal.example.test/api/'},
        )
        self.assertEqual(get.call_args[0][0], 'https://portal.example.test/api/status')

    def test_an_illegal_transition_is_ignored(self):
        """submitted -> approved is not an edge: the portal cannot skip review."""
        allowed = ALLOWED_TRANSITIONS[PermitSubmission.STATUS_SUBMITTED]
        self.assertNotIn(PermitSubmission.STATUS_APPROVED, allowed)

        result, _ = self._poll(
            PermitSubmission.STATUS_SUBMITTED, None, response=_Resp(payload={'status': 'approved'})
        )
        self.assertIsNone(result)

    def test_an_unknown_remote_status_is_ignored(self):
        result, _ = self._poll(
            PermitSubmission.STATUS_UNDER_REVIEW,
            None,
            response=_Resp(payload={'status': 'escalated_to_the_senate'}),
        )
        self.assertIsNone(result)

    def test_a_missing_status_is_ignored(self):
        result, _ = self._poll(
            PermitSubmission.STATUS_UNDER_REVIEW,
            None,
            response=_Resp(payload={'reference': 'AUTH-9'}),
        )
        self.assertIsNone(result)

    def test_a_non_2xx_is_ignored_and_does_not_raise(self):
        result, _ = self._poll(
            PermitSubmission.STATUS_UNDER_REVIEW,
            None,
            response=_Resp(status_code=503, payload=None, text='upstream down'),
        )
        self.assertIsNone(result)

    def test_a_network_failure_is_swallowed(self):
        with mock.patch.dict(os.environ, PORTAL_ENV, clear=True):
            with mock.patch('requests.get', side_effect=OSError('no route to host')):
                result = HttpPortalAdapter().poll(_submission(PermitSubmission.STATUS_UNDER_REVIEW))
        self.assertIsNone(result)

    def test_non_json_content_type_is_treated_as_no_opinion(self):
        response = _Resp(payload={'status': 'approved'})
        response.headers = {'content-type': 'text/html'}
        result, _ = self._poll(PermitSubmission.STATUS_UNDER_REVIEW, None, response=response)
        self.assertIsNone(result)

    def test_a_closed_submission_is_never_reopened_by_the_portal(self):
        self.assertEqual(ALLOWED_TRANSITIONS[PermitSubmission.STATUS_CLOSED], frozenset())
        result, _ = self._poll(
            PermitSubmission.STATUS_CLOSED, None, response=_Resp(payload={'status': 'under_review'})
        )
        self.assertIsNone(result)


class ConfigurableStatusMapTests(SimpleTestCase):
    """A portal is chosen later, so its vocabulary must be config, not code."""

    def test_without_config_it_is_the_built_in_map(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(HttpPortalAdapter().status_map(), HttpPortalAdapter.STATUS_MAP)

    def test_a_portal_specific_synonym_can_be_added(self):
        env = {**PORTAL_ENV, 'PERMITS_PORTAL_STATUS_MAP': '{"in Bearbeitung": "under_review"}'}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                HttpPortalAdapter().status_map().get('in bearbeitung'),
                PermitSubmission.STATUS_UNDER_REVIEW,
            )

    def test_config_overrides_a_built_in_synonym(self):
        env = {**PORTAL_ENV, 'PERMITS_PORTAL_STATUS_MAP': '{"granted": "closed"}'}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                HttpPortalAdapter().status_map()['granted'], PermitSubmission.STATUS_CLOSED
            )

    def test_an_entry_targeting_an_unknown_status_is_ignored(self):
        env = {**PORTAL_ENV, 'PERMITS_PORTAL_STATUS_MAP': '{"weird": "not_a_status"}'}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertNotIn('weird', HttpPortalAdapter().status_map())

    def test_malformed_config_is_ignored_not_raised(self):
        for raw in ('{not json', '[]', '{"a": "b"}'):
            with self.subTest(raw=raw):
                env = {**PORTAL_ENV, 'PERMITS_PORTAL_STATUS_MAP': raw}
                with mock.patch.dict(os.environ, env, clear=True):
                    self.assertEqual(HttpPortalAdapter().status_map(), HttpPortalAdapter.STATUS_MAP)

    def test_a_configured_status_drives_the_poll(self):
        env = {**PORTAL_ENV, 'PERMITS_PORTAL_STATUS_MAP': '{"freigegeben": "approved"}'}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch('requests.get', return_value=_Resp(payload={'status': 'Freigegeben'})):
                result = HttpPortalAdapter().poll(_submission(PermitSubmission.STATUS_UNDER_REVIEW))
        self.assertEqual(result.to_status, PermitSubmission.STATUS_APPROVED)


class JsonPayloadRoundTripTests(SimpleTestCase):
    """The portal is conventionally JSON; make sure a real-ish body parses."""

    def test_a_realistic_body_round_trips(self):
        body = json.dumps(
            {
                'reference': 'AUTH-2026-0042',
                'status': 'GRANTED',
                'notes': 'Approved subject to conditions',
                'expiry': '2027-03-31',
            }
        )
        response = _Resp(payload=json.loads(body))
        with mock.patch.dict(os.environ, PORTAL_ENV, clear=True):
            with mock.patch('requests.get', return_value=response):
                result = HttpPortalAdapter().poll(_submission(PermitSubmission.STATUS_UNDER_REVIEW))
        self.assertEqual(result.to_status, PermitSubmission.STATUS_APPROVED)
        self.assertEqual(result.reference, 'AUTH-2026-0042')
        self.assertEqual(result.expiry_date, '2027-03-31')
