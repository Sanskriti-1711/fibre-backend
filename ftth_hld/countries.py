"""ISO 3166-1 country options for the area input, served from local data.

The area form asks for a country explicitly, because guessing one from the
shape of a postcode points Nominatim at the wrong continent (a bare US ``10001``
was being read as a German code).  The dropdown was filled by asking the engine
over HTTP for a list that never changes, which made a static list one network
round trip away: a sleeping engine instance left the dropdown empty and the
page showed "Could not load the country list".

The list lives in ``data/countries.json`` instead — generated once from the
engine's ``countries.py`` (ISO 3166-1 alpha-2 short names, plus ``XK``/Kosovo,
which is not in ISO 3166-1 but is the code OSM and Nominatim use).  Serving it
from here makes the dropdown independent of the engine being awake, booted or
reachable at all.  ISO names change very rarely; when they do, regenerate the
JSON from the engine module and the parity test in ``tests/test_countries.py``
will confirm the new count.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_DATA_FILE = Path(__file__).resolve().with_name('data') / 'countries.json'


@lru_cache(maxsize=1)
def _entries() -> tuple[tuple[str, str], ...]:
    """``((code, name), ...)`` parsed once per process.

    An unreadable data file is a deployment error, not a request error, and the
    module is imported at startup — but it still degrades to an empty list
    rather than taking the whole API down with it.
    """
    try:
        with _DATA_FILE.open(encoding='utf-8') as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return ()
    entries = payload.get('countries') if isinstance(payload, dict) else payload
    out = []
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        code = str(entry.get('code') or '').strip().upper()
        name = str(entry.get('name') or '').strip()
        if code and name:
            out.append((code, name))
    return tuple(out)


def country_options() -> list[dict]:
    """The dropdown's options, ordered by country name (as the engine serves them)."""
    return [{'code': code, 'name': name} for code, name in _entries()]


def country_count() -> int:
    """How many countries the dropdown offers (0 means the data file is missing)."""
    return len(_entries())
