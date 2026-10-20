#!/usr/bin/env python3
"""Fibre-FTTH API & credential expiry check.

Answers one question per credential: **does it expire, when, and what breaks
when it does.**  Sibling of ``tools/check_stack.py`` -- that one asks "is the
stack alive?", this one asks "how long do the keys that make it work last?".

Why it exists
-------------
Most of the platform's credentials are the kind that do *not* expire, and
nobody can tell the difference by looking at a dashboard: a revoked key and a
key with no expiry date look identical until the first call fails.  So this
script states, per credential, which of the three worlds it is in:

  * it expires on a date the provider publishes  (GitLab access tokens),
  * it never expires but can be revoked          (Gemini, Render, Gmail),
  * it expires by design and is renewed for you (JWT access tokens).

and it fails loudly when the first kind is inside the warning window.

Usage
-----
    python tools/check_api_expiries.py             # human table
    python tools/check_api_expiries.py --json      # machine-readable
    python tools/check_api_expiries.py --quiet     # only problems
    python tools/check_api_expiries.py --strict    # WARN also exits 1
    python tools/check_api_expiries.py --sync-known # refresh the date file

Exit status is 1 when something has expired or been revoked (and, with
``--strict``, when something expires inside the warning window), so it drops
into a scheduler the same way ``check_stack.py`` does.

Secrets are never printed.  The table shows whether a credential is present,
never its value.

Environment
-----------
Everything is read from the environment, so run it with the same ``.env`` the
stack uses.  ``FTTH_EXPIRY_<ID>_EXPIRES`` (e.g. ``FTTH_EXPIRY_GITLAB_TOKEN_EXPIRES``)
overrides a date by hand; ``EXPIRY_CHECK_TIMEOUT`` sets the per-request timeout.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

TIMEOUT = float(os.getenv('EXPIRY_CHECK_TIMEOUT', '30'))
DEFAULT_WARN_DAYS = 14

# Last known expiry facts, committed so a scheduled run WITHOUT the secrets
# (GitHub Actions, a cold box) still knows the deadline.  Refreshed by
# --sync-known from whatever the provider reports.
KNOWN_FILE = Path(__file__).with_name('api_expiry_known.json')

PASS, WARN, FAIL, INFO = 'PASS', 'WARN', 'FAIL', 'INFO'

_SSL = ssl.create_default_context()


@dataclass
class Probe:
    """What a live call to the provider returned."""

    state: str  # 'ok' | 'denied' | 'unreachable' | 'skipped'
    detail: str
    expires_at: str | None = None  # ISO date (YYYY-MM-DD)


@dataclass
class Credential:
    id: str
    name: str
    kind: str  # 'external api' | 'secret' | 'minted token' | 'quota'
    env: tuple[str, ...]
    powers: str
    expiry: str  # 'provider' | 'never' | 'by-design' | 'quota' | 'unknown'
    basis: str
    renewal: str
    breaks: str
    probe: Callable[[dict], Probe] | None = None  # env -> Probe
    lifetime: str = ''  # for 'by-design'
    reset: str = ''  # for 'quota'


# ── HTTP helper ──────────────────────────────────────────────────────────────


def _request(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Return (status_code, body_text).  HTTP errors are values, not raises."""
    req = urllib.request.Request(url, method=method)
    req.add_header('User-Agent', 'fibre-ftth-expiry-check/1.0')
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_SSL) as r:
            return r.status, r.read(20_000).decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read(20_000).decode('utf-8', 'replace')
    except Exception as e:  # noqa: BLE001 - a network failure is a result
        reason = f'{type(e).__name__}: {e}'
        raise ConnectionError(reason) from e


def _json_or_none(text: str) -> dict | None:
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


# ── Probes ───────────────────────────────────────────────────────────────────


