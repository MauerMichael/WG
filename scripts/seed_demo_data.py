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


# (title, kind, recurrence, anchor_weekday, recurrence_interval_days,
#  difficulty, required, description)
DEFINITIONS: list[dict] = [
    {
        "title": "Geschirrspueler ein-/ausraeumen",
        "kind": TaskKind.AUFGABE,
        "recurrence": Recurrence.CUSTOM,
        "anchor_weekday": None,
        "recurrence_interval_days": 2,
        "difficulty": 2,
        "required": 1,
        "description": "Abends anstellen, am naechsten Tag bis 14 Uhr ausraeumen.",
    },
    {
        "title": "Toilette 1 (Check)",
        "kind": TaskKind.DIENST,
        "recurrence": Recurrence.WEEKLY,
        "anchor_weekday": MON,
        "recurrence_interval_days": None,
        "difficulty": 3,
        "required": 1,
        "description": "Woechentlicher Check — wenn was zu meckern ist, direkt mit der Person sprechen.",
    },
    {
        "title": "Toilette 2 (Check)",
        "kind": TaskKind.DIENST,
        "recurrence": Recurrence.WEEKLY,
        "anchor_weekday": MON,
        "recurrence_interval_days": None,
        "difficulty": 3,
        "required": 1,
        "description": "Woechentlicher Check — wenn was zu meckern ist, direkt mit der Person sprechen.",
    },
    {
        "title": "Toilette 3 (Check)",
        "kind": TaskKind.DIENST,
        "recurrence": Recurrence.WEEKLY,
        "anchor_weekday": MON,
        "recurrence_interval_days": None,
        "difficulty": 3,
        "required": 1,
        "description": "Woechentlicher Check — wenn was zu meckern ist, direkt mit der Person sprechen.",
    },
    {
        "title": "Toilette 4 (Check)",
        "kind": TaskKind.DIENST,
        "recurrence": Recurrence.WEEKLY,
        "anchor_weekday": MON,
        "recurrence_interval_days": None,
        "difficulty": 3,
        "required": 1,
        "description": "Woechentlicher Check — wenn was zu meckern ist, direkt mit der Person sprechen.",
    },
    {
        "title": "Kuechendienst",
        "kind": TaskKind.DIENST,
        "recurrence": Recurrence.WEEKLY,
        "anchor_weekday": MON,
        "recurrence_interval_days": None,
        "difficulty": 7,
        "required": 1,
        "description": "Woechentlicher Check — bei Bedarf Hauswart anschreiben fuer Event-Aufgaben.",
    },
    {
        "title": "Muelldienst",
        "kind": TaskKind.DIENST,
        "recurrence": Recurrence.BIWEEKLY,
        "anchor_weekday": MON,
        "recurrence_interval_days": None,
        "difficulty": 5,
        "required": 1,
        "description": "Muelltonnen rausstellen + reinholen, Muell-Logistik fuer 2 Wochen.",
    },
    # Random Demo-Aufgaben fuer mehr Vielfalt in der Vorstellung.
    {
        "title": "Waeschekorb leeren",
        "kind": TaskKind.AUFGABE,
        "recurrence": Recurrence.WEEKLY,
        "anchor_weekday": WED,
        "recurrence_interval_days": None,
        "difficulty": 1,
        "required": 1,
        "description": "Korb in die Waschkueche bringen — keine Wartepflicht.",
    },
    {
        "title": "Kuehlschrank auswischen",
        "kind": TaskKind.AUFGABE,
        "recurrence": Recurrence.BIWEEKLY,
        "anchor_weekday": SUN,
        "recurrence_interval_days": None,
        "difficulty": 3,
        "required": 1,
        "description": "Alles raus, Faecher abwischen, Abgelaufenes wegwerfen.",
    },
]


