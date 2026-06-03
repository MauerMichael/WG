"""Seed-Skript: befuellt die letzten ~2 Wochen mit realistischem WG-Alltag.

Damit jeder Screen "Leben" zeigt statt Leerzustand: wiederkehrende Aufgaben,
ueberwiegend erledigte (teils verpasste/abgelehnte) Termine -> echte Punkte +
Karma + Scores, dazu Einkaufsliste, Abwesenheiten und Sonderleistungen. Heutige
und kommende Aufgaben bleiben OFFEN, damit Abhaken/Pruefen testbar sind.

Setzt auf den Accounts aus ``seed_dev_data.py`` auf (legt sie bei Bedarf an) und
nutzt die Fairness-Logik aus ``app.services.scheduling`` fuers Zuweisen.

Idempotent: jeder Lauf loescht ZUERST die Demo-Aktivitaeten (Aufgaben, Termine,
Zuweisungen, Karma, Einkaeufe, Abwesenheiten, Extras) und erzeugt sie frisch.
Accounts und Rollen bleiben unangetastet.

Aufruf:
    .\\venv\\Scripts\\python.exe .\\scripts\\seed_demo_data.py
"""

from __future__ import annotations

import random
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.domain.enums import (  # noqa: E402
    AssignmentStatus,
    KarmaKind,
    Recurrence,
    ReviewStatus,
    Role,
    TaskKind,
    TaskStatus,
)
from app.extensions import db  # noqa: E402
from app.models.absence import Absence  # noqa: E402
from app.models.extra import ExtraContribution  # noqa: E402
from app.models.karma import KarmaEvent  # noqa: E402
from app.models.shopping import ShoppingItem  # noqa: E402
from app.models.task import (  # noqa: E402
    TaskAssignment,
    TaskDefinition,
    TaskDefinitionEligibleUser,
    TaskOccurrence,
)
from app.models.user import User  # noqa: E402
from app.services import scheduling  # noqa: E402
from scripts.seed_dev_data import SEED_USERS, _upsert_user  # noqa: E402

# Reproduzierbar: gleicher Lauf -> gleiche (zufaellige) Verteilung.
random.seed(42)

PAST_DAYS = 14   # wie weit zurueck "Leben" simuliert wird
FUTURE_DAYS = 7  # kleiner Auslauf nach vorne fuer offene/testbare Aufgaben

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)


# (title, kind, recurrence, anchor_weekday, difficulty, required, description)
DEFINITIONS: list[dict] = [
    {
        "title": "Kueche putzen",
        "kind": TaskKind.AUFGABE,
        "recurrence": Recurrence.WEEKLY,
        "anchor_weekday": MON,
        "difficulty": 3,
        "required": 1,
        "description": "Arbeitsflaechen abwischen, Herd reinigen, Boden wischen.",
    },
    {
        "title": "Bad putzen",
        "kind": TaskKind.AUFGABE,
        "recurrence": Recurrence.WEEKLY,
        "anchor_weekday": WED,
        "difficulty": 4,
        "required": 1,
        "description": "Dusche, WC und Waschbecken gruendlich, Spiegel polieren.",
    },
    {
        "title": "Muell & Altpapier rausbringen",
        "kind": TaskKind.AUFGABE,
        "recurrence": Recurrence.WEEKLY,
        "anchor_weekday": FRI,
        "difficulty": 2,
        "required": 1,
        "description": None,
    },
    {
        "title": "Wohnzimmer & Flur staubsaugen",
        "kind": TaskKind.AUFGABE,
        "recurrence": Recurrence.WEEKLY,
        "anchor_weekday": SAT,
        "difficulty": 2,
        "required": 1,
        "description": None,
    },
    {
        "title": "Spuelmaschine ausraeumen",
        "kind": TaskKind.AUFGABE,
        "recurrence": Recurrence.DAILY,
        "anchor_weekday": None,
        "difficulty": 1,
        "required": 1,
        "description": "Taeglich nach dem Fruehstueck.",
    },
    {
        "title": "Einkaufsdienst (Woche)",
        "kind": TaskKind.DIENST,
        "recurrence": Recurrence.WEEKLY,
        "anchor_weekday": MON,
        "difficulty": 3,
        "required": 1,
        "description": "Grundnahrungsmittel & Haushaltskram fuer die WG besorgen.",
    },
    {
        "title": "Grossputz Gemeinschaftskueche",
        "kind": TaskKind.AUFGABE,
        "recurrence": Recurrence.BIWEEKLY,
        "anchor_weekday": SUN,
        "difficulty": 5,
        "required": 2,
        "description": "Kuehlschrank ausraeumen, Schraenke wischen, alles entkalken.",
    },
]


