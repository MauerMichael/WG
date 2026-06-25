"""Catch-up-Job: rebalanced OPEN-Future-Assignments fair zwischen Bewohnern.

Wird zusätzlich zu den admin-getriggerten Aufrufen täglich via Cron ausgeführt,
damit Drift (z.B. nach Absences, manuellen DB-Edits) zeitnah eingefangen wird.
Idempotent: wenn schon ausgewogen, keine Änderung.

Aufruf:
    .\\venv\\Scripts\\python.exe .\\scripts\\rebalance.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.services import scheduling  # noqa: E402


def main() -> int:
    app = create_app("dev")
    with app.app_context():
        swaps = scheduling.rebalance_open_assignments(db.session)
        db.session.commit()
        print(f"{swaps} Verteilungs-Swap(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
