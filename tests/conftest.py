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
def _no_ecb_fetch(settings):
    """EZB-Kurs (A11) im Test nicht abrufen: Der Worker holt ihn sonst täglich
    aus dem Internet. Tests in tests/test_billing*.py schalten ihn gezielt ein."""
    settings.BILLING_ECB_FETCH = False


@pytest.fixture(autouse=True)
def _no_sandbox(monkeypatch):
    """Werkzeug run_python (M4a-10) standardmäßig nicht anbieten: Ob bubblewrap
    läuft, hängt vom Rechner ab, und viele Tests prüfen genaue Werkzeuglisten.
    tests/test_python_tool.py überschreibt diese Fixture."""
    from multigpt.chat import sandbox

    off = sandbox.Status(False, "Im Test abgeschaltet.")
    monkeypatch.setattr(sandbox, "status", lambda refresh=False: off)


@pytest.fixture(autouse=True)
def _no_pdf_tool(monkeypatch):
    """Werkzeug create_pdf standardmäßig nicht anbieten (genaue Werkzeuglisten in
    vielen Tests); tests/test_create_pdf.py überschreibt diese Fixture."""
    from multigpt.chat import documents_pdf

    monkeypatch.setattr(documents_pdf, "weasyprint_installed", lambda: False)


@pytest.fixture
def password():
    return "Geheim-Test-1234"


@pytest.fixture
def user(django_user_model, password):
    return django_user_model.objects.create_user(username="anna", password=password)


@pytest.fixture(scope="session")
def runner_server_key_path(tmp_path_factory):
    """Wegwerf-Installationsschlüssel des Runners (je Testlauf, nie im Repository)."""
    import json

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from multigpt.runner import trust
    from runner_agent import tools as agent_tools

    path = tmp_path_factory.mktemp("runner-key") / "server.key"
    _new, key = trust.generate_server_key(path)
    (path.parent / "server.pub").write_text(key.public_b64 + "\n")
    # Release-Schlüssel wie in der CI: Katalog des Python-Agents signieren, nur den
    # öffentlichen Teil behalten (release_keys.json), den privaten verwerfen.
    release = Ed25519PrivateKey.generate()
    raw = release.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    kid = trust.protocol.key_id(raw)
    catalog = agent_tools.catalog_text("0.0-test").encode()
    (path.parent / "catalog.json").write_bytes(catalog)
    (path.parent / "catalog.json.sig").write_text(trust.public_text(release.sign(catalog)) + "\n")
    (path.parent / "catalog.key_id").write_text(kid)
    keys = {
        "version": 1,
        "keys": [{"release": "0.0-test", "key_id": kid, "public_key": trust.public_text(raw)}],
    }
    (path.parent / "release_keys.json").write_text(json.dumps(keys))
    del release
    return path


@pytest.fixture(autouse=True)
def _runner_server_key(settings, runner_server_key_path):
    """Der Runner-Dienst signiert jeden Rahmen (Protokoll 2) – im Test mit dem
    Wegwerf-Schlüssel; Release-Schlüssel gibt es im Test nur, wo ein Test sie setzt."""
    settings.RUNNER_SERVER_KEY = str(runner_server_key_path)
    settings.RUNNER_RELEASE_KEYS_FILE = str(runner_server_key_path.parent / "release_keys.json")
    settings.RUNNER_DIST_DIR = str(runner_server_key_path.parent / "dist")
