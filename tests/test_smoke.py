"""Smoke-Test: App bootet und der Auth-Guard greift fuer anonyme Zugriffe auf /."""

from __future__ import annotations

from flask.testing import FlaskClient


def test_root_redirects_anonymous_to_login(client: FlaskClient) -> None:
    response = client.get("/")
    assert response.status_code == 302
    assert "/auth/login" in response.headers["Location"]


def test_login_page_renders(client: FlaskClient) -> None:
    response = client.get("/auth/login")
    assert response.status_code == 200
    assert "Anmelden".encode() in response.data
