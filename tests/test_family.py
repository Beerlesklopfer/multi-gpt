"""Seite „Familie“ (M6-04) und Einsicht in Jugendlichen-Chats (M6-05)."""

import json
import re
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse

from multigpt.accounts import views_family
from multigpt.accounts.models import Role, User, UserGroup
from multigpt.accounts.permissions import Action, can
from multigpt.chat.models import AIModel, Conversation, Message, Provider, Share

pytestmark = pytest.mark.django_db

PASSWORD = "Geheim-Test-1234"


def make_user(username, role_key="adult", **extra):
    return User.objects.create_user(
        username, password=PASSWORD, role=Role.objects.get(key=role_key), **extra
    )


@pytest.fixture
def ai_model():
    provider = Provider.objects.create(name="Testanbieter", kind=Provider.Kind.OPENAI_COMPAT)
    return AIModel.objects.create(provider=provider, model_id="m-1", display_name="Modell Eins")


@pytest.fixture
def admin_user(ai_model):
    return make_user("mama", "admin", display_name="Mama")


@pytest.fixture
def teen():
    return make_user("tim", "teen", display_name="Tim")


@pytest.fixture
def adult():
    return make_user("anna", "adult", display_name="Anna")


@pytest.fixture
def admin_client(client, admin_user):
    client.force_login(admin_user)
    return client


@pytest.fixture
def teen_chat(teen, ai_model):
    conv = Conversation.objects.create(user=teen, title="Hausaufgaben Mathe")
    user_msg = Message.objects.create(conversation=conv, role="user", content="Was ist 2+2?")
    answer = Message.objects.create(
        conversation=conv,
        role="assistant",
        content="Vier.",
        parent=user_msg,
        model=ai_model,
        cost=Decimal("0.0200"),
        tokens_in=10,
        tokens_out=3,
    )
    Conversation.objects.filter(pk=conv.pk).update(current_leaf=answer)
    conv.refresh_from_db()
    return conv


def post(client, url_name, *args, **data):
    return client.post(reverse(f"family:{url_name}", args=args), data)


def no_inline_js(html):
    scripts = re.findall(r"<script\b[^>]*>", html)
    return all("src=" in tag for tag in scripts) and not re.search(r"\son[a-z]+=", html)


# --- Zugriff --------------------------------------------------------------------


def family_urls(member_pk, group_pk):
    return [
        reverse("family:overview"),
        reverse("family:member", args=[member_pk]),
        reverse("family:groups"),
        reverse("family:group", args=[group_pk]),
        reverse("family:usage"),
    ]


def family_post_urls(member_pk, group_pk):
    return [
        reverse("family:member_create"),
        reverse("family:member_password", args=[member_pk]),
        reverse("family:member_active", args=[member_pk]),
        reverse("family:member_role", args=[member_pk]),
        reverse("family:member_budget", args=[member_pk]),
        reverse("family:member_supervision", args=[member_pk]),
        reverse("family:group_create"),
        reverse("family:group_rename", args=[group_pk]),
        reverse("family:group_members", args=[group_pk]),
        reverse("family:group_delete", args=[group_pk]),
    ]


@pytest.mark.parametrize("role_key", ["guest", "teen", "adult"])
def test_only_managers_get_access(client, role_key, admin_user):
    member = make_user("someone", role_key)
    group = UserGroup.objects.create(name="Eltern")
    client.force_login(member)
    for url in family_urls(admin_user.pk, group.pk):
        assert client.get(url).status_code == 403, url
    for url in family_post_urls(admin_user.pk, group.pk):
        assert client.post(url, {"active": "0", "name": "x"}).status_code == 403, url
    admin_user.refresh_from_db()
    assert admin_user.is_active
    assert UserGroup.objects.filter(pk=group.pk, name="Eltern").exists()


def test_anonymous_redirected_to_login(client, admin_user):
    response = client.get(reverse("family:overview"))
    assert response.status_code == 302
    assert reverse("login") in response["Location"]


