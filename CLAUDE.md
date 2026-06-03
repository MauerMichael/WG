# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

WG (Wohngemeinschaft / shared apartment) organisation tool. The stack **is chosen and built**: Flask app-factory + SQLAlchemy 2.0 (`pg8000`) + Alembic, server-rendered Jinja + HTMX + Tailwind. Features ship as feature-split blueprints under `app/blueprints/`: `auth`, `dashboard`, `tasks`, `absences`, `shopping`, `admin`, `hauswart`, `extras`.

**UI text, flash messages, docstrings and comments are in German** — match that when adding code. The Negativ-Karma + Ehrenpunkte scoring extension specced in **`ALGORITHM.md`** is implemented (`KarmaEvent` ledger + `apply_overdue_penalties` cron + `extras` blueprint).

## Implementierungs-Stand

Was schon **live** ist:

- **Auth & Rollen** — Google OAuth (Authlib) + Approval-Flow (PENDING/APPROVED/REJECTED), Dev-Login `/auth/dev` (nur DevConfig), drei Rollen (HAUSBEWOHNER/HAUSWART/ADMIN, kombinierbar), globale `before_request`-Guard.
- **Dashboard** — „Heute" + „Diese Woche", **Dienst-Banner** für laufende Dienste, **Statistik-Karte** (erledigt/Punkte/verpasst/Zuverlässigkeit), Top-5 Einkauf, kommende Abwesenheiten, WG-Score-Footer.
- **Aufgaben** — Wochen-Kalender (Mo–So, Vor/Zurück) + Listen-Ansicht. Form mit **Typ AUFGABE/DIENST**, Recurrence (NONE/DAILY/WEEKLY/BIWEEKLY/MONTHLY/CUSTOM) + **Wochentag-Anker** (WEEKLY/BIWEEKLY) bzw. **Tag-im-Monat-Anker** (MONTHLY), Schwierigkeits-Punkte, Mehrfach-Zuweisung, Eligibility-Liste. „Erledigt"-Button erst ab `period_start` (Future-Lock + serverseitige Sperre in `mark_done`).
- **Hauswart** (`/hauswart`, nur HAUSWART/ADMIN) — Review-Queue (Dienste am Periodenende + überfällige unbeanspruchte), pro Eintrag Genehmigen/Ablehnen-mit-Notiz/Für-ihn-abhaken. Personen-7-Tage-Ansicht mit Statistik + denselben Aktionen.
- **Abwesenheit** — **Monats-Grid** (Bewohner × Tage, grün=da/rot=weg), klickbare Zellen mit Toggle (eigene immer, fremde nur Hauswart/Admin). Toggle splittet Ranges sauber. Zusätzlich klassisches Bis-Von-Formular für längere Abwesenheiten mit Grund. Nach Toggle automatisches Task-Reassignment.
- **Einkauf** — HTMX-Liste mit Quick-Add, Abhaken/Rückgängig, sektioniert (Offen / Kürzlich gekauft).
- **Karma-System** — separates `KarmaEvent`-Ledger (HONOR/PENALTY). Wird in `effective_scores_for` mit aufaddiert: HONOR im 80-Tage-Fenster, PENALTY im 40-Tage. PENALTY entsteht automatisch via `scripts/apply_overdue_penalties.py` für überfällig-unbeanspruchte Zuweisungen.
- **Extras** (`/extras`) — Bewohner reichen freiwillige Sonderleistungen ein; Hauswart vergibt beim Genehmigen Ehrenpunkte → erzeugt HONOR-`KarmaEvent`.
- **Admin** — Nutzer-Verwaltung (freischalten/ablehnen/Rollen toggeln/entfernen) mit HTMX-Row-Swap, AuditLog-Einträge.
- **Performance** — `pool_pre_ping`+`pool_recycle`, eager-loaded Rollen im user_loader, eager-Loading in Listen/Dashboard, Batch-Score (`effective_scores_for`).
- **E-Mail-Reminders** — `scripts/send_reminders.py` (Skript-Hülle, SMTP-Setup via env).
- **Dev-Daten** — `seed_dev_data.py` (7 Accounts + txt-Export), `seed_demo_data.py` (~2 Wochen realistisches Treiben).
- **Design-System** — „Sanft & gemütlich" (violett/gelb/weiß), komponenten-basiert (s. `DESIGN.md`).

