# Slotly

A free web app for finding shared availability across team calendars. Users subscribe to their iCal/ICS URLs (Google, Apple iCloud, Outlook) and the app shows when everyone in a selected group is free. Supports connecting multiple Google and Microsoft calendar accounts; one is the active write target for bookings and events.

See [`PRD.md`](./PRD.md) for the full product spec.

## Repo layout

```
.
├── PRD.md              Product requirements — single source of truth
├── backend/            Django + DRF API (Python 3.12)
│   └── Dockerfile      Image used by celery-worker and celery-beat
├── frontend/           Next.js 16 + TypeScript + Tailwind (PWA)
├── docker-compose.yml  Postgres 16, Redis 7, MailHog, Celery worker + beat
├── .env.example        Template for local environment variables
└── .gitignore
```

## Stack

| Layer | Choice |
|---|---|
| Frontend | Next.js 16 (App Router) + React 19 + TypeScript + Tailwind 4 |
| Backend | Django 5 + Django REST Framework + django-allauth (headless) |
| Async | Celery 5 + Celery Beat + Redis 7 |
| Database | PostgreSQL 16 |
| Calendar parsing | `icalendar` + `recurring-ical-events` + `httpx` |
| Hosting (planned) | Azure Container Apps (West Europe) + Azure Database for PostgreSQL + Azure Cache for Redis |

## Local development

### Prerequisites

- Python 3.12+
- Node 22+ (Node 24 verified)
- Docker Desktop

### First-time setup

```bash
# 1. Build the backend image (used by celery worker + beat).
docker compose build celery-worker

# 2. Start the infra: Postgres, Redis, MailHog, Celery worker, Celery beat.
docker compose up -d

# 3. Backend (host venv).
cd backend
python -m venv .venv
.venv/bin/pip install -r requirements.txt
cp ../.env.example .env
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py runserver   # http://localhost:8000

# 4. Frontend (in another terminal).
cd frontend
npm install
npm run dev   # http://localhost:3000
```

### Service map

| Service | URL / Port | Purpose |
|---|---|---|
| Next.js dev | http://localhost:3000 | Frontend; rewrites `/_allauth/*` and `/api/*` to backend |
| Django dev | http://localhost:8000 | API + admin (`/admin/`) |
| Postgres 16 | `postgres://slotly:slotly_dev_password@localhost:5432/slotly` | Persistent — data survives `docker compose stop` |
| Redis 7 | `redis://localhost:6379` | Cache + Celery broker (DB 1) + result backend (DB 2) |
| MailHog | http://localhost:8025 | Catches all outgoing email locally |
| Celery worker | (no port) | Processes jobs queued on Redis DB 1 |
| Celery beat | (no port) | Fires scheduled tasks (e.g. ICS poll every 5 min) |

### Useful commands

```bash
# Logs
docker compose logs -f celery-worker
docker compose logs -f celery-beat

# Restart a service after a tasks.py change (worker imports are cached)
docker compose restart celery-worker

# Trigger a sync manually (host venv)
cd backend
.venv/bin/python -c "from slotly_api.celery import app; app.send_task('calendars.sync_all_due')"

# Tear down infra (volumes preserved)
docker compose stop

# Tear down + delete volumes (full reset)
docker compose down -v
```

### Running tests

```bash
# Django unit tests
cd backend
.venv/bin/python manage.py test

# HTTP-level E2E smoke (87 checks against the live stack)
.venv/bin/python ../scripts/e2e.py

# Browser-level UI smoke — loads every page in headless Chromium
# and asserts no console.error / pageerror events. Catches React
# hydration mismatches and DOM nesting violations the HTTP suite
# can't see.
.venv/bin/pip install playwright            # one-time
.venv/bin/playwright install chromium       # one-time, ~150 MB
.venv/bin/python ../scripts/smoke_browser.py
```

## MCP API

MCP API — read + write capabilities (bookings, calendar events) via a
stable HTTP endpoint. See [`docs/mcp-api.md`](./docs/mcp-api.md).

## Status

MVP feature-complete. Tracking branch on https://github.com/hulinl/slotly. Production deploy is the last milestone.

Roadmap: see PRD §10.

## License

TBD before public launch.
