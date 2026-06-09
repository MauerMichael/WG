"""Konfigurations-Klassen für Dev und Prod.

Liest Werte aus Umgebungsvariablen (gefüllt aus `.env`). Die Datenbank-URL
wird automatisch auf den `pg8000`-Dialekt umgeschrieben, da auf Windows ARM64
kein psycopg verfügbar ist.
"""

from __future__ import annotations

import os


def _normalize_db_url(url: str | None) -> str | None:
    if not url:
        return None
    # SQLAlchemy braucht den expliziten Dialect-Hint, damit pg8000 statt
    # psycopg geladen wird.
    if url.startswith("postgresql://"):
        return "postgresql+pg8000://" + url[len("postgresql://"):]
    if url.startswith("postgres://"):
        return "postgresql+pg8000://" + url[len("postgres://"):]
    return url


class BaseConfig:
    SECRET_KEY: str = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    SQLALCHEMY_DATABASE_URI: str | None = _normalize_db_url(os.environ.get("DATABASE_URL"))
    SQLALCHEMY_TRACK_MODIFICATIONS: bool = False

    # Connection-Pool: Die DB liegt remote auf Hostinger -> jede tote
    # Idle-Connection würde sonst einen mehrsekündigen Hänger verursachen.
    # `pool_pre_ping` validiert die Connection vor jedem Checkout.
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 1800,
        "pool_size": 5,
        "max_overflow": 10,
    }

    # OAuth (Welle 2 füllt das vollständig aus).
    GOOGLE_CLIENT_ID: str | None = os.environ.get("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET: str | None = os.environ.get("GOOGLE_CLIENT_SECRET")

    # SMTP (Welle 2 / Reminders).
    SMTP_HOST: str | None = os.environ.get("SMTP_HOST")
    SMTP_PORT: int = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USER: str | None = os.environ.get("SMTP_USER")
    SMTP_PASSWORD: str | None = os.environ.get("SMTP_PASSWORD")
    SMTP_FROM: str | None = os.environ.get("SMTP_FROM")

    # Cookies / Session.
    SESSION_COOKIE_SAMESITE: str = "Lax"
    SESSION_COOKIE_HTTPONLY: bool = True

    # Dev-Login (ohne Google). Erlaubt Einloggen als beliebiger User per Klick.
    # Niemals in Prod aktivieren.
    DEV_LOGIN_ENABLED: bool = False


class DevConfig(BaseConfig):
    DEBUG: bool = True
    SESSION_COOKIE_SECURE: bool = False
    DEV_LOGIN_ENABLED: bool = True


class ProdConfig(BaseConfig):
    DEBUG: bool = False
    # SECURE-Cookie standardmaessig an; ueber Env abschaltbar, falls die App
    # (noch) ohne HTTPS lauft (Browser blockt sonst die Session ueber HTTP und
    # Flask-WTF wirft "CSRF session token is missing"). In .env:
    # SESSION_COOKIE_SECURE=false
    SESSION_COOKIE_SECURE: bool = (
        os.environ.get("SESSION_COOKIE_SECURE", "true").lower() != "false"
    )
    DEV_LOGIN_ENABLED: bool = False