def _aware(d: date, hour: int = 12) -> datetime:
    """Timezone-aware UTC-Datetime aus einem Datum + Stunde."""

    return datetime.combine(d, time(hour=hour), tzinfo=timezone.utc)


def _point_share(difficulty: int, n_assignees: int) -> int:
    """Punkte-Anteil pro Assignee (Spiegel von ``mark_done``: round(diff/n))."""

    return int(round(int(difficulty) / max(n_assignees, 1)))


def _reset_demo_data() -> None:
    """Loescht alle Demo-Aktivitaeten (Accounts/Rollen bleiben).

    Reihenfolge FK-sicher: KarmaEvent zuerst (occurrence_id ist SET NULL, wuerde
    sonst verwaisen), dann der Rest.
    """

    for model in (
        KarmaEvent,
        ExtraContribution,
        ShoppingItem,
        Absence,
        TaskAssignment,
        TaskOccurrence,
        TaskDefinitionEligibleUser,
        TaskDefinition,
    ):
        db.session.query(model).delete(synchronize_session=False)

    # last_assigned_at zuruecksetzen, damit die Fairness-Verteilung frisch startet.
    for user in db.session.query(User).all():
        user.last_assigned_at = None

    db.session.flush()


def _ensure_users() -> dict[str, User]:
    """Stellt die Standard-Accounts sicher und liefert sie als email->User-Map."""

    for name, email, roles, joined_days_ago in SEED_USERS:
        _upsert_user(name, email, roles, joined_days_ago)
    db.session.flush()
    return {u.email: u for u in db.session.query(User).all()}


def _hauswarte(users: dict[str, User]) -> list[User]:
    out = []
    for u in users.values():
        if any(r.role == Role.HAUSWART for r in u.roles):
            out.append(u)
    return out


def _create_definitions(creator: User) -> list[TaskDefinition]:
    defs: list[TaskDefinition] = []
    for spec in DEFINITIONS:
        d = TaskDefinition(
            title=spec["title"],
            description=spec["description"],
            kind=spec["kind"],
            difficulty_points=spec["difficulty"],
            recurrence=spec["recurrence"],
            anchor_weekday=spec["anchor_weekday"],
            required_assignees=spec["required"],
            is_active=True,
            created_by_id=creator.id,
        )
        db.session.add(d)
        defs.append(d)
    db.session.flush()
    return defs


def _period_count(definition: TaskDefinition) -> int:
    """Genug Perioden, um das Fenster [today-PAST_DAYS, today+FUTURE_DAYS] zu decken."""

    span = PAST_DAYS + FUTURE_DAYS + 7  # Puffer fuer Anker-Snapping
    rec = definition.recurrence
    if rec == Recurrence.DAILY:
        return span
    if rec == Recurrence.WEEKLY:
        return span // 7 + 2
    if rec == Recurrence.BIWEEKLY:
        return span // 14 + 2
    if rec == Recurrence.MONTHLY:
        return 3
    return 2


