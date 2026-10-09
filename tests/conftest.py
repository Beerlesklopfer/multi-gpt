import pytest


@pytest.fixture(autouse=True)
def _ohne_manifest(settings):
    """Im Test gibt es kein collectstatic-Manifest, also einfachen Storage nutzen."""
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
