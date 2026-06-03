"""Seed-Skript: legt 7 Test-Accounts in der DB an und exportiert sie als txt.

Da der Login (vorerst) ohne Google laeuft, haben die Accounts kein Passwort.
Eingeloggt wird via Dev-Login: ``/auth/dev`` (nur aktiv, wenn DEV_LOGIN_ENABLED).
``google_sub`` bleibt leer; meldet sich spaeter jemand mit Google an, dessen
E-Mail zu einem Seed-Account passt, wird der Account automatisch verknuepft.

Idempotent: mehrmaliger Aufruf aktualisiert bestehende Accounts (per E-Mail),
legt keine Duplikate an.

Aufruf:
    .\\venv\\Scripts\\python.exe .\\scripts\\seed_dev_data.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.domain.enums import Role, UserStatus  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models.user import User, UserRole  # noqa: E402

OUTPUT_FILE = ROOT / "test_accounts.txt"

# (name, email, roles, joined_days_ago)
SEED_USERS: list[tuple[str, str, list[Role], int]] = [
    ("Michael Mauer", "michael.mauer@solveant.com", [Role.ADMIN, Role.HAUSWART, Role.HAUSBEWOHNER], 365),
    ("Lena Hauser", "lena.hauser@wg.test", [Role.HAUSWART, Role.HAUSBEWOHNER], 200),
    ("Jonas Becker", "jonas.becker@wg.test", [Role.HAUSBEWOHNER], 365),
    ("Sophie Wagner", "sophie.wagner@wg.test", [Role.HAUSBEWOHNER], 180),
    ("Felix Schaefer", "felix.schaefer@wg.test", [Role.HAUSBEWOHNER], 90),
    ("Marie Hoffmann", "marie.hoffmann@wg.test", [Role.HAUSBEWOHNER], 30),
    ("Tim Krueger", "tim.krueger@wg.test", [Role.HAUSBEWOHNER], 5),
]


def _upsert_user(name: str, email: str, roles: list[Role], joined_days_ago: int) -> User:
    joined_at = datetime.now(timezone.utc) - timedelta(days=joined_days_ago)
    user = db.session.query(User).filter_by(email=email).first()
    if user is None:
        user = User(email=email)
        db.session.add(user)
    user.name = name
    user.status = UserStatus.APPROVED
    user.joined_at = joined_at
    # Rollen synchronisieren: bestehende leeren, gewuenschte setzen.
    user.roles.clear()
    db.session.flush()
    for role in roles:
        user.roles.append(UserRole(user_id=user.id, role=role))
    return user


def _write_txt(users: list[tuple[User, list[Role]]]) -> None:
    lines: list[str] = []
    lines.append("WG-App – Test-Accounts (Dev)")
    lines.append("=" * 60)
    lines.append("")
    lines.append("Login OHNE Google (vorerst): http://localhost:5000/auth/dev")
    lines.append("Dort den gewuenschten Account anklicken -> 'Einloggen'.")
    lines.append("Kein Passwort noetig. Spaeter ersetzt Google-OAuth den Dev-Login.")
    lines.append(f"Generiert: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("")
    for user, roles in users:
        role_str = ", ".join(r.value for r in roles)
        joined = user.joined_at.strftime("%Y-%m-%d") if user.joined_at else "-"
        lines.append("-" * 60)
        lines.append(f"Name:        {user.name}")
        lines.append(f"E-Mail:      {user.email}")
        lines.append(f"Rollen:      {role_str}")
        lines.append(f"Status:      {user.status.value}")
        lines.append(f"Beigetreten: {joined}")
        lines.append(f"User-ID:     {user.id}")
    lines.append("-" * 60)
    OUTPUT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    app = create_app("dev")
    with app.app_context():
        created: list[tuple[User, list[Role]]] = []
        for name, email, roles, joined_days_ago in SEED_USERS:
            user = _upsert_user(name, email, roles, joined_days_ago)
            created.append((user, roles))
        db.session.commit()
        # Nach Commit ggf. neu laden, damit IDs/Status sicher gesetzt sind.
        for user, _roles in created:
            db.session.refresh(user)
        _write_txt(created)
        print(f"{len(created)} Test-Accounts angelegt/aktualisiert.")
        print(f"Daten exportiert nach: {OUTPUT_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
