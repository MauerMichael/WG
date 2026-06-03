"""Singletons für Flask-Extensions.

Diese Instanzen leben modulglobal und werden in `create_app()` an die App
gebunden. So können Blueprints und Modelle sie ohne Zirkular-Importe nutzen.
"""

from __future__ import annotations

from authlib.integrations.flask_client import OAuth
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
login_manager = LoginManager()
oauth = OAuth()