Was bewusst **noch offen** (Backlog / Iteration 4): Gemeinschafts-Event mit Unter-Checkliste (3. TaskKind EVENT), Foto-Nachweis beim Abhaken, Pinnwand, Leaderboard, Hausregeln-Seite, Swap = „Abgeben → offen für alle". Außerdem: Push-Notifications, editierbare bestehende Aufgaben-Definitionen, Verlauf > 7 Tage.

## Environment specifics that bite

- **Host platform is Windows ARM64** (`Python312-arm64`). There are no prebuilt wheels for `psycopg`, `psycopg2`, or `psycopg2-binary` on this platform — `pip install` will fail with a dependency-resolution error. Use **`pg8000`** (pure-Python, already installed) for sync work, or **`asyncpg`** if going async (has win-arm64 wheels). Do not suggest `psycopg` without checking wheel availability first.
- Shell is **PowerShell 5.1**, not bash. `&&` chaining does not work — use `;` or `if ($?) { ... }`. The venv activator is `.\venv\Scripts\Activate.ps1`; or call the interpreter directly as `.\venv\Scripts\python.exe` (preferred in scripts, no activation needed).

## Database

PostgreSQL 17.10 running in a Docker container on Hostinger (`srv1368949.hstgr.cloud`, exposed as `187.77.70.233:32770`). Credentials live in `.env` (gitignored); `.env.example` is the committed template. Both `POSTGRES_*` individual vars and a combined `DATABASE_URL` are set — keep them in sync when editing.

## Commands

```powershell
# Verify DB connectivity (prints server version, current db, current user)
.\venv\Scripts\python.exe .\scripts\test_db.py

# Install a new dependency into the venv
.\venv\Scripts\python.exe -m pip install <package>

# Run the dev server (Flask factory in `app/`)
.\venv\Scripts\python.exe -m flask --app app run --debug

# Seed 7 dev test accounts + export them to test_accounts.txt (idempotent)
.\venv\Scripts\python.exe .\scripts\seed_dev_data.py

# Seed ~2 weeks of realistic demo activity (tasks/occurrences/karma/shopping/
# absences/extras) so every screen shows "life". Idempotent: wipes prior demo
# activity each run, keeps accounts.
.\venv\Scripts\python.exe .\scripts\seed_demo_data.py

# Materialize upcoming task occurrences + assign them (cron job — run DAILY)
# Materializes at least DEFAULT_LOOKAHEAD_PERIODS periods AND every period starting
# within MIN_NOTICE_DAYS (=7) days, so every task is assigned >= 1 week ahead.
# The >= 1-week guarantee only holds if this runs at least daily.
.\venv\Scripts\python.exe .\scripts\generate_occurrences.py

# Mark overdue-unclaimed assignments SKIPPED + write PENALTY karma events
# (cron job, idempotent: bereits SKIPPED-Zeilen werden nicht erneut bestraft)
.\venv\Scripts\python.exe .\scripts\apply_overdue_penalties.py

# Send due-task reminder emails (cron job)
.\venv\Scripts\python.exe .\scripts\send_reminders.py

# Apply DB migrations against the remote Hostinger Postgres
.\venv\Scripts\python.exe -m alembic upgrade head

# Generate a new migration after model changes
.\venv\Scripts\python.exe -m alembic revision --autogenerate -m "<msg>"

# Rebuild Tailwind output (watch-mode rebuilds on template changes)
tools\tailwindcss.exe -i app/static/css/input.css -o app/static/css/output.css --watch

# Run tests (against in-memory SQLite — no remote DB needed)
.\venv\Scripts\python.exe -m pytest

# Run a single test file / single test
.\venv\Scripts\python.exe -m pytest tests/test_scheduling.py
.\venv\Scripts\python.exe -m pytest tests/test_scheduling.py::test_name -q

# Lint (and autofix)
.\venv\Scripts\python.exe -m ruff check .
.\venv\Scripts\python.exe -m ruff check --fix .
```

