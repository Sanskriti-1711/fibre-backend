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
# The worker layout is pinned, and the reason is that the platform's liveness
# probe shares the worker pool with every API request: one slow request on a
# 0.1-CPU instance makes the probe miss the host's 5s budget, and the host then
# kills a container that was still serving. The measurements -- and why threads,
# not more workers, are what keep the probe answered -- are in docs/DEPLOY.md,
# "The health check shares the worker pool". The count is pinned rather than
# left to $WEB_CONCURRENCY, which resolved to one worker in the running
# container while this file asked for three.
# --timeout is the ARBITER's patience, not the request's: a worker that has not
# reported back within it is killed mid-request. It was 120s, while this app
# deliberately waits longer than that for the engine -- `FTTH_RESOLVE_TIMEOUT`
# and `FTTH_INPUT_LAYER_TIMEOUT` both default to 1200s because a cold OSM city
# download takes 10-16 minutes. Any wait past two minutes was therefore killed
# by gunicorn and surfaced as a 502 to the page, no matter what the call itself
# allowed. The budget here must stay ABOVE the longest blocking call; raise it
# with them or the mismatch comes back. 1300 = 1200 + a margin for the reply.
CMD ["sh", "-c", "gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers 2 --worker-class gthread --threads 4 --preload --max-requests 800 --max-requests-jitter 100 --timeout 1300 --graceful-timeout 30 --access-logfile - --error-logfile -"]
