"""Tests für die PWA-Endpunkte (Manifest, Service-Worker, Offline-Seite).

Alle drei müssen ANONYM (ohne Login) erreichbar sein — der Auth-Guard darf sie
nicht abfangen — und die korrekten Content-Types/Header liefern.
"""

from __future__ import annotations

import json


def test_manifest_anonymous_and_mime(client):
    resp = client.get("/manifest.webmanifest")
    assert resp.status_code == 200
    assert resp.mimetype == "application/manifest+json"
    data = json.loads(resp.get_data(as_text=True))
    assert data["name"] == "WG-Organisation"
    assert data["start_url"] == "/"
    assert data["scope"] == "/"
    assert data["display"] == "standalone"
    # Mindestens je ein 192/512-Icon und ein maskable-Icon.
    purposes = {icon["purpose"] for icon in data["icons"]}
    sizes = {icon["sizes"] for icon in data["icons"]}
    assert "any" in purposes and "maskable" in purposes
    assert {"192x192", "512x512"} <= sizes


def test_service_worker_root_scope_header(client):
    resp = client.get("/sw.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["Content-Type"]
    # Muss Root-Scope erlauben, sonst kontrolliert der SW nicht die ganze App.
    assert resp.headers.get("Service-Worker-Allowed") == "/"
    body = resp.get_data(as_text=True)
    assert "addEventListener" in body


def test_service_worker_cache_name_is_content_busted(client):
    """Der Platzhalter wird durch einen content-abhängigen Token ersetzt."""
    body = client.get("/sw.js").get_data(as_text=True)
    # Platzhalter ist weg, ein abgeleiteter Cache-Name ist drin.
    assert '"wg-static-v1"' not in body
    assert "wg-static-" in body


def test_offline_page_anonymous(client):
    resp = client.get("/offline")
    assert resp.status_code == 200
    assert "Gerade offline" in resp.get_data(as_text=True)


def test_login_page_links_manifest_and_registers_sw(client):
    """Selbst die (öffentliche) Login-Seite trägt Manifest + SW-Registrierung."""
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'rel="manifest"' in html
    assert "/manifest.webmanifest" in html
    assert "serviceWorker" in html
    assert "/sw.js" in html
    assert '<meta name="theme-color" content="#7c3aed">' in html
