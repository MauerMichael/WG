"""Materialisierungs-Job: erzeugt für jede aktive TaskDefinition die nächsten
zwei Perioden und ruft die Fairness-Zuweisung auf.

Lokal manuell oder via Windows Task Scheduler ausführen, in Prod via Cron.
Idempotent: mehrmaliger Aufruf am gleichen Tag erzeugt keine Duplikate.

Aufruf:
    .\\venv\\Scripts\\python.exe .\\scripts\\generate_occurrences.py
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
from app.domain.points import DEFAULT_LOOKAHEAD_PERIODS  # noqa: E402
from app.extensions import db  # noqa: E402
from app.services import scheduling  # noqa: E402


def main() -> int:
    app = create_app("dev")
    with app.app_context():
        count = scheduling.generate_occurrences(
            db.session, lookahead_periods=DEFAULT_LOOKAHEAD_PERIODS
        )
        db.session.commit()
        print(f"{count} neue Occurrences")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
