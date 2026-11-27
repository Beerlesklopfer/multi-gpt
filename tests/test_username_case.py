"""Anmeldenamen ohne Unterschied von Groß- und Kleinschreibung (Nutzer, 2026-11-27)."""

import importlib
import io

import pytest
from django.apps import apps as global_apps
from django.contrib.auth import authenticate, get_user_model
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.db import IntegrityError, transaction
from django.test import override_settings

from multigpt.accounts.client_ip import login_username
from multigpt.accounts.forms_family import AccountCreateForm
from multigpt.accounts.models import Role

pytestmark = pytest.mark.django_db
PASSWORD = "Geheim-Test-1234"


@pytest.fixture
def anna():
    return get_user_model().objects.create_user(
        "Anna", password=PASSWORD, role=Role.objects.get(key="adult")
    )


def test_login_with_any_case(client, rf, anna):
    for name in ("Anna", "anna", "ANNA"):
        request = rf.post("/konto/login/")
        assert authenticate(request, username=name, password=PASSWORD) == anna
    response = client.post("/konto/login/", {"username": "aNNa", "password": PASSWORD})
    assert response.status_code == 302
    anna.refresh_from_db()
    assert anna.username == "Anna"  # Schreibweise bleibt erhalten


def test_no_second_account_differing_only_in_case(anna):
    User = get_user_model()
    with pytest.raises(ValidationError, match="gibt es schon"):
        User(username="ANNA", role=anna.role).validate_constraints()
    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.create_user("anna", password=PASSWORD)
    form = AccountCreateForm(data={"username": "aNNA", "display_name": "", "role": anna.role.pk})
    assert not form.is_valid()
    assert "gibt es schon" in str(form.errors)


def test_create_account_command_rejects_case_duplicate(anna):
    with pytest.raises(CommandError, match="existiert bereits"):
        call_command("create_account", "aNnA", "--role", "adult", stdout=io.StringIO())


def test_lockout_counts_all_spellings(client, anna):
    assert login_username(None, {"username": " AnNa "}) == "anna"
    with override_settings(AXES_FAILURE_LIMIT=3):
        for name in ("anna", "ANNA", "Anna"):
            client.post("/konto/login/", {"username": name, "password": "falsch"})
        locked = client.post("/konto/login/", {"username": "aNNa", "password": PASSWORD})
    assert locked.status_code != 302  # gesperrt, obwohl das Kennwort stimmt


def test_migration_stops_on_existing_case_duplicates():
    migration = importlib.import_module(
        "multigpt.accounts.migrations.0017_username_case_insensitive"
    )

    class FakeQuerySet:
        def values_list(self, *args, **kwargs):
            return ["Ben", "ben", "lea"]

    class FakeUser:
        objects = FakeQuerySet()

    class FakeApps:
        def get_model(self, app, name):
            return FakeUser

    with pytest.raises(RuntimeError, match="Ben, ben"):
        migration.check_duplicates(FakeApps(), None)
    assert global_apps.get_model("accounts", "User") is get_user_model()
