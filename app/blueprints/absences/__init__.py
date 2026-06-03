"""Absences-Blueprint: Abwesenheits-Kalender.

Routen:
- ``GET  /absences``                  — Monats-Grid (Bewohner = Zeilen, Tage =
  Spalten, grün=anwesend/rot=abwesend) mit klickbaren Zellen.
- ``POST /absences/toggle``           — Toggelt einen Tag an/abwesend (HTMX);
  stanzt Tage aus Ranges heraus bzw. legt 1-Tages-Absences an.
- ``GET  /absences/new``              — Formular für längere/geplante Abwesenheit.
- ``POST /absences``                  — Anlegen + Reassignment-Trigger.
- ``POST /absences/<id>/delete``      — Löschen (Owner oder Hauswart/Admin).
"""

from flask import Blueprint

bp = Blueprint(
    "absences",
    __name__,
    template_folder="../../templates/absences",
)

# Routen werden in `routes.py` definiert und beim Import an `bp` gehängt.
from app.blueprints.absences import routes  # noqa: E402,F401
