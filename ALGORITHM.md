# ALGORITHM.md — Verteil-Algorithmus (Fairness + Karma)

Diese Datei ist die **maßgebliche Regel** für die Aufgabenverteilung. Code lebt in
`app/services/scheduling.py`, Konstanten in `app/domain/points.py`. Bei Änderungen
am Algorithmus: erst diese Datei aktualisieren, dann den Code.

> **Status: vollständig implementiert (2026-05-28).** Karma-Scoring, Cap,
> automatischer + manueller Penalty UND die Extra-Aufgaben/Ehrenpunkte-UI sind
> gebaut und getestet (`tests/test_karma.py`, `tests/test_extras.py`). Die
> Migrationen sind gegen die Remote-DB angewendet (`alembic current` →
> `b7e2d4a6c9f1`). **Offen** nur noch: das Schwierigkeits-Routing (s.u.).

## Grundidee

Jeder Bewohner hat einen **Verteil-Score**. Aufgaben gehen an die Person(en) mit
dem **niedrigsten** Score. Wer wenig getan hat (oder Negativ-Karma angesammelt
hat), kommt zuerst dran; wer viel getan oder Ehrenpunkte gesammelt hat, kommt
seltener dran.

## Score-Formel

Pro User (in `effective_scores_for`):

```
raw   = Σ Task-Punkte (DONE, ≤ 90 Tage)
      + Σ Ehrenpunkte (HONOR, ≤ 80 Tage)
      − Σ Strafen      (PENALTY, ≤ 40 Tage)
score = raw / max(days_active, SCORE_WINDOW_DAYS) * SCORE_WINDOW_DAYS
```

- **Task-Punkte** = `TaskAssignment.points_earned` aus DONE-Zuweisungen.
- **Ehrenpunkte** = HONOR-`KarmaEvent`s (vom Hauswart vergeben).
- **Strafen** = PENALTY-`KarmaEvent`s (nicht oder schlecht erledigt).
- **Tenure-Normalisierung** (unverändert): ein Neuzugang wird durch
  `SCORE_WINDOW_DAYS` (90) statt durch seine wenigen Tatsächlich-Tage geteilt,
  damit eine einzelne Aufgabe an Tag 1 ihn nicht nach oben katapultiert.

**Sortierung der Kandidaten:** `score ASC`, Tiebreak `last_assigned_at ASC` (wer
länger nicht zugewiesen wurde zuerst), dann `user_id`. Dann obersten N picken
(`TaskDefinition.required_assignees`), danach das Cap (s.u.) anwenden.

`last_assigned_at` wird nach der Zuweisung auf „jetzt" gesetzt — **außer** bei
einer Notnagel-Zuweisung *während eigener Abwesenheit* (`assigned_during_absence`):
sonst würde die abwesende Person beim Tiebreak unverdient nach hinten rutschen,
obwohl sie nie eine faire Runde hatte.

**Wirkung:**

- **Negativ-Karma senkt den Score** (er darf negativ werden) → die Person rutscht
  nach vorne → bekommt **öfter** Aufgaben.
- **Ehrenpunkte heben den Score** → die Person rutscht nach hinten → bekommt
  **seltener** Aufgaben. Ehrenpunkte **verrechnen Negativ-Karma direkt**, weil
  beide in derselben Summe stehen (Plus gegen Minus).

## Decay (pro Event, harte Kante)

Jeder Punkt zählt voll bis zu seiner Lebensdauer **ab `occurred_at`**, danach 0:

- **Strafe (PENALTY):** **40 Tage** (`PENALTY_LIFESPAN_DAYS`).
- **Ehrenpunkt (HONOR):** **80 Tage** (`HONOR_LIFESPAN_DAYS`).
- **Task-Punkt:** **90 Tage** (`SCORE_WINDOW_DAYS`, unverändert beibehalten).

