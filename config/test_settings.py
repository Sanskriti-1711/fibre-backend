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

# Disable HTTPS requirement for JWT in dev
SIMPLE_JWT["AUTH_HEADER_TYPES"] = ("Bearer",)  # noqa: F405
SIMPLE_JWT["USER_AUTHENTICATION_RULE"] = "rest_framework_simplejwt.authentication.default_user_authentication_rule"

# Allow all hosts for local testing
ALLOWED_HOSTS = ["*"]

# Set debug to see detailed errors
DEBUG = True

# CORS - allow all for testing
CORS_ALLOW_ALL_ORIGINS = True