def probe_gitlab(env) -> Probe:
    """GitLab publishes the expiry date of an access token.  Ask it.

    ``GET /personal_access_tokens/self`` answers for a personal access token
    with the scopes it carries and the day it dies.  A project access token
    is not visible here (404), which is reported rather than guessed at.
    """
    token = env.get('GITLAB_CI_TOKEN', '')
    if not token:
        return Probe('skipped', 'no GITLAB_CI_TOKEN')
    try:
        code, body = _request(
            'GET',
            'https://gitlab.com/api/v4/personal_access_tokens/self',
            headers={'PRIVATE-TOKEN': token},
        )
    except ConnectionError as e:
        return Probe('unreachable', f'gitlab.com unreachable -- {e}')
    if code == 200:
        d = _json_or_none(body) or {}
        expires = d.get('expires_at')
        name = d.get('name', '?')
        active = d.get('active')
        revoked = d.get('revoked')
        if not active or revoked:
            return Probe('denied', f'token {name!r} is not active (revoked={revoked})', expires)
        scopes = ','.join(d.get('scopes') or [])
        detail = f'PAT {name!r}'
        if scopes:
            detail += f' scopes={scopes}'
        if expires:
            detail += f', expires {expires}'
        else:
            detail += ', no expiry set'
        return Probe('ok', detail, expires)
    if code in (401, 403):
        return Probe('denied', f'GitLab rejected the token (HTTP {code})')
    return Probe(
        'unreachable', f'HTTP {code} -- not a personal access token (project tokens 404 here)'
    )


def probe_render(env) -> Probe:
    """Render keys carry no expiry date; the API only says valid or not."""
    key = env.get('RENDER_API', '')
    if not key:
        return Probe('skipped', 'no RENDER_API')
    try:
        code, body = _request(
            'GET',
            'https://api.render.com/v1/services?limit=1',
            headers={'Authorization': f'Bearer {key}'},
        )
    except ConnectionError as e:
        return Probe('unreachable', f'api.render.com unreachable -- {e}')
    if code == 200:
        return Probe('ok', 'Render API key accepted')
    if code in (401, 403):
        return Probe('denied', f'Render rejected the key (HTTP {code})')
    return Probe('unreachable', f'HTTP {code}')


def probe_gemini(env) -> Probe:
    """Google AI Studio keys do not expire; they stop working when revoked."""
    key = env.get('GEMINI_API_KEY', '')
    if not key:
        return Probe('skipped', 'no GEMINI_API_KEY')
    try:
        code, body = _request(
            'GET',
            f'https://generativelanguage.googleapis.com/v1beta/models?key={key}',
        )
    except ConnectionError as e:
        return Probe('unreachable', f'generativelanguage.googleapis.com unreachable -- {e}')
    if code == 200:
        return Probe('ok', 'Gemini key accepted')
    if code == 400:
        # 400 + "API key not valid" is how a dead key answers here.
        d = _json_or_none(body) or {}
        msg = (d.get('error') or {}).get('message', '')
        if 'not valid' in msg.lower() or 'API_KEY_INVALID' in body:
            return Probe('denied', f'Gemini key rejected -- {msg}')
        return Probe('unreachable', f'HTTP 400 -- {msg or "unexpected body"}')
    if code in (401, 403):
        return Probe('denied', f'Gemini rejected the key (HTTP {code})')
    return Probe('unreachable', f'HTTP {code}')


def probe_llm(env) -> Probe:
    """The Permit Copilot's OpenAI-compatible endpoint.

    Works for a hosted provider and for the local OmniRoute gateway; the
    gateway answers ``/v1/models`` with the key it was given.
    """
    url = env.get('PERMITS_LLM_URL', '')
    key = env.get('PERMITS_LLM_API_KEY', '')
    if not url:
        return Probe('skipped', 'no PERMITS_LLM_URL')
    base = url.split('/chat/completions')[0].rstrip('/')
    models_url = base if base.endswith('/v1') else base + '/v1'
    try:
        code, body = _request(
            'GET',
            f'{models_url}/models',
            headers={'Authorization': f'Bearer {key}'} if key else {},
        )
    except ConnectionError as e:
        return Probe('unreachable', f'{models_url} unreachable -- {e}')
    if code == 200:
        host = base.split('//')[-1].split('/')[0]
        return Probe('ok', f'{host} accepted the key')
    if code in (401, 403):
        return Probe('denied', f'{models_url} rejected the key (HTTP {code})')
    return Probe('unreachable', f'HTTP {code}')


