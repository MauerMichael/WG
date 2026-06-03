"""CLI: Versendet den täglichen WG-Aufgaben-Digest an alle approved User.

Aufruf:

    .\\venv\\Scripts\\python.exe .\\scripts\\send_reminders.py

Exit-Codes:
    0 – Versand erfolgreich (Anzahl wird auf stdout ausgegeben).
    1 – Mindestens eine E-Mail konnte nicht verschickt werden oder die
        SMTP-Konfiguration fehlt.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Sicherstellen, dass das Repo-Root auf sys.path liegt, wenn das Skript direkt
# (statt via `python -m`) aufgerufen wird.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.services.notifications import send_daily_digests  # noqa: E402


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("send_reminders")

    app = create_app("dev")
    with app.app_context():
        try:
            count = send_daily_digests(db.session)
        except Exception as exc:  # noqa: BLE001
            log.error("Reminder-Versand fehlgeschlagen: %s", exc)
            return 1

    print(f"{count} E-Mails verschickt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
