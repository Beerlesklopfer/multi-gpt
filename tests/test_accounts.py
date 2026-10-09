"""Konten, Gruppen, Rollen-Startdaten, Signale und `create_account` (M2-01, M2-04, M2-06)."""

from decimal import Decimal
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import CommandError, call_command

from multigpt.accounts.models import Role, User, UserGroup

PASSWORD = "Geheim-Test-1234"


def test_custom_user_model_active():
    assert get_user_model() is User


@pytest.mark.django_db
def test_user_group_extends_django_group():
    group = UserGroup.objects.create(name="Eltern")
    user = User.objects.create_user("anna", password="geheim-123-x")
    user.groups.add(group)

    assert user.groups.filter(name="Eltern").exists()
    django_group = user.groups.get(name="Eltern")
    assert isinstance(django_group, Group)
    assert django_group.usergroup == group


# --- Datenmigration -----------------------------------------------------------


@pytest.mark.django_db
def test_four_start_roles_exist():
    roles = dict(Role.objects.values_list("key", "name"))
    assert roles == {
        "admin": "Verwalter",
        "adult": "Erwachsener",
        "teen": "Jugendlicher",
        "guest": "Gast",
    }


@pytest.mark.django_db
def test_start_role_rights():
    admin, adult, teen, guest = (
        Role.objects.get(key=k) for k in ("admin", "adult", "teen", "guest")
    )
    assert admin.is_admin and not adult.is_admin and not teen.is_admin and not guest.is_admin
    assert admin.all_models and adult.all_models
    assert not teen.all_models and not guest.all_models
    assert adult.can_share and not teen.can_share and not guest.can_share
    assert not guest.can_upload_documents
    assert teen.fixed_system_prompt
    assert admin.monthly_budget is None and adult.monthly_budget is None
    assert guest.monthly_budget < teen.monthly_budget


@pytest.mark.django_db
def test_family_group_is_default():
    family = UserGroup.objects.get(name="Familie")
    assert family.is_default


# --- Signale ------------------------------------------------------------------


@pytest.mark.django_db
def test_new_user_joins_default_groups_only():
    UserGroup.objects.create(name="Eltern")
    UserGroup.objects.create(name="Gäste-WLAN", is_default=True)
    user = User.objects.create_user("bert", password=PASSWORD)
    assert set(user.groups.values_list("name", flat=True)) == {"Familie", "Gäste-WLAN"}


@pytest.mark.django_db
def test_default_role_is_guest():
    user = User.objects.create_user("carla", password=PASSWORD)
    assert user.role.key == Role.GUEST


@pytest.mark.django_db
def test_superuser_default_role_is_admin():
    user = User.objects.create_superuser("root", password=PASSWORD)
    assert user.role.key == Role.ADMIN


@pytest.mark.django_db
def test_explicit_role_is_kept():
    user = User.objects.create_user("dora", password=PASSWORD, role=Role.objects.get(key="teen"))
    assert user.role.key == Role.TEEN


@pytest.mark.django_db
def test_role_with_users_cannot_be_deleted():
    from django.db.models import ProtectedError

    User.objects.create_user("emil", password=PASSWORD)
    with pytest.raises(ProtectedError):
        Role.objects.get(key="guest").delete()


@pytest.mark.django_db
def test_monthly_budget_override():
    user = User.objects.create_user("finn", password=PASSWORD, role=Role.objects.get(key="teen"))
    assert user.monthly_budget == Decimal("10.00")
    user.monthly_budget_override = Decimal("25.00")
    assert user.monthly_budget == Decimal("25.00")
    user.role = Role.objects.get(key="adult")
    user.monthly_budget_override = None
    assert user.monthly_budget is None


# --- Management-Kommando create_account ----------------------------------------


def _getpass(*answers):
    return mock.patch(
        "multigpt.accounts.management.commands.create_account.getpass.getpass",
        side_effect=list(answers),
    )


@pytest.mark.django_db
def test_create_account_with_role_option():
    with _getpass(PASSWORD, PASSWORD):
        call_command("create_account", "gerda", role="teen", display_name="Gerda")
    user = User.objects.get(username="gerda")
    assert user.role.key == "teen"
    assert user.display_name == "Gerda"
    assert user.check_password(PASSWORD)
    assert not user.is_staff and not user.is_superuser
    assert user.groups.filter(name="Familie").exists()


@pytest.mark.django_db
def test_create_account_interactive_role_by_number():
    roles = list(Role.objects.order_by("-is_admin", "name"))
    number = [r.key for r in roles].index("adult") + 1
    with (
        mock.patch("builtins.input", side_effect=["hans", str(number)]),
        _getpass(PASSWORD, PASSWORD),
    ):
        call_command("create_account")
    assert User.objects.get(username="hans").role.key == "adult"


@pytest.mark.django_db
def test_create_account_admin_role_sets_staff():
    with _getpass(PASSWORD, PASSWORD):
        call_command("create_account", "ida", role="admin")
    user = User.objects.get(username="ida")
    assert user.is_staff and not user.is_superuser


@pytest.mark.django_db
def test_create_account_unknown_role():
    with pytest.raises(CommandError, match="Unbekannte Rolle"):
        call_command("create_account", "jan", role="king")
    assert not User.objects.filter(username="jan").exists()


@pytest.mark.django_db
def test_create_account_existing_username():
    User.objects.create_user("karl", password=PASSWORD)
    with pytest.raises(CommandError, match="existiert bereits"):
        call_command("create_account", "karl", role="adult")


@pytest.mark.django_db
def test_create_account_retries_weak_and_mismatched_password():
    with _getpass("123", "123", PASSWORD, "anders", PASSWORD, PASSWORD):
        call_command("create_account", "lena", role="guest")
    assert User.objects.get(username="lena").check_password(PASSWORD)


@pytest.mark.django_db
def test_create_account_gives_up_after_three_bad_passwords():
    with (
        _getpass("a", "b", "c", "d", "e", "f"),
        pytest.raises(CommandError, match="Kein gültiges Passwort"),
    ):
        call_command("create_account", "mia", role="guest")
    assert not User.objects.filter(username="mia").exists()


@pytest.mark.django_db
def test_create_account_aborts_cleanly_without_input():
    with (
        mock.patch("builtins.input", side_effect=EOFError),
        pytest.raises(CommandError, match="Abgebrochen"),
    ):
        call_command("create_account")