def probe_epc(env) -> Probe:
    """The GOV.UK EPC open-data API.

    A good token with a bad query answers **400** with a JSON search error; a
    bad or missing token answers **403 "Bad authentication header"**.  Only
    the second means the credential is dead, so the 400 has to be read
    carefully rather than treated as a failure.
    """
    token = env.get('EPC_API_TOKEN', '')
    if not token:
        return Probe('skipped', 'no EPC_API_TOKEN')
    url = (
        'https://api.get-energy-performance-data.communities.gov.uk' '/api/domestic/search?count=1'
    )
    try:
        code, body = _request('GET', url, headers={'Authorization': f'Bearer {token}'})
    except ConnectionError as e:
        return Probe('unreachable', f'EPC API unreachable -- {e}')
    if code == 403:
        return Probe('denied', 'EPC API rejected the token (403)')
    if code == 200:
        return Probe('ok', 'EPC API accepted the token')
    if code == 400 and 'search query was invalid' in body:
        # Authentication passed; the query was not acceptable.  The token works.
        return Probe('ok', 'EPC API accepted the token (query rejected, auth passed)')
    return Probe('unreachable', f'HTTP {code}')


# ── The registry ─────────────────────────────────────────────────────────────


CREDENTIALS: list[Credential] = [
    Credential(
        id='gitlab_token',
        name='GitLab access token',
        kind='external api',
        env=('GITLAB_CI_TOKEN',),
        powers='GitLab API calls from this workspace (repo admin, releases). The '
        'fibre-fe Pages pipeline uses the built-in CI_JOB_TOKEN, not this one.',
        expiry='provider',
        basis='GitLab access tokens carry an expiry date; the API reports it '
        '(`/api/v4/personal_access_tokens/self`).',
        renewal='GitLab -> Settings -> Access Tokens -> issue a new one, then '
        'update GITLAB_CI_TOKEN in the root .env and on every host.',
        breaks='Any GitLab API call made with it. Nothing in the deployed app '
        'breaks -- the Pages pipeline keeps working on CI_JOB_TOKEN.',
        probe=probe_gitlab,
    ),
    Credential(
        id='permits_portal',
        name='Permit authority portal credential',
        kind='external api',
        env=('PERMITS_PORTAL_TOKEN', 'PERMITS_PORTAL_API_KEY'),
        powers='Live status sync with an authority portal (permits/notifications).',
        expiry='unknown',
        basis='The portal decides the lifetime; nothing in this repository '
        'knows it. Record the date in FTTH_EXPIRY_PERMITS_PORTAL_EXPIRES.',
        renewal='Ask the authority for a new token, then update the env var on '
        'the backend host.',
        breaks='Permit status sync stops; submissions still work and fall back '
        'to the CSV inbox.',
    ),
    Credential(
        id='epc_api',
        name='GOV.UK EPC open data API token',
        kind='external api',
        env=('EPC_API_TOKEN',),
        powers='Household register (address-level dwelling counts) in the engine.',
        expiry='never',
        basis='The Open Data Communities key is issued at sign-up and shown in '
        'the page footer. No expiry date is published for it; it is revoked '
        'manually by the account owner.',
        renewal='Re-request a key at '
        'https://get-energy-performance-data.communities.gov.uk/ and update the '
        'engine env var.',
        breaks='Household counting falls back to the OSM heuristic (one '
        'household per untagged building).',
        probe=probe_epc,
    ),
    Credential(
        id='permits_llm',
        name='Permit Copilot LLM key',
        kind='external api',
        env=('PERMITS_LLM_API_KEY',),
        powers='Written permit prose in the Copilot (PERMITS_LLM_URL + model).',
        expiry='never',
        basis='OpenAI-compatible provider keys carry no expiry; this one points '
        'at a locally hosted OmniRoute gateway, whose upstream free providers '
        'have their own daily quotas.',
        renewal='Rotate the provider key inside OmniRoute, or point '
        'PERMITS_LLM_URL at a different provider.',
        breaks='Nothing: the Copilot falls back to its deterministic templates.',
        probe=probe_llm,
    ),
    Credential(
        id='gemini_api',
        name='Gemini API key (engine imagery review)',
        kind='external api',
        env=('GEMINI_API_KEY',),
        powers='SURFACE_AI_REVIEW on the engine: the imagery review behind the '
        'results page (SURFACE_AI_PROVIDER=gemini).',
        expiry='never',
        basis='Google AI Studio keys have no expiry date. The free tier is '
        'limited by a daily request quota instead, which resets at midnight '
        'Pacific.',
        renewal='Issue a new key in Google AI Studio and update the engine env '
        'var (SURFACE_AI_* on the Render service).',
        breaks='Imagery review answers `awaiting_imagery_source`; the rest of '
        'the engine is unaffected.',
        probe=probe_gemini,
    ),
    Credential(
        id='render_api',
        name='Render API key',
        kind='external api',
        env=('RENDER_API',),
        powers='Deploys (env vars, deploys) of the engine and the backend.',
        expiry='never',
        basis='Render API keys have no expiry date; they are revoked from the '
        'dashboard. The *free plan* is limited instead: 750 instance-hours per '
        'workspace per calendar month, and a service sleeps after 15 minutes '
        'idle.',
        renewal='Create a new key in the Render dashboard and update the var.',
        breaks='Deploys stop. Already-deployed services keep running.',
        probe=probe_render,
    ),
    Credential(
        id='smtp_app_password',
        name='Gmail app password',
        kind='secret',
        env=('EMAIL_HOST_PASSWORD',),
        powers='Permit notification emails (permits/notifications.py).',
        expiry='never',
        basis='Google app passwords carry no expiry date. They stop working if '
        'the account password or 2-step verification changes.',
        renewal='Revoke and re-create at '
        'https://myaccount.google.com/apppasswords (needs 2-step verification on).',
        breaks='No email is sent. A failure is logged on the notification and '
        'never breaks a submission.',
    ),
    Credential(
        id='database_password',
        name='Shared Postgres password',
        kind='secret',
        env=('PGPASSWORD',),
        powers='Every database read and write (business + GIS schemas).',
        expiry='never',
        basis='Database passwords do not expire. This one was rotated on '
        '2026-10-17 after an older revision published it, and no rotation '
        'cadence is set.',
        renewal='ALTER ROLE ... PASSWORD, then update PG* on the engine, the '
        'backend and the worker, and restart them.',
        breaks='Everything. The engine reports postgis.available: false.',
    ),
    Credential(
        id='django_secret_key',
        name='Django secret key',
        kind='secret',
        env=('DJANGO_SECRET_KEY',),
        powers='Signing of sessions and refresh tokens.',
        expiry='never',
        basis='A random key, not a credential with a lifetime.',
        renewal='Rotate it and expect every session and refresh token to be '
        'invalidated (users sign in again).',
        breaks='Sessions and refresh tokens stop validating.',
    ),
    Credential(
        id='qgis_worker_token',
        name='QGIS worker token',
        kind='secret',
        env=('QGIS_WORKER_TOKEN',),
        powers='Engine -> QGIS worker calls (X-FTTH-Token).',
        expiry='never',
        basis='A shared secret with no lifetime, set on both sides.',
        renewal='Change it on the engine and the worker together and restart ' 'both.',
        breaks='The worker rejects every job call; LLD runs stop at the export ' 'step.',
    ),
    Credential(
        id='expo_token',
        name='Expo access token',
        kind='external api',
        env=('EXPO_TOKEN',),
        powers='EAS phone builds (FTTH_SURVEY_APP/rebuild-phone.sh).',
        expiry='never',
        basis='Expo access tokens do not expire. The EAS *free plan* build '
        'quota is the limit that bites: it resets on the 1st of each month.',
        renewal='Issue a new token with `npx expo whoami`-style login and ' 'update EXPO_TOKEN.',
        breaks='Phone builds fail. The survey app on Expo Web is unaffected.',
    ),
    Credential(
        id='jwt_access',
        name='Backend access token (JWT)',
        kind='minted token',
        env=(),
        powers='Every authenticated API call from the UI.',
        expiry='by-design',
        basis='Minted by the backend with a 12 h lifetime ' '(SIMPLE_JWT ACCESS_TOKEN_LIFETIME).',
        renewal='Automatic: the UI refreshes it with the refresh token.',
        breaks='The user is asked to sign in again.',
        lifetime='12 h, refreshed automatically',
    ),
    Credential(
        id='jwt_refresh',
        name='Backend refresh token (JWT)',
        kind='minted token',
        env=(),
        powers='Renewing the access token without a new password.',
        expiry='by-design',
        basis='Minted by the backend with a 30 day lifetime '
        '(SIMPLE_JWT REFRESH_TOKEN_LIFETIME).',
        renewal='Automatic while it is valid; afterwards the user signs in.',
        breaks='The user is asked to sign in again after 30 days of no refresh.',
        lifetime='30 days, refreshed automatically',
    ),
    Credential(
        id='permit_action_link',
        name='Permit approval link',
        kind='minted token',
        env=('PERMIT_ACTION_TOKEN_MAX_AGE',),
        powers='The Approve/Reject buttons inside permit notification emails.',
        expiry='by-design',
        basis='Signed link with PERMIT_ACTION_TOKEN_MAX_AGE, default 14 days.',
        renewal='Automatic: the next email carries a fresh link.',
        breaks='An old email stops working; the permit is still actioned from ' 'the UI.',
        lifetime='14 days, re-issued with every email',
    ),
    Credential(
        id='permit_validity',
        name='Permit validity dates',
        kind='minted token',
        env=(),
        powers='Nothing -- these are dates the authority granted.',
        expiry='by-design',
        basis='Each permit carries its own expiry_date.',
        renewal='The authority grants an extension; the platform then records ' 'it.',
        breaks='Nothing automatic. Already covered by '
        '`manage.py check_permit_expiries --email`.',
        lifetime='per permit, tracked in the database',
    ),
    Credential(
        id='github_actions_token',
        name='GitHub Actions token',
        kind='minted token',
        env=(),
        powers='The Actions workflows in this repository (issues, deploys).',
        expiry='by-design',
        basis='GITHUB_TOKEN is minted per job and expires with it (at most 24 h).',
        renewal='Automatic.',
        breaks='Nothing: each run mints its own.',
        lifetime='per job (<= 24 h)',
    ),
    Credential(
        id='render_instance_hours',
        name='Render free-plan instance hours',
        kind='quota',
        env=(),
        powers='The engine and backend while they are awake.',
        expiry='quota',
        basis='750 hours per workspace per calendar month, shared by every free '
        'web service. Three services awake 24/7 is ~2160 h, so the allowance '
        'runs out about ten days into the month.',
        renewal='It resets on the 1st of each month. The keep-warm workflow '
        'only warms between 07:00 and 18:00 UTC to stay inside it.',
        breaks='Render suspends every free web service until the next month.',
        reset='resets on the 1st',
    ),
    Credential(
        id='eas_build_quota',
        name='EAS free-plan build quota',
        kind='quota',
        env=('EXPO_TOKEN',),
        powers='Phone builds of the survey app.',
        expiry='quota',
        basis='Android builds are capped per month on the free plan.',
        renewal='It resets monthly; the current allowance is exhausted.',
        breaks='Phone builds fail until the reset.',
        reset='resets monthly',
    ),
    Credential(
        id='github_actions_minutes',
        name='GitHub Actions minutes',
        kind='quota',
        env=(),
        powers='The scheduled monitors (stack monitor, keep-warm).',
        expiry='quota',
        basis='Free private-repo minutes per month; runs on a public '
        'repository do not draw on them.',
        renewal='Resets at the start of each month.',
        breaks='Scheduled workflows stop running, so the stack goes unwatched.',
        reset='resets monthly',
    ),
]