def test_manager_sees_all_pages_without_inline_js(admin_client, admin_user, teen, teen_chat):
    group = UserGroup.objects.create(name="Eltern")
    teen.allow_supervision = True
    teen.save()
    for url in family_urls(teen.pk, group.pk):
        response = admin_client.get(url)
        assert response.status_code == 200, url
        assert no_inline_js(response.content.decode()), url


def test_actions_need_post_and_csrf(admin_user, teen):
    client = Client(enforce_csrf_checks=True)
    client.force_login(admin_user)
    url = reverse("family:member_active", args=[teen.pk])
    assert client.get(url).status_code == 405
    assert client.post(url, {"active": "0"}).status_code == 403  # ohne CSRF-Token
    teen.refresh_from_db()
    assert teen.is_active


def test_sidebar_link_only_for_managers(client, admin_user, adult):
    url = reverse("family:overview")
    client.force_login(admin_user)
    assert f'href="{url}"' in client.get(reverse("chat:index")).content.decode()
    client.force_login(adult)
    assert f'href="{url}"' not in client.get(reverse("chat:index")).content.decode()


def test_overview_lists_members_with_role_status_and_usage(admin_client, teen, teen_chat):
    teen.monthly_budget_override = Decimal("1.00")
    teen.is_active = False
    teen.save()
    html = admin_client.get(reverse("family:overview")).content.decode()
    assert "Tim" in html
    assert "Jugendlich" in html or teen.role.name in html
    assert "gesperrt" in html
    assert "0,02 €" in html and "1,00 €" in html
    # Ohne Einsicht keine Chatinhalte, nur Zahlen
    assert "Hausaufgaben Mathe" not in html


# --- Konten anlegen, Passwort ------------------------------------------------------


def test_create_account_shows_password_once(admin_client):
    response = post(
        admin_client,
        "member_create",
        username="lena",
        display_name="Lena",
        role=Role.objects.get(key="teen").pk,
    )
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    html = response.content.decode()
    password = re.search(r'id="new-password"[^>]*>([^<]+)<', html).group(1)
    lena = User.objects.get(username="lena")
    assert lena.role.key == "teen" and not lena.is_staff
    assert lena.check_password(password)
    assert lena.groups.filter(name="Familie").exists()
    # Danach nirgends mehr sichtbar
    for url in (reverse("family:overview"), reverse("family:member", args=[lena.pk])):
        assert password not in admin_client.get(url).content.decode()


def test_create_account_requires_role_and_unique_name(admin_client, teen):
    response = post(admin_client, "member_create", username="ohne", display_name="")
    assert response.status_code == 302
    assert not User.objects.filter(username="ohne").exists()
    response = post(
        admin_client, "member_create", username="tim", role=Role.objects.get(key="guest").pk
    )
    assert response.status_code == 302
    assert User.objects.filter(username="tim").count() == 1


def test_admin_account_gets_staff_flag(admin_client):
    post(admin_client, "member_create", username="papa", role=Role.objects.get(key="admin").pk)
    assert User.objects.get(username="papa").is_staff


def test_generated_password_passes_validators():
    from django.contrib.auth.password_validation import validate_password

    first = views_family.generate_password()
    validate_password(first)
    assert first != views_family.generate_password()
    assert len(first) >= 20


def test_password_reset_shows_new_password_once(admin_client, teen):
    response = post(admin_client, "member_password", teen.pk)
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    password = re.search(r'id="new-password"[^>]*>([^<]+)<', response.content.decode()).group(1)
    teen.refresh_from_db()
    assert teen.check_password(password)
    assert not teen.check_password(PASSWORD)
    assert (
        password not in admin_client.get(reverse("family:member", args=[teen.pk])).content.decode()
    )


def test_password_reset_logs_member_out(admin_client, teen):
    teen_client = Client()
    teen_client.force_login(teen)
    assert teen_client.get(reverse("chat:index")).status_code == 200
    post(admin_client, "member_password", teen.pk)
    assert teen_client.get(reverse("chat:index")).status_code == 302


