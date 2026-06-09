"""Seed-Skript: legt die WG-Test-Accounts mit Username + Demo-Passwort an.

Login lokal: http://localhost:5000/auth/login mit Username + ``wg1234`` (alle
Seed-User haben dasselbe Demo-Passwort, ``must_change_password=False`` damit
das Demo direkt funktioniert). Alternativ Dev-Login: ``/auth/dev``.

Idempotent: mehrmaliger Aufruf aktualisiert bestehende Accounts (per Username,
mit Email-Fallback fuer Bestands-Rows ohne Username).

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

from werkzeug.security import generate_password_hash  # noqa: E402

from app import create_app  # noqa: E402
from app.domain.enums import Role, UserStatus  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models.user import User, UserRole  # noqa: E402

OUTPUT_FILE = ROOT / "test_accounts.txt"
DEMO_PASSWORD = "wg1234"

# (name, username, email, roles, joined_days_ago)
# Alle 365 Tage — gleicher Tenure-Faktor in der Fairness-Normalisierung,
# sodass Assignments rein nach Score/Rotation verteilt werden (kein Bias zu
# Lasten der laenger-tenured User).
SEED_USERS: list[tuple[str, str, str, list[Role], int]] = [
    ("Michael Mauer", "michael", "michael.mauer@solveant.com",
        [Role.ADMIN, Role.HAUSWART, Role.HAUSBEWOHNER], 365),
    ("Kylian",  "kylian",  "kylian@wg.test",  [Role.HAUSBEWOHNER], 365),
    ("Maurice", "maurice", "maurice@wg.test", [Role.HAUSBEWOHNER], 365),
    ("Bishal",  "bishal",  "bishal@wg.test",  [Role.HAUSBEWOHNER], 365),
    ("Alex",    "alex",    "alex@wg.test",    [Role.HAUSBEWOHNER], 365),
    ("Ngya",    "ngya",    "ngya@wg.test",    [Role.HAUSBEWOHNER], 365),
]


def _upsert_user(
    name: str,
    username: str,
    email: str,
    roles: list[Role],
    joined_days_ago: int,
) -> User:
    joined_at = datetime.now(timezone.utc) - timedelta(days=joined_days_ago)
    # Suche zuerst nach Username, dann Fallback E-Mail (Bestands-Rows aus der
    # Vor-Username-Welt). So koennen wir auch alte Rows nahtlos hochziehen.
    user = db.session.query(User).filter_by(username=username).first()
    if user is None:
        user = db.session.query(User).filter_by(email=email).first()
    if user is None:
        user = User(username=username)
        db.session.add(user)
    user.username = username
    user.email = email
    user.name = name
    user.status = UserStatus.APPROVED
    user.joined_at = joined_at
    # Demo-Passwort + Flag aus, damit beim Login kein Redirect zur Change-Form
    # erzwungen wird.
    user.password_hash = generate_password_hash(DEMO_PASSWORD)
    user.must_change_password = False
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
    lines.append("Login: http://localhost:5000/auth/login")
    lines.append(f"Demo-Passwort fuer ALLE Accounts: {DEMO_PASSWORD}")
    lines.append("Alternativ Dev-Login (ohne Passwort): http://localhost:5000/auth/dev")
    lines.append(f"Generiert: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("")
    for user, roles in users:
        role_str = ", ".join(r.value for r in roles)
        joined = user.joined_at.strftime("%Y-%m-%d") if user.joined_at else "-"
        lines.append("-" * 60)
        lines.append(f"Name:        {user.name}")
        lines.append(f"Username:    {user.username}")
        lines.append(f"Passwort:    {DEMO_PASSWORD}")
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
        for name, username, email, roles, joined_days_ago in SEED_USERS:
            user = _upsert_user(name, username, email, roles, joined_days_ago)
            created.append((user, roles))
        db.session.commit()
        # Nach Commit ggf. neu laden, damit IDs/Status sicher gesetzt sind.
        for user, _roles in created:
            db.session.refresh(user)
        _write_txt(created)
        print(f"{len(created)} Test-Accounts angelegt/aktualisiert.")
        print(f"Demo-Passwort fuer alle: {DEMO_PASSWORD}")
        print(f"Daten exportiert nach: {OUTPUT_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
