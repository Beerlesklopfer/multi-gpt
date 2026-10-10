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


@pytest.fixture(autouse=True)
def _no_sandbox(monkeypatch):
    """Werkzeug run_python (M4a-10) standardmäßig nicht anbieten: Ob bubblewrap
    läuft, hängt vom Rechner ab, und viele Tests prüfen genaue Werkzeuglisten.
    tests/test_python_tool.py überschreibt diese Fixture."""
    from multigpt.chat import sandbox

    off = sandbox.Status(False, "Im Test abgeschaltet.")
    monkeypatch.setattr(sandbox, "status", lambda refresh=False: off)


@pytest.fixture
def password():
    return "Geheim-Test-1234"


@pytest.fixture
def user(django_user_model, password):
    return django_user_model.objects.create_user(username="anna", password=password)
