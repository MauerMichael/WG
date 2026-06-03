"""Shopping-Blueprint: geteilte Einkaufsliste.

Routen:
- ``GET  /shopping``                — Offen + Erledigt (letzte 14 Tage).
- ``POST /shopping``                — Item hinzufügen (HTMX-fähig).
- ``POST /shopping/<id>/check``     — Als erledigt markieren (HTMX).
- ``POST /shopping/<id>/uncheck``   — Erledigt-Status zurücknehmen (HTMX).
- ``POST /shopping/<id>/delete``    — Item löschen (Owner oder Admin/Hauswart).
"""

from flask import Blueprint

bp = Blueprint(
    "shopping",
    __name__,
    template_folder="../../templates/shopping",
)

from app.blueprints.shopping import routes  # noqa: E402,F401