# Einmalige Aufgaben (recurrence=NONE) — bekommen genau EINE Occurrence in der Zukunft.
# `due_in_days` = Offset zu heute fuer due_date / period_start.
# `required` = 1 -> Einmalig (gelb), >1 -> Event (rosa).
ONE_SHOT_DEFINITIONS: list[dict] = [
    {
        "title": "Fenster putzen",
        "kind": TaskKind.AUFGABE,
        "difficulty": 4,
        "required": 1,
        "description": "Innen + aussen, auch die Rahmen.",
        "due_in_days": 5,
    },
    {
        "title": "Fruehjahrsputz Bad",
        "kind": TaskKind.AUFGABE,
        "difficulty": 6,
        "required": 2,
        "description": "Dusche entkalken, Fugen reinigen, Spiegel polieren.",
        "due_in_days": 8,
    },
    {
        "title": "Bohrmaschine zurueckbringen",
        "kind": TaskKind.AUFGABE,
        "difficulty": 2,
        "required": 1,
        "description": "Geliehene Bohrmaschine an Nachbarn zurueck.",
        "due_in_days": 3,
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

    for name, username, email, roles, joined_days_ago in SEED_USERS:
        _upsert_user(name, username, email, roles, joined_days_ago)
    db.session.flush()
    # Map per E-Mail bleibt fuer die restlichen Helper-Funktionen unveraendert,
    # damit die uebrigen Lookup-Stellen weiter funktionieren. User ohne E-Mail
    # werden bewusst uebersprungen (alle Seed-Accounts haben eine).
    return {u.email: u for u in db.session.query(User).all() if u.email}


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
            recurrence_interval_days=spec.get("recurrence_interval_days"),
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
    if rec == Recurrence.CUSTOM:
        interval = definition.recurrence_interval_days or 1
        return span // max(interval, 1) + 2
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


def _create_one_shot_occurrences(
    today: date, creator: User
) -> tuple[list[TaskDefinition], int]:
    """Legt fuer jede ONE_SHOT_DEFINITION eine Definition + eine einmalige
    Occurrence in der Zukunft an (period_start = period_end = today + due_in_days).
    Verteilt sie ueber assign_occurrence."""

    defs: list[TaskDefinition] = []
    assigned = 0
    for spec in ONE_SHOT_DEFINITIONS:
        d = TaskDefinition(
            title=spec["title"],
            description=spec["description"],
            kind=spec["kind"],
            difficulty_points=spec["difficulty"],
            recurrence=Recurrence.NONE,
            anchor_weekday=None,
            recurrence_interval_days=None,
            required_assignees=spec["required"],
            is_active=True,
            created_by_id=creator.id,
        )
        db.session.add(d)
        db.session.flush()
        defs.append(d)

        due = today + timedelta(days=spec["due_in_days"])
        occ = TaskOccurrence(
            task_definition_id=d.id,
            period_start=due,
            period_end=due,
            due_date=due,
            status=TaskStatus.OPEN,
        )
        db.session.add(occ)
        db.session.flush()
        occ.task_definition = d
        assignments = scheduling.assign_occurrence(db.session, occ)
        assigned += len(assignments)

    db.session.flush()
    return defs, assigned


def _rebalance_open_assignments(
    definitions: list[TaskDefinition], today: date, users: list[User]
) -> int:
    """Stellt sicher, dass OPEN-Assignments (heute + Zukunft) gleichmaessig
    verteilt sind. Greedy: solange max-min > 1, schiebe je eine Zuweisung von
    der ueberlasteten zu der unterlasteten Person — falls Occurrence diese nicht
    schon enthaelt."""

    open_assigns: list[TaskAssignment] = []
    for d in definitions:
        for occ in d.occurrences:
            if occ.period_end < today:
                continue
            for a in occ.assignments:
                if a.status == AssignmentStatus.OPEN:
                    open_assigns.append(a)

    if not open_assigns or not users:
        return 0

    user_ids = [u.id for u in users]

    def counts() -> dict:
        c = {uid: 0 for uid in user_ids}
        for a in open_assigns:
            c[a.user_id] = c.get(a.user_id, 0) + 1
        return c

    swaps = 0
    for _ in range(500):  # safety bound
        c = counts()
        max_uid = max(c, key=lambda k: c[k])
        min_uid = min(c, key=lambda k: c[k])
        if c[max_uid] - c[min_uid] <= 1:
            break
        moved = False
        for a in open_assigns:
            if a.user_id != max_uid:
                continue
            # Occurrence darf min_uid nicht schon enthalten (kein Duplikat).
            if any(other.user_id == min_uid for other in a.occurrence.assignments):
                continue
            a.user_id = min_uid
            moved = True
            swaps += 1
            break
        if not moved:
            break

    db.session.flush()
    return swaps


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
    """Drei Abwesenheiten: eine laufend, eine vergangen, eine zukuenftig."""
    alex = users.get("alex@wg.test")
    kylian = users.get("kylian@wg.test")
    ngya = users.get("ngya@wg.test")
    rows = []
    if alex:
        rows.append(Absence(user_id=alex.id, start_date=today - timedelta(days=2),
                            end_date=today + timedelta(days=3), reason="Urlaub"))
    if kylian:
        rows.append(Absence(user_id=kylian.id, start_date=today - timedelta(days=10),
                            end_date=today - timedelta(days=8), reason="Krank"))
    if ngya:
        rows.append(Absence(user_id=ngya.id, start_date=today + timedelta(days=5),
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

    maurice = users.get("maurice@wg.test")
    bishal = users.get("bishal@wg.test")
    alex = users.get("alex@wg.test")
    kylian = users.get("kylian@wg.test")
    reviewer = hauswarte[0] if hauswarte else None

    extras = 0
    honors = 0

    pending = [
        (maurice, "Keller entruempelt und Sperrmuell rausgebracht."),
        (bishal, "Eingangsbereich gefegt und Fussmatte ausgeklopft."),
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
        (alex, "Waschkueche komplett geputzt und Flusensieb gereinigt.", 10),
        (kylian, "Defekte Lampe im Flur repariert.", 6),
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

    if bishal is not None:
        db.session.add(ExtraContribution(
            user_id=bishal.id,
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
        users = {u.email: u for u in db.session.query(User).all() if u.email}
        hauswarte = _hauswarte(users)
        michael = users.get("michael.mauer@solveant.com")
        creator = michael or next(iter(users.values()))

        # Reihenfolge: Abwesenheiten ZUERST, damit assign_occurrence sie kennt.
        n_absences = _seed_absences(users, today)
        definitions = _create_definitions(creator)
        counts = _generate_and_animate(definitions, today, hauswarte)

        # Einmalige + Event-Aufgaben (NONE-Recurrence) zusaetzlich.
        one_shots, one_shot_assigns = _create_one_shot_occurrences(today, creator)
        counts["one_shots"] = len(one_shots)
        counts["assignments"] += one_shot_assigns

        # Demo-Affordance: Michael sieht garantiert "Heute" + "Aktueller Dienst".
        if michael is not None:
            by_title = {d.title: d for d in definitions}
            for title in ("Geschirrspueler ein-/ausraeumen", "Kuechendienst"):
                d = by_title.get(title)
                if d is not None:
                    _ensure_assigned(d, today, michael)

        # Fairness-Rebalancer: OPEN-Assignments (heute + Zukunft) gleichmaessig
        # auf alle 6 Bewohner verteilen.
        all_defs = definitions + one_shots
        residents = [u for u in users.values()
                     if any(r.role == Role.HAUSBEWOHNER for r in u.roles)]
        n_swaps = _rebalance_open_assignments(all_defs, today, residents)

        n_shopping = _seed_shopping(users, today)
        n_extras, n_honors = _seed_extras(users, hauswarte, today)

        db.session.commit()

        # Verteilung pro User loggen (Demo-Sanity-Check).
        per_user = {u.name: 0 for u in residents}
        for d in all_defs:
            for occ in d.occurrences:
                if occ.period_end < today:
                    continue
                for a in occ.assignments:
                    if a.status == AssignmentStatus.OPEN:
                        name = next((u.name for u in residents if u.id == a.user_id), None)
                        if name:
                            per_user[name] = per_user.get(name, 0) + 1

        print("Demo-Daten erzeugt (Fenster: "
              f"{today - timedelta(days=PAST_DAYS)} .. {today + timedelta(days=FUTURE_DAYS)})")
        print(f"  Accounts:        {len(users)}")
        print(f"  Aufgaben-Defs:   {len(definitions)} wiederkehrend + {len(one_shots)} einmalig/event")
        print(f"  Occurrences:     {counts['occurrences']} + {len(one_shots)} (einmalig)")
        print(f"  Zuweisungen:     {counts['assignments']}"
              f" (erledigt {counts['done']}, abgelehnt {counts['rejected']}, verpasst {counts['skipped']})")
        print(f"  Karma-Strafen:   {counts['penalties']}")
        print(f"  Ehrenpunkte:     {n_honors}")
        print(f"  Abwesenheiten:   {n_absences}")
        print(f"  Einkaufs-Items:  {n_shopping}")
        print(f"  Sonderleistungen:{n_extras}")
        print(f"  Rebalance-Swaps: {n_swaps}")
        print(f"  OPEN/Person (heute+Zukunft):")
        for name, n in sorted(per_user.items(), key=lambda kv: -kv[1]):
            print(f"    {name:18}{n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
