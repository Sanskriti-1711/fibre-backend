"""The import/geo service: base URL and request headers.

Local development runs `fiber-import` on :8180 (`dev/start.sh`). A deployment may
point `MICROSERVICE_BASE_URL` at a hosted copy instead (`supervisor-imports` on
Render, see `fiber-import/README.md`).

A hosted copy is a public URL with no user accounts, so it refuses anonymous
calls when `IMPORT_SERVICE_TOKEN` is set: every request must carry the same value
in `X-Import-Token`. Unset on both sides (the default locally) means the service
accepts anonymous calls, which is what the dev loop wants.
"""

import os

MICROSERVICE_BASE_URL = os.getenv('MICROSERVICE_BASE_URL', 'http://localhost:8180').rstrip('/')

IMPORT_SERVICE_TOKEN = os.getenv('IMPORT_SERVICE_TOKEN', '')


def headers() -> dict[str, str]:
    """Auth header for the import service, or nothing when no token is set."""
    return {'X-Import-Token': IMPORT_SERVICE_TOKEN} if IMPORT_SERVICE_TOKEN else {}
