"""Persönliches Menü und Profilbild (accounts/avatar.py, views_avatar.py)."""

import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image

from multigpt.accounts import avatar
from multigpt.accounts.models import Avatar, Role, User

pytestmark = pytest.mark.django_db

PASSWORD = "Geheim-Test-1234"


def make_user(username, role_key="adult", **fields):
    return User.objects.create_user(
        username, password=PASSWORD, role=Role.objects.get(key=role_key), **fields
    )


def jpeg_with_exif(width=400, height=200) -> bytes:
    image = Image.new("RGB", (width, height), (200, 30, 30))
    exif = Image.Exif()
    exif[0x010F] = "Kamerahersteller"  # Make
    exif[0x0112] = 6  # Orientation: 90° drehen
    out = io.BytesIO()
    image.save(out, "JPEG", exif=exif.tobytes())
    return out.getvalue()


def test_process_crops_square_and_strips_metadata():
    data = avatar.process(io.BytesIO(jpeg_with_exif()))
    image = Image.open(io.BytesIO(data))
    assert image.format == "WEBP"
    assert image.size == (avatar.AVATAR_EDGE, avatar.AVATAR_EDGE)
    assert not image.getexif()
    assert "exif" not in image.info


def test_process_rejects_broken_and_large_files():
    with pytest.raises(avatar.AvatarError):
        avatar.process(io.BytesIO(b"kein Bild"))
    with pytest.raises(avatar.AvatarError, match="10 MB"):
        avatar.process(io.BytesIO(b""), size=avatar.MAX_UPLOAD_BYTES + 1)


def test_initials_and_stable_color():
    anna = make_user("anna", display_name="Anna Maria Bernau")
    assert avatar.initials(anna) == "AB"
    ben = make_user("ben")
    assert avatar.initials(ben) == "BE"
    assert avatar.color(ben) == avatar.color(User.objects.get(pk=ben.pk))


def test_upload_show_and_remove(client):
    anna = make_user("anna")
    client.force_login(anna)
    page = client.get(reverse("settings")).content.decode()
    assert 'id="profilbild"' in page and "avatar-initials" in page
    upload = SimpleUploadedFile("ich.jpg", jpeg_with_exif(), content_type="image/jpeg")
    response = client.post(reverse("avatar_update"), {"avatar": upload})
    assert response.status_code == 302
    anna.refresh_from_db()
    assert anna.avatar_updated is not None
    image_url = reverse("avatar_image", args=[anna.pk])
    html = client.get(reverse("chat:index")).content.decode()
    assert f'src="{image_url}?v=' in html  # Menü zeigt das Bild statt Initialen
    response = client.get(image_url)
    assert response["Content-Type"] == "image/webp"
    assert response["Cache-Control"].startswith("private")
    # Andere angemeldete Konten sehen es (geteilte Chats), Abgemeldete nicht.
    other = client.__class__()
    assert other.get(image_url).status_code == 302
    other.force_login(make_user("ben"))
    assert other.get(image_url).status_code == 200
    client.post(reverse("avatar_update"), {"remove": "1"})
    anna.refresh_from_db()
    assert anna.avatar_updated is None and not Avatar.objects.exists()
    assert client.get(image_url).status_code == 404


def test_broken_upload_shows_message(client):
    client.force_login(make_user("anna"))
    upload = SimpleUploadedFile("x.png", b"kaputt", content_type="image/png")
    response = client.post(reverse("avatar_update"), {"avatar": upload}, follow=True)
    assert "beschädigt" in response.content.decode()
    assert not Avatar.objects.exists()


def test_menu_entries_follow_rights(client):
    teen = make_user("tim", role_key="teen")
    client.force_login(teen)
    html = client.get(reverse("chat:index")).content.decode()
    assert 'id="user-menu"' in html
    for label in ("Einstellungen", "Profilbild ändern", "Mein Verbrauch", "Abmelden"):
        assert label in html
    assert reverse("admin:index") not in html
    assert reverse("family:overview") not in html
    admin = make_user("jo", role_key="admin", is_staff=True)
    client.force_login(admin)
    html = client.get(reverse("chat:index")).content.decode()
    assert reverse("admin:index") in html and reverse("family:overview") in html


def test_version_shown_in_menu_login_and_admin(client):
    from multigpt.version import app_version

    version = app_version()
    assert version and version != "0"
    login = client.get(reverse("login")).content.decode()
    assert f"MultiGPT {version}" in login
    admin = make_user("jo", role_key="admin", is_staff=True, is_superuser=True)
    client.force_login(admin)
    assert f"MultiGPT {version}" in client.get(reverse("chat:index")).content.decode()
    assert f"MultiGPT-Verwaltung {version}" in client.get(reverse("admin:index")).content.decode()
