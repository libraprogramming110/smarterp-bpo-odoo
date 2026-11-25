# n8n/

The workflow behind the **Enrich with AI** button on a CRM lead. Used by the `n8n` service in `docker-compose.yml` (this folder is mounted read-only at `/files`).

| File | What it does |
|---|---|
| `enrich-company.json` | Webhook `POST /webhook/smarterp-enrich` gets `{lead_id, company, website, email_domain}`, fetches the website (or `https://<email domain>`, skipped for free mail), strips it to text, asks Gemini, answers `{industry, summary, support_channels, company_size, source}` or `{error}`. |

The Gemini key comes from `GEMINI_API_KEY` in `.env` (read with `$env`), so there are no credentials in the workflow.

Import / update it (also after editing the JSON), from the project folder:

```powershell
docker compose exec -T n8n n8n import:workflow --input=/files/enrich-company.json
docker compose exec -T n8n n8n publish:workflow --id=smarterpEnrich01
docker compose restart n8n
```

Importing again unpublishes it, so always run the publish and restart after an import.
