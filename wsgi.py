"""WSGI-Entry-Point fuer Produktion.

Aufruf auf dem Server (z.B. via systemd / Docker):

    gunicorn -w 3 -b 0.0.0.0:8000 wsgi:app

Wichtig: Im Gegensatz zu ``flask run`` (DevConfig) waehlt dieser Entrypoint
explizit ``create_app("prod")`` — also ``ProdConfig`` mit:

* ``DEBUG = False``
* ``DEV_LOGIN_ENABLED = False`` (kein passwortloser Bypass)
* ``SESSION_COOKIE_SECURE = True`` (Cookies nur via HTTPS — braucht TLS am Reverse-Proxy)

Damit das Cookies-Secure-Flag und der OAuth-Redirect-URI sauber funktionieren,
laeuft die App hinter einem Reverse-Proxy (Caddy/Nginx), und ``ProxyFix`` in der
Factory vertraut dessen ``X-Forwarded-Proto``/``X-Forwarded-For``-Headern.
"""

from app import create_app

app = create_app("prod")