## Architecture

- **Flask factory** in `app/__init__.py` (`create_app(config_name)`, `dev`/`prod`). `.env` is loaded at import time via `python-dotenv`. `app/config.py::_normalize_db_url` rewrites any `postgres(ql)://` URL to the `postgresql+pg8000://` dialect; when the URI is SQLite (tests) the PG-only pool options are stripped. A global `@app.context_processor` injects `today = date.today()` into all templates (used e.g. to lock the „Erledigt"-Button for future periods).
- **Auth & roles**: Google OAuth (Authlib) with a manual approval flow — `User.status` ∈ PENDING/APPROVED/REJECTED. A global `before_request` guard (`register_auth_guard` in `app/blueprints/auth/__init__.py`) forces login and lets only APPROVED users reach app internals; others get status pages. Roles (HAUSBEWOHNER/HAUSWART/ADMIN) live in `user_roles`; gate handlers with the helpers there (`user_has_role`, `user_has_any_role`, `require_admin_or_hauswart`). Dev-only password-less login at `/auth/dev` is gated by `DEV_LOGIN_ENABLED` (DevConfig only). The user_loader eager-loads `User.roles` to keep the per-request guard cheap on the remote DB.
- **Blueprints** are feature-split under `app/blueprints/` (`auth`, `dashboard`, `tasks`, `absences`, `shopping`, `admin`, `hauswart`, `extras`). Each blueprint owns its own templates and routes. No cross-feature imports of route handlers — but services and auth helpers are shared.
- **Services** (`app/services/`) hold cross-cutting logic the blueprints reuse: `scheduling.py` (fairness, occurrence generation, review actions, karma penalties — pure functions), `notifications.py` (E-Mail-Komposition), `contributions.py` (Extras: submit/approve/reject → HONOR-KarmaEvent).
- **Domain layer** (`app/domain/`) is Flask-free / DB-free — pure Enums (`Role`, `UserStatus`, `Recurrence`, `TaskStatus`, `AssignmentStatus`, `TaskKind`, `ReviewStatus`, `KarmaKind`) and constants (`points.py`).
- **Models** (`app/models/`): `user`, `task` (Definition/Occurrence/Assignment + EligibleUser M2M), `absence`, `shopping`, `audit`, `karma` (`KarmaEvent`-Ledger), `extra` (`ExtraContribution`). SQLAlchemy 2.0 declarative with `Mapped[...]`/`mapped_column(...)`, UUID PKs, timezone-aware timestamps. `Flask-SQLAlchemy` provides the `db` singleton; `pg8000` is the DB driver (Windows ARM64 constraint — no psycopg). Two FKs to `users.id` on `TaskAssignment` (`user_id`, `reviewed_by_id`) require explicit `foreign_keys=` on the `User.assignments` relationship.
- **Alembic from day 1** under `migrations/`. `migrations/env.py` pulls metadata from `app.extensions.db` and the URL from the Flask config — `alembic.ini` keeps `sqlalchemy.url` empty. When adding NOT NULL columns to existing rows, set a `server_default` in the migration (else upgrade against the remote with existing data fails); explicit `CREATE TYPE` is needed for new enums (pg8000 doesn't auto-create them under `add_column+server_default`).
- **Frontend** is server-rendered Jinja + HTMX partials, styled with Tailwind built by the standalone `tools/tailwindcss.exe` binary (no Node toolchain). Output CSS lives at `app/static/css/output.css` (gitignored, rebuilt locally). The visual design system („Sanft & gemütlich" — violet/yellow/white) is documented in **`DESIGN.md`** — read it before touching any template.
- **Scheduled jobs** are standalone scripts under `scripts/`, not in-process schedulers (avoids reloader-double-spawn + multi-worker pitfalls). Cron / Windows Task Scheduler invokes them. **Run `generate_occurrences.py` daily** — the ">= 1 week advance notice" guarantee (`MIN_NOTICE_DAYS`) only holds with a regular run. Reference cron entries for prod live in `ops/crontab`; locally use Windows Task Scheduler (`schtasks`).
- **Tests** run against **in-memory SQLite**, never the remote Postgres: `tests/conftest.py` forces `DATABASE_URL=sqlite:///:memory:` *before* importing `app`, and registers a `JSONB`→`TEXT` compiler shim (only `AuditLog.payload` uses JSONB). Fixtures `app` / `client` create+drop all tables per test. Some test modules define their own `app` fixture; mirror that style when adding tests that need different setup.
- **Fairness model**: tenure-normalized rolling score per user. Sort by `effective_score ASC`, tiebreak `last_assigned_at ASC`, then `user_id`. Pick Top-N per `TaskDefinition.required_assignees`. Logic lives in `app/services/scheduling.py` and is materialized by `scripts/generate_occurrences.py`. `effective_scores_for` is the batched, GROUP-BY query used by hot paths (dashboard, assign-sort) — prefer it over per-user `effective_score`. **Karma is now folded in**: HONOR events add positively (80-day window), PENALTY negatively (40-day window). The full distribution rule — incl. per-event decay and per-period assignment cap — is documented in **`ALGORITHM.md`**; read it before touching scoring/assignment logic.
- **Jinja gotcha**: variables assigned inside `{% for %}` loops do NOT leak to the outer scope. Templates that need a value computed in a loop (e.g. `own_assignment`, `verdict`) use `{% set ns = namespace(...) %}` and reference `ns.field`. Don't reintroduce the `{% set foo = none %}` outside / `{% set foo = x %}` inside-loop pattern — it silently leaves `foo` as `none`. Examples: `app/templates/tasks/_components/calendar_entry.html`, `occurrence_card.html`.

## Frontend / Design-System

Full guidelines: **`DESIGN.md`** (read it before any UI work). The essentials future changes MUST follow:

- **Vibe „Sanft & gemütlich":** violet (`brand-*`) leads, yellow (`accent-*`) for positive highlights, `surface`/white backgrounds, `rounded-xl/2xl` + `shadow-soft`, Nunito font. Mobile = bottom tab bar, desktop = top bar.
- **Reuse, don't re-invent.** Tokens in `tailwind.config.js`; component classes in `app/static/css/input.css` (`.btn`+variant, `.card`, `.badge`+variant, `.field/.label/.input`, `.empty-state`, `.page-title`); macros in `app/templates/components/` (`icon()`, `badge()`, `page_header()`, `empty_state()`). No ad-hoc `<button class="px-3 py-1 bg-…">`.
- **Never build class names dynamically** (`"badge-" ~ x`) — Tailwind only scans literal strings. Use full class names or the `badge()` macro.
- **Flash messages** render centrally in `base.html` — never add per-page `get_flashed_messages` blocks.
- **New top-level page:** add an entry to `nav_items` in `base.html` (gets top-bar + bottom-tab + active-state automatically); role-gate via `user_has_any_role` for hauswart/admin tabs.
- **HTMX partials must render standalone** — put their `{% from "components/…" import … %}` at the top. When restyling, never touch `hx-*`, `id=`, `name=`, or `hx-target`/`hx-swap`.
- **Always rebuild CSS after template/CSS edits:** `tools\tailwindcss.exe -i app/static/css/input.css -o app/static/css/output.css`.
