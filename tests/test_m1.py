"""Tests für Meilenstein 1: Login, Logout, Passwort, pgvector (Health-Check: test_health.py)."""

import pytest
from django.conf import settings
from django.db import connection
from django.urls import reverse

pytestmark = pytest.mark.django_db

LOGIN = reverse("login")


def test_anonymous_redirected_to_login(client):
    response = client.get(reverse("chat:index"))
    assert response.status_code == 302
    assert response.url.startswith(LOGIN)


def test_login_with_correct_password(client, user, password):
    response = client.post(LOGIN, {"username": user.username, "password": password})
    assert response.status_code == 302
    assert response.url == reverse("chat:index")
    assert client.get(response.url).status_code == 200


def test_login_with_wrong_password(client, user):
    response = client.post(LOGIN, {"username": user.username, "password": "falsch"})
    assert response.status_code == 200
    assert "_auth_user_id" not in client.session


def test_lockout_after_too_many_failures(client, user, password):
    for _ in range(settings.AXES_FAILURE_LIMIT):
        client.post(LOGIN, {"username": user.username, "password": "falsch"})
    # Danach ist auch das richtige Passwort gesperrt.
    response = client.post(LOGIN, {"username": user.username, "password": password})
    assert response.status_code == 429
    assert "vorübergehend gesperrt" in response.content.decode()
    assert "_auth_user_id" not in client.session


def test_logout_post_only(client, user):
    client.force_login(user)
    assert client.get(reverse("logout")).status_code == 405
    response = client.post(reverse("logout"))
    assert response.status_code == 302
    assert "_auth_user_id" not in client.session


def test_password_change_requires_login(client):
    response = client.get(reverse("password_change"))
    assert response.status_code == 302
    assert response.url.startswith(LOGIN)


def test_password_change(client, user, password):
    client.force_login(user)
    assert client.get(reverse("password_change")).status_code == 200
    new_password = "Noch-Geheimer-5678"
    response = client.post(
        reverse("password_change"),
        {"old_password": password, "new_password1": new_password, "new_password2": new_password},
    )
    assert response.status_code == 302
    assert response.url == reverse("password_change_done")
    user.refresh_from_db()
    assert user.check_password(new_password)


def test_pgvector_extension_present():
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        assert cursor.fetchone() == (1,)
