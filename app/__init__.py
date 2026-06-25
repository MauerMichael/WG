"""Flask Application-Factory.

Lädt `.env` beim Import, baut die App, initialisiert Extensions und
registriert alle Feature-Blueprints.
"""

from __future__ import annotations

import os
import uuid
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

# .env so früh wie möglich laden, damit Config-Klassen die Werte sehen.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from app.config import DevConfig, ProdConfig  # noqa: E402
from app.extensions import db, login_manager  # noqa: E402

csrf = CSRFProtect()

_CONFIGS = {
    "dev": DevConfig,
    "prod": ProdConfig,
}


def create_app(config_name: str = "dev") -> Flask:
    app = Flask(__name__)
    app.config.from_object(_CONFIGS[config_name])

    # Pool-Optionen sind für die Remote-Postgres gedacht. SQLite (Tests) nutzt
    # SingletonThreadPool und lehnt `pool_size`/`max_overflow` ab -> für SQLite
    # entfernen, damit die Test-Engine baubar bleibt.
    uri = app.config.get("SQLALCHEMY_DATABASE_URI") or ""
    if uri.startswith("sqlite"):
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {}

    # Modelle importieren, damit SQLAlchemy / Alembic sie kennt.
    from app import models  # noqa: F401

    db.init_app(app)
    login_manager.init_app(app)
    # CSRF-Schutz: Forms brauchen ein verstecktes csrf_token; HTMX-Requests
    # schicken den Token via X-CSRFToken-Header (siehe base.html). In Tests
    # via WTF_CSRF_ENABLED=False (conftest.py) deaktiviert.
    csrf.init_app(app)

    # ProxyFix: NUR aktivieren wenn die App tatsaechlich hinter einem
    # Reverse-Proxy (Caddy/Nginx/Traefik) laeuft. Ohne Proxy davor wuerde
    # ProxyFix nichts oder falsche X-Forwarded-Header lesen — z.B. Mobile-
    # Clients senden andere Header und Flask wuerde dann ggf. faelschlich
    # https-Cookies setzen. In .env explizit `USE_PROXY_FIX=true` setzen,
    # sobald ein Reverse-Proxy davor steht.
    if os.environ.get("USE_PROXY_FIX", "false").lower() == "true":
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    # Browser-Cache fuer Static-Assets (output.css ~52 KB, htmx.min.js ~47 KB):
    # 7 Tage. Bei dringender Aktualisierung im Browser per Strg+Shift+R neu
    # ziehen — beim naechsten Tailwind-Rebuild aendert sich der Inhalt eh.
    app.config.setdefault("SEND_FILE_MAX_AGE_DEFAULT", 7 * 24 * 60 * 60)

    # Google-OAuth wurde komplett entfernt (Username+Passwort statt OAuth).
    # ``google_sub`` bleibt als Spalte erhalten, ist aber ungenutzt.

    # Flask-Login konfigurieren.
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Bitte melde dich an."
    login_manager.login_message_category = "info"

    from sqlalchemy.orm import joinedload

    from app.models.user import User

    @login_manager.user_loader
    def load_user(user_id: str) -> "User | None":
        try:
            # Rollen eager-laden: der Auth-Guard liest `current_user.roles` bei
            # jedem Request -> sonst eine zweite Query pro Request (Netzwerk-
            # Roundtrip zur Remote-DB).
            return db.session.get(
                User, uuid.UUID(user_id), options=[joinedload(User.roles)]
            )
        except (ValueError, TypeError):
            return None

    # Blueprints registrieren.
    from app.blueprints.absences import bp as absences_bp
    from app.blueprints.admin import bp as admin_bp
    from app.blueprints.auth import bp as auth_bp
    from app.blueprints.dashboard import bp as dashboard_bp
    from app.blueprints.extras import bp as extras_bp
    from app.blueprints.hauswart import bp as hauswart_bp
    from app.blueprints.pwa import bp as pwa_bp
    from app.blueprints.shopping import bp as shopping_bp
    from app.blueprints.tasks import bp as tasks_bp

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(tasks_bp, url_prefix="/tasks")
    app.register_blueprint(absences_bp, url_prefix="/absences")
    app.register_blueprint(shopping_bp, url_prefix="/shopping")
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(hauswart_bp, url_prefix="/hauswart")
    app.register_blueprint(extras_bp, url_prefix="/extras")
    # PWA: Manifest, Service-Worker, Offline-Seite (Routen am Root: /sw.js etc.).
    app.register_blueprint(pwa_bp)

    # Globale Auth-Guard: nur eingeloggte+approved User dürfen ins App-Innere.
    from app.blueprints.auth import register_auth_guard

    register_auth_guard(app)

    # `today` global in allen Templates verfügbar (z.B. um den „Erledigt"-Button
    # für noch nicht begonnene Perioden zu sperren). Views, die `today` explizit
    # übergeben, überschreiben diesen Wert.
    @app.context_processor
    def _inject_today() -> dict:
        return {"today": date.today()}

    # `asset_version` global in allen Templates: kurzer Content-Hash ueber die
    # cache-relevanten Static-Assets. Wird in base.html als ?v=<hash> an
    # output.css/htmx.min.js gehaengt -> aenderung des Build-Inhalts bricht den
    # Browser-Cache automatisch. Nutzt denselben Token wie der Service-Worker
    # (app.blueprints.pwa._cache_token), sodass beide synchron updaten.
    @app.context_processor
    def _inject_asset_version() -> dict:
        try:
            from app.blueprints.pwa import _cache_token
            return {"asset_version": _cache_token()}
        except Exception:
            return {"asset_version": ""}

    # `pending_review_count` global verfügbar für das Nav-Badge an Verwaltung.
    # Cheap COUNT-Query, nur ausgeführt wenn der User Hauswart/Admin ist.
    from flask_login import current_user as _current_user

    from app.blueprints.auth import user_has_any_role
    from app.domain.enums import Role
    from app.services import scheduling

    @app.context_processor
    def _inject_pending_review_count() -> dict:
        try:
            if not _current_user.is_authenticated:
                return {"pending_review_count": 0}
            if not user_has_any_role(_current_user, Role.HAUSWART, Role.ADMIN):
                return {"pending_review_count": 0}
            return {
                "pending_review_count": scheduling.review_queue_count(db.session)
            }
        except Exception:
            return {"pending_review_count": 0}

    return app
