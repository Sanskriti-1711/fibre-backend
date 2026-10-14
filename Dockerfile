# Production image for the FTTH Django backend.
#
# Python 3.11 matches the interpreter the project is developed and tested
# against locally (Anaconda 3.11). psycopg2-binary ships wheels, so no
# compiler toolchain is needed at build time.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libpq is what psycopg2 links against. It is bundled in the -binary wheel, but
# installing it explicitly keeps the runtime predictable if the wheel ever
# falls back to a source build.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so a code-only change reuses the cached layer.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Gather admin + DRF assets into STATIC_ROOT. WhiteNoise serves them from
# gunicorn afterwards, so no separate web server or CDN is required.
# --noinput because the build is non-interactive.
RUN python manage.py collectstatic --noinput

EXPOSE 8000

# Honour $PORT so the same image runs locally (8000) and on platforms that
# inject their own port. Migrations are deliberately NOT here: running them in
# the start command races when several workers boot at once. Run
# `python manage.py migrate` as the platform's pre-deploy/release step.
#
# The worker layout is pinned deliberately, and the platform's liveness probe is
# the reason. /healthz shares the worker pool with every API request, so on one
# synchronous worker a slow request makes the probe miss the host's 5s budget
# and the host kills a healthy instance: measured 2026-10-14, three concurrent
# logins pushed /healthz to 7.06s, and the container was SIGKILLed every
# 1-2 minutes. A login costs ~3s of PBKDF2 on a 0.1-CPU free instance, so that
# overlap is routine, not a corner case.
#
# Threads are what fix it, not more workers alone: hashlib releases the GIL for
# the hash, so a gthread worker can answer the probe while another thread is
# hashing (verified: 70 of 74 timer ticks landed during an 870k-iteration hash).
# Two workers add a second process-level slot; --preload shares the imported app
# between them so both fit a 512MB instance, and --max-requests recycles a
# worker before any slow leak can matter.
#
# The count is NOT left to $WEB_CONCURRENCY: the running container was observed
# booting a single worker despite this file defaulting to 3.
CMD ["sh", "-c", "gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers 2 --worker-class gthread --threads 4 --preload --max-requests 800 --max-requests-jitter 100 --timeout 120 --access-logfile - --error-logfile -"]
