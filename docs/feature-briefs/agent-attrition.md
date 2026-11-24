# Feature brief: Agent Attrition Prediction

**Owner:** Failah Maomin · **Reviewer:** Nel · **Due:** Wed Nov 26, 11:59 PM (Pull Request opened)

## 1. What it is

A BPO loses money every time a trained agent quits. This feature gives every agent an
**attrition risk score (0–100)** and a **Low / Medium / High** level, so a team lead can spot
who might leave and act early. It is the attrition module from your React prototype, rebuilt
inside Odoo.

**Demo line:** "Change an agent's overtime or satisfaction and watch the risk move: that's the
what-if a team lead uses before a 1-on-1."

No AI call is needed. It is a rule-based weighted score (the course requires "basic tests +
scoring logic", and this is it).

## 2. How our project works (read once)

- We run **stock Odoo 20 Community in Docker**. Odoo's own code is inside the Docker image; we
  never edit it.
- All our work is one Odoo add-on: **`addons/smarterp_bpo/`**. It extends Odoo's records and
  screens from the outside (Python `_inherit` for fields, XML `inherit_id` + `xpath` for views).
- 5 AI features share this add-on. Yours lives on **Employees** (`hr.employee`).
- **Copy the Lead Scoring feature** (landing on `main` shortly; run `git pull` if you don't see
  these files yet). It is the closest example of exactly what you are building:
  - `models/crm_lead.py`: new fields on an Odoo model, a stored computed score, weights in one dict, tiers from thresholds
  - `views/crm_lead_views.xml`: a "SmartERP" tab on the form, extra list columns, a group-by
  - `tests/test_lead_scoring.py`: the tests

## 3. Setup (about 30 minutes)

1. Accept the GitHub invite to `libraprogramming110/smarterp-bpo-odoo` (check your email).
2. Install **Docker Desktop** and **Git**, and start Docker Desktop.
3. Clone the repo, then follow **README.md → First run** in **PowerShell**.
4. Open http://localhost:8069 and log in with **admin / admin**. Go to **Employees** and open any
   employee. You should see a **SmartERP** tab with a placeholder message. That tab is yours.
5. Create your branch: `git checkout -b feature/agent-attrition`.
   **`main` is protected.** You can't push to it; everything comes in through a Pull Request.

Stuck on setup for more than 30 minutes? Message Nel right away rather than losing a day.

## 4. What to build

### Fields on `hr.employee` (all names start with `smarterp_`)

| Field | Type | Meaning |
|---|---|---|
| `smarterp_tenure_months` | Integer | Months at the company |
| `smarterp_absences_90d` | Integer | Absences in the last 90 days |
| `smarterp_performance` | Selection 1–5 | Last performance rating |
| `smarterp_overtime_hours` | Integer | Overtime hours per month |
| `smarterp_satisfaction` | Selection 1–5 | Last satisfaction survey score |
| `smarterp_risk_score` | Integer, **stored compute** | 0–100 |
| `smarterp_risk_level` | Selection, **stored compute** | Low / Medium / High |

### Scoring

- One readable dict of weights that add up to 100, e.g. short tenure, many absences, low
  performance, high overtime and low satisfaction each push the risk up. Pick the weights from
  your prototype.
- Thresholds for the level, e.g. **High ≥ 70, Medium ≥ 40, Low below 40**.
- The compute uses `@api.depends(...)` on the 5 inputs, so **editing an input on the form updates
  the score instantly.** That is the what-if, and you get it for free.

### Screens

- Replace the placeholder in **`views/hr_employee_views.xml`** with:
  - an "Agent Inputs" group (the 5 inputs)
  - a "Attrition Risk" group: score and level, with the level shown as a coloured badge
    (red High / orange Medium / green Low)
- Add score and level as columns on the employee list view, plus a "Risk Level" group-by in the
  search.

## 5. Files

- **Create:** `models/hr_employee.py`, `tests/test_agent_attrition.py`
- **Edit:** `views/hr_employee_views.xml`, plus one import line each in `models/__init__.py` and
  `tests/__init__.py`
- **Do NOT edit:** `__manifest__.py`, `views/menus.xml`, `views/placeholders.xml`,
  `models/ai_helper.py`, or any other feature's files. Nel wires up the SmartERP → Agent
  Attrition menu after merging.

## 6. Tests (required)

Copy the pattern in `tests/test_lead_scoring.py` (`TransactionCase`,
`@tagged('post_install', '-at_install', 'smarterp')`). At least:

1. A clearly high-risk agent gets level **High**.
2. A clearly low-risk agent gets level **Low**.
3. Changing one input (e.g. overtime up) raises the score.

Run all tests from **PowerShell** (Git Bash breaks the command):

```powershell
docker compose run --rm odoo odoo -d smarterp -u smarterp_bpo --test-tags /smarterp_bpo --stop-after-init
docker compose restart odoo
```

Look for `0 failed, 0 error(s) of N tests` near the end. All existing tests must still pass.

## 7. Gotchas

- After changing Python files or `views/*.xml`, run the update command above (the `-u smarterp_bpo`
  part reloads our add-on), then refresh the browser.
- Always run docker commands from the project folder.
- Don't commit `.env`. It's already gitignored; leave it that way.
- Keep it simple: no extra models, libraries or AI calls. Small, readable code wins.

## 8. Hand-in

1. Push your branch: `git push -u origin feature/agent-attrition`.
2. Open a Pull Request into `main`. In the description, include:
   - a screenshot of the employee SmartERP tab
   - your test output line (`0 failed … of N tests`)
3. Nel reviews and merges. If it isn't working with tests by the due date, the team builds it on
   Nov 27 so the demo stays on schedule.
