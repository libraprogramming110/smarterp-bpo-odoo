# SmartERP for BPO

Odoo 20 + PostgreSQL 16 in Docker, with our custom addon in `addons/smarterp_bpo`.

## Prerequisites

- Windows 10/11 with [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed and running.
- Git. Nothing else (no Python or Postgres needed on your laptop).

## First run

Run these in PowerShell from the project folder:

```powershell
copy .env.example .env
docker compose up -d
docker compose run --rm odoo odoo -d smarterp -i crm,contacts,smarterp_bpo --with-demo --stop-after-init
docker compose restart odoo
```

Open http://localhost:8069 and log in with **admin / admin**.

The first `up` downloads about 1-2 GB of images, so it takes a while.
Wait for it to finish before running the next command.

## After changing the module code

```powershell
docker compose run --rm odoo odoo -d smarterp -u smarterp_bpo --stop-after-init
docker compose restart odoo
```

If you added a new Python file or changed `__manifest__.py`, this is required. For a quick restart only: `docker compose restart odoo`.

## Running tests

```powershell
docker compose run --rm odoo odoo -d smarterp -u smarterp_bpo --test-tags /smarterp_bpo --stop-after-init
docker compose restart odoo
```

Tests live in `addons/smarterp_bpo/tests/` and never call Gemini (it is mocked). Look for `0 failed, 0 error(s) of N tests` near the end of the output. Run it from PowerShell: Git Bash rewrites `/smarterp_bpo` into a Windows path.

## Enrich with AI (n8n)

The **Enrich with AI** button (lead form, SmartERP tab) asks our own n8n (http://localhost:5678) to read the company website with Gemini; Odoo writes the result under **Company Research**. Odoo does not need n8n to run: if n8n is down, only this button shows "Enrich service is offline" and the lead is left unchanged.

One-time setup (needs `GEMINI_API_KEY` in `.env`):

```powershell
docker compose up -d
docker compose exec -T n8n n8n import:workflow --input=/files/enrich-company.json
docker compose exec -T n8n n8n publish:workflow --id=smarterpEnrich01
docker compose restart n8n
docker compose run --rm odoo odoo -d smarterp -u smarterp_bpo --stop-after-init
docker compose restart odoo
```

Demo day: `docker compose up -d` starts n8n with the workflow already active. The button works without logging in to n8n. Opening http://localhost:5678 the first time asks you to create a local owner account: only needed to look at or edit the workflow. Workflow details: `n8n/README.md`.

Port 5678 must be free: stop any other n8n container first.

## Logs

```powershell
docker compose logs -f odoo
```

## Stop / reset

- Stop: `docker compose down` (data is kept).
- Full reset (deletes the database and files): `docker compose down -v`, then repeat "First run".
