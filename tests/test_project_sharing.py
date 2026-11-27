"""Projekte als Ganzes teilen (C6, entschieden 2026-10-22).

Freigabe über ``Share.project``; gilt für alle Chats des Projekts, auch später angelegte,
außer für Chats mit ``private_in_project``. Rechtestufen wie bei geteilten Chats; das
Projekt selbst ändert nur der Besitzer, Empfänger sehen es nur lesend.
"""

import json

import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse

from multigpt.accounts.models import UserGroup
from multigpt.accounts.permissions import Action, can
from multigpt.chat import projects, sharing
from multigpt.chat.models import Conversation, Project, Share
from tests.test_api_stream import make_user

pytestmark = pytest.mark.django_db
PASSWORD = "Geheim-Test-1234"


@pytest.fixture
def anna():
    return make_user("adult", "anna_besitzerin")


@pytest.fixture
def ben():
    return make_user("adult", "ben_empfaenger")


@pytest.fixture
def carl():
    return make_user("adult", "carl_fremd")


@pytest.fixture
def project(anna):
    return Project.objects.create(owner=anna, name="Umzug", instructions="Immer duzen.")


@pytest.fixture
def chats(anna, project):
    shared = Conversation.objects.create(user=anna, title="Kisten", project=project)
    private = Conversation.objects.create(
        user=anna, title="Mietvertrag", project=project, private_in_project=True
    )
    outside = Conversation.objects.create(user=anna, title="Urlaub")
    return shared, private, outside


def share_project(project, user, *, write=False, update=False, delete=False):
    return Share.objects.create(
        project=project, user=user, can_write=write, can_update=update, can_delete=delete
    )


def login(client, user):
    client.force_login(user)
    return client


def jpost(client, url, data=None):
    return client.post(url, json.dumps(data or {}), content_type="application/json")


# --- Rechte ---------------------------------------------------------------------------------


def test_project_share_grants_access_to_its_chats(ben, carl, project, chats):
    shared, private, outside = chats
    share_project(project, ben)
    assert can(ben, Action.READ, project)
    assert can(ben, Action.READ, shared) and not can(ben, Action.WRITE, shared)
    assert not can(ben, Action.READ, private)  # ausgenommen
    assert not can(ben, Action.READ, outside)  # nicht im Projekt
    assert not can(carl, Action.READ, shared) and not can(carl, Action.READ, project)
    # Projekt selbst ändern darf nur die Besitzerin
    for action in (Action.WRITE, Action.UPDATE, Action.DELETE):
        assert not can(ben, action, project)


def test_rights_follow_the_share_and_later_chats(anna, ben, project, chats):
    share_project(project, ben, write=True, update=True)
    later = Conversation.objects.create(user=anna, title="Neu", project=project)
    assert can(ben, Action.WRITE, later) and can(ben, Action.UPDATE, later)
    assert not can(ben, Action.DELETE, later)
    access = sharing.access_for(ben, later)
    assert access.shared and access.label == "RWU"


def test_group_share_and_leave(ben, project, chats):
    group = UserGroup.objects.create(name="Familie")
    ben.groups.add(group)
    Share.objects.create(project=project, group=group)
    shared = chats[0]
    assert can(ben, Action.READ, shared)
    assert sharing.leave_project(ben, project)
    assert not can(ben, Action.READ, shared) and not can(ben, Action.READ, project)
    assert Share.objects.filter(project=project).exists()  # Gruppe behält die Freigabe


def test_shared_with_lists_project_chats_but_not_private(ben, project, chats):
    shared, private, _ = chats
    share_project(project, ben)
    listed = list(sharing.shared_with(ben))
    assert shared in listed and private not in listed
    assert [p.pk for p in sharing.shared_projects(ben)] == [project.pk]
    assert sharing.is_shared(shared) and not sharing.is_shared(private)


