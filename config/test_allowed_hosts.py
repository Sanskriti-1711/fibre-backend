"""A production service must never answer for every Host header.

``DJANGO_ALLOWED_HOSTS`` used to default to ``*``, so a host that set nothing —
or that set ``*`` to make things work — had Host-header validation switched off
on a public URL. Each case imports ``config.settings`` in a subprocess, so one
environment cannot leak into the rest of the suite.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _load(**env: str) -> tuple[int, str]:
    """Import config.settings in a fresh interpreter; return (code, output)."""
    full_env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in (
            'DJANGO_ALLOWED_HOSTS',
            'DJANGO_DEBUG',
            'DJANGO_SECRET_KEY',
            'PYTHONPATH',
            'PYTHONHOME',
        )
    }
    full_env.update(env)
    proc = subprocess.run(
        [
            sys.executable,
            '-c',
            'import config.settings as s; print(",".join(s.ALLOWED_HOSTS))',
        ],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode, (proc.stdout or '') + (proc.stderr or '')


class AllowedHostsTests(unittest.TestCase):
    def test_a_wildcard_is_refused_in_production(self):
        code, output = _load(DJANGO_DEBUG='false', DJANGO_ALLOWED_HOSTS='*')
        self.assertNotEqual(code, 0)
        self.assertIn('ImproperlyConfigured', output)
        self.assertIn('DJANGO_ALLOWED_HOSTS', output)

    def test_production_without_the_variable_uses_the_service_host(self):
        code, output = _load(DJANGO_DEBUG='false')
        self.assertEqual(code, 0, output)
        self.assertEqual(output.strip().split(','), ['fiber-backend-wml3.onrender.com'])

    def test_development_defaults_to_local_hosts(self):
        code, output = _load(DJANGO_DEBUG='true')
        self.assertEqual(code, 0, output)
        hosts = output.strip().split(',')
        self.assertNotIn('*', hosts)
        self.assertIn('127.0.0.1', hosts)
        self.assertIn('localhost', hosts)

    def test_explicit_production_hosts_are_kept(self):
        code, output = _load(
            DJANGO_DEBUG='false', DJANGO_ALLOWED_HOSTS='api.example.com,.example.com'
        )
        self.assertEqual(code, 0, output)
        self.assertEqual(output.strip().split(','), ['api.example.com', '.example.com'])

    def test_development_may_still_be_explicit(self):
        code, output = _load(DJANGO_DEBUG='true', DJANGO_ALLOWED_HOSTS='my-dev-box')
        self.assertEqual(code, 0, output)
        self.assertEqual(output.strip(), 'my-dev-box')
