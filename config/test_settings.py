"""
Test settings — overrides the main settings for local testing.

Uses SQLite so we don't need the remote Zeabur PostgreSQL.
"""

from .settings import *  # noqa: F403, F401

# Use SQLite for local testing
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',  # noqa: F405
    }
}

# Hash passwords cheaply — the API tests create users and never verify a
# password hash. PBKDF2 (the default) costs ~0.4 s per user, which dominates
# the runtime of a suite this size.
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']

# Disable HTTPS requirement for JWT in dev
SIMPLE_JWT['AUTH_HEADER_TYPES'] = ('Bearer',)  # noqa: F405
SIMPLE_JWT['USER_AUTHENTICATION_RULE'] = (  # noqa: F405
    'rest_framework_simplejwt.authentication.default_user_authentication_rule'
)

# Allow all hosts for local testing
ALLOWED_HOSTS = ['*']

# Set debug to see detailed errors
DEBUG = True

# CORS - allow all for testing
CORS_ALLOW_ALL_ORIGINS = True

# Evidence media stays on local disk for the suite, whatever the developer's
# `.env` says. With a bucket configured the store talks to the network: an
# upload writes a real object, and `/media/` answers a redirect to the storage
# URL instead of serving bytes, so the media tests (404 for a missing file,
# byte-for-byte compare, HEAD, traversal) would assert against the wrong
# contract. Pinning it here keeps the suite offline and environment-independent.
EVIDENCE_S3_BUCKET = ''
EVIDENCE_S3_ACCESS_KEY_ID = ''
EVIDENCE_S3_SECRET_ACCESS_KEY = ''
EVIDENCE_PUBLIC_BASE_URL = ''

# `get_evidence_storage()` caches the store it built; drop anything the import
# above may have created so the empty bucket above is what the tests see.
from supervision.storage import reset_evidence_storage  # noqa: E402

reset_evidence_storage()
