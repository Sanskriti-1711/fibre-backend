"""Local development settings for the Field Supervision stage.

Runs the whole platform on SQLite so a dev machine needs no PostgreSQL, no
Docker and no credentials from the deployed database — and points the GIS
microservice at the local `fiber-import` service instead of the hosted one.

    cd fiber-backend
    python manage.py migrate  --settings=config.dev_settings
    python manage.py runserver 8000 --settings=config.dev_settings

Use `dev/setup.sh` instead of the two commands above: it also seeds a demo
project. `config.settings` remains the deployment configuration; nothing here
is read by a deployed host.
"""

import os

from .settings import *  # noqa: F403, F401

# The import/geo service runs beside Django locally (dev/start.sh starts it on
# :8180). 127.0.0.1 rather than `localhost`: on Windows `localhost` resolves to
# ::1 first, and each call then waits for that to fail before retrying.
os.environ.setdefault('MICROSERVICE_BASE_URL', 'http://127.0.0.1:8180')

DEBUG = True

# A dev box is reached as localhost, 127.0.0.1, or by LAN IP from a phone for
# testing the engineer pages.
ALLOWED_HOSTS = ['*']

# SQLite: one file, no server. `timeout` lets a request wait instead of failing
# with "database is locked" while another write is in flight (runserver is
# threaded, and a GeoPackage import writes hundreds of rows).
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db_dev.sqlite3',  # noqa: F405
        'OPTIONS': {'timeout': 20},
    }
}

# The static frontend is served from :3000 and the engineer pages are opened
# from file:// or any local port during development, so preflight must not
# depend on an exact origin list.
CORS_ALLOW_ALL_ORIGINS = True

# DEBUG is off while base settings import, so this was computed as False.
WHITENOISE_AUTOREFRESH = True

# One port in dev: Django also serves the fiber-fe pages, so the browser only
# needs http://localhost:8000 — pages, /api/ and /admin/ share one origin, the
# way a deployed host is reached through one hostname. WhiteNoise publishes the
# directory at the site root; /api/ and /admin/ still resolve because no file in
# fiber-fe is named like them. Autorefresh above keeps page edits live without a
# restart, and nothing here is read by a deployed host (which serves the
# frontend from GitLab Pages).
WHITENOISE_ROOT = BASE_DIR.parent / 'fiber-fe'  # noqa: F405
WHITENOISE_INDEX_FILE = True

# Permit / review mail prints in the runserver console instead of failing on
# missing SMTP credentials.
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
