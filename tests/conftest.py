import pytest


@pytest.fixture(autouse=True)
def _test_settings(settings, tmp_path):
    """Im Test gibt es kein collectstatic-Manifest, also einfachen Storage nutzen.

    STATIC_ROOT zeigt auf einen leeren Ordner (sonst warnt WhiteNoise), und ein
    schneller Passwort-Hasher spart Zeit.
    """
    settings.STATIC_ROOT = tmp_path
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.fixture
def passwort():
    return "Geheim-Test-1234"


@pytest.fixture
def nutzer(django_user_model, passwort):
    return django_user_model.objects.create_user(username="anna", password=passwort)
