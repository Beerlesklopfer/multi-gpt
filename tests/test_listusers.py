"""``manage.py listusers`` (mgpt-ctl): Konten mit Rolle und Status, ohne Geheimnisse."""

import io

import pytest
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command

from multigpt.accounts.models import Role

pytestmark = pytest.mark.django_db


@pytest.fixture
def users():
    User = get_user_model()
    anna = User.objects.create_user(
        "anna",
        password="Geheim-Test-1234",
        role=Role.objects.get(key="admin"),
        display_name="Anna",
        is_staff=True,
        email="anna@example.invalid",
    )
    lea = User.objects.create_user("lea", password="x", role=Role.objects.get(key="teen"))
    old = User.objects.create_user(
        "olaf", password="x", role=Role.objects.get(key="adult"), is_active=False
    )
    return anna, lea, old


def run(*args) -> str:
    out = io.StringIO()
    call_command("listusers", *args, stdout=out)
    return out.getvalue()


def test_table_lists_all_without_secrets(users):
    text = run()
    lines = text.splitlines()
    assert lines[0].startswith("Anmeldename") and lines[-1] == "3 Konto/Konten."
    assert "anna" in text and "Verwalter" in text and "aktiv, Admin" in text
    assert "gesperrt" in text and "nie" in text
    assert "pbkdf2" not in text and "example.invalid" not in text  # kein Hash, keine E-Mail


def test_filters_and_csv(users):
    assert run("--role", "teen").count("lea") == 1 and "anna" not in run("--role", "teen")
    assert "olaf" not in run("--active") and "olaf" in run("--inactive")
    csv_text = run("--format", "csv", "--active")
    assert csv_text.splitlines()[0] == "Anmeldename,Anzeigename,Rolle,Status,Letzte Anmeldung"
    assert len(csv_text.splitlines()) == 3


def test_unknown_role_and_empty(users):
    with pytest.raises(CommandError, match="Möglich"):
        run("--role", "gibtsnicht")
    get_user_model().objects.all().delete()
    assert run() == "Keine Konten gefunden.\n"