def _generate_and_animate(
    definitions: list[TaskDefinition],
    today: date,
    hauswarte: list[User],
) -> dict[str, int]:
    """Erzeugt Occurrences uebers Fenster, weist zu und 'belebt' die Vergangenheit."""

    window_end = today + timedelta(days=FUTURE_DAYS)
    window_start = today - timedelta(days=PAST_DAYS)

    # Alle (start, end, definition) global chronologisch sammeln.
    planned: list[tuple[date, date, TaskDefinition]] = []
    for definition in definitions:
        anchor = today - timedelta(days=PAST_DAYS)
        for period_start, period_end in scheduling._iter_periods(
            definition, anchor, _period_count(definition)
        ):
            if period_start > window_end:
                continue
            if period_end < window_start:
                continue
            planned.append((period_start, period_end, definition))
    planned.sort(key=lambda t: t[0])

    counts = {"occurrences": 0, "assignments": 0, "done": 0, "skipped": 0,
              "rejected": 0, "penalties": 0}

    reject_notes = [
        "Boden war noch klebrig.",
        "Muelltonne stand noch voll im Flur.",
        "Spiegel und Armaturen vergessen.",
    ]

    for period_start, period_end, definition in planned:
        occurrence = TaskOccurrence(
            task_definition_id=definition.id,
            period_start=period_start,
            period_end=period_end,
            due_date=period_start,
            status=TaskStatus.OPEN,
        )
        db.session.add(occurrence)
        db.session.flush()
        occurrence.task_definition = definition

        assignments = scheduling.assign_occurrence(db.session, occurrence)
        counts["occurrences"] += 1
        counts["assignments"] += len(assignments)

        # last_assigned_at von "jetzt" (assign_occurrence) auf die Periode ruecken,
        # damit die Historie zeitlich stimmig bleibt.
        for a in assignments:
            a.user.last_assigned_at = _aware(period_start, 9)

        if period_end >= today:
            # Heute/Zukunft: offen lassen -> testbar (Abhaken, Review-Queue).
            continue

        share = max(_point_share(definition.difficulty_points, len(assignments)), 1)
        done_points = _point_share(definition.difficulty_points, len(assignments))

        for a in assignments:
            r = random.random()
            if r < 0.75:
                # Erledigt.
                a.status = AssignmentStatus.DONE
                a.completed_at = _aware(period_start, random.randint(8, 20))
                a.points_earned = done_points
                counts["done"] += 1
                if random.random() < 0.8 and hauswarte:
                    reviewer = random.choice(hauswarte)
                    a.review_status = ReviewStatus.APPROVED
                    a.reviewed_by_id = reviewer.id
                    a.reviewed_at = a.completed_at + timedelta(hours=3)
                # sonst: bleibt PENDING -> fuellt die Hauswart-Pruef-Queue
            elif r < 0.88:
                # Erledigt, aber vom Hauswart abgelehnt -> Punkte weg + Strafe.
                reviewer = random.choice(hauswarte) if hauswarte else None
                a.status = AssignmentStatus.DONE
                a.completed_at = _aware(period_start, random.randint(8, 20))
                a.points_earned = 0
                a.review_status = ReviewStatus.REJECTED
                a.review_note = random.choice(reject_notes)
                if reviewer is not None:
                    a.reviewed_by_id = reviewer.id
                    a.reviewed_at = a.completed_at + timedelta(hours=3)
                db.session.add(KarmaEvent(
                    user_id=a.user_id,
                    kind=KarmaKind.PENALTY,
                    points=share,
                    occurred_at=_aware(period_end, 18),
                    created_by_id=reviewer.id if reviewer is not None else None,
                    note="Schlecht erledigt – abgelehnt.",
                    occurrence_id=occurrence.id,
                ))
                counts["rejected"] += 1
                counts["penalties"] += 1
            else:
                # Verpasst -> SKIPPED + automatische Strafe (entgangene Punkte).
                a.status = AssignmentStatus.SKIPPED
                db.session.add(KarmaEvent(
                    user_id=a.user_id,
                    kind=KarmaKind.PENALTY,
                    points=share,
                    occurred_at=_aware(period_end, 23),
                    created_by_id=None,
                    note="Ueberfaellig – nicht erledigt",
                    occurrence_id=occurrence.id,
                ))
                counts["skipped"] += 1
                counts["penalties"] += 1

        # Occurrence-Status aus den Assignments ableiten.
        if all(a.status == AssignmentStatus.DONE for a in assignments):
            occurrence.status = TaskStatus.DONE
        elif all(a.status == AssignmentStatus.SKIPPED for a in assignments):
            occurrence.status = TaskStatus.SKIPPED
        else:
            occurrence.status = TaskStatus.OPEN

    db.session.flush()
    return counts


