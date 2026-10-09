"""Tests für Meilenstein 1: Login, Logout, Passwort, Health-Check, pgvector."""

from unittest import mock

import pytest
from django.conf import settings
from django.db import OperationalError, connection
from django.urls import reverse

pytestmark = pytest.mark.django_db

LOGIN = reverse("login")


def test_anonym_wird_zum_login_umgeleitet(client):
    antwort = client.get(reverse("chat:index"))
    assert antwort.status_code == 302
    assert antwort.url.startswith(LOGIN)


def test_login_mit_richtigem_passwort(client, nutzer, passwort):
    antwort = client.post(LOGIN, {"username": nutzer.username, "password": passwort})
    assert antwort.status_code == 302
    assert antwort.url == reverse("chat:index")
    assert client.get(antwort.url).status_code == 200


def test_login_mit_falschem_passwort(client, nutzer):
    antwort = client.post(LOGIN, {"username": nutzer.username, "password": "falsch"})
    assert antwort.status_code == 200
    assert "_auth_user_id" not in client.session


def test_sperre_nach_zu_vielen_fehlversuchen(client, nutzer, passwort):
    for _ in range(settings.AXES_FAILURE_LIMIT):
        client.post(LOGIN, {"username": nutzer.username, "password": "falsch"})
    # Danach ist auch das richtige Passwort gesperrt.
    antwort = client.post(LOGIN, {"username": nutzer.username, "password": passwort})
    assert antwort.status_code == 429
    assert "_auth_user_id" not in client.session


def test_logout_nur_per_post(client, nutzer):
    client.force_login(nutzer)
    assert client.get(reverse("logout")).status_code == 405
    antwort = client.post(reverse("logout"))
    assert antwort.status_code == 302
    assert "_auth_user_id" not in client.session


def test_passwort_aendern_erfordert_login(client):
    antwort = client.get(reverse("password_change"))
    assert antwort.status_code == 302
    assert antwort.url.startswith(LOGIN)


def test_passwort_aendern(client, nutzer, passwort):
    client.force_login(nutzer)
    assert client.get(reverse("password_change")).status_code == 200
    neu = "Noch-Geheimer-5678"
    antwort = client.post(
        reverse("password_change"),
        {"old_password": passwort, "new_password1": neu, "new_password2": neu},
    )
    assert antwort.status_code == 302
    assert antwort.url == reverse("password_change_done")
    nutzer.refresh_from_db()
    assert nutzer.check_password(neu)


def test_healthz_ok_ohne_login(client):
    antwort = client.get(reverse("healthz"))
    assert antwort.status_code == 200
    assert antwort.content == b"ok"


def test_healthz_503_ohne_db(client):
    with mock.patch("multigpt.chat.views.connection") as verbindung:
        verbindung.ensure_connection.side_effect = OperationalError("weg")
        antwort = client.get(reverse("healthz"))
    assert antwort.status_code == 503


def test_pgvector_extension_vorhanden():
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        assert cursor.fetchone() == (1,)