def test_no_password_reset_for_self(admin_client, admin_user):
    response = post(admin_client, "member_password", admin_user.pk)
    assert response.status_code == 302
    admin_user.refresh_from_db()
    assert admin_user.check_password(PASSWORD)


def test_password_not_logged(admin_client, teen, caplog):
    with caplog.at_level("DEBUG"):
        response = post(admin_client, "member_password", teen.pk)
    password = re.search(r'id="new-password"[^>]*>([^<]+)<', response.content.decode()).group(1)
    assert password not in caplog.text


# --- Sperren, Rolle, Budget -------------------------------------------------------


def test_lock_and_unlock(admin_client, teen):
    teen_client = Client()
    teen_client.force_login(teen)
    post(admin_client, "member_active", teen.pk, active="0")
    teen.refresh_from_db()
    assert not teen.is_active
    assert teen_client.get(reverse("chat:index")).status_code == 302  # sofort abgemeldet
    post(admin_client, "member_active", teen.pk, active="1")
    teen.refresh_from_db()
    assert teen.is_active


def test_cannot_lock_self(admin_client, admin_user):
    response = post(admin_client, "member_active", admin_user.pk, active="0")
    assert response.status_code == 302
    admin_user.refresh_from_db()
    assert admin_user.is_active


def test_cannot_change_own_role(admin_client, admin_user):
    post(admin_client, "member_role", admin_user.pk, role=Role.objects.get(key="adult").pk)
    admin_user.refresh_from_db()
    assert admin_user.role.key == "admin"


def test_last_manager_is_protected(admin_user):
    assert views_family._last_manager(admin_user)
    other = make_user("papa", "admin")
    assert not views_family._last_manager(admin_user)
    other.is_active = False
    other.save()
    assert views_family._last_manager(admin_user)


def test_last_manager_lock_and_demotion_refused(admin_user, monkeypatch):
    """Ein Superuser ohne Verwalterrolle zählt als Verwalter; gesperrt kann er das
    letzte Verwalterkonto weder sperren noch herabstufen, solange es das letzte ist."""
    boss = User.objects.create_superuser("boss", password=PASSWORD)
    client = Client()
    client.force_login(boss)
    # boss zählt selbst – darum hier die Schutzregel mit nur einem Verwalter erzwingen
    monkeypatch.setattr(views_family, "_last_manager", lambda member: member.pk == admin_user.pk)
    post(client, "member_active", admin_user.pk, active="0")
    post(client, "member_role", admin_user.pk, role=Role.objects.get(key="adult").pk)
    admin_user.refresh_from_db()
    assert admin_user.is_active and admin_user.role.key == "admin"


def test_demote_other_manager(admin_client, ai_model):
    other = make_user("papa", "admin", is_staff=True)
    post(admin_client, "member_role", other.pk, role=Role.objects.get(key="adult").pk)
    other.refresh_from_db()
    assert other.role.key == "adult" and not other.is_staff
    assert not can(other, Action.MANAGE_FAMILY)


def test_superuser_accounts_only_for_superusers(admin_client):
    boss = User.objects.create_superuser("boss", password=PASSWORD)
    assert post(admin_client, "member_active", boss.pk, active="0").status_code == 403
    assert post(admin_client, "member_password", boss.pk).status_code == 403
    boss.refresh_from_db()
    assert boss.is_active and boss.check_password(PASSWORD)


def test_change_role(admin_client, adult):
    post(admin_client, "member_role", adult.pk, role=Role.objects.get(key="guest").pk)
    adult.refresh_from_db()
    assert adult.role.key == "guest"
    response = post(admin_client, "member_role", adult.pk, role="999999")
    assert response.status_code == 302
    adult.refresh_from_db()
    assert adult.role.key == "guest"