def _ensure_assigned(definition: TaskDefinition, today: date, user: User) -> None:
    """Stellt sicher, dass ``user`` der laufenden/heutigen Occurrence zugewiesen ist.

    Demo-Affordance: garantiert, dass der eingeloggte Test-User (Michael) auf dem
    Dashboard "Heute" bzw. "Aktueller Dienst" wirklich etwas sieht.
    """

    occ = next(
        (
            o for o in definition.occurrences
            if o.period_start <= today <= o.period_end
            and o.status == TaskStatus.OPEN
        ),
        None,
    )
    if occ is None:
        return
    if any(a.user_id == user.id for a in occ.assignments):
        return
    # Einen offenen Slot auf den User umbiegen (required=1 -> genau einer).
    open_slot = next(
        (a for a in occ.assignments if a.status == AssignmentStatus.OPEN), None
    )
    if open_slot is not None:
        open_slot.user_id = user.id
    else:
        db.session.add(TaskAssignment(
            occurrence_id=occ.id,
            user_id=user.id,
            status=AssignmentStatus.OPEN,
            points_earned=0,
        ))
    db.session.flush()


def _seed_absences(users: dict[str, User], today: date) -> int:
    sophie = users.get("sophie.wagner@wg.test")
    jonas = users.get("jonas.becker@wg.test")
    felix = users.get("felix.schaefer@wg.test")
    rows = []
    if sophie:
        rows.append(Absence(user_id=sophie.id, start_date=today - timedelta(days=2),
                            end_date=today + timedelta(days=3), reason="Urlaub"))
    if jonas:
        rows.append(Absence(user_id=jonas.id, start_date=today - timedelta(days=10),
                            end_date=today - timedelta(days=8), reason="Krank"))
    if felix:
        rows.append(Absence(user_id=felix.id, start_date=today + timedelta(days=5),
                            end_date=today + timedelta(days=9), reason="Heimfahrt"))
    for r in rows:
        db.session.add(r)
    db.session.flush()
    return len(rows)


def _seed_shopping(users: dict[str, User], today: date) -> int:
    members = list(users.values())
    now = datetime.now(timezone.utc)

    open_items = [
        ("Milch", "2x"),
        ("Spuelmaschinen-Tabs", None),
        ("Klopapier", "1 Pack"),
        ("Haferflocken", "500 g"),
        ("Kaffee", None),
        ("Spuelmittel", None),
        ("Bananen", "1 Hand"),
    ]
    bought_items = [
        ("Butter", "250 g"),
        ("Nudeln", "1 kg"),
        ("Tomatensosse", "3x"),
        ("Muellbeutel", None),
        ("Zahnpasta", None),
    ]

    count = 0
    for title, qty in open_items:
        db.session.add(ShoppingItem(
            title=title,
            quantity=qty,
            added_by_id=random.choice(members).id,
            added_at=now - timedelta(days=random.randint(0, 6), hours=random.randint(0, 12)),
        ))
        count += 1
    for title, qty in bought_items:
        adder = random.choice(members)
        buyer = random.choice(members)
        bought = now - timedelta(days=random.randint(1, 12), hours=random.randint(0, 12))
        db.session.add(ShoppingItem(
            title=title,
            quantity=qty,
            added_by_id=adder.id,
            added_at=bought - timedelta(days=1),
            bought_at=bought,
            bought_by_id=buyer.id,
        ))
        count += 1
    db.session.flush()
    return count