Negativ-Karma „verzeiht“ damit am schnellsten (40 Tage) — ein einmaliger
Ausrutscher hängt nicht ewig nach. Die Task-Punkte behalten bewusst ihr
bestehendes 90-Tage-Fenster (additive Erweiterung, keine Verhaltensänderung am
bestehenden Score).

> **Optionale Verfeinerung (nicht v1):** statt harter Kante (voll → 0) ein
> lineares Abklingen über die Lebensdauer. Wäre im Score-Reader eine
> Gewichtungs-Zeile, sonst keine Strukturänderung.

## Cap gegen „ganzer Haushalt“

Damit eine Person mit stark negativem Karma nicht den ganzen Haushalt
zugeschoben bekommt, begrenzt das Cap die **Zuweisungsrate**, nicht den
Karma-Wert:

- `MAX_OPEN_ASSIGNMENTS_PER_USER` (**5**, tunebar): Wer so viele gleichzeitige
  **OPEN**-Zuweisungen hält, wird in der Kandidatenliste **ans Ende gereiht**.
- `SOFT_CAP_OPEN_ASSIGNMENTS` (**2**, tunebar): weicher Burst-Schutz. Wer schon
  so viele OPEN-Zuweisungen hält, wird **hinter** alle noch unbelasteten
  Kandidaten gereiht (aber **vor** die am harten Limit). Damit sammelt eine
  Person (frischer Neuzugang mit Score 0, oder stark negatives Karma) nicht in
  einer einzigen Verteil-Runde alles auf einmal auf.
- `_order_by_cap` sortiert die fairness-sortierte Liste also in **drei Stufen**
  (0 = unbelastet, 1 = ≥ Soft-Cap, 2 = ≥ Hard-Cap); die Fairness-Reihenfolge
  bleibt **innerhalb** jeder Stufe erhalten (stabiler Sort).
- **Kein hartes Entfernen:** Sind *alle* Anwesenden am Limit, wird trotzdem an
  Anwesende zugewiesen (statt an Abwesende oder gar nicht). Das Cap ist also eine
  starke Priorisierung, kein Ausschluss.
- Der **Karma-Wert selbst bleibt ungedeckelt** — das überschüssige Minus wirkt
  weiter, sobald Slots frei werden.
- Greift in `assign_occurrence` und `reassign_open_overlap`.

## Vorlauf-Garantie (Materialisierung)

`generate_occurrences` materialisiert je Definition so viele Perioden, dass
**beide** Mindestgrenzen erfüllt sind: mindestens `DEFAULT_LOOKAHEAD_PERIODS`
Perioden (Floor, großzügig bei langen Intervallen) **und** mindestens der
`MIN_NOTICE_DAYS`-Tage-Horizont (jede Periode, die in den nächsten
`MIN_NOTICE_DAYS` Tagen *startet*, wird erzeugt). Damit ist **jede** Aufgabe —
auch eine tägliche — garantiert **≥ 1 Woche im Voraus** zugewiesen. Die Garantie
hält nur, wenn `scripts/generate_occurrences.py` **regelmäßig (täglich)** läuft.

## Abwesenheit & Umverteilung

Trägt sich jemand als abwesend ein (oder togglet einen Tag), ruft der
Absences-Blueprint `reassign_open_overlap(..., skip_dienst=True)`:

- **AUFGABE** (abhakbar): wird an anwesende Bewohner **umverteilt** (Co-Assignees
  bleiben; nur OPEN; DONE/SKIPPED unangetastet).
- **DIENST** (Zeitraum-Verantwortung): bleibt bei der abwesenden Person und wird
  **normal behandelt** — er läuft weiter, landet am Periodenende in der
  Hauswart-Review-Queue und kann bei Nicht-Erledigung wie sonst eine Strafe
  auslösen. Dienste werden also **nicht** wegen Abwesenheit delegiert.
- **Entfernen eines Users** (dauerhaft, `reassign_all_open_for`) ruft mit
  `skip_dienst=False` → hier wird **alles** umverteilt, auch Dienste, weil
  niemand mehr da ist, der sie übernehmen könnte.