def test_set_and_clear_budget(admin_client, teen):
    post(admin_client, "member_budget", teen.pk, monthly_budget_override="5,50")
    teen.refresh_from_db()
    assert teen.monthly_budget_override == Decimal("5.50")
    post(admin_client, "member_budget", teen.pk, monthly_budget_override="-1")
    teen.refresh_from_db()
    assert teen.monthly_budget_override == Decimal("5.50")
    post(admin_client, "member_budget", teen.pk, monthly_budget_override="")
    teen.refresh_from_db()
    assert teen.monthly_budget_override is None


# --- Gruppen ----------------------------------------------------------------------


def test_create_rename_and_fill_group(admin_client, adult, teen):
    response = post(admin_client, "group_create", name="Eltern")
    group = UserGroup.objects.get(name="Eltern")
    assert response["Location"] == reverse("family:group", args=[group.pk])
    post(admin_client, "group_rename", group.pk, name="Große")
    group.refresh_from_db()
    assert group.name == "Große"
    post(admin_client, "group_members", group.pk, members=[adult.pk, teen.pk])
    assert set(group.user_set.values_list("username", flat=True)) == {"anna", "tim"}
    post(admin_client, "group_members", group.pk, members=[adult.pk])
    assert list(group.user_set.values_list("username", flat=True)) == ["anna"]


def test_duplicate_group_name_refused(admin_client):
    post(admin_client, "group_create", name="familie")
    assert UserGroup.objects.filter(name__iexact="familie").count() == 1


def test_default_group_stays(admin_client, adult):
    family = UserGroup.objects.get(is_default=True)
    before = set(family.user_set.values_list("pk", flat=True))
    post(admin_client, "group_members", family.pk, members=[adult.pk])
    assert set(family.user_set.values_list("pk", flat=True)) == before
    post(admin_client, "group_delete", family.pk)
    assert UserGroup.objects.filter(pk=family.pk, is_default=True).exists()


def test_delete_group_removes_shares(admin_client, adult):
    group = UserGroup.objects.create(name="Eltern")
    conv = Conversation.objects.create(user=adult, title="Geteilt")
    Share.objects.create(conversation=conv, group=group)
    post(admin_client, "group_delete", group.pk)
    assert not UserGroup.objects.filter(pk=group.pk).exists()
    assert not Share.objects.filter(conversation=conv).exists()


# --- Verbrauch aller --------------------------------------------------------------


def test_usage_page_per_member_and_model(admin_client, teen, teen_chat):
    html = admin_client.get(reverse("family:usage")).content.decode()
    assert "Tim" in html and "Modell Eins" in html and "0,02 €" in html
    assert "Hausaufgaben" not in html
    # Unbekannter Monat -> aktueller Monat, kein Fehler
    assert admin_client.get(reverse("family:usage") + "?monat=1999-01").status_code == 200


# --- Einsicht (M6-05) -------------------------------------------------------------


def chat_urls(conv):
    return [
        reverse("chat:conversation", args=[conv.pk]),
        reverse("chat:conversation_messages", args=[conv.pk]),
        reverse("chat:conversation_export", args=[conv.pk]),
        reverse("chat:api_messages", args=[conv.pk]),
    ]


def test_without_supervision_no_access(admin_client, teen, teen_chat):
    for url in chat_urls(teen_chat):
        assert admin_client.get(url).status_code == 404, url
    html = admin_client.get(reverse("family:member", args=[teen.pk])).content.decode()
    assert "Hausaufgaben Mathe" not in html


def test_supervision_only_for_teens(admin_client, adult):
    post(admin_client, "member_supervision", adult.pk, allow_supervision="on")
    adult.refresh_from_db()
    assert not adult.allow_supervision


