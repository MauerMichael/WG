"""Sleep-Loop fuer den Cron-Container.

Statt einen ``cron``-Daemon im Image zu installieren, laeuft dieser kleine
Python-Loop in Endlosschleife und fuehrt einmal pro Tag (~02:00 UTC) die drei
Wartungs-Skripte aus:

    generate_occurrences.py  — neue Perioden materialisieren + zuweisen
    rebalance.py             — OPEN-Future-Verteilung fair halten
    apply_overdue_penalties.py — ueberfaellig-unbeanspruchte bestrafen

Nach Container-Restart laeuft der erste Lauf SOFORT (damit der Backfill nach
Down-Time greift), danach im Tagesrhythmus. Stdout/-err landen via Docker-Logs
in der ueblichen Pipeline.

Lokal manuell startbar via:
    .\\venv\\Scripts\\python.exe .\\scripts\\cron_loop.py
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


SCRIPTS = (
    "scripts/generate_occurrences.py",
    "scripts/rebalance.py",
    "scripts/apply_overdue_penalties.py",
)


def _run_once() -> None:
    for path in SCRIPTS:
        print(f"[cron] running {path}", flush=True)
        try:
            subprocess.run(
                [sys.executable, path],
                cwd=str(ROOT),
                check=False,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[cron] {path} failed: {exc}", flush=True)


def _seconds_until_next_two_am_utc() -> float:
    now = datetime.now(timezone.utc)
    target = now.replace(hour=2, minute=0, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return max((target - now).total_seconds(), 60.0)


def main() -> int:
    while True:
        _run_once()
        delay = _seconds_until_next_two_am_utc()
        next_run = datetime.now(timezone.utc) + timedelta(seconds=delay)
        print(
            f"[cron] sleep {delay:.0f}s, next run ~ {next_run.isoformat()}",
            flush=True,
        )
        time.sleep(delay)


if __name__ == "__main__":
    raise SystemExit(main())
