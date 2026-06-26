"""Aktualisiert die Beschreibungen aktiver TaskDefinitions auf bessere Vorlagen.

Heuristisch ueber Titel-Fragmente: passende Eintraege bekommen eine
detailliertere Beschreibung. Idempotent: schreibt nur, wenn sich die
Beschreibung tatsaechlich aendert.

Der zentrale Hauswart-WhatsApp-Hinweis wird NICHT in die Beschreibung
geschrieben — er kommt automatisch via Config (`HAUSWART_REPORT_NOTICE`) als
gelbe Box unter jeden Dienst-Kartenkopf. So muss er nicht bei jeder Definition
einzeln gepflegt werden.

Aufruf:
    .\\venv\\Scripts\\python.exe .\\scripts\\improve_descriptions.py

Auf der VPS:
    docker compose exec app python scripts/improve_descriptions.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models.task import TaskDefinition  # noqa: E402


# Titel-Fragment (lowercase, substring-match) -> verbesserte Beschreibung.
# Reihenfolge der Eintraege entscheidet bei mehreren Treffern (erster gewinnt).
# Standard-Footer fuer Kontroll-Dienste: explizit auch dann melden, wenn alles
# in Ordnung war — sonst kann der Hauswart's nicht entscheiden.
_REPORT_REMINDER = (
    " Nach der Kontrolle JEDES MAL Michael per WhatsApp melden — auch wenn "
    "alles in Ordnung ist (z.B. 'alles okay'). Ohne Meldung gilt der Dienst "
    "als nicht erledigt."
)


DESCRIPTION_TEMPLATES: list[tuple[str, str]] = [
    (
        "geschirrsp",
        "Abends nach dem Essen einraeumen und starten. Am naechsten Morgen "
        "ausraeumen und das saubere Geschirr zurueck in die Schraenke.",
    ),
    (
        "toilette",
        "Woechentliche Sichtkontrolle: Klobrille gereinigt, Klorolle voll, "
        "Buerste sauber, Boden gewischt. Bei Maengeln direkt mit dem letzten "
        "Zugewiesenen sprechen." + _REPORT_REMINDER,
    ),
    (
        "kueche",
        "Taegliche Sichtkontrolle: Spuele leer, Arbeitsflaeche sauber, Muell "
        "nicht ueberfuellt, Geschirrspueler bedient. Maengel direkt mit "
        "Verursacher klaeren." + _REPORT_REMINDER,
    ),
    (
        "muell",
        "Muelltonnen zum richtigen Termin zur Strasse stellen und nach "
        "Abholung wieder reinholen. Gelben Sack laut Plan vorbereiten." + _REPORT_REMINDER,
    ),
    (
        "bar",
        "Woechentliche Bar-Kontrolle: Tresen gewischt, Flaschen sortiert, "
        "Glas-Muell geleert, Boden trocken." + _REPORT_REMINDER,
    ),
    (
        "bad",
        "Woechentliche Bad-Kontrolle: Dusche/Wanne sauber, Spiegel klar, "
        "Boden trocken, Muell leer." + _REPORT_REMINDER,
    ),
    (
        "fernseh",
        "Wohnzimmer/Fernseher-Bereich kontrollieren: Sofa-Decken gefaltet, "
        "Couchtisch frei, Fernbedienungen am Platz, Boden gewischt." + _REPORT_REMINDER,
    ),
    (
        "saal",
        "Gemeinschafts-Saal kontrollieren: Stuehle eingestellt, Tische "
        "abgewischt, Muell leer, Boden gewischt, Licht aus, Tueren zu." + _REPORT_REMINDER,
    ),
    (
        "waesche",
        "Waeschekoerbe leeren und in den Waescheraum bringen. Vergessene "
        "Waesche aus der Maschine in den Trockner umlegen oder dem Besitzer "
        "kurz Bescheid geben." + _REPORT_REMINDER,
    ),
    (
        "flur",
        "Flur und Treppenhaus wischen. Keine Schuhe oder Pakete laenger als "
        "2 Tage stehen lassen." + _REPORT_REMINDER,
    ),
    (
        "wohnzimmer",
        "Wohnzimmer aufgeraeumt: Sofa-Decken gefaltet, Tisch frei, Muell "
        "leer, Boden gewischt." + _REPORT_REMINDER,
    ),
    (
        "balkon",
        "Balkon kontrollieren: Stuehle und Tisch aufgeraeumt, Aschenbecher "
        "geleert, Boden gefegt." + _REPORT_REMINDER,
    ),
    (
        "keller",
        "Keller-Sichtkontrolle: keine herumliegenden Sachen, Licht aus, "
        "Tuere abgeschlossen." + _REPORT_REMINDER,
    ),
    (
        "fenster",
        "Fenster putzen (innen). Vorher Insektenschutz pruefen, danach "
        "Lueftung kurz oeffnen." + _REPORT_REMINDER,
    ),
]


def _find_template(title: str) -> str | None:
    t = (title or "").lower()
    for fragment, template in DESCRIPTION_TEMPLATES:
        if fragment in t:
            return template
    return None


def main() -> int:
    app = create_app("dev")
    with app.app_context():
        definitions = list(db.session.query(TaskDefinition).all())
        updated = 0
        skipped = 0
        no_match = 0
        for d in definitions:
            new_desc = _find_template(d.title)
            if new_desc is None:
                no_match += 1
                continue
            if (d.description or "").strip() == new_desc:
                skipped += 1
                continue
            print(f"[update] {d.title}: {new_desc[:60]}...")
            d.description = new_desc
            updated += 1
        db.session.commit()
        print(
            f"\n{updated} aktualisiert, {skipped} schon aktuell, "
            f"{no_match} ohne Vorlage (Titel matched nicht)."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
