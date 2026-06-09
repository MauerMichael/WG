"""PWA-Blueprint: Web-App-Manifest, Service-Worker und Offline-Seite.

Diese drei Endpunkte müssen OHNE Login erreichbar sein — der Browser holt
Manifest und Service-Worker anonym (schon auf der Login-Seite, und der SW läuft
sitzungsübergreifend). Sie sind darum in ``app.blueprints.auth._PUBLIC_ENDPOINTS``
freigeschaltet.

Der Service-Worker wird bewusst unter ``/sw.js`` (Root) ausgeliefert, nicht aus
``/static/js/``: ein SW kontrolliert nur Pfade unterhalb seines eigenen Pfads,
aus ``/static/js/`` heraus käme also kein App-weiter Scope (``/``) zustande.
"""

from __future__ import annotations

import hashlib
import json
import os

from flask import (
    Blueprint,
    current_app,
    render_template,
    url_for,
)

bp = Blueprint("pwa", __name__)

# Assets, deren Inhalt den Cache-Namen des Service-Workers bestimmt. Ändert sich
# eines davon (v.a. das bei jedem Build neu gebaute, NICHT content-gehashte
# output.css), bekommt der SW einen neuen Cache-Namen -> der Browser erkennt das
# Update, lädt neu vor und `activate` räumt den alten Cache. Pfade relativ zum
# Static-Ordner.
_CACHE_KEY_ASSETS = ("css/output.css", "js/htmx.min.js", "js/sw.js")


@bp.route("/manifest.webmanifest")
def manifest():
    """Liefert das Web-App-Manifest mit korrektem MIME-Type.

    Bewusst als Route (nicht statische Datei): so stimmt der
    ``application/manifest+json``-Type plattformunabhängig, und die Icon-Pfade
    werden über ``url_for`` aufgelöst.
    """
    data = {
        "id": "/",
        "name": "WG-Organisation",
        "short_name": "WG",
        "description": (
            "Aufgaben, Dienste, Einkauf und Abwesenheiten für eure WG – "
            "fair organisiert."
        ),
        "lang": "de",
        "dir": "ltr",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "display_override": ["standalone", "minimal-ui"],
        "background_color": "#f7f5fb",
        "theme_color": "#7c3aed",
        "categories": ["productivity", "lifestyle"],
        "icons": [
            {
                "src": url_for("static", filename="img/icons/icon-192.png"),
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": url_for("static", filename="img/icons/icon-512.png"),
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": url_for("static", filename="img/icons/icon-maskable-192.png"),
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "maskable",
            },
            {
                "src": url_for("static", filename="img/icons/icon-maskable-512.png"),
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable",
            },
        ],
    }
    resp = current_app.response_class(
        json.dumps(data, ensure_ascii=False),
        mimetype="application/manifest+json",
    )
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


def _cache_token() -> str:
    """SHA1-Kürzel über den Inhalt der cache-relevanten Assets.

    Verschiebt den SW-Cache-Namen automatisch bei jedem Asset-Wechsel, ohne
    Build-Pipeline / manuelles Hochzählen.
    """
    h = hashlib.sha1()
    for rel in _CACHE_KEY_ASSETS:
        path = os.path.join(current_app.static_folder, *rel.split("/"))
        try:
            with open(path, "rb") as fh:
                h.update(fh.read())
        except OSError:
            # Fehlt z.B. output.css (noch nicht gebaut) -> Token bleibt stabil.
            h.update(rel.encode())
    return h.hexdigest()[:12]


@bp.route("/sw.js")
def service_worker():
    """Liefert den Service-Worker unter Root-Scope, mit content-basiertem Cache-Namen.

    Der Platzhalter ``wg-static-v1`` in ``sw.js`` wird durch einen aus dem
    Asset-Inhalt abgeleiteten Token ersetzt -> ändern sich CSS/JS, ändern sich
    die SW-Bytes und der Browser zieht das Update inkl. sauberem Cache-Rollover.
    """
    sw_path = os.path.join(current_app.static_folder, "js", "sw.js")
    with open(sw_path, encoding="utf-8") as fh:
        source = fh.read()
    source = source.replace('"wg-static-v1"', f'"wg-static-{_cache_token()}"')

    resp = current_app.response_class(source, mimetype="text/javascript")
    resp.headers["Content-Type"] = "text/javascript; charset=utf-8"
    # Erlaubt App-weiten Scope, falls der Browser den Pfad streng prüft.
    resp.headers["Service-Worker-Allowed"] = "/"
    # SW-Skript nicht lange cachen, damit Updates zeitnah greifen.
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@bp.route("/offline")
def offline():
    """Offline-Fallback-Seite (vom Service-Worker vorgeladen)."""
    return render_template("pwa/offline.html")