## Negativ-Karma — Auslöser

Beide Wege erzeugen ein PENALTY-`KarmaEvent` (40-Tage-Lebensdauer) in Höhe des
**entgangenen Punkte-Anteils** (`difficulty_points / anzahl_assignees`, mind. 1,
via `_assignment_point_share`):

1. **Automatisch:** Eine Occurrence ist überfällig (`period_end < heute`) und die
   Zuweisung noch `OPEN` → Assignment wird auf `SKIPPED` gesetzt + PENALTY. Läuft
   als Cron-Skript `scripts/apply_overdue_penalties.py` (Service:
   `apply_overdue_penalties`). Idempotent (SKIPPED-Zeilen werden nicht erneut
   bestraft); sind danach alle Assignments einer Occurrence SKIPPED, wird die
   Occurrence selbst SKIPPED. **Ausnahme:** Wurde die Zuweisung nur als Notnagel
   *während der eigenen Abwesenheit* vergeben (`assigned_during_absence = True`),
   wird die Occurrence zwar SKIPPED, aber **keine Strafe** gebucht — die Person
   konnte die Aufgabe gar nicht erledigen.
2. **Manuell:** Der Hauswart lehnt eine erledigte Aufgabe ab
   (`review_assignment(approved=False)`, „schlecht gemacht“) → Punkte werden auf 0
   gesetzt **und** ein PENALTY gebucht. Der Penalty wird nur beim *Übergang* nach
   REJECTED gebucht (kein Doppel-Penalty bei erneutem Review).

## Ehrenpunkte / Extra-Aufgaben

End-to-end gebaut (Blueprint `extras`, Service `app/services/contributions.py`):

1. Bewohner reicht unter `/extras` eine Sonderleistung mit Beschreibung ein
   (`submit_contribution` → `ExtraContribution`, Status PENDING).
2. Hauswart/Admin sehen auf derselben Seite einen Prüf-Bereich und vergeben beim
   Genehmigen Ehrenpunkte (`approve_contribution` → `award_honor` → HONOR-
   `KarmaEvent`, positiv, 80 Tage) oder lehnen ab (`reject_contribution`, keine
   Punkte). Genehmigen ist idempotent (keine doppelten Ehrenpunkte).
- **Wirkung:** gleicht Negativ-Karma aus und schiebt — im Plus — die Person nach
  hinten (seltener dran). `award_honor` bleibt der zentrale Andock-Punkt.

## Offen / später

- **Schwierigkeits-Routing:** höhere `difficulty_points` gezielt an Personen mit
  viel Negativ-Karma. Braucht perioden-weites Matching statt des heutigen
  per-Occurrence-Greedy. Bewusst aus v1 ausgeklammert.
- **Akzeptierte Lücke:** ist in einer Periode niemand verfügbar (auch kein
  abwesender Fallback), bleibt die Occurrence unbesetzt.

## Umgesetzte Bausteine

- **Konstanten** (`app/domain/points.py`): `HONOR_LIFESPAN_DAYS = 80`,
  `PENALTY_LIFESPAN_DAYS = 40`, `MAX_OPEN_ASSIGNMENTS_PER_USER = 5`,
  `SCORE_WINDOW_DAYS = 90` (Task-Punkte + Tenure-Norm).
- **Enum** `KarmaKind {HONOR, PENALTY}` (`app/domain/enums.py`).
- **Modell** `KarmaEvent` (`app/models/karma.py`, Tabelle `karma_events`):
  `user_id, kind, points, occurred_at, created_by_id?, note?, occurrence_id?`.
- **Service** (`app/services/scheduling.py`): `effective_scores_for` (Karma-Summen),
  `award_honor`, `record_penalty`, `_order_by_cap`, `apply_overdue_penalties`,
  Penalty-Integration in `review_assignment`.
- **Migration:** `migrations/versions/f3a9c1d2b4e7_karma_events.py` — **noch nicht**
  gegen die Remote-DB ausgeführt (`alembic upgrade head` steht aus).