# ── Known-date file ──────────────────────────────────────────────────────────


def load_known() -> dict:
    if not KNOWN_FILE.exists():
        return {}
    try:
        data = json.loads(KNOWN_FILE.read_text(encoding='utf-8'))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def save_known(known: dict) -> None:
    KNOWN_FILE.write_text(json.dumps(known, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def env_override(env: dict, cred_id: str) -> str | None:
    key = 'FTTH_EXPIRY_' + cred_id.upper().replace('-', '_') + '_EXPIRES'
    value = env.get(key, '').strip()
    return value or None


# ── Grading ──────────────────────────────────────────────────────────────────


@dataclass
class Row:
    cred: Credential
    status: str
    detail: str
    expires_at: str | None = None
    days_left: int | None = None
    configured: bool = False


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        # Only the date matters here, so parse a date rather than building a
        # naive datetime and throwing its time away (`date.fromisoformat` takes
        # exactly the same YYYY-MM-DD form `strptime('%Y-%m-%d')` did).
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def grade(
    cred: Credential,
    env: dict,
    known: dict,
    today: date,
    warn_days: int,
    allow_network: bool = True,
) -> Row:
    configured = any(env.get(name) for name in cred.env)
    override = env_override(env, cred.id)
    known_entry = known.get(cred.id) or {}
    known_date = known_entry.get('expires_at') if isinstance(known_entry, dict) else None

    if cred.expiry == 'quota':
        return Row(cred, INFO, f'{cred.reset} -- {cred.basis}', configured=configured)

    if cred.expiry == 'by-design':
        return Row(cred, PASS, cred.lifetime or cred.basis, configured=configured)

    if cred.expiry == 'never':
        detail = cred.basis
        if configured and cred.probe and allow_network:
            probe = cred.probe(env)
            if probe.state == 'denied':
                return Row(
                    cred, FAIL, f'{probe.detail} -- no expiry, but it is revoked', configured=True
                )
            if probe.state == 'ok':
                detail = f'{probe.detail}. {cred.basis}'
            else:
                detail = f'not checked live ({probe.detail}). {cred.basis}'
        elif not configured:
            detail = f'not configured here. {cred.basis}'
        return Row(cred, PASS, detail, configured=configured)

    # 'provider' and 'unknown' both end up comparing against a date.
    dates: list[tuple[str, date]] = []
    detail = cred.basis
    probe_note = ''
    if override:
        parsed = _parse_date(override)
        if parsed:
            dates.append(('override', parsed))
    if cred.expiry == 'provider' and configured and cred.probe and allow_network:
        probe = cred.probe(env)
        if probe.state == 'denied':
            return Row(
                cred, FAIL, f'{probe.detail} (no expiry date -- it was revoked)', configured=True
            )
        if probe.state == 'ok':
            detail = probe.detail
            parsed = _parse_date(probe.expires_at)
            if parsed:
                dates.append(('provider', parsed))
            else:
                # The provider answered and says the key does not expire.
                return Row(cred, PASS, f'{detail}. No expiry date', configured=True)
        else:
            detail = f'live check failed: {probe.detail}'
            probe_note = detail

    if not dates and known_date:
        parsed = _parse_date(known_date)
        if parsed:
            dates.append(('known', parsed))

    if not dates:
        if cred.expiry == 'unknown':
            if configured:
                return Row(
                    cred,
                    WARN,
                    'configured, but its expiry date is unknown -- record it in '
                    'FTTH_EXPIRY_PERMITS_PORTAL_EXPIRES to be warned before it lapses',
                    configured=True,
                )
            return Row(cred, INFO, f'not configured here. {cred.basis}', configured=False)
        if not configured:
            return Row(cred, INFO, f'not configured here. {cred.basis}', configured=False)
        if not allow_network:
            return Row(
                cred,
                INFO,
                'network check skipped and no expiry date has been recorded',
                configured=True,
            )
        return Row(cred, INFO, f'could not determine the expiry date. {detail}', configured=True)

    # Conservative: the earliest date we know of is the one that counts.
    source, when = min(dates, key=lambda item: item[1])
    days_left = (when - today).days
    stamp = when.isoformat()
    if source == 'known':
        verified = known_entry.get('verified_on') if isinstance(known_entry, dict) else None
        label = f' (recorded date, last verified {verified or "unknown"})'
    elif source == 'override':
        label = ' (hand-set override)'
    else:
        label = ''
    if probe_note:
        label += f' [{probe_note}]'
    if days_left < 0:
        return Row(
            cred,
            FAIL,
            f'EXPIRED on {stamp} ({-days_left} days ago){label}',
            stamp,
            days_left,
            configured,
        )
    if days_left <= warn_days:
        return Row(
            cred,
            WARN,
            f'expires {stamp} -- {days_left} day(s) left{label}',
            stamp,
            days_left,
            configured,
        )
    return Row(
        cred, PASS, f'expires {stamp} -- {days_left} days left{label}', stamp, days_left, configured
    )


# ── Reporting ────────────────────────────────────────────────────────────────


def report(rows: list[Row], today: date, warn_days: int, quiet: bool = False) -> int:
    stamp = datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%SZ')
    print(f'Fibre-FTTH credential expiry check -- {stamp}')
    print(f'  warning window: {warn_days} days')
    print('-' * 100)
    print(f'{"STATUS":5} {"CREDENTIAL":24} {"EXPIRES":12} {"DAYS":>5}  DETAIL')
    for r in rows:
        if quiet and r.status in (PASS, INFO):
            continue
        days = '' if r.days_left is None else str(r.days_left)
        print(f'{r.status:5} {r.cred.id:24} {(r.expires_at or "-"):12} {days:>5}  {r.detail}')
    print('-' * 100)
    failed = [r for r in rows if r.status == FAIL]
    warned = [r for r in rows if r.status == WARN]
    info = [r for r in rows if r.status == INFO]
    if failed:
        print(f'{len(failed)} expired or revoked, {len(warned)} expiring soon')
    elif warned:
        print(f'{len(warned)} expiring within {warn_days} days')
    else:
        print(f'Nothing expires within {warn_days} days')
    if info:
        print(
            f'{len(info)} entry/entries carry no expiry date (quotas and unconfigured credentials)'
        )
    return len(failed)


def main() -> int:
    ap = argparse.ArgumentParser(description='Fibre-FTTH API & credential expiry check')
    ap.add_argument('--json', action='store_true', help='machine-readable output')
    ap.add_argument('--quiet', action='store_true', help='only print problems')
    ap.add_argument(
        '--warn-days',
        type=int,
        default=DEFAULT_WARN_DAYS,
        help=f'days before expiry to warn (default {DEFAULT_WARN_DAYS})',
    )
    ap.add_argument(
        '--strict', action='store_true', help='exit 1 on a warning as well as a failure'
    )
    ap.add_argument(
        '--sync-known',
        action='store_true',
        help='write the expiry dates the providers report into api_expiry_known.json',
    )
    ap.add_argument(
        '--no-network',
        action='store_true',
        help='do not call the providers; use the recorded dates only',
    )
    args = ap.parse_args()

    env = dict(os.environ)
    known = load_known()
    today = datetime.now(UTC).date()

    rows = [
        grade(
            cred,
            env,
            known,
            today,
            args.warn_days,
            allow_network=not args.no_network,
        )
        for cred in CREDENTIALS
    ]

    if args.sync_known:
        changed = False
        for row in rows:
            if row.cred.expiry != 'provider' or not row.expires_at:
                continue
            entry = known.get(row.cred.id) or {}
            if not isinstance(entry, dict):
                entry = {}
            if entry.get('expires_at') != row.expires_at:
                entry['expires_at'] = row.expires_at
                entry['verified_on'] = today.isoformat()
                entry['source'] = 'provider'
                known[row.cred.id] = entry
                changed = True
        if changed:
            save_known(known)
            print(f'updated {KNOWN_FILE.name}', file=sys.stderr)

    failed = [r for r in rows if r.status == FAIL]
    warned = [r for r in rows if r.status == WARN]

    if args.json:
        print(
            json.dumps(
                {
                    'checked_at': datetime.now(UTC).isoformat(),
                    'warn_days': args.warn_days,
                    'credentials': [
                        {
                            'id': r.cred.id,
                            'name': r.cred.name,
                            'kind': r.cred.kind,
                            'status': r.status,
                            'configured': r.configured,
                            'expires_at': r.expires_at,
                            'days_left': r.days_left,
                            'detail': r.detail,
                            'powers': r.cred.powers,
                            'breaks': r.cred.breaks,
                            'renewal': r.cred.renewal,
                        }
                        for r in rows
                    ],
                    'failed': len(failed),
                    'warned': len(warned),
                },
                indent=2,
            )
        )
    else:
        report(rows, today, args.warn_days, quiet=args.quiet)

    if failed:
        return 1
    if warned and args.strict:
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
