import os
from datetime import timedelta
from pathlib import Path
from typing import Any

from django.core.exceptions import ImproperlyConfigured

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/6.0/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
# Production must set DJANGO_SECRET_KEY (Zeabur service variable).
SECRET_KEY = os.getenv(
    'DJANGO_SECRET_KEY',
    'django-insecure-%p=ow#x9d^a8!#t%5p-m0#@(29wyu1f258$ae$a77_jz1rppf0',
)

# SECURITY WARNING: don't run with debug turned on in production!
# Default: DEBUG OFF. A host that forgets this variable must come up safe, not
# in debug. Leaving it on 404'd with the URLconf and a traceback (observed on the
# live service, 2026-10-09) and, through ftth_hld.config._default_engine_url(),
# silently pointed the HLD/LLD engine at http://127.0.0.1:8080 in production.
# Local development sets DJANGO_DEBUG=true explicitly -- see `.env`,
# `.env.example` and ../start-servers.sh -- so this default is only reached by a
# host that did not set it.
DEBUG = os.getenv('DJANGO_DEBUG', 'false').lower() == 'true'

# Comma-separated at deploy time. A wildcard is refused once DEBUG is off: it
# switches Host-header validation off entirely, so a service that did set the
# variable was still answering for every name pointed at it (found by the
# 2026-10-22 audit). Local development needs no variable at all, and a host
# that forgets it in production falls back to the one name this app answers on
# instead of to "any name".
_ALLOWED_HOSTS_ENV = os.getenv('DJANGO_ALLOWED_HOSTS', '').strip()
if _ALLOWED_HOSTS_ENV:
    ALLOWED_HOSTS = [h.strip() for h in _ALLOWED_HOSTS_ENV.split(',') if h.strip()]
    if not DEBUG and '*' in ALLOWED_HOSTS:
        raise ImproperlyConfigured(
            'DJANGO_ALLOWED_HOSTS must list the real hostname(s) when DJANGO_DEBUG '
            'is off — "*" turns Host-header validation off.'
        )
else:
    ALLOWED_HOSTS = (
        ['localhost', '127.0.0.1', '[::1]', 'testserver']
        if DEBUG
        else ['fiber-backend-wml3.onrender.com']
    )


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'corsheaders',
    'users',
    'projects',
    'assignments',
    'ftth_hld',
    'ftth_lld',
    'permits',
    'survey',
    'notifications',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


# Database
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases

# ---------------------------------------------------------------------------
# Database configuration
# ---------------------------------------------------------------------------
# Production: uses two remote PostgreSQL databases (business + GIS) on Zeabur.
# Local dev:   uses a single local Docker PostGIS with gis + business schemas.
#              Switch by setting FTTH_DB=local or uncommenting the local block.
# ---------------------------------------------------------------------------

if os.getenv('FTTH_DB', '').lower() in ('local', 'dev', 'docker'):
    # Local development — share Docker PostGIS with the FastAPI engine.
    # The Docker postgis service runs on localhost:5432 with database "ftth".
    # Django creates its tables in the "business" schema (search_path order).
    # The FastAPI engine uses the "gis" schema for spatial tables.
    # PASSWORD may legitimately be None (no password) — hence Any values.
    DATABASES: dict[str, Any] = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.getenv('PGDATABASE', 'ftth'),
            'USER': os.getenv('PGUSER', 'ftth'),
            'PASSWORD': os.getenv('PGPASSWORD', 'ftth'),
            'HOST': os.getenv('PGHOST', 'localhost'),
            'PORT': os.getenv('PGPORT', '5432'),
            'OPTIONS': {'options': '-c search_path=business,public'},
        },
    }
else:
    # Production — cloud PostgreSQL with business + gis schemas.
    # Every credential below comes from the environment (.env locally, the
    # service's env when deployed); none of it is defaulted — see PGPASSWORD.
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.getenv('PGDATABASE', 'zeabur'),
            'USER': os.getenv('PGUSER', 'root'),
            # From the environment only (.env locally, the host's env when
            # deployed) -- no committed fallback. docs/DEPLOY.md, "No credential
            # fallback, and a bounded database connect".
            'PASSWORD': os.getenv('PGPASSWORD'),
            'HOST': os.getenv('PGHOST', '91.98.18.217'),
            'PORT': os.getenv('PGPORT', '32467'),
            # Remote database: bound the connect so a stall cannot pin a worker
            # (and the liveness probe that shares it). docs/DEPLOY.md, "No
            # credential fallback, and a bounded database connect".
            'OPTIONS': {
                'options': '-c search_path=business,public',
                'connect_timeout': 5,
            },
        },
    }

# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

AUTH_USER_MODEL = 'users.User'

# Internationalization
# https://docs.djangoproject.com/en/6.0/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/

STATIC_URL = 'static/'

# collectstatic had no target directory, so a container build could not gather
# the admin/DRF assets. Pinned explicitly for deploy.
STATIC_ROOT = BASE_DIR / 'staticfiles'

# WhiteNoise serves the collected static files straight from gunicorn, so the
# container needs no separate web server or CDN for /static/.
# CompressedStaticFilesStorage (not the Manifest variant) is used on purpose:
# the manifest variant hard-fails the build if a template references a missing
# file, which would make deploys brittle for a repo this size.
STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedStaticFilesStorage',
    },
}

# Development never runs collectstatic, so WhiteNoise would warn about the
# missing STATIC_ROOT on every boot. Autorefresh makes it read through the app
# finders instead. It evaluates to False in production (DEBUG=false), where it
# serves the collected STATIC_ROOT as normal.
WHITENOISE_AUTOREFRESH = DEBUG

# Media files (user uploads)
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# CORS Configuration
CORS_ALLOW_CREDENTIALS = True

# Set CORS_ALLOW_ALL_ORIGINS=true in the environment if you want to allow any origin (dev only).
CORS_ALLOW_ALL_ORIGINS = os.getenv('CORS_ALLOW_ALL_ORIGINS', 'false').lower() == 'true'

CORS_ALLOWED_ORIGINS = [
    # The admin/engineer UI's GitLab Pages home.  It is the origin
    # fibre-fe deploys to, so a preflight from it must not depend on a host
    # env var being set correctly: leaving it out is what made every browser
    # call fail while both services were healthy.  `EXTRA_CORS_ORIGINS`
    # below stays the mechanism for a host that moves.
    'https://fibre-fe-98f8e0.gitlab.io',
    # The survey app's Render deployment.  Its web bundle is baked with this
    # backend's host, so this is the origin its browser calls come from.
    # Committing it keeps a redeploy from depending on EXTRA_CORS_ORIGINS being
    # set correctly in the platform dashboard -- the failure that already cost
    # the GitLab Pages origin above.
    'https://fibre-survey-web.onrender.com',
    'http://localhost:3000',
    'http://localhost:8081',
    'http://127.0.0.1:8081',
]

# Extra browser origins supplied at deploy time (comma-separated), so moving
# the frontend to a new host needs an env var rather than a code change.
CORS_ALLOWED_ORIGINS += [
    o.strip() for o in os.getenv('EXTRA_CORS_ORIGINS', '').split(',') if o.strip()
]

# Allow any https *.zeabur.app subdomain (future frontend/other services on
# Zeabur).  Explicit origins above still take precedence for non-Zeabur domains.
CORS_ALLOWED_ORIGIN_REGEXES = [
    r'^https://[a-z0-9-]+\.zeabur\.app$',
]

# A package download is a 200 with a valid ZIP even when half its contract is
# missing, so the X-Package-* response headers are the only warning the reader
# gets.  The browser can only read them if they are exposed: without this list
# a cross-origin fetch sees an empty header set and the UI stays silent about
# the same gap the archive reports in PACKAGE_REPORT.txt.
CORS_EXPOSE_HEADERS = [
    'X-Package-Complete',
    'X-Package-Bundled',
    'X-Package-Missing',
    'X-Package-Missing-Names',
]

CSRF_TRUSTED_ORIGINS = [
    # Same origin as above: a cross-origin POST from the Pages UI is rejected
    # by CSRF before it reaches a view unless it is trusted here too.
    'https://fibre-fe-98f8e0.gitlab.io',
    'https://fibre-survey-web.onrender.com',
    'http://localhost:3000',
    'http://127.0.0.1:3000',
    'http://localhost:8081',
]

