"""Verschlüsseltes Feld (M2-02): Roundtrip, Speicherung, Schlüsselfehler."""

import pytest
from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured
from django.db import connection

from multigpt.chat.models import McpServer, Provider
from multigpt.core.crypto import DecryptionError, decrypt, encrypt
from multigpt.core.fields import mask_secret

SECRET = "sk-test-0123456789-abcd"


@pytest.fixture(autouse=True)
def _key(settings):
    settings.FIELD_ENCRYPTION_KEY = Fernet.generate_key().decode()


def _raw(table, column, pk):
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {column} FROM {table} WHERE id = %s", [pk])
        return cursor.fetchone()[0]


def test_encrypt_decrypt_roundtrip():
    token = encrypt(SECRET)
    assert token != SECRET
    assert SECRET not in token
    assert decrypt(token) == SECRET


@pytest.mark.django_db
def test_provider_api_key_roundtrip_and_not_plaintext_in_db():
    provider = Provider.objects.create(name="OpenAI", kind="openai_compat", api_key=SECRET)

    raw = _raw("chat_provider", "api_key", provider.pk)
    assert raw != SECRET
    assert SECRET not in raw
    assert "abcd" not in raw
    assert decrypt(raw) == SECRET

    assert Provider.objects.get(pk=provider.pk).api_key == SECRET


@pytest.mark.django_db
def test_mcp_credentials_encrypted():
    server = McpServer.objects.create(
        name="Bilder", transport="stdio", command="mcp-imagetools", credentials=SECRET
    )
    raw = _raw("chat_mcpserver", "credentials", server.pk)
    assert SECRET not in raw
    assert McpServer.objects.get(pk=server.pk).credentials == SECRET


@pytest.mark.django_db
def test_empty_key_stays_empty():
    provider = Provider.objects.create(name="LM Studio", kind="openai_compat", is_local=True)
    assert _raw("chat_provider", "api_key", provider.pk) == ""
    assert Provider.objects.get(pk=provider.pk).api_key == ""


@pytest.mark.django_db
def test_wrong_key_fails_cleanly(settings):
    provider = Provider.objects.create(name="OpenAI", kind="openai_compat", api_key=SECRET)
    settings.FIELD_ENCRYPTION_KEY = Fernet.generate_key().decode()

    with pytest.raises(DecryptionError) as excinfo:
        Provider.objects.get(pk=provider.pk)
    # Eigene Meldung, kein verketteter cryptography-Traceback, kein Klartext.
    assert "FIELD_ENCRYPTION_KEY" in str(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__
    assert SECRET not in str(excinfo.value)


@pytest.mark.parametrize("key", ["", "   ", "kein-gueltiger-schluessel"])
def test_missing_or_invalid_key_is_improperly_configured(settings, key):
    settings.FIELD_ENCRYPTION_KEY = key
    with pytest.raises(ImproperlyConfigured, match="FIELD_ENCRYPTION_KEY"):
        encrypt(SECRET)


def test_import_without_key_is_fine(settings):
    """Ohne Schlüssel scheitert erst der Zugriff, nicht Import oder Modellaufbau."""
    settings.FIELD_ENCRYPTION_KEY = ""
    provider = Provider(name="X", kind="anthropic", api_key=SECRET)
    assert "sk-" not in repr(provider)


def test_mask_secret():
    assert mask_secret(SECRET) == "••••abcd"
    assert mask_secret("kurz") == "••••"
    assert mask_secret("") == ""
    assert mask_secret(None) == ""