def _seed_extras(users: dict[str, User], hauswarte: list[User], today: date) -> tuple[int, int]:
    """Sonderleistungen: 2 offen, 2 genehmigt (+HONOR-Karma), 1 abgelehnt."""

    marie = users.get("marie.hoffmann@wg.test")
    tim = users.get("tim.krueger@wg.test")
    jonas = users.get("jonas.becker@wg.test")
    felix = users.get("felix.schaefer@wg.test")
    reviewer = hauswarte[0] if hauswarte else None

    extras = 0
    honors = 0

    pending = [
        (marie, "Keller entruempelt und Sperrmuell rausgebracht."),
        (tim, "Eingangsbereich gefegt und Fussmatte ausgeklopft."),
    ]
    for user, desc in pending:
        if user is None:
            continue
        db.session.add(ExtraContribution(
            user_id=user.id, description=desc, status=ReviewStatus.PENDING,
            created_at=datetime.now(timezone.utc) - timedelta(days=random.randint(0, 3)),
        ))
        extras += 1

    approved = [
        (jonas, "Waschkueche komplett geputzt und Flusensieb gereinigt.", 10),
        (felix, "Defekte Lampe im Flur repariert.", 6),
    ]
    for user, desc, pts in approved:
        if user is None:
            continue
        awarded = datetime.now(timezone.utc) - timedelta(days=random.randint(4, 10))
        db.session.add(ExtraContribution(
            user_id=user.id, description=desc, status=ReviewStatus.APPROVED,
            honor_points=pts,
            awarded_by_id=reviewer.id if reviewer else None,
            awarded_at=awarded,
            created_at=awarded - timedelta(days=1),
        ))
        extras += 1
        db.session.add(KarmaEvent(
            user_id=user.id, kind=KarmaKind.HONOR, points=pts,
            occurred_at=awarded,
            created_by_id=reviewer.id if reviewer else None,
            note=desc,
        ))
        honors += 1

    if tim is not None:
        db.session.add(ExtraContribution(
            user_id=tim.id,
            description="Pflanzen auf dem Balkon gegossen (war eh mein Hobby).",
            status=ReviewStatus.REJECTED,
            review_note="Zaehlt nicht als WG-Sonderleistung.",
            created_at=datetime.now(timezone.utc) - timedelta(days=6),
        ))
        extras += 1

    db.session.flush()
    return extras, honors


def main() -> int:
    app = create_app("dev")
    with app.app_context():
        today = datetime.now(timezone.utc).date()

        users = _ensure_users()
        _reset_demo_data()
        # Accounts nach dem Reset frisch laden (Map-Referenzen bleiben gueltig).
        users = {u.email: u for u in db.session.query(User).all()}
        hauswarte = _hauswarte(users)
        michael = users.get("michael.mauer@solveant.com")
        creator = michael or next(iter(users.values()))

        # Reihenfolge: Abwesenheiten ZUERST, damit assign_occurrence sie kennt.
        n_absences = _seed_absences(users, today)
        definitions = _create_definitions(creator)
        counts = _generate_and_animate(definitions, today, hauswarte)

        # Demo-Affordance: Michael sieht garantiert "Heute" + "Aktueller Dienst".
        if michael is not None:
            by_title = {d.title: d for d in definitions}
            for title in ("Spuelmaschine ausraeumen", "Einkaufsdienst (Woche)"):
                d = by_title.get(title)
                if d is not None:
                    _ensure_assigned(d, today, michael)

        n_shopping = _seed_shopping(users, today)
        n_extras, n_honors = _seed_extras(users, hauswarte, today)

        db.session.commit()

        print("Demo-Daten erzeugt (Fenster: "
              f"{today - timedelta(days=PAST_DAYS)} .. {today + timedelta(days=FUTURE_DAYS)})")
        print(f"  Accounts:        {len(users)}")
        print(f"  Aufgaben-Defs:   {len(definitions)}")
        print(f"  Occurrences:     {counts['occurrences']}")
        print(f"  Zuweisungen:     {counts['assignments']}"
              f" (erledigt {counts['done']}, abgelehnt {counts['rejected']}, verpasst {counts['skipped']})")
        print(f"  Karma-Strafen:   {counts['penalties']}")
        print(f"  Ehrenpunkte:     {n_honors}")
        print(f"  Abwesenheiten:   {n_absences}")
        print(f"  Einkaufs-Items:  {n_shopping}")
        print(f"  Sonderleistungen:{n_extras}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
