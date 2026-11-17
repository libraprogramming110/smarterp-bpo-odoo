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

## Logs

```powershell
docker compose logs -f odoo
```

## Stop / reset

- Stop: `docker compose down` (data is kept).
- Full reset (deletes the database and files): `docker compose down -v`, then repeat "First run".
