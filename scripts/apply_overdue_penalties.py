"""Strafen-Job: bucht Negativ-Karma für überfällig-unbeanspruchte Zuweisungen.

Setzt alle OPEN-Assignments auf Occurrences mit abgelaufener Periode auf
SKIPPED und schreibt je ein PENALTY-Karma-Event. Idempotent: bereits
übersprungene Zeilen werden nicht erneut bestraft.

Lokal manuell oder via Windows Task Scheduler ausführen, in Prod via Cron
(z.B. einmal täglich nach Mitternacht).

Aufruf:
    .\\venv\\Scripts\\python.exe .\\scripts\\apply_overdue_penalties.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Repo-Root in sys.path aufnehmen, damit `import app` funktioniert, wenn man
# das Skript direkt aufruft.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.services import scheduling  # noqa: E402


def main() -> int:
    app = create_app("dev")
    with app.app_context():
        count = scheduling.apply_overdue_penalties(db.session)
        db.session.commit()
        print(f"{count} überfällige Zuweisungen bestraft")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