def test_exactly_one_target(anna, ben, project, chats):
    with pytest.raises(IntegrityError), transaction.atomic():
        Share.objects.create(project=project, conversation=chats[0], user=ben)
    share_project(project, ben)
    with pytest.raises(IntegrityError), transaction.atomic():
        share_project(project, ben)


def test_instructions_apply_for_recipient_turns(ben, project, chats):
    share_project(project, ben, write=True)
    block = projects.instruction_block(chats[0])
    assert block and "Immer duzen." in block


# --- Seiten ---------------------------------------------------------------------------------


def test_recipient_sees_read_only_project_page(client, ben, carl, project, chats):
    share_project(project, ben)
    login(client, ben)
    url = reverse("chat:project_detail", args=[project.pk])
    page = client.get(url)
    text = page.content.decode()
    assert page.status_code == 200 and "Geteilt von" in text and "Immer duzen." in text
    assert "Kisten" in text and "Mietvertrag" not in text
    assert "Projekt löschen" not in text and 'name="instructions"' not in text
    assert client.post(url, {"name": "Gekapert"}).status_code == 403
    assert Project.objects.get(pk=project.pk).name == "Umzug"
    assert "Mit mir geteilt" in client.get(reverse("chat:project_list")).content.decode()
    login(client, carl)
    assert client.get(url).status_code == 404


def test_owner_page_has_share_button_and_chat_toggle(client, anna, ben, project, chats):
    login(client, anna)
    page = client.get(reverse("chat:project_detail", args=[project.pk])).content.decode()
    assert "data-project-share" in page
    chat_page = client.get(reverse("chat:conversation", args=[chats[0].pk])).content.decode()
    assert "data-chat-private" not in chat_page  # Projekt noch nicht geteilt
    share_project(project, ben)
    chat_page = client.get(reverse("chat:conversation", args=[chats[0].pk])).content.decode()
    assert "data-chat-private" in chat_page and "Nicht mit dem Projekt teilen" in chat_page


# --- API ------------------------------------------------------------------------------------


def test_api_owner_manages_project_shares(client, anna, ben, project):
    login(client, anna)
    url = reverse("chat:api_project_shares", args=[project.pk])
    created = jpost(client, url, {"kind": "user", "target": ben.pk, "can_write": True})
    assert created.status_code == 201
    rows = created.json()["shares"]
    assert rows[0]["target_id"] == ben.pk and rows[0]["label"] == "RW"
    detail = reverse("chat:api_project_share_detail", args=[project.pk, rows[0]["id"]])
    patched = client.patch(
        detail, json.dumps({"can_write": False}), content_type="application/json"
    )
    assert patched.json()["shares"][0]["label"] == "R"
    assert client.delete(detail).json()["shares"] == []


def test_api_non_owner_cannot_manage(client, ben, carl, project):
    share_project(project, ben)
    url = reverse("chat:api_project_shares", args=[project.pk])
    login(client, ben)
    assert client.get(url).status_code == 403
    login(client, carl)
    assert client.get(url).status_code == 404


def test_api_leave_project_and_chat_leave_hint(client, ben, project, chats):
    share_project(project, ben)
    login(client, ben)
    left_chat = jpost(client, reverse("chat:api_conversation_leave", args=[chats[0].pk]))
    assert left_chat.status_code == 400 and "Projekt" in left_chat.json()["error"]
    left = jpost(client, reverse("chat:api_project_leave", args=[project.pk]))
    assert left.status_code == 200
    assert not can(ben, Action.READ, chats[0])


def test_api_private_toggle_owner_only(client, anna, ben, project, chats):
    shared = chats[0]
    share_project(project, ben)
    url = reverse("chat:api_conversation_private", args=[shared.pk])
    login(client, ben)
    assert jpost(client, url, {"private": True}).status_code == 403
    login(client, anna)
    response = jpost(client, url, {"private": True})
    assert response.status_code == 200 and response.json()["private"] is True
    shared.refresh_from_db()
    assert shared.private_in_project and not can(ben, Action.READ, shared)
    assert jpost(client, url, {"private": "ja"}).status_code == 400
