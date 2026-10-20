#!/usr/bin/env python3
"""Tests for tools/check_api_expiries.py.

Run from the repository root:

    python -m unittest discover -s tools -p 'test_*.py' -v

The module under test is loaded by path, so the tests do not depend on
``tools`` being a package.
"""

from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import sys
import unittest
from contextlib import redirect_stdout
from datetime import date
from typing import Any

MODULE_PATH = pathlib.Path(__file__).with_name('check_api_expiries.py')
_spec = importlib.util.spec_from_file_location('check_api_expiries', MODULE_PATH)
assert _spec and _spec.loader
exp = importlib.util.module_from_spec(_spec)
# @dataclass looks the defining module up in sys.modules, so it has to be
# registered before the module is executed.
sys.modules['check_api_expiries'] = exp
_spec.loader.exec_module(exp)

TODAY = date(2026, 10, 20)


# The module under test is loaded by path, so mypy cannot see it as anything
# more than a Module: its names are annotated `Any` in this file on purpose.
def fake_credential(**overrides) -> Any:
    base = {
        'id': 'probe_me',
        'name': 'Probe me',
        'kind': 'external api',
        'env': ('PROBE_ME_TOKEN',),
        'powers': 'nothing',
        'expiry': 'provider',
        'basis': 'the provider says',
        'renewal': 'ask for a new one',
        'breaks': 'a call fails',
    }
    base.update(overrides)
    return exp.Credential(**base)


class GradingTests(unittest.TestCase):
    def test_expiry_far_away_passes(self):
        cred = fake_credential(probe=lambda _env: exp.Probe('ok', 'ok', '2026-11-05'))
        row = exp.grade(cred, {'PROBE_ME_TOKEN': 'x'}, {}, TODAY, 14)
        self.assertEqual(row.status, exp.PASS)
        self.assertEqual(row.days_left, 16)
        self.assertEqual(row.expires_at, '2026-11-05')

    def test_expiry_inside_the_window_warns(self):
        cred = fake_credential(probe=lambda _env: exp.Probe('ok', 'ok', '2026-10-28'))
        row = exp.grade(cred, {'PROBE_ME_TOKEN': 'x'}, {}, TODAY, 14)
        self.assertEqual(row.status, exp.WARN)
        self.assertEqual(row.days_left, 8)

    def test_expired_fails(self):
        cred = fake_credential(probe=lambda _env: exp.Probe('ok', 'ok', '2026-10-01'))
        row = exp.grade(cred, {'PROBE_ME_TOKEN': 'x'}, {}, TODAY, 14)
        self.assertEqual(row.status, exp.FAIL)
        self.assertEqual(row.days_left, -19)

    def test_revoked_credential_fails_even_without_a_date(self):
        cred = fake_credential(probe=lambda _env: exp.Probe('denied', 'HTTP 401'))
        row = exp.grade(cred, {'PROBE_ME_TOKEN': 'x'}, {}, TODAY, 14)
        self.assertEqual(row.status, exp.FAIL)
        self.assertIn('revoked', row.detail)

    def test_provider_reporting_no_expiry_passes(self):
        cred = fake_credential(probe=lambda _env: exp.Probe('ok', 'no expiry set', None))
        row = exp.grade(cred, {'PROBE_ME_TOKEN': 'x'}, {}, TODAY, 14)
        self.assertEqual(row.status, exp.PASS)
        self.assertIn('No expiry date', row.detail)

    def test_recorded_date_is_used_when_the_provider_cannot_be_reached(self):
        cred = fake_credential(probe=lambda _env: exp.Probe('unreachable', 'no route'))
        known = {'probe_me': {'expires_at': '2026-10-25', 'verified_on': '2026-10-18'}}
        row = exp.grade(cred, {'PROBE_ME_TOKEN': 'x'}, known, TODAY, 14)
        self.assertEqual(row.status, exp.WARN)
        self.assertEqual(row.days_left, 5)
        self.assertIn('recorded date', row.detail)

    def test_env_override_wins_over_the_recorded_date(self):
        cred = fake_credential(probe=lambda _env: exp.Probe('unreachable', 'no route'))
        known = {'probe_me': {'expires_at': '2026-10-25', 'verified_on': '2026-10-18'}}
        env = {'PROBE_ME_TOKEN': 'x', 'FTTH_EXPIRY_PROBE_ME_EXPIRES': '2026-12-01'}
        row = exp.grade(cred, env, known, TODAY, 14)
        self.assertEqual(row.expires_at, '2026-12-01')
        self.assertEqual(row.status, exp.PASS)

    def test_the_earliest_known_date_wins(self):
        cred = fake_credential(probe=lambda _env: exp.Probe('ok', 'ok', '2026-12-01'))
        known = {'probe_me': {'expires_at': '2026-10-25', 'verified_on': '2026-10-18'}}
        env = {'PROBE_ME_TOKEN': 'x', 'FTTH_EXPIRY_PROBE_ME_EXPIRES': '2026-10-25'}
        row = exp.grade(cred, env, known, TODAY, 14)
        self.assertEqual(row.expires_at, '2026-10-25')

    def test_unknown_expiry_of_a_configured_credential_warns(self):
        cred = fake_credential(expiry='unknown')
        row = exp.grade(cred, {'PROBE_ME_TOKEN': 'x'}, {}, TODAY, 14)
        self.assertEqual(row.status, exp.WARN)
        self.assertIn('unknown', row.detail)

    def test_unknown_expiry_of_an_unconfigured_credential_is_informational(self):
        cred = fake_credential(expiry='unknown')
        row = exp.grade(cred, {}, {}, TODAY, 14)
        self.assertEqual(row.status, exp.INFO)

    def test_never_expiring_credential_passes(self):
        cred = fake_credential(expiry='never')
        row = exp.grade(cred, {'PROBE_ME_TOKEN': 'x'}, {}, TODAY, 14)
        self.assertEqual(row.status, exp.PASS)

    def test_a_never_expiring_credential_that_was_revoked_fails(self):
        cred = fake_credential(expiry='never', probe=lambda _env: exp.Probe('denied', 'revoked'))
        row = exp.grade(cred, {'PROBE_ME_TOKEN': 'x'}, {}, TODAY, 14)
        self.assertEqual(row.status, exp.FAIL)


