"""Punkte- und Score-Konstanten.

Diese Werte werden vom Fairness-Algorithmus in ``app.services.scheduling``
genutzt. Sie hier zentralisiert zu halten erleichtert spätere Tuning-Runden.
"""

from __future__ import annotations

# Standard-Schwierigkeit, wenn der User keine angibt.
DEFAULT_DIFFICULTY_POINTS = 1

# Untergrenzen / Obergrenzen für das Formular.
MIN_DIFFICULTY_POINTS = 1
MAX_DIFFICULTY_POINTS = 10

# Fenstergröße des rollenden Scores für TASK-Punkte in Tagen. Auch die untere
# Schranke für `days_active` (Tenure-Normalisierung, siehe `effective_score`).
SCORE_WINDOW_DAYS = 90

# Lebensdauer eines Ehrenpunkts (HONOR, positiv) in Tagen — eigenes Fenster,
# unabhängig vom Task-Punkte-Fenster.
HONOR_LIFESPAN_DAYS = 80

# Lebensdauer eines Negativ-Karma-Punkts (PENALTY) in Tagen. Strafen „verzeihen"
# schneller als positive Leistung wirkt.
PENALTY_LIFESPAN_DAYS = 40

# Harte Obergrenze gleichzeitiger OPEN-Zuweisungen pro Person. Verhindert, dass
# eine Person mit stark negativem Karma den ganzen Haushalt zugeschoben bekommt:
# Wer am Limit ist, wird beim Verteilen ans Ende gereiht (nicht hart entfernt,
# damit eine Occurrence nie an Abwesende fällt, solange Anwesende da sind).
MAX_OPEN_ASSIGNMENTS_PER_USER = 5

# Weicher Burst-Schutz: Wer schon so viele OPEN-Zuweisungen hält, wird beim
# Verteilen HINTER alle noch unbelasteten Kandidaten gereiht (aber vor die am
# harten Limit). Bremst das „Newbie-/Negativ-Karma sammelt in einer Runde alles
# auf einmal"-Problem, ohne die Fairness-Reihenfolge innerhalb der Stufen zu
# brechen. Muss < MAX_OPEN_ASSIGNMENTS_PER_USER sein.
SOFT_CAP_OPEN_ASSIGNMENTS = 2

# Wie viele Perioden im Voraus `generate_occurrences` materialisiert (Perioden-
# Mindest-Floor — gibt langen Intervallen großzügigen Vorlauf).
DEFAULT_LOOKAHEAD_PERIODS = 2

# Garantierter Mindest-Zeit-Horizont in Kalendertagen, UNABHÄNGIG vom Recurrence-
# Typ. Stellt sicher, dass jede Aufgabe mindestens 1 Woche im Voraus materialisiert
# + zugewiesen ist — auch tägliche oder kurz-intervallige (wo `lookahead_periods`
# allein zu wenig Vorlauf gäbe). Später leicht auf 14/30 erhöhbar; bei Werten, die
# kein Vielfaches von 7 sind, können WEEKLY-Counts kalenderabhängig schwanken.
MIN_NOTICE_DAYS = 7