def test_supervision_read_only(admin_client, teen, teen_chat, ai_model):
    post(admin_client, "member_supervision", teen.pk, allow_supervision="on")
    teen.refresh_from_db()
    assert teen.allow_supervision
    # Liste der Chats in „Familie“
    member_html = admin_client.get(reverse("family:member", args=[teen.pk])).content.decode()
    assert "Hausaufgaben Mathe" in member_html
    assert reverse("chat:conversation", args=[teen_chat.pk]) in member_html
    # Lesen geht
    for url in chat_urls(teen_chat):
        assert admin_client.get(url).status_code == 200, url
    page = admin_client.get(reverse("chat:conversation", args=[teen_chat.pk])).content.decode()
    assert "Vier." in page
    assert 'id="chat-form"' not in page
    assert "data-edit-message" not in page and "data-regenerate-message" not in page
    assert 'data-chat-action="rename"' not in page and 'data-chat-action="delete"' not in page
    assert "Einsicht" in page
    assert no_inline_js(page)
    # Schreiben nicht
    api = reverse("chat:api_messages", args=[teen_chat.pk])
    body = json.dumps({"content": "Hallo", "model": ai_model.pk})
    assert admin_client.post(api, body, content_type="application/json").status_code == 403
    regen = json.dumps(
        {"regenerate": True, "model": ai_model.pk, "message_id": teen_chat.current_leaf_id}
    )
    assert admin_client.post(api, regen, content_type="application/json").status_code == 403
    branch = reverse("chat:api_branch", args=[teen_chat.pk])
    switch = json.dumps({"message_id": teen_chat.current_leaf_id})
    assert admin_client.post(branch, switch, content_type="application/json").status_code == 403
    detail = reverse("chat:api_conversation_detail", args=[teen_chat.pk])
    rename = json.dumps({"title": "Gekapert"})
    assert admin_client.patch(detail, rename, content_type="application/json").status_code == 403
    assert admin_client.delete(detail).status_code == 403
    teen_chat.refresh_from_db()
    assert teen_chat.title == "Hausaufgaben Mathe"
    assert Message.objects.filter(conversation=teen_chat).count() == 2
    assert not can(User.objects.get(username="mama"), Action.WRITE, teen_chat)


def test_supervision_needs_manager(client, teen, teen_chat, adult):
    teen.allow_supervision = True
    teen.save()
    client.force_login(adult)
    assert client.get(reverse("chat:conversation", args=[teen_chat.pk])).status_code == 404


def test_supervision_does_not_cover_collections(admin_user, teen):
    from multigpt.chat.models import Collection

    teen.allow_supervision = True
    teen.save()
    coll = Collection.objects.create(owner=teen, name="Privat")
    assert not can(admin_user, Action.READ, coll)


def test_member_sees_notice_while_supervised(teen, admin_client):
    client = Client()
    client.force_login(teen)
    assert "supervision-notice" not in client.get(reverse("chat:index")).content.decode()
    post(admin_client, "member_supervision", teen.pk, allow_supervision="on")
    html = client.get(reverse("chat:index")).content.decode()
    assert "Deine Chats können von Verwaltern eingesehen werden" in html
    post(admin_client, "member_supervision", teen.pk)
    assert "supervision-notice" not in client.get(reverse("chat:index")).content.decode()


def test_revoking_supervision_works_immediately(admin_client, teen, teen_chat):
    post(admin_client, "member_supervision", teen.pk, allow_supervision="on")
    url = reverse("chat:conversation", args=[teen_chat.pk])
    assert admin_client.get(url).status_code == 200
    post(admin_client, "member_supervision", teen.pk)
    assert admin_client.get(url).status_code == 404


def test_role_change_ends_supervision(admin_client, teen, teen_chat):
    post(admin_client, "member_supervision", teen.pk, allow_supervision="on")
    post(admin_client, "member_role", teen.pk, role=Role.objects.get(key="adult").pk)
    teen.refresh_from_db()
    assert not teen.allow_supervision
    assert admin_client.get(reverse("chat:conversation", args=[teen_chat.pk])).status_code == 404


def test_supervision_flag_without_teen_role_has_no_effect(admin_user, adult):
    """Setzt jemand das Feld im Admin bei einem Erwachsenen, wirkt es trotzdem nicht."""
    adult.allow_supervision = True
    adult.save()
    conv = Conversation.objects.create(user=adult, title="Privat")
    assert not can(admin_user, Action.READ, conv)
