"""
Test settings — overrides the main settings for local testing.

Uses SQLite so we don't need the remote Zeabur PostgreSQL.
"""

from .settings import *  # noqa: F403, F401

# Use SQLite for local testing
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",  # noqa: F405
    }
}

# Hash passwords cheaply — the API tests create users and never verify a
# password hash. PBKDF2 (the default) costs ~0.4 s per user, which dominates
# the runtime of a suite this size.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Disable HTTPS requirement for JWT in dev
SIMPLE_JWT["AUTH_HEADER_TYPES"] = ("Bearer",)  # noqa: F405
SIMPLE_JWT["USER_AUTHENTICATION_RULE"] = "rest_framework_simplejwt.authentication.default_user_authentication_rule"

# Allow all hosts for local testing
ALLOWED_HOSTS = ["*"]

# Set debug to see detailed errors
DEBUG = True

# CORS - allow all for testing
CORS_ALLOW_ALL_ORIGINS = True