# Extra CSRF-trusted origins at deploy time (comma-separated).
CSRF_TRUSTED_ORIGINS += [
    o.strip() for o in os.getenv('EXTRA_CSRF_TRUSTED_ORIGINS', '').split(',') if o.strip()
]

# REST Framework + JWT Config
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': ('rest_framework.permissions.IsAuthenticated',),
}

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(hours=12),
    # Refresh must OUTLIVE the access token — it is the renewal credential.
    # 30 days = "stay logged in on the survey device for a month", with the
    # 401→refresh path in apiFetch renewing the 12h access token silently.
    'REFRESH_TOKEN_LIFETIME': timedelta(days=30),
    'AUTH_HEADER_TYPES': ('Bearer',),
}


# ---------------------------------------------------------------------------
# Email — permit review notifications (permits/notifications.py)
# ---------------------------------------------------------------------------
# Degrades rather than fails, like every other permit integration (see
# `.env.example`): with no credentials the messages are printed to the console
# instead of reaching for the network, and with no PERMIT_NOTIFY_EMAIL nobody
# is emailed at all. Local development therefore needs nothing configured; a
# deployed host sets EMAIL_HOST_USER + EMAIL_HOST_PASSWORD — a Gmail *app
# password*, since Gmail rejects the account password for SMTP — plus
# PERMIT_NOTIFY_EMAIL and PERMITS_PUBLIC_BASE_URL.
_EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', '')
EMAIL_HOST = os.getenv('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT = int(os.getenv('EMAIL_PORT', '587'))
EMAIL_USE_TLS = os.getenv('EMAIL_USE_TLS', 'true').lower() == 'true'
EMAIL_HOST_USER = _EMAIL_HOST_USER
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '')
# A stalled SMTP handshake must not pin a worker (same reasoning as the bounded
# database connect above).
EMAIL_TIMEOUT = int(os.getenv('EMAIL_TIMEOUT', '20'))
# Gmail refuses a From that is not the authenticated account (or a verified
# alias), so the authenticated address is the default sender.
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', _EMAIL_HOST_USER or 'ftth-permits@localhost')
# smtp only once a credential exists — an unconfigured host prints instead of
# failing on a rejected connection.
EMAIL_BACKEND = os.getenv(
    'EMAIL_BACKEND',
    'django.core.mail.backends.smtp.EmailBackend'
    if _EMAIL_HOST_USER
    else 'django.core.mail.backends.console.EmailBackend',
)

# Who gets the permit review notifications (comma-separated for more than one
# reviewer). Empty = the whole notification layer is inert.
PERMIT_NOTIFY_EMAIL = os.getenv('PERMIT_NOTIFY_EMAIL', '')
# Absolute, publicly reachable base URL of THIS backend — the one-click
# Approve/Reject links in an email are opened from a mail client, which cannot
# resolve 127.0.0.1.  The deployed Render host is committed here for the same
# reason as PERMITS_TRACKER_URL below: an email link that only works when a
# host env var was remembered is a broken link, and a reviewer opening the
# mail from a phone cannot reach 127.0.0.1 at all.  Set it to '' on a host that
# must not carry the buttons (they then degrade to tracker links), or override
# it when the backend moves.  Under DEBUG an explicitly empty value still
# falls back to http://localhost:8000 for local testing.
PERMITS_PUBLIC_BASE_URL = os.getenv(
    'PERMITS_PUBLIC_BASE_URL', 'https://fibre-backend-wml3.onrender.com'
)
# The planner UI, linked from every notification email. The public Pages host
# is committed here for the same reason it is in CORS_ALLOWED_ORIGINS: a link
# that only works when a host env var is set correctly is a broken link.
PERMITS_TRACKER_URL = os.getenv(
    'PERMITS_TRACKER_URL', 'https://fibre-fe-98f8e0.gitlab.io/ftth-permits.html'
)
# How long an emailed Approve/Reject link stays usable (default 14 days).
PERMIT_ACTION_TOKEN_MAX_AGE = int(os.getenv('PERMIT_ACTION_TOKEN_MAX_AGE', str(60 * 60 * 24 * 14)))
