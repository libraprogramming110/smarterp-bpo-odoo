# demo/

Helpers for the live demo. Local only: they use the default `admin / admin` login on `localhost`.

| File | What it does |
|---|---|
| `send-customer-reply.ps1` | Simulates a customer replying to a lead by email. Feeds a real-format email into Odoo's mail gateway, so it shows in the lead's chatter as a customer email, Gemini reads it, and the lead score updates. |

Run from the project folder in PowerShell, with Odoo running:

```powershell
powershell -ExecutionPolicy Bypass -File demo\send-customer-reply.ps1 -Lead "Bayside" -Body "We need about 40 agents starting next month."
```

It prints the lead's score before and after. Refresh the lead in the browser to see the email and the new score.
