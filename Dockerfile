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
CMD ["sh", "-c", "gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers ${WEB_CONCURRENCY:-3} --timeout 120 --access-logfile - --error-logfile -"]