class RegistryTests(unittest.TestCase):
    def test_every_credential_is_fully_described(self):
        seen: set[str] = set()
        for cred in exp.CREDENTIALS:
            self.assertNotIn(cred.id, seen, f'duplicate id {cred.id}')
            seen.add(cred.id)
            for field in ('name', 'kind', 'powers', 'basis', 'renewal', 'breaks'):
                self.assertTrue(getattr(cred, field).strip(), f'{cred.id} has an empty {field}')
            self.assertIn(cred.expiry, {'provider', 'never', 'by-design', 'quota', 'unknown'})

    def test_the_registry_covers_the_credentials_the_stack_uses(self):
        ids = {c.id for c in exp.CREDENTIALS}
        for expected in (
            'gitlab_token',
            'epc_api',
            'permits_llm',
            'gemini_api',
            'render_api',
            'database_password',
            'smtp_app_password',
            'jwt_access',
            'jwt_refresh',
        ):
            self.assertIn(expected, ids)

    def test_the_recorded_gitlab_expiry_matches_the_provider(self):
        known = exp.load_known()
        self.assertIn('gitlab_token', known)
        self.assertEqual(known['gitlab_token']['expires_at'], '2026-11-05')


class OutputTests(unittest.TestCase):
    def _run_main(self, argv, env=None, credentials=None):
        import os
        import sys

        old_argv = sys.argv
        old_env = dict(os.environ)
        old_credentials = exp.CREDENTIALS
        sys.argv = ['check_api_expiries.py', *argv]
        os.environ.clear()
        os.environ.update(env or {})
        if credentials is not None:
            # Through the module dict: mypy only sees `exp` as a Module, so
            # `exp.CREDENTIALS = ...` is reported as an unknown attribute.
            exp.__dict__['CREDENTIALS'] = credentials
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                code = exp.main()
        finally:
            sys.argv = old_argv
            os.environ.clear()
            os.environ.update(old_env)
            exp.__dict__['CREDENTIALS'] = old_credentials
        return code, buf.getvalue()

    def test_the_table_never_prints_a_secret(self):
        secret = 'sk-super-secret-value-1234567890'
        cred = fake_credential(expiry='never', probe=lambda _env: exp.Probe('ok', 'accepted', None))
        rows = [exp.grade(cred, {'PROBE_ME_TOKEN': secret}, {}, TODAY, 14)]
        buf = io.StringIO()
        with redirect_stdout(buf):
            exp.report(rows, TODAY, 14)
        text = buf.getvalue()
        self.assertNotIn(secret, text)
        self.assertIn('probe_me', text)

    def test_an_expired_credential_exits_1(self):
        cred = fake_credential(probe=lambda _env: exp.Probe('ok', 'ok', '2026-10-01'))
        code, text = self._run_main([], env={'PROBE_ME_TOKEN': 'x'}, credentials=[cred])
        self.assertEqual(code, 1)
        self.assertIn('EXPIRED', text)

    def test_a_warning_is_tolerated_unless_strict(self):
        cred = fake_credential(probe=lambda _env: exp.Probe('ok', 'ok', '2026-10-28'))
        code, text = self._run_main([], env={'PROBE_ME_TOKEN': 'x'}, credentials=[cred])
        self.assertEqual(code, 0)
        self.assertIn('WARN', text)
        code, _ = self._run_main(['--strict'], env={'PROBE_ME_TOKEN': 'x'}, credentials=[cred])
        self.assertEqual(code, 1)

    def test_nothing_due_exits_0(self):
        cred = fake_credential(probe=lambda _env: exp.Probe('ok', 'ok', '2027-01-01'))
        code, _ = self._run_main([], env={'PROBE_ME_TOKEN': 'x'}, credentials=[cred])
        self.assertEqual(code, 0)

    def test_the_json_output_is_machine_readable(self):
        cred = fake_credential(probe=lambda _env: exp.Probe('ok', 'ok', '2026-11-05'))
        code, text = self._run_main(['--json'], env={'PROBE_ME_TOKEN': 'x'}, credentials=[cred])
        payload = json.loads(text)
        self.assertEqual(code, 0)
        self.assertEqual(payload['failed'], 0)
        self.assertEqual(payload['credentials'][0]['id'], 'probe_me')
        self.assertEqual(payload['credentials'][0]['expires_at'], '2026-11-05')

    def test_the_json_output_carries_no_secret(self):
        secret = 'sk-super-secret-value-1234567890'
        cred = fake_credential(expiry='never')
        _, text = self._run_main(['--json'], env={'PROBE_ME_TOKEN': secret}, credentials=[cred])
        self.assertNotIn(secret, text)


if __name__ == '__main__':
    unittest.main()
