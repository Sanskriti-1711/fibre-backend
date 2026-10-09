# Deploying fibre-backend

This is the runbook for getting the Django backend and the static frontend onto
infrastructure you control, with CI/CD. The QGIS design engine
(`HLD_Planning_01/web/backend`) is **out of scope** here — see
[Why the engine is out of scope](#why-the-engine-is-out-of-scope).

---

## 1. The constraint that decides everything: one shared database

**The Django backend and the FastAPI engine must point at the same PostgreSQL
database.** They are not independent services that happen to share a DB by
convenience — they are coupled through it:

- `HLD_Planning_01/web/backend/postgis.py` (line 33) names the schemas
  *"shared with Django (settings.py search_path=business,public)"*.
- The engine writes the pipeline output into `gis.*` layer tables and rows into
  `business.ftth_hld_layers`.
- Django reads exactly those tables to serve the layer GeoJSON, the results
  maps, package downloads and the BOQ.
- Deleting a project row in `business.ftth_projects` CASCADEs the spatial rows
  in `gis.*` — one schema graph, two writers.

Consequences you must accept before choosing an option:

1. If you keep the engine running on the current Zeabur deployment, your new
   backend **must use the current Zeabur database**. A fresh free database
   (Neon, Supabase, anything) would be empty, and the engine's output would keep
   landing in the Zeabur database where your new backend cannot see it. The
   results map would simply be blank.
2. If you want a genuinely self-owned database, you must move the engine too —
   and the engine needs a VM, not a free tier (see below).
3. Two Django deployments against one database means `migrate` run from the new
   deployment mutates the schema **underneath the still-running production
   backend**. Treat migrations as a coordinated, deliberate act, never as an
   automatic step, until the old deployment is retired.

Nothing here is a reason not to proceed — it just fixes the order of operations
and rules out the "just point it at a shiny new free Postgres" shortcut.

---

## 2. Hosting reality

| Component | Runtime | Free hosting? |
|---|---|---|
| `fiber-fe` | static HTML/CSS/JS, no build step | **Yes.** Cloudflare Pages, Netlify, Vercel Hobby, or the existing GitLab Pages pipeline. |
| `fiber-backend` (this repo) | Django + gunicorn, Dockerfile added here | **Yes, with cold starts.** Render free web service, Koyeb, or Cloud Run. |
| `HLD_Planning_01` engine | FastAPI + QGIS 3.44 in Docker, runs `qgis_process` | **No.** |

There is no free always-on container host for the engine. Its Dockerfile is
`FROM qgis/qgis:3.44` — a multi-GB image that compiles shapely against system
GEOS and performs long geoprocessing runs. Serverless platforms (Vercel, Cloud
Run) cannot host it at all. Running it means a small paid VM
(Hetzner CX22 ≈ €4/mo, DigitalOcean, Vultr) or the status quo.

### Why the engine is out of scope

Because it cannot be hosted free, keeping it where it is, is the only option
consistent with a zero-cost deployment. That is a legitimate choice — it just
means the engine *and its database* stay on Zeabur, which is exactly the
coupling described in section 1.

---

## 3. Recommended target

- **Database:** the existing Zeabur PostgreSQL + PostGIS (forced by section 1).
- **Backend:** Render free web service, Docker, auto-deploy on push to `main`.
- **Frontend:** keep the working GitLab Pages pipeline, or move to Cloudflare
  Pages for a custom domain.

Render was chosen over the alternatives because it takes a Dockerfile as-is,
redeploys on git push (that *is* the CD), supports a pre-deploy command and a
health-check path, and has a free web tier. The free tier **spins down after
inactivity**, so the first request after a quiet period takes tens of seconds.
That is the cost of free; there is no free always-on alternative.

---

## 4. Deploy order

Do it in this order. Each step is verifiable before the next depends on it.

```
1. Database   -> confirm reachability + schemas  (no change)
2. Backend    -> deploy, migrate, seed admin, smoke-test the API
3. Frontend   -> point at the new backend, deploy
4. Verify     -> end-to-end login + one HLD/LLD page
```

### Step 1 — Database (no changes)

Confirm the schemas and extension already exist:

```bash
psql "$PGHOST" -c "\dn"                       # expect: business, gis, public
psql "$PGHOST" -c "\dx postgis"               # expect: postgis installed
```

If `gis` is missing, the engine has never completed a run against this
database.

### Step 2 — Backend

Create a Render **Web Service** from `Sanskriti-1711/fibre-backend`, branch
`main`, runtime **Docker**. Render detects the `Dockerfile` in the repo root.

Environment variables (Render → Environment):

| Variable | Value | Why |
|---|---|---|
| `DJANGO_SECRET_KEY` | long random string | Without it, `settings.py` falls back to a committed insecure key. **Required.** |
| `DJANGO_DEBUG` | `false` | Otherwise Django serves verbose error pages publicly. **Required.** |
| `DJANGO_ALLOWED_HOSTS` | `<service>.onrender.com` | Replaces the permissive `*`. |
| `PGDATABASE` / `PGUSER` / `PGPASSWORD` / `PGHOST` / `PGPORT` | the Zeabur database | Must match what the engine writes to. See section 1. |
| `FTTH_ENGINE_URL` | `https://ftth-planning.onrender.com` | Keeps HLD/LLD working on the existing engine. |
| `EXTRA_CORS_ORIGINS` | the deployed frontend origin | e.g. `https://fe.example.com`. Comma-separated for several. |
| `EXTRA_CSRF_TRUSTED_ORIGINS` | same origin | Required for admin/login POSTs. |
| `DJANGO_DEBUG` / `CORS_ALLOW_ALL_ORIGINS` | leave unset | Falls back to existing behaviour. |

Do **not** set `FTTH_DB` — leaving it unset selects the production branch of
`config/settings.py`, which reads every connection detail from the variables
above.

Set the **pre-deploy command** to:

```bash
python manage.py migrate --noinput
```

Set the **health check path** to `/healthz`.

### The two probes, and why they are separate

| Path | Purpose | Point what at it |
|---|---|---|
| `/healthz` | Liveness. Touches no external dependency. | **The platform's health check.** |
| `/healthz/engine` | Dependency probe for the FastAPI engine. `200` healthy, `503` unreachable/unhealthy. | **Your monitor.** |

`/healthz/engine` exists because the engine runs on a host you have no log,
metric or restart access to — the backend is the only vantage point you have on
it.

**Do not point the platform's health check at `/healthz/engine`.** A non-200
there makes the platform tear down and restart the container, so an engine
outage would be converted into a restart loop of a service that was still
healthy enough to be useful.

Sample response:

```json
{
  "ok": true,
  "engine_url": "https://ftth-planning.onrender.com",
  "checked_url": "https://ftth-planning.onrender.com/health",
  "latency_ms": 1328,
  "http_status": 200,
  "engine_status": "ok",
  "service": "ftth-engine-api",
  "uptime_seconds": 6986880,
  "qgis_available": true
}
```

Notes for whoever wires up the alerting:

- `ok` / HTTP `503` is the primary alert condition — the engine is unreachable
  or reports itself unhealthy.
- `qgis_available: false` is a *separate* alert worth having on its own. The
  engine answers normally but cannot run a single pipeline, because QGIS is
  missing. It is deliberately not folded into the `503`, so the two failure
  modes stay distinguishable.
- `uptime_seconds` resets when the engine restarts. Since you cannot see the
  engine directly, a drop to a low value is your only signal that it bounced.
  On the free tier it resets to ~0 every time the sleeping engine is woken.
- The probe is inherently slow: the engine's `/health` gathers PostGIS and OSM
  status before replying, measured at ~2.5–3s against the current engine. The
  client timeout is therefore **15s**. Do not tune it down toward the observed
  latency — the timeout is per socket operation, so a 5s value does not even
  fire reliably on a 5.3s response.
- **A `503` just after the stack has been idle is expected, and is NOT a
  timeout.** The engine runs on a sleeping free tier: while it boots, the
  platform's edge answers `502` in ~150ms and this probe reports any non-200 as
  `503`. Raising `ENGINE_PROBE_TIMEOUT` therefore changes nothing — the failure
  arrives long before any timeout could fire (measured 2026-10-09: `502` in
  143ms, while the engine took 76s to become healthy). During a cold start the
  engine genuinely is not serving, so `503` is the honest answer: a monitor
  should read it as degraded, not as an outage. `tools/check_stack.py`'s
  `backend -> engine` check does exactly that — WARN when the backend points at
  the host being monitored, FAIL only when it is configured for a different one.
- The response is trimmed on purpose: the engine's own `/health` exposes its
  `qgis_process` filesystem path and database details, and this endpoint is
  unauthenticated so a monitor can reach it.

First deploy only — create the login account the docs reference:

```bash
python manage.py seed_dev_admin     # admin@admin.com / admin123$
```

`seed_dev_admin` refuses to run when `DEBUG` is false. Either run it once with
`DJANGO_DEBUG=true` and then switch back, or pass explicit credentials from a
`render shell`. Then **change that password** — it is a documented, public
default.

Smoke-test before moving on:

```bash
curl -i https://<service>.onrender.com/healthz
curl -X POST https://<service>.onrender.com/api/users/login/ \
  -H 'Content-Type: application/json' \
  -d '{"email":"a@b.c","password":"x"}'      # expect 400
```

### Step 3 — Frontend

The frontend resolves its API base URL in `fiber-fe/js/ftth-config.js`:

```js
var LOCAL_API = 'http://localhost:8000';
var PROD_API  = 'https://fibre-backend-wml3.onrender.com';   // <- the live backend
```

Change `PROD_API` (and `FTTH_BASE_URL` if it should differ) to the new backend
URL, then deploy. Two acceptable ways:

- **Edit the constant** in `js/ftth-config.js` — simplest, and the file is the
  documented single source of truth for this.
- **Inject `window.FIBER_BASE_URL` / `window.FTTH_BASE_URL`** in the host's
  build/deploy step. The file already honours a pre-set value, so no code change
  is needed and the same commit can serve both environments.

Add the new frontend origin to the backend's `EXTRA_CORS_ORIGINS` **before**
bringing the frontend up, or every browser call fails CORS.

Also update `CORS_ALLOWED_ORIGINS` / `CSRF_TRUSTED_ORIGINS` in
`config/settings.py` once the old origins are permanently retired.

### Step 4 — Verify end to end

Log in from the deployed frontend, open a project, and load an HLD/LLD results
page. That single flow exercises the frontend, the backend, the shared
database and the HLD engine together. If the map is blank but the API
answers, the engine is writing somewhere your backend isn't reading — re-read
section 1.

---

## 5. CI/CD

**Backend — GitHub Actions** (`.github/workflows/ci.yml`, added here). Runs on
push to `main` and on PRs:

| Job | What it proves |
|---|---|
| `tests` | The 202-test suite passes against **PostGIS 16**, not SQLite. |
| `image` | The Dockerfile builds, `collectstatic` gathers assets, gunicorn boots, `/healthz` and `/static/*` answer 200. |
| `advisory` | Non-blocking report of pre-existing drift (see section 7). |

Deployment itself is the platform's git integration: Render redeploys on push
to `main`. That is the CD half — no extra workflow needed. If you later move to
a VM (Coolify or plain Docker), add a deploy job that builds, pushes, and
restarts the container.

**Frontend — GitLab CI** (`fiber-fe/.gitlab-ci.yml`, already present). Its
`validate` job syntax-checks every JS file and verifies internal links; `pages`
deploys to GitLab Pages. Leave it alone unless you move hosts.

Why the suite must not run on SQLite: `ftth_lld/views.py:508` uses
`.distinct("ftth_project_id")`, which is PostgreSQL `DISTINCT ON`. Under the
existing SQLite test settings this raises
`NotSupportedError: DISTINCT ON fields is not supported by this database
backend`, so `config/test_settings.py` can never produce a green run. Verified
baseline on PostGIS: **202 tests, OK (skipped=4)**.

---

## 6. Runner time

The suite takes **~7.5 minutes** on PostGIS versus ~6 seconds on SQLite. One
test in `permits/tests/test_sync_portal.py` makes real network calls that have
to time out (`http_portal poll failed for REF-1: no route to host`). That
accounts for nearly all of it. It is worth shortening by injecting a fast
failure for the portal URL in tests, but it is not blocking.

---

## 7. Known drift (not introduced by this change)

Three pre-existing issues, deliberately not fixed here because each one changes
behaviour you may have opinions about:

1. **`permits/` has models with no migration.** `makemigrations --check
   --dry-run` reports two index renames on `permitaidraft`. Confirmed on both
   Django 5.1.15 and 5.2.17, so it is real drift, not a version artifact.
   Cosmetic (index names only, same columns) but it makes `makemigrations`
   always dirty. Fix by generating and reviewing the migration.
2. **Django version is unpinned.** `requirements.txt` declares
   `Django>=5.1,<6.0`. Local development runs **5.1.15**; the container
   installs **5.2.17**. Deploy and development are therefore on different
   Djangos. Pin to the version you actually test on.
3. **Production credentials are committed.** `config/settings.py` hardcodes the
   live Zeabur database host, user and password as the fallback, and the repo is
   public. Left untouched deliberately — the owner needs to rotate it. Until
   then, treat that database as compromised.

Static serving for `MEDIA_ROOT` is also unhandled: `whitenoise` covers `/static/`
only, and uploads land on the container's ephemeral disk. Photo fields and any
`MEDIA_ROOT`-backed download will 404, and are lost on redeploy. Fix with object
storage (S3/R2) or a mounted persistent disk before relying on uploads.

---

## 8. Useful local commands

Run the suite exactly as CI does:

```bash
docker run -d --name ftth-pg -e POSTGRES_PASSWORD=ftth -e POSTGRES_USER=ftth \
  -e POSTGRES_DB=ftth -p 55432:5432 postgis/postgis:16-3.4

docker exec ftth-pg psql -U ftth -d ftth \
  -c "CREATE EXTENSION IF NOT EXISTS postgis;" \
  -c "CREATE SCHEMA IF NOT EXISTS business;" \
  -c "CREATE SCHEMA IF NOT EXISTS gis;"

FTTH_DB=local PGHOST=127.0.0.1 PGPORT=55432 PGUSER=ftth PGPASSWORD=ftth \
  PGDATABASE=ftth python manage.py test
```

Build and run the production image:

```bash
docker build -t fibre-backend:ci .
docker run -p 8000:8000 -e DJANGO_DEBUG=false -e DJANGO_ALLOWED_HOSTS='*' \
  -e DJANGO_SECRET_KEY=dev-only fibre-backend:ci
```

> Windows note: if a global `PYTHONPATH` points at a QGIS Python 3.12
> `site-packages`, it shadows Anaconda's 3.11 and Django fails with
> `fields.E210: Cannot use ImageField because Pillow is not installed`.
> Clear it first: `unset PYTHONPATH PYTHONHOME`.
