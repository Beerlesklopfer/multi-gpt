"""Health-Check /healthz/ (multigpt.health.HealthCheckMiddleware)."""

import logging
from unittest import mock

import pytest
from django.db import OperationalError

URL = "/healthz/"


@pytest.fixture
def db_down():
    with mock.patch("multigpt.health.connection") as conn:
        conn.ensure_connection.side_effect = OperationalError("weg")
        yield conn


@pytest.mark.django_db
def test_ok_without_login(client):
    response = client.get(URL)
    assert response.status_code == 200
    assert response.content == b"ok"
    assert response["Cache-Control"] == "no-store"


@pytest.mark.django_db
def test_head_ok_without_body(client):
    response = client.head(URL)
    assert response.status_code == 200
    assert response.content == b""


@pytest.mark.django_db
def test_foreign_host_header_allowed(client, settings):
    settings.ALLOWED_HOSTS = ["multigpt.example.lan"]
    response = client.get(URL, HTTP_HOST="10.0.0.5")
    assert response.status_code == 200
    assert response.content == b"ok"
    # Gegenprobe: Alle anderen Pfade prüfen den Host weiterhin.
    assert client.get("/konto/login/", HTTP_HOST="10.0.0.5").status_code == 400


def test_post_not_allowed(client, db_down):
    response = client.post(URL)
    assert response.status_code == 405
    assert response["Allow"] == "GET, HEAD"
    db_down.ensure_connection.assert_not_called()


def test_503_without_db_logs_warning_only(client, db_down, caplog):
    with caplog.at_level(logging.INFO):
        response = client.get(URL)
    assert response.status_code == 503
    assert response.content == b"db unavailable"
    assert response["Cache-Control"] == "no-store"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    warnings = [r for r in caplog.records if r.name == "multigpt.health"]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert warnings[0].exc_info is None
