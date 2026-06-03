# WG-Organise Production Image
# Single-stage Python 3.12-slim. Klein genug fuer eine 5-User-WG-App.

FROM python:3.12-slim

# Minimale System-Abhaengigkeiten: curl fuer den Tailwind-Binary-Download
# beim Image-Build, ca-certificates fuer TLS-Trust.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python-Deps zuerst (Layer-Caching: aendert sich requirements.txt nicht,
# wird dieser Layer beim Rebuild nicht neu gebaut).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Tailwind-Standalone-Binary holen und ausfuehrbar machen.
RUN curl -fsSL \
        https://github.com/tailwindlabs/tailwindcss/releases/latest/download/tailwindcss-linux-x64 \
        -o /usr/local/bin/tailwindcss \
    && chmod +x /usr/local/bin/tailwindcss

# App-Code (Reihenfolge so, dass App-Edits nicht die Dep-Layer invalidieren).
COPY app/ ./app/
COPY migrations/ ./migrations/
COPY scripts/ ./scripts/
COPY wsgi.py alembic.ini tailwind.config.js ./

# CSS im Image bauen → zur Laufzeit kein Build noetig.
RUN tailwindcss \
        -i app/static/css/input.css \
        -o app/static/css/output.css \
        --minify

ENV PYTHONUNBUFFERED=1 \
    FLASK_APP=app

EXPOSE 8000

# gunicorn: 3 Worker reicht fuer eine WG. Logs auf stdout/stderr fuer
# `docker logs` und systemd-journal.
CMD ["gunicorn", "-w", "3", "-b", "0.0.0.0:8000", \
     "--access-logfile", "-", "--error-logfile", "-", \
     "wsgi:app"]
