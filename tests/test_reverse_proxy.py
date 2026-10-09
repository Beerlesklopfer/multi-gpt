"""Betrieb hinter nginx (M12-01): Client-IP für axes, X-Accel-Redirect für Downloads."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory
from django.urls import reverse

from multigpt.accounts.client_ip import client_ip
from multigpt.accounts.models import Role, User
from multigpt.chat.models import Collection, Document
from multigpt.chat.views_collections import x_accel_path

LOGIN = reverse("login")
PASSWORD = "Geheim-Test-1234"


def _request(remote="127.0.0.1", forwarded=None):
    meta = {"REMOTE_ADDR": remote}
    if forwarded is not None:
        meta["HTTP_X_FORWARDED_FOR"] = forwarded
    return RequestFactory().get("/", **meta)


# --- Client-IP ------------------------------------------------------------------------


def test_client_ip_without_proxy_ignores_header(settings):
    settings.REVERSE_PROXY_COUNT = 0
    assert client_ip(_request("192.0.2.7", "203.0.113.9")) == "192.0.2.7"


@pytest.mark.parametrize(
    "forwarded, expected",
    [
        ("203.0.113.9", "203.0.113.9"),
        # Vom Client vorangestellte Einträge zählen nicht.
        ("10.6.6.6, 203.0.113.9", "203.0.113.9"),
        (" 2001:db8::1 ", "2001:db8::1"),
        ("", "127.0.0.1"),
        ("kein-ip", "127.0.0.1"),
        (None, "127.0.0.1"),
    ],
)
def test_client_ip_one_proxy(settings, forwarded, expected):
    settings.REVERSE_PROXY_COUNT = 1
    assert client_ip(_request("127.0.0.1", forwarded)) == expected


def test_client_ip_two_proxies(settings):
    settings.REVERSE_PROXY_COUNT = 2
    assert client_ip(_request("127.0.0.1", "1.1.1.1, 203.0.113.9, 10.0.0.2")) == "203.0.113.9"
    assert client_ip(_request("127.0.0.1", "10.0.0.2")) == "127.0.0.1"


@pytest.mark.django_db
def test_lockout_per_client_ip_behind_proxy(client, settings):
    """Behind nginx all requests come from 127.0.0.1; the lockout must still
    distinguish clients by X-Forwarded-For."""
    settings.REVERSE_PROXY_COUNT = 1
    user = User.objects.create_user("lotte", password=PASSWORD)
    for _ in range(settings.AXES_FAILURE_LIMIT):
        client.post(
            LOGIN,
            {"username": "lotte", "password": "falsch"},
            REMOTE_ADDR="127.0.0.1",
            HTTP_X_FORWARDED_FOR="192.0.2.66",
        )
    locked = client.post(
        LOGIN,
        {"username": "lotte", "password": PASSWORD},
        REMOTE_ADDR="127.0.0.1",
        HTTP_X_FORWARDED_FOR="192.0.2.66",
    )
    assert locked.status_code == 429
    # Ein anderer Client (andere IP) ist nicht gesperrt – auch nicht, wenn er die
    # gesperrte Adresse selbst voranstellt.
    other = client.post(
        LOGIN,
        {"username": user.username, "password": PASSWORD},
        REMOTE_ADDR="127.0.0.1",
        HTTP_X_FORWARDED_FOR="192.0.2.66, 192.0.2.77",
    )
    assert other.status_code == 302


# --- X-Accel-Redirect -----------------------------------------------------------------


@pytest.fixture
def owner_client(client, db, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    user = User.objects.create_user("anna", password=PASSWORD, role=Role.objects.get(key="adult"))
    client.force_login(user)
    collection = Collection.objects.create(owner=user, name="Anleitungen")
    document = Document.objects.create(
        collection=collection,
        title="Mein Handbuch.txt",
        file=SimpleUploadedFile("egal.txt", b"Inhalt 123"),
        status="indexed",
    )
    return client, document


def test_x_accel_path_off_by_default(settings):
    settings.USE_X_ACCEL_REDIRECT = False
    assert x_accel_path("documents/1/abc.pdf") is None


@pytest.mark.parametrize(
    "name, ok",
    [
        ("documents/1/0123abcd.pdf", True),
        ("attachments/7/ff00", True),
        ("documents/1/../../etc/passwd", False),
        ("/etc/passwd", False),
        ("documents/1/a b.pdf", False),
        ("documents/1/a.pdf\nX-Evil: 1", False),
        ("", False),
    ],
)
def test_x_accel_path_only_safe_names(settings, name, ok):
    settings.USE_X_ACCEL_REDIRECT = True
    result = x_accel_path(name)
    assert (result == "/_protected/media/" + name) if ok else result is None


def test_download_via_x_accel(owner_client, settings):
    settings.USE_X_ACCEL_REDIRECT = True
    client, document = owner_client
    response = client.get(reverse("chat:document_download", args=[document.pk]))
    assert response.status_code == 200
    assert response["X-Accel-Redirect"] == "/_protected/media/" + document.file.name
    assert response.content == b""
    assert response["Content-Disposition"].startswith("attachment;")
    assert "Mein Handbuch.txt" in response["Content-Disposition"]
    assert response["Content-Type"].startswith("text/plain")
    assert response["Cache-Control"] == "private, no-store"
    assert response["X-Content-Type-Options"] == "nosniff"


def test_download_without_x_accel_streams(owner_client, settings):
    settings.USE_X_ACCEL_REDIRECT = False
    client, document = owner_client
    response = client.get(reverse("chat:document_download", args=[document.pk]))
    assert response.status_code == 200
    assert "X-Accel-Redirect" not in response
    assert b"".join(response.streaming_content) == b"Inhalt 123"
