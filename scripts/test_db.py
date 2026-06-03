"""Schneller DB-Connection-Test. Lädt .env, baut eine Verbindung auf,
führt SELECT version() aus und gibt das Ergebnis zurück."""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

import pg8000.native  # noqa: E402

host = os.environ["POSTGRES_HOST"]
port = int(os.environ["POSTGRES_PORT"])
user = os.environ["POSTGRES_USER"]
password = os.environ["POSTGRES_PASSWORD"]
database = os.environ["POSTGRES_DB"]

print(f"Connecting to {user}@{host}:{port}/{database} ...")

try:
    conn = pg8000.native.Connection(
        user=user,
        password=password,
        host=host,
        port=port,
        database=database,
        timeout=10,
    )
except Exception as e:
    print(f"FAILED: {type(e).__name__}: {e}")
    sys.exit(1)

try:
    version = conn.run("SELECT version()")[0][0]
    current_db = conn.run("SELECT current_database()")[0][0]
    current_user = conn.run("SELECT current_user")[0][0]
    print("OK")
    print(f"  current_database: {current_db}")
    print(f"  current_user:     {current_user}")
    print(f"  server_version:   {version}")
finally:
    conn.close()
